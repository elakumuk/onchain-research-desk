"""The deterministic memo: formatting, rendering, missing-data handling, commentary binding."""
import random

import pytest

from desk import facts as FA
from desk import memo as M
from desk import verify as V
from desk.config import REPORTS_DIR


@pytest.mark.parametrize("value,unit,expected", [
    (84005.7, "usd", "$84,006"),
    (88.75, "usd", "$88.8"),
    (964_123_456, "usd", "$964M"),
    (1_000_000, "usd", "$1M"),
    (1_004_000, "usd", "$1.00M"),          # zeros kept when dropping them would lose precision
    (1.6873e12, "usd", "$1.69T"),
    (0.00594887, "bps", "0.00595 bps"),
    (371.385, "bps", "371 bps"),
    (20079.5, "x", "20,080x"),             # never round inside the integer part
    (-40.6438, "pct", "-40.6%"),
    (0, "pct", "0%"),
    (620_000_000, "count", "620M"),
    (20, "count", "20"),
    (365, "days", "365 days"),
    (1.07545, "number", "1.08"),
])
def test_format_value(value, unit, expected):
    assert M.format_value(value, unit) == expected


def test_every_formatted_value_verifies(tmp_path):
    """Property: whatever the renderer writes, the verifier accepts (and a nudged value it rejects)."""
    rng = random.Random(7)
    facts = []
    for i in range(400):
        unit = rng.choice(["usd", "pct", "bps", "x", "days", "count", "number"])
        mag = 10 ** rng.uniform(-4, 12 if unit in ("usd", "count") else 4)
        v = FA.round_sig(rng.choice([-1, 1]) * mag if unit in ("pct", "number") else mag)
        if unit == "count":
            v = int(round(v))
        facts.append(FA.Fact(f"t.v{i}_{unit}", v, unit, "2026-01-01T00:00:00Z", "d", ("s:a",)))
    labels = {"tokens": {}, "_sources": {"s:a": {"api": "A", "endpoint": "GET /a", "cache_file": "a",
                                                 "fetched_at": "2026-01-01T00:00:00Z"}}, "_source_groups": {}}
    FA.write_registry(tmp_path / "f.json", facts, labels, "snapshot")
    reg = FA.load_registry(tmp_path / "f.json")
    body = "\n".join(f"Value {{{{fact:{f.id}}}}}." for f in facts)
    res = V.verify_text(M.render(body, reg, symbol="T"), reg)
    assert res.ok, [(i.text, i.detail) for i in res.issues[:5]]
    assert res.n_verified == len(facts)


def test_render_rejects_unknown_ids_and_is_idempotent(reg):
    with pytest.raises(M.RenderError):
        M.render("Cost {{fact:liq.AAA.slip_99m_bps}}", reg, symbol="AAA")
    once = M.render("Cost {{fact:liq.AAA.slip_1m_bps}}.", reg, symbol="AAA")
    assert M.render(once, reg) == once
    assert once.count("[^liq.AAA.slip_1m_bps]:") == 1


def _labels(**over):
    base = {"kind": "DeFi", "venues": "kraken", "llama_name": None, "protocol_type": None,
            "holders_revenue_reported": None, "holders_revenue_method": None, "accrual_flag": None,
            "slippage_na": ["1m", "10m"], "in_portfolio": False, "notes": None}
    return {**base, **over}


def test_missing_data_becomes_analyst_research_not_invention(reg):
    reg.labels["tokens"]["AAA"] = _labels()
    tpl = M.build_template("AAA", reg)
    text = M.render(tpl.replace("{{COMMENTARY}}", "none"), reg, symbol="AAA")
    assert "no fee data" in text                       # no fund.* facts except fees? none for supply either
    assert text.count("*Requires analyst research.*") >= 5
    assert "## 4. Governance" in text and "## What the data can't tell us" in text
    assert "not in the portfolio analysis" in text
    assert V.verify_text(text, reg).ok


def test_unreported_holder_revenue_is_unknown_not_zero(reg):
    reg.labels["tokens"]["AAA"] = _labels(holders_revenue_reported=False)
    tpl = M.build_template("AAA", reg)
    assert "not a zero" in tpl and "not reported" in tpl


def test_commentary_is_bound_to_the_registry(reg, tmp_path):
    reg.labels["tokens"]["AAA"] = _labels()
    cdir = tmp_path / "commentary"
    cdir.mkdir()
    good = f"<!-- commentary symbol=AAA facts_sha256={reg.sha256} -->\nSlippage of {{{{fact:liq.AAA.slip_1m_bps}}}} is high.\n"
    (cdir / "AAA.md").write_text(good)
    memo = M.build_memo("AAA", reg, cdir)
    assert "Slippage of 371 bps[^liq.AAA.slip_1m_bps] is high." in memo
    assert V.verify_text(memo, reg).ok

    (cdir / "AAA.md").write_text(good.replace(reg.sha256, "f" * 64))     # written against last week's data
    memo = M.build_memo("AAA", reg, cdir)
    assert "Commentary withheld" in memo and "Slippage of" not in memo

    (cdir / "AAA.md").write_text(good + "Fees tripled to 12% of volume.\n")   # LLM typed a number
    res = V.verify_text(M.build_memo("AAA", reg, cdir), reg)
    assert not res.ok and {i.kind for i in res.issues} == {"uncited"}


def test_committed_memos_have_the_framework_sections():
    for f in sorted((REPORTS_DIR / "memos").glob("*.md")):
        text = f.read_text()
        for h in ["## Key figures", "## 1. Supply model", "## 2. Utility and demand drivers",
                  "## 3. Incentive and value-accrual mechanisms", "## 4. Governance",
                  "## 5. Liquidity, market and technical risk", "## 6. Portfolio context",
                  "## Analyst commentary", "## What the data can't tell us", "## Sources"]:
            assert h in text, (f.name, h)
