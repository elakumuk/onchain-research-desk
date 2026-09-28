"""The verifier must pass honest memos and fail every way a memo can lie about a number."""
from datetime import datetime, timezone

import pytest

from desk import memo as M
from desk import verify as V
from desk.config import REPORTS_DIR
from desk.facts import load_registry

BODY = """# AAA: memo

A {{fact:param.order_size_1m_usd}} market order costs {{fact:liq.AAA.slip_1m_bps}}; the spread is
{{fact:liq.AAA.spread_bps}}. Holders receive {{fact:fund.AAA.holder_share_365d_pct}} of
{{fact:fund.AAA.fees_365d_usd}} in fees. The drawdown was {{fact:risk.AAA.max_drawdown_365d_pct}}.
"""


def rendered(reg, body=BODY):
    return M.render(body, reg, symbol="AAA")


def kinds(res):
    return sorted({i.kind for i in res.issues})


def check(reg, text, **kw):
    return V.verify_text(text, reg, **kw)


# ------------------------------------------------------------------ honest memos pass
def test_rendered_memo_passes(reg):
    res = check(reg, rendered(reg))
    assert res.ok, res.issues
    assert res.n_claims == res.n_verified == 6


@pytest.mark.parametrize("written", [
    "371 bps[^liq.AAA.slip_1m_bps]",                 # rounded to integer
    "371.4 bps[^liq.AAA.slip_1m_bps]",               # rounded to 1 decimal
    "371.39bps[^liq.AAA.slip_1m_bps]",               # no space
    "**371 bps**[^liq.AAA.slip_1m_bps]",             # bold
    "$964M[^fund.AAA.fees_365d_usd]",
    "$0.96B[^fund.AAA.fees_365d_usd]",
    "$964,123,456[^fund.AAA.fees_365d_usd]",
    "75.5%[^fund.AAA.holder_share_365d_pct]",
    "75%[^fund.AAA.holder_share_365d_pct]",          # 75 is a correct integer rounding of 75.49
    "75.49 percent[^fund.AAA.holder_share_365d_pct]",
    "-74.6%[^risk.AAA.max_drawdown_365d_pct]",
    "20,080x[^fund.AAA.p_fees_mcap_x]",
    "620M[^mkt.AAA.circulating_supply_count]",
    "0.39 days[^liq.AAA.days_to_liq_10m_at_10pct_days]",
    "1.08[^bt.x.sharpe]",
    "$1M[^param.order_size_1m_usd]",                 # 1 significant figure is fine when exact
])
def test_rounded_but_correct_numbers_pass(reg, written):
    text = rendered(reg, BODY + f"\nAlso: {written}.\n")
    res = check(reg, text)
    assert res.ok, [(i.kind, i.detail) for i in res.issues]


# ------------------------------------------------------------------ adversarial: each must fail
def test_fabricated_number_with_real_citation_fails(reg):
    text = rendered(reg).replace("371 bps[^liq.AAA.slip_1m_bps]", "171 bps[^liq.AAA.slip_1m_bps]")
    res = check(reg, text)
    assert kinds(res) == ["value_mismatch"]


def test_fabricated_fact_id_fails(reg):
    text = rendered(reg) + "\nA $10M order costs 950 bps[^liq.AAA.slip_10m_bps].\n"
    assert "unknown_fact" in kinds(check(reg, text))


def test_number_with_no_citation_fails(reg):
    for extra in ["Fees were $964M last year.", "Over a 30-day window.", "It is 3 times larger.",
                  "Market share is 12%."]:
        res = check(reg, rendered(reg, BODY + "\n" + extra + "\n"))
        assert kinds(res) == ["uncited"], (extra, res.issues)


def test_percent_vs_bps_confusion_fails(reg):
    for wrong in ["371%[^liq.AAA.slip_1m_bps]",              # right digits, wrong unit
                  "3.71%[^liq.AAA.slip_1m_bps]",             # even a correct conversion must use the fact's unit
                  "75.5 bps[^fund.AAA.holder_share_365d_pct]",
                  "$75.5[^fund.AAA.holder_share_365d_pct]"]:
        res = check(reg, rendered(reg, BODY + f"\nNote {wrong}.\n"))
        assert kinds(res) == ["unit_mismatch"], (wrong, res.issues)


def test_stale_fact_fails(reg):
    # fetched ten days before the rest of the registry: a stale-cache fallback
    res = check(reg, rendered(reg, BODY + "\nDepth is {{fact:liq.AAA.old_depth_usd}}.\n"))
    assert kinds(res) == ["stale"]


