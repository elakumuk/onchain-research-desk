import numpy as np
import pandas as pd
import pytest

from desk import liquidity as L

# Synthetic book, mid = 100. Asks: 1 @ 100.5, 2 @ 101, 5 @ 102. Bids mirror it.
ASKS = np.array([[100.5, 1.0], [101.0, 2.0], [102.0, 5.0]])
BIDS = np.array([[99.5, 1.0], [99.0, 2.0], [98.0, 5.0]])


def test_walk_book_vwap_is_exact():
    # buy 2.5 units: 1 @ 100.5 + 1.5 @ 101 -> (100.5 + 151.5) / 2.5 = 100.8
    px, exhausted = L.walk_book(ASKS, 2.5)
    assert not exhausted
    assert px == pytest.approx(100.8)


def test_walk_book_exhausted_returns_nan_not_extrapolation():
    px, exhausted = L.walk_book(ASKS, 8.01)
    assert exhausted and np.isnan(px)


def test_slippage_symmetric_book_in_bps():
    # $250 at mid 100 = 2.5 units -> buy avg 100.8 = 80 bps; sell avg 99.2 = 80 bps
    s = L.slippage_bps(BIDS, ASKS, 250.0)
    assert s["buy_bps"] == pytest.approx(80.0)
    assert s["sell_bps"] == pytest.approx(80.0)
    assert not s["exhausted"]


def test_slippage_grows_with_size():
    small = L.slippage_bps(BIDS, ASKS, 50.0)["buy_bps"]
    large = L.slippage_bps(BIDS, ASKS, 600.0)["buy_bps"]
    assert large > small > 0


def test_depth_within_band():
    bid, ask = L.depth_within(BIDS, ASKS, 0.01)   # +/-1% of 100 -> levels 99..101
    assert bid == pytest.approx(99.5 * 1 + 99.0 * 2)
    assert ask == pytest.approx(100.5 * 1 + 101.0 * 2)


def test_merge_books_never_crossed_when_venues_are_offset():
    # venue B is quoted 2% higher than venue A (e.g. snapshots taken a moment apart)
    a = {"bids": BIDS.tolist(), "asks": ASKS.tolist()}
    b = {"bids": (BIDS * [1.02, 1]).tolist(), "asks": (ASKS * [1.02, 1]).tolist()}
    # a naive stack of raw prices WOULD be crossed: B's best bid > A's best ask
    assert b["bids"][0][0] > a["asks"][0][0]
    bids, asks = L.merge_books([a, b])
    assert bids[0, 0] < asks[0, 0]
    # reference mid is the average of the venue mids; spread preserved at 100 bps
    assert (bids[0, 0] + asks[0, 0]) / 2 == pytest.approx(101.0)
    assert (asks[0, 0] - bids[0, 0]) / 101.0 == pytest.approx(0.01)
    # sizes are conserved
    assert bids[:, 1].sum() == pytest.approx(16.0)
    assert asks[:, 1].sum() == pytest.approx(16.0)


def test_amihud_on_toy_data():
    price = pd.Series([100.0, 110.0, 99.0])        # returns +10%, -10%
    dv = pd.Series([1e6, 2e6, 1e6])                # volume on the return days: 2e6, 1e6
    # mean(|0.1|/2e6, |0.1|/1e6) = 7.5e-8 per $ -> x1e6 x1e4 = 750 bps per $1M
    assert L.amihud(price, dv, window=10) == pytest.approx(750.0)


def test_amihud_skips_zero_volume_days():
    price = pd.Series([100.0, 110.0, 99.0])
    dv = pd.Series([1e6, 0.0, 1e6])
    assert L.amihud(price, dv, window=10) == pytest.approx(0.1 / 1e6 * 1e10)


def test_adv_is_median_and_days_to_liquidate():
    dv = pd.Series([10.0, 10.0, 1000.0])           # one spike day
    assert L.adv(dv, 3) == 10.0
    assert L.days_to_liquidate(100.0, 10.0, 0.10) == pytest.approx(100.0)
    assert L.days_to_liquidate(100.0, 0.0, 0.10) == float("inf")
