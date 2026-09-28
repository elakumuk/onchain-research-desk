"""A tiny hand-made facts registry, so verifier tests do not depend on live data."""
import pytest

from desk import facts as FA

T0 = "2026-09-28T19:00:00Z"
T_OLD = "2026-09-18T19:00:00Z"      # ten days before the rest of the registry


def _f(fid, value, unit, as_of=T0, src=("api:a",)):
    return FA.Fact(fid, value, unit, as_of, f"test fact {fid}", src)


FACTS = [
    _f("liq.AAA.slip_1m_bps", 371.385, "bps"),
    _f("liq.AAA.spread_bps", 3.35, "bps"),
    _f("fund.AAA.holder_share_365d_pct", 75.4896, "pct"),
    _f("fund.AAA.fees_365d_usd", 964123456, "usd"),
    _f("fund.AAA.p_fees_mcap_x", 20079.5, "x"),
    _f("mkt.AAA.circulating_supply_count", 620000000, "count"),
    _f("risk.AAA.max_drawdown_365d_pct", -74.6123, "pct"),
    _f("liq.AAA.days_to_liq_10m_at_10pct_days", 0.388, "days"),
    _f("bt.x.sharpe", 1.07545, "number"),
    _f("liq.AAA.old_depth_usd", 1580000, "usd", as_of=T_OLD, src=("api:old",)),
    FA.Fact("param.order_size_1m_usd", 1000000, "usd", None, "order size", ("config:x",)),
]


@pytest.fixture
def reg(tmp_path):
    labels = {"tokens": {}, "_sources": {
        "api:a": {"api": "TestAPI", "endpoint": "GET /a", "cache_file": "data/a.json", "fetched_at": T0},
        "api:old": {"api": "TestAPI", "endpoint": "GET /old", "cache_file": "data/old.json", "fetched_at": T_OLD},
        "config:x": {"api": "config", "endpoint": "desk/config.py Params.x", "cache_file": None, "fetched_at": None},
    }, "_source_groups": {}}
    path = tmp_path / "facts.json"
    FA.write_registry(path, sorted(FACTS, key=lambda f: f.id), labels, "live")
    return FA.load_registry(path)
