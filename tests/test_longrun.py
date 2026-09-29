"""v2 long-run layer: point-in-time universe, no look-ahead, survivorship, deaths, stores, statistics."""
import gzip
import io
import zipfile

import numpy as np
import pandas as pd
import pytest

from desk import longrun as LRN
from desk import portfolio as P
from desk.data import binance as BN
from desk.data import coinmetrics as CM

UKW = dict(top_k=3, min_history_days=60, est_window=30, rank_window=30, floor_usd=1e6)


def _market(n=400, seed=3, die_at=None, list_at=None):
    """Five synthetic assets. 'D' stops trading at day `die_at`; 'N' lists at day `list_at`."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    cols = ["A", "B", "C", "D", "N"]
    rets = rng.normal(0.0005, 0.03, size=(n, len(cols)))
    price = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=cols)
    vol = pd.DataFrame({"A": 5e8, "B": 3e8, "C": 5e5, "D": 4e8, "N": 9e8}, index=idx)
    if die_at is not None:
        price.iloc[die_at:, 3] = np.nan
        vol.iloc[die_at:, 3] = np.nan
    if list_at is not None:
        price.iloc[:list_at, 4] = np.nan
        vol.iloc[:list_at, 4] = np.nan
    return price, vol


def _universe(price, vol, dates, **kw):
    return LRN.universe_table(dates, price, vol, **{**UKW, **kw})


def _run(price, vol, univ, dates, fn=P.inverse_vol, haircut=0.0):
    members = {t: g.loc[g["eligible"], "symbol"].tolist() for t, g in univ.groupby("date")}
    return P.backtest(price, vol, fn, est_window=30, every=None, adv_window=30, rebalance_on=dates,
                      universe_fn=lambda t: members[t], exit_haircut=haircut)


# ------------------------------------------------------------------ point in time
def test_eligibility_ignores_every_row_on_or_after_the_date():
    price, vol = _market(list_at=150)
    t = price.index[200]
    base = LRN.eligibility(t, price, vol, **UKW)
    p2, v2 = price.copy(), vol.copy()
    p2.loc[p2.index >= t] = np.nan                       # the future: deaths, zero volume, anything
    v2.loc[v2.index >= t] = 0.0
    v2.loc[v2.index >= t, "C"] = 1e12                    # C becomes huge the day of the rebalance
    pd.testing.assert_frame_equal(base, LRN.eligibility(t, p2, v2, **UKW))


def test_universe_rules_history_floor_and_top_k():
    price, vol = _market(list_at=150)
    e = LRN.eligibility(price.index[180], price, vol, **UKW)
    assert e.loc["N", "reason"] == "history"             # listed 30 days ago, needs 60
    assert e.loc["C", "reason"] == "below floor"         # $0.5M median volume, floor $1M
    assert list(e.index[e["eligible"]]) == ["A", "B", "D"]
    e2 = LRN.eligibility(price.index[260], price, vol, **UKW)
    assert e2.loc["N", "eligible"] and e2.loc["N", "rank"] == 1   # largest volume once it has history
    assert e2.loc["B", "reason"] == "outside top K"


def test_pit_backtest_has_no_look_ahead():
    """Rewriting prices and volumes after day d changes neither the universe nor any return up to d."""
    price, vol = _market(die_at=300, list_at=100)
    dates = list(pd.date_range("2020-04-01", price.index[-1], freq="MS"))
    univ = _universe(price, vol, dates)
    base = _run(price, vol, univ, dates)["returns"]
    cut = price.index[250]
    p2, v2 = price.copy(), vol.copy()
    after = p2.index > cut
    p2.loc[after] *= np.linspace(0.2, 4.0, after.sum())[:, None]
    v2.loc[after] = v2.loc[after][::-1].values * 7
    univ2 = _universe(p2, v2, dates)
    keep = univ["date"] <= cut
    pd.testing.assert_frame_equal(univ[keep].reset_index(drop=True), univ2[univ2["date"] <= cut].reset_index(drop=True))
    alt = _run(p2, v2, univ2, dates)["returns"]
    pd.testing.assert_series_equal(base.loc[:cut], alt.loc[:cut])
    assert not np.allclose(base.loc[cut:].iloc[1:], alt.loc[cut:].iloc[1:])


# ------------------------------------------------------------------ survivorship
def test_asset_that_dies_stays_in_the_returns_until_its_death():
    """D is held at equal weight, dies mid-month: its returns count every day it traded, then it is sold."""
    die = 250
    price, vol = _market(die_at=die)
    dates = list(pd.date_range("2020-04-01", price.index[-1], freq="MS"))
    univ = _universe(price, vol, dates)
    ew = lambda h: P.equal_weight(h.columns)
    res = _run(price, vol, univ, dates, fn=ew, haircut=0.25)
    r = res["returns"]
    death = price.index[die]
    last_rb = max(d for d in dates if d < death)
    assert "D" in univ[(univ["date"] == last_rb) & univ["eligible"]]["symbol"].tolist()
    assert "D" not in univ[(univ["date"] > death) & univ["eligible"]]["symbol"].tolist()
    # hand-drift the portfolio from the last rebalance to the day before death: D's return is in it
    simple = price.pct_change(fill_method=None)
    held = sorted(univ[(univ["date"] == last_rb) & univ["eligible"]]["symbol"])
    assert len(held) == 3 and "D" in held
    w = pd.Series(1 / 3, index=held)
    for t in r.loc[last_rb:death - pd.Timedelta(days=1)].index:
        assert r.loc[t] == pytest.approx(float((w * simple.loc[t, held]).sum()))
        g = w * (1 + simple.loc[t, held])
        w = g / g.sum()
    # the day it stops: sold at the last price less the 25% haircut, proceeds to cash
    others = [a for a in held if a != "D"]
    expect = float((w[others] * simple.loc[death, others]).sum() - w["D"] * 0.25)
    assert r.loc[death] == pytest.approx(expect)
    assert res["exits"] == [{"date": death, "asset": "D", "weight": pytest.approx(float(w["D"]))}]
    # dropping D from the data altogether (survivorship bias) gives a different history
    gone = _run(price.drop(columns="D"), vol.drop(columns="D"),
                _universe(price.drop(columns="D"), vol.drop(columns="D"), dates), dates, fn=ew)["returns"]
    assert not np.allclose(r.loc[:death - pd.Timedelta(days=1)], gone.loc[:death - pd.Timedelta(days=1)])


def test_mask_deaths_uses_last_traded_day_and_grace_period():
    idx = pd.date_range("2021-01-01", periods=200, freq="D")
    price = pd.DataFrame({"X": 1.0, "Y": 1.0, "Z": 1.0}, index=idx)
    vol = pd.DataFrame({"X": 1e6, "Y": 1e6, "Z": 1e6}, index=idx)
    vol.iloc[100:, 0] = 0.0          # X: price continues (e.g. a migrated token), trading stops -> dies
    vol.iloc[190:, 1] = np.nan       # Y: stops 10 days before the end -> within grace, not a death
    out = LRN.mask_deaths(price, vol, idx[-1], grace_days=30)
    life = out["lifetimes"]
    assert life.loc["X", "died"] and life.loc["X", "last_traded"] == idx[99]
    assert not life.loc["Y", "died"] and not life.loc["Z", "died"]
    assert out["price"]["X"].iloc[:100].notna().all() and out["price"]["X"].iloc[100:].isna().all()


def test_binance_first_segment_never_splices_a_reused_ticker():
    idx = pd.date_range("2022-01-01", periods=60, freq="D")
    s = pd.Series(np.arange(60.0) + 1, index=idx)
    s.iloc[30:40] = np.nan                               # ticker delisted, later reused by another token
    out = LRN.first_segment(s, gap_days=5)
    assert out.iloc[:30].notna().all() and out.iloc[30:].isna().all()
    one_day = pd.Series(np.arange(60.0) + 1, index=idx).where(idx != idx[10])
    assert LRN.first_segment(one_day, gap_days=5).iloc[11:].notna().all()          # a 1-day gap is not an end


# ------------------------------------------------------------------ statistics and capacity
def test_sharpe_diff_is_zero_for_identical_series_and_symmetric():
    rng = np.random.default_rng(1)
    a = pd.Series(rng.normal(0.001, 0.02, 1000))
    b = pd.Series(rng.normal(0.0005, 0.03, 1000))
    d, se = LRN.sharpe_diff(a, a, 365)
    assert d == pytest.approx(0) and se == pytest.approx(0, abs=1e-6)
    d1, s1 = LRN.sharpe_diff(a, b, 365)
    d2, s2 = LRN.sharpe_diff(b, a, 365)
    assert d1 == pytest.approx(-d2) and s1 == pytest.approx(s2) and s1 > 0


def test_block_bootstrap_is_deterministic_and_brackets_the_estimate():
    rng = np.random.default_rng(2)
    df = pd.DataFrame({"x": rng.normal(0.002, 0.02, 1500), "BTC": rng.normal(0.001, 0.02, 1500)})
    b1 = LRN.block_bootstrap_sharpe(df, 365, n=400, block=20, seed=7, bench="BTC")
    b2 = LRN.block_bootstrap_sharpe(df, 365, n=400, block=20, seed=7, bench="BTC")
    pd.testing.assert_frame_equal(b1, b2)
    sr = df["x"].mean() / df["x"].std() * np.sqrt(365)
    assert b1.loc["x", "boot_lo"] < sr < b1.loc["x", "boot_hi"]
    assert b1.loc["BTC", "boot_diff_lo"] == 0 and b1.loc["BTC", "boot_diff_hi"] == 0


def test_capacity_over_time_matches_hand_calc():
    w = pd.Series({"A": 0.5, "B": 0.5})
    adv = pd.Series({"A": 1e9, "B": 5e7})
    cap = LRN.capacity_over_time([{"date": pd.Timestamp("2020-01-01"), "target": w, "adv": adv}],
                                 (1e7, 1e8), 0.10)
    row = cap.iloc[0]
    assert row["max_aum_before_any_cap_usd"] == pytest.approx(1e7)      # 0.10 x 5e7 / 0.5
    assert row["first_binding_name"] == "B"
    assert row["weight_moved_10m"] == pytest.approx(0.0)
    assert row["weight_moved_100m"] == pytest.approx(0.45)               # B capped at 5%: 45% moves to A
    s = pd.Series([1, 5, 2, 6, 7], index=pd.date_range("2020-01-01", periods=5, freq="MS"))
    assert LRN.first_sustained(s, 5) == s.index[3]


def test_rolling_amihud_matches_the_v1_definition():
    from desk import liquidity as L
    idx = pd.date_range("2020-01-01", periods=40, freq="D")
    rng = np.random.default_rng(4)
    p = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 40))), index=idx)
    v = pd.Series(rng.uniform(1e6, 5e6, 40), index=idx)
    roll = LRN.rolling_amihud(p, v, window=30, min_obs=20)
    assert roll.iloc[-1] == pytest.approx(L.amihud(p, v, 30), rel=1e-9)


def test_coverage_counts_top_names_without_a_price():
    idx = pd.date_range("2020-01-01", periods=120, freq="D")
    price = pd.DataFrame({"P1": 1.0, "P2": 1.0, "U": np.nan}, index=idx)
    vol = pd.DataFrame({"P1": 5e8, "P2": 1e8, "U": 9e8}, index=idx)
    cov = LRN.coverage([idx[-1]], price, vol, top_k=2, min_history_days=60, rank_window=30, floor_usd=1e6)
    assert cov.iloc[0]["unpriced_count"] == 1 and cov.iloc[0]["unpriced_names"] == "U"
    assert cov.iloc[0]["unpriced_volume_share"] == pytest.approx(9e8 / 14e8)


# ------------------------------------------------------------------ stores
def _cm_rows(asset, start, n, p0=100.0):
    d = pd.date_range(start, periods=n, freq="D")
    return pd.DataFrame({"asset": asset, "date": d, "price_usd": p0 + np.arange(n), "volume_usd": 1e6})


def test_history_store_merges_the_tail_and_leaves_closed_years_untouched(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(self, assets, metrics, start, now):
        asset = assets[0]
        calls.append((asset, start))
        end = pd.Timestamp("2021-01-10") if len(calls) > 1 else pd.Timestamp("2021-01-05")
        n = (end - start).days + 1
        rows = _cm_rows(asset, start, n, p0=1000.0 if len(calls) > 1 else 100.0)
        return rows

    monkeypatch.setattr(CM.HistoryStore, "_fetch", fake_fetch)
    s = CM.HistoryStore({"btc": {CM.PRICE, CM.VOLUME}}, directory=tmp_path, start="2020-12-01", overlap_days=3)
    s.update()
    closed = (tmp_path / "2020.csv.gz").read_bytes()
    assert (tmp_path / "2021.csv").exists() and not (tmp_path / "2021.csv.gz").exists()
    s2 = CM.HistoryStore({"btc": {CM.PRICE, CM.VOLUME}}, directory=tmp_path, start="2020-12-01", overlap_days=3)
    s2.update()
    assert calls[1][1] == pd.Timestamp("2021-01-02")         # last day (01-05) minus the 3-day overlap
    assert (tmp_path / "2020.csv.gz").read_bytes() == closed # closed year: byte-identical, not rewritten
    d = s2.data.set_index("date")["price_usd"]
    assert d.loc["2021-01-01"] == 131.0 and d.loc["2021-01-02"] == 1000.0 and d.index.max() == pd.Timestamp("2021-01-10")
    s3 = CM.HistoryStore({"btc": {CM.PRICE, CM.VOLUME}}, directory=tmp_path, start="2020-12-01")
    s3.load()
    pd.testing.assert_frame_equal(s3.data.reset_index(drop=True), s2.data.reset_index(drop=True), check_dtype=False)
    assert s3.served[0]["origin"] == "history-store" and s3.served[0]["fetched_at"]


def test_history_store_keeps_history_when_the_source_returns_nothing(tmp_path, monkeypatch):
    rows = {"n": 0}

    def fake_fetch(self, assets, metrics, start, now):
        asset = assets[0]
        rows["n"] += 1
        return _cm_rows(asset, start, 20) if rows["n"] == 1 else _cm_rows(asset, start, 0)

    monkeypatch.setattr(CM.HistoryStore, "_fetch", fake_fetch)
    s = CM.HistoryStore({"x": {CM.PRICE}}, directory=tmp_path, start="2021-01-01")
    s.update()
    s.update()
    assert len(s.data) == 20


def test_year_files_are_deterministic_gzip():
    df = _cm_rows("btc", "2019-01-01", 5)
    t1 = CM._year_text(df)
    assert t1 == CM._year_text(df.sample(frac=1, random_state=1))
    assert gzip.compress(t1.encode(), mtime=0) == gzip.compress(t1.encode(), mtime=0)


def _zip(csv_text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", csv_text)
    return buf.getvalue()


def test_binance_klines_parse_milliseconds_and_microseconds():
    ms = "1609459200000,1,2,0.5,1.5,10,1609545599999,15.0,5,1,1,0\n"
    us = "1735689600000000,1,2,0.5,2.5,10,1735775999999999,25.0,5,1,1,0\n"
    a, b = BN.parse_klines(_zip(ms)), BN.parse_klines(_zip(us))
    assert a.iloc[0]["date"] == pd.Timestamp("2021-01-01") and a.iloc[0]["close_usdt"] == 1.5
    assert b.iloc[0]["date"] == pd.Timestamp("2025-01-01") and b.iloc[0]["quote_volume_usdt"] == 25.0
    hdr = "open_time,open,high,low,close,volume,close_time,quote_volume,count,tb,tq,ignore\n" + ms
    assert BN.parse_klines(_zip(hdr)).iloc[0]["close_usdt"] == 1.5


def test_history_store_batches_recent_assets_into_one_request(tmp_path, monkeypatch):
    seen = []
    today = pd.Timestamp.utcnow().tz_localize(None).normalize()

    def fake_fetch(self, assets, metrics, start, now):
        seen.append((tuple(assets), start))
        return pd.concat([_cm_rows(a, start, (today - start).days) for a in assets], ignore_index=True)

    monkeypatch.setattr(CM.HistoryStore, "_fetch", fake_fetch)
    needs = {a: {CM.VOLUME} for a in ("a", "b", "c")}
    s = CM.HistoryStore(needs, directory=tmp_path, start=f"{today - pd.Timedelta(days=100):%Y-%m-%d}")
    s.update()
    seen.clear()
    s.update()
    assert len(seen) == 1 and seen[0][0] == ("a", "b", "c")          # one request for all three
    assert len(s.data) == 3 * 100 and not s.data.duplicated(["asset", "date"]).any()