def test_whole_registry_too_old_for_the_clock_fails(reg):
    later = datetime(2026, 10, 9, tzinfo=timezone.utc)
    res = check(reg, rendered(reg), now=later)
    assert kinds(res) == ["stale"]
    assert check(reg, rendered(reg), now=datetime(2026, 9, 29, tzinfo=timezone.utc)).ok


def test_sign_error_fails(reg):
    text = rendered(reg).replace("-74.6%[^", "74.6%[^")
    assert kinds(check(reg, text)) == ["value_mismatch"]


def test_too_imprecise_rounding_fails(reg):
    res = check(reg, rendered(reg, BODY + "\nRoughly $1B[^fund.AAA.fees_365d_usd].\n"))
    assert kinds(res) == ["imprecise"]


def test_edited_source_footnote_fails(reg):
    text = rendered(reg).replace("| as of 2026-09-28T19:00:00Z | source: TestAPI `GET /a`",
                                 "| as of 2026-09-28T19:00:00Z | source: TestAPI `GET /made-up`", 1)
    assert kinds(check(reg, text)) == ["footnote"]


def test_citation_without_sources_entry_fails(reg):
    text = rendered(reg) + "\nBy hand: 3.35 bps[^liq.AAA.spread_bps] and 0.388 days[^liq.AAA.days_to_liq_10m_at_10pct_days].\n"
    assert kinds(check(reg, text)) == ["missing_footnote"]      # spread has an entry, days_to_liq does not


def test_memo_bound_to_another_registry_fails(reg):
    text = rendered(reg).replace(reg.sha256, "0" * 64)
    assert kinds(check(reg, text)) == ["wrong_registry"]
    no_header = "\n".join(rendered(reg).split("\n")[1:])
    assert kinds(check(reg, no_header)) == ["no_header"]


def test_unrendered_token_fails(reg):
    text = rendered(reg) + "\nLeftover {{fact:liq.AAA.spread_bps}}.\n"
    assert "unrendered_token" in kinds(check(reg, text))
    # regression: str.format() once turned {{fact:x}} into {fact:x}; no digits show, but it is still broken
    mangled = rendered(reg) + "\nCould not fill {fact:param.order_size_1m_usd}.\n"
    assert kinds(check(reg, mangled)) == ["unrendered_token"]


@pytest.mark.parametrize("phrase", [
    "Returns are guaranteed.", "Our price target is high.", "We recommend accumulating.",
    "Strong buy rating.", "Holders should buy before the unlock.", "It is a risk-free yield.",
    "The token will double.", "The token is undervalued.", "Fees of five million dollars.",
])
def test_banned_language_fails(reg, phrase):
    res = check(reg, rendered(reg, BODY + "\n" + phrase + "\n"))
    assert kinds(res) == ["banned"], res.issues


def test_allowed_look_alikes_pass(reg):
    extra = ("\nSharpe uses a zero risk-free rate. Buybacks and the buy side of the book are fine words. "
             "L2 tokens, ERC tokens, version V3 and dates like 2026-09-28 or 18:54 UTC are not claims. "
             "See [the docs](https://example.com/v2/page?x=10).\n\n1. first item\n2. second item\n\n## 3. A heading\n")
    res = check(reg, rendered(reg, BODY + extra))
    assert res.ok, res.issues


def test_digits_inside_fact_ids_are_not_claims(reg):
    res = check(reg, rendered(reg))
    assert all("slip_1m" not in i.text for i in res.issues)
    assert "liq.AAA.slip_1m_bps" in res.cited_ids


# ------------------------------------------------------------------ CLI and committed output
def test_cli_exit_codes(reg, tmp_path):
    good = tmp_path / "good.md"
    good.write_text(rendered(reg))
    bad = tmp_path / "bad.md"
    bad.write_text(rendered(reg) + "\nInvented: 42%.\n")
    args = ["--facts", str(reg.path), "--report", str(tmp_path / "r.json"), "--no-wall-clock", "-q"]
    assert V.main([str(good), *args]) == 0
    assert V.main([str(good), str(bad), *args]) == 1
    assert '"ok": false' in (tmp_path / "r.json").read_text()


def test_committed_memos_verify_against_committed_facts():
    reg = load_registry(REPORTS_DIR / "facts.json")
    memos = sorted((REPORTS_DIR / "memos").glob("*.md"))
    assert len(memos) == len(reg.labels["tokens"])
    for f in memos:
        res = V.verify_text(f.read_text(), reg, path=f.name)
        assert res.ok, (f.name, res.issues[:3])
        assert res.n_claims > 30
