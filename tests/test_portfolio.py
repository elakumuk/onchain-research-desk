import numpy as np
import pandas as pd
import pytest

from desk import portfolio as P


def test_caps_not_binding_leave_weights_unchanged():
    base = pd.Series({"A": 0.5, "B": 0.3, "C": 0.2})
    out = P.apply_caps(base, pd.Series({"A": 1.0, "B": 1.0, "C": 1.0}))
    pd.testing.assert_series_equal(out, base)


def test_cap_binds_and_excess_is_redistributed_pro_rata():
    base = pd.Series({"A": 0.5, "B": 0.3, "C": 0.2})
    out = P.apply_caps(base, pd.Series({"A": 0.2, "B": 1.0, "C": 1.0}))
    assert out["A"] == pytest.approx(0.2)
    assert out.sum() == pytest.approx(1.0)
    # B and C keep their 3:2 ratio
    assert out["B"] / out["C"] == pytest.approx(1.5)


def test_cascading_caps_respected():
    base = pd.Series({"A": 0.5, "B": 0.3, "C": 0.2})
    caps = pd.Series({"A": 0.2, "B": 0.35, "C": 0.9})
    out = P.apply_caps(base, caps)
    assert (out <= caps + 1e-12).all()
    assert out.sum() == pytest.approx(1.0)


def test_caps_with_zero_weight_names_leave_cash_not_nan():
    """Point-in-time universes carry zero-weight columns; capping every held name must give cash, not 0/0."""
    base = pd.Series({"A": 0.6, "B": 0.4, "X": 0.0, "Y": 0.0})
    out = P.apply_caps(base, pd.Series({"A": 0.1, "B": 0.1, "X": 1.0, "Y": 1.0}))
    assert out.notna().all()
    assert out["A"] == pytest.approx(0.1) and out["B"] == pytest.approx(0.1) and out[["X", "Y"]].sum() == 0
    assert out.sum() == pytest.approx(0.2)


def test_capacity_binds_as_aum_grows_and_leaves_cash():
    adv = pd.Series({"A": 1e9, "B": 5e7})
    base = pd.Series({"A": 0.5, "B": 0.5})
    small = P.apply_caps(base, P.capacity_caps(adv, 1e7, 0.10))     # B cap = 50%: not binding
    large = P.apply_caps(base, P.capacity_caps(adv, 1e10, 0.10))    # A cap 1%, B cap 0.05%
    assert small.sum() == pytest.approx(1.0) and small["B"] == pytest.approx(0.5)
    assert large["A"] == pytest.approx(0.01) and large["B"] == pytest.approx(0.0005)
    assert large.sum() < 0.02            # the rest is cash: capacity exhausted
    # the largest AUM with no cap binding is set by B: 0.10 * 5e7 / 0.5
    assert P.max_uncapped_aum(base, adv, 0.10) == pytest.approx(1e7)


def test_weight_moved_and_effective_n():
    t = pd.Series({"A": 0.5, "B": 0.5})
    assert P.weight_moved(t, t) == 0.0
    assert P.weight_moved(t, pd.Series({"A": 0.8, "B": 0.2})) == pytest.approx(0.3)
    assert P.effective_n(t) == pytest.approx(2.0)


def test_ledoit_wolf_is_better_conditioned_than_sample():
    rng = np.random.default_rng(0)
    rets = pd.DataFrame(rng.normal(0, 0.03, size=(60, 20)))
    cov, shrink = P.ledoit_wolf_cov(rets)
    assert 0 < shrink <= 1
    assert np.linalg.cond(cov.values) < np.linalg.cond(rets.cov().values)


def test_min_variance_prefers_low_vol_asset():
    cov = pd.DataFrame([[0.04, 0.0], [0.0, 0.01]], index=["hi", "lo"], columns=["hi", "lo"])
    w = P.min_variance(cov)
    assert w["lo"] == pytest.approx(0.8, abs=1e-4)   # analytic: w ~ 1/var -> 0.8/0.2
    assert w.sum() == pytest.approx(1.0)


def _toy_prices(n=200, k=4, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    rets = rng.normal(0.0005, 0.03, size=(n, k))
    prices = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=list("ABCD"))
    vol = pd.DataFrame(1e8, index=idx, columns=prices.columns)
    return prices, vol


def test_backtest_has_no_look_ahead():
    """Changing prices AFTER day d must not change any portfolio return up to day d."""
    prices, vol = _toy_prices()
    kw = dict(est_window=60, every=20, adv_window=30)
    base = P.backtest(prices, vol, P.inverse_vol, **kw)["returns"]
    cut = prices.index[130]
    shocked = prices.copy()
    shocked.loc[shocked.index > cut] *= np.linspace(0.3, 3.0, (shocked.index > cut).sum())[:, None]
    alt = P.backtest(shocked, vol, P.inverse_vol, **kw)["returns"]
    pd.testing.assert_series_equal(base.loc[:cut], alt.loc[:cut])
    assert not np.allclose(base.loc[cut:].iloc[1:], alt.loc[cut:].iloc[1:])


def test_backtest_equal_weight_first_day_matches_hand_calc():
    prices, vol = _toy_prices()
    r = P.backtest(prices, vol, lambda h: P.equal_weight(h.columns), est_window=60, every=20, adv_window=30)
    simple = prices.pct_change().iloc[1:]
    first = simple.index[60]
    assert r["returns"].iloc[0] == pytest.approx(simple.loc[first].mean())


def test_token_risk_on_hand_computed_series():
    idx = pd.date_range("2026-01-01", periods=4, freq="D")
    price = pd.Series([100.0, 120.0, 90.0, 110.0], index=idx)
    r = P.token_risk(price, price, window=3, periods=365)
    assert r["return_total"] == pytest.approx(0.10)
    assert r["max_drawdown"] == pytest.approx(90 / 120 - 1)      # -25%
    assert r["corr_btc"] == pytest.approx(1.0)                    # a series vs itself
    lr = np.log(price / price.shift(1)).dropna()
    assert r["vol_ann"] == pytest.approx(lr.std(ddof=1) * np.sqrt(365))
