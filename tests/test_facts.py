"""The facts registry: shape, units, provenance, and agreement with the report tables."""
import json
import math
import re

import pandas as pd
import pytest

from desk import facts as FA
from desk.config import REPORTS_DIR, ROOT

UNIT_SUFFIX = {"usd": "_usd", "pct": "_pct", "bps": ("_bps", "_bps_per_1m"), "x": "_x",
               "days": "_days", "count": "_count"}


@pytest.fixture(scope="module")
def reg():
    return FA.load_registry(REPORTS_DIR / "facts.json")


def test_ids_units_and_values_are_well_formed(reg):
    assert len(reg.facts) > 500
    for fid, f in reg.facts.items():
        assert re.fullmatch(r"[a-z]+(\.[A-Za-z0-9_]+)+", fid), fid
        assert f.unit in FA.UNITS, fid
        assert isinstance(f.value, (int, float)) and math.isfinite(f.value), fid
        suffix = UNIT_SUFFIX.get(f.unit)
        if suffix:   # the id names its unit, so unit confusion is visible to a human reader too
            assert fid.endswith(suffix), f"{fid} should end with {suffix}"


def test_every_data_fact_has_as_of_and_resolvable_sources(reg):
    for fid, f in reg.facts.items():
        srcs = reg.expand_sources(f)
        assert srcs, fid
        if fid.startswith("param."):
            assert f.as_of is None and srcs[0]["api"] == "config"
            continue
        assert f.as_of is not None, fid
        assert f.as_of == min(s["fetched_at"] for s in srcs), fid
        for s in srcs:
            assert s["endpoint"].startswith("GET /"), (fid, s)
            assert (ROOT / s["cache_file"]).exists(), (fid, s["cache_file"])   # committed snapshot file


def test_registry_agrees_with_report_tables(reg):
    liq = pd.read_csv(REPORTS_DIR / "liquidity.csv", index_col=0)
    fund = pd.read_csv(REPORTS_DIR / "fundamentals.csv", index_col=0)
    assert reg["liq.AAVE.slip_1m_bps"].value == pytest.approx(liq.loc["AAVE", "slip_1m_bps"], rel=1e-5)
    # ratios are stored in percent, never as 0-1
    assert reg["fund.HYPE.holder_share_365d_pct"].value == pytest.approx(
        100 * fund.loc["HYPE", "holder_accrual_ratio"], rel=1e-5)
    # "not available" is never a number: no 10m slippage fact where the book ran out
    assert pd.isna(liq.loc["AAVE", "slip_10m_bps"]) and "liq.AAVE.slip_10m_bps" not in reg
    assert "AAVE" in [s for s, t in reg.labels["tokens"].items() if "10m" in t["slippage_na"]]


def test_builder_skips_non_finite_rejects_duplicates_and_bad_units():
    b = FA._Builder({})
    b.add("x.a_usd", float("nan"), "usd", "d")
    b.add("x.b_usd", None, "usd", "d")
    assert b.facts == {}
    b.add("x.c_usd", 1.23456789, "usd", "d")
    assert b.facts["x.c_usd"].value == 1.23457                 # 6 significant figures
    with pytest.raises(ValueError, match="duplicate"):
        b.add("x.c_usd", 2, "usd", "d")
    with pytest.raises(ValueError, match="unknown unit"):
        b.add("x.d", 2, "percent", "d")


def test_as_of_is_oldest_source_fetch():
    prov = {("s", "a"): {"api": "A", "endpoint": "GET /a", "cache_file": "a", "fetched_at": "2026-01-02T00:00:00Z"},
            ("s", "b"): {"api": "B", "endpoint": "GET /b", "cache_file": "b", "fetched_at": "2026-01-01T00:00:00Z"}}
    b = FA._Builder(prov)
    b.add("x.v_bps", 5, "bps", "d", b.src(("s", "a"), ("s", "b")))
    assert b.facts["x.v_bps"].as_of == "2026-01-01T00:00:00Z"


def test_write_is_deterministic_and_loadable(tmp_path):
    facts = [FA.Fact("a.b_pct", 12.5, "pct", "2026-01-01T00:00:00Z", "d", ("s:a",))]
    labels = {"_sources": {"s:a": {"api": "A", "endpoint": "GET /a", "cache_file": "a",
                                   "fetched_at": "2026-01-01T00:00:00Z"}}, "_source_groups": {}}
    h1 = FA.write_registry(tmp_path / "f1.json", facts, labels, "snapshot")
    h2 = FA.write_registry(tmp_path / "f2.json", facts, labels, "snapshot")
    assert h1 == h2
    r = FA.load_registry(tmp_path / "f1.json")
    assert r.sha256 == h1 and r["a.b_pct"].value == 12.5
    assert r.meta["data_as_of"] == "2026-01-01T00:00:00Z"
    assert json.loads((tmp_path / "f1.json").read_text())["facts"][0]["id"] == "a.b_pct"


def test_tags():
    assert FA.size_tag(1e5) == "100k" and FA.size_tag(1e6) == "1m" and FA.size_tag(1e9) == "1b"
    assert FA.pct_tag(0.005) == "0p5pct" and FA.pct_tag(0.02) == "2pct"
    assert FA.scheme_slug("Min-var (LW)") == "min_var"
    assert FA.scheme_slug("Inverse vol, capped @ $1B") == "inverse_vol_capped_1b"
