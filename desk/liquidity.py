"""Liquidity profile: how expensive is it to get in and out of each token?

Two complementary lenses, deliberately:
  * Amihud illiquidity  -- a *historical, daily* measure: how far price moves per
    dollar traded. Cheap to compute from any price/volume history, comparable
    across time, but blind to intraday execution.
  * Order-book walk      -- a *live, microstructure* measure: what a market order
    of a given size would actually pay against the resting book right now.
    Exact for the instant it was captured, but a single snapshot.
Together they answer "is this liquid?" from both the past-average and the
right-now angles. See ARCHITECTURE.md for the trade-offs.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# --------------------------------------------------------------------- Amihud
def amihud(price: pd.Series, dollar_volume: pd.Series, window: int) -> float:
    """Amihud (2002) illiquidity over the last `window` days.

    ILLIQ = mean_t( |R_t| / DollarVolume_t ), with R_t the *simple* daily return.
    Returned in basis points of price move per $1M traded, so a value of 0.5
    means "on an average day, each $1M of volume went with a 0.5 bp move".
    Days with zero or missing volume are excluded (division undefined).
    """
    r = price.pct_change()
    df = pd.DataFrame({"r": r, "dv": dollar_volume}).dropna()
    df = df[df["dv"] > 0].tail(window)
    if df.empty:
        return float("nan")
    return float((df["r"].abs() / df["dv"]).mean() * 1e6 * 1e4)


def adv(dollar_volume: pd.Series, window: int) -> float:
    """Average daily dollar volume, as the *median* of the trailing window.

    Median, not mean: crypto volume has single-day spikes (listings,
    liquidation cascades) that would overstate the volume you can rely on.
    """
    s = dollar_volume.dropna().tail(window)
    return float(s.median()) if len(s) else float("nan")


def days_to_liquidate(position_usd: float, adv_usd: float, participation: float) -> float:
    """Days needed to exit `position_usd` trading at most `participation` x ADV per day."""
    if not adv_usd or adv_usd <= 0 or np.isnan(adv_usd):
        return float("inf")
    return position_usd / (participation * adv_usd)


# ----------------------------------------------------------------- order book
def merge_books(books: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """Consolidate several venues' books into one (price, size) ladder per side.

    Each venue's levels are first re-expressed *relative to that venue's own
    mid*, then placed on a common reference mid (the average of the venue
    mids). Why: the venues are snapshotted a second or two apart, so their
    raw prices can be offset by a few bps; naively stacking raw prices would
    create a "crossed" book (best bid above best ask) and report negative
    spreads and free fills that no one could actually capture. Normalising
    keeps each venue's *shape* (how much size sits how far from its mid),
    which is what slippage depends on.

    The result is the book a smart-order-router could hit if it split an
    order across venues. It ignores per-venue fees and the latency of hitting
    two venues at once, so it is a mildly optimistic best case.
    """
    mids, rel_b, rel_a = [], [], []
    for b in books:
        bb = np.asarray(b["bids"], dtype=float).reshape(-1, 2)
        aa = np.asarray(b["asks"], dtype=float).reshape(-1, 2)
        m = (bb[0, 0] + aa[0, 0]) / 2
        mids.append(m)
        rel_b.append(np.column_stack([bb[:, 0] / m, bb[:, 1]]))
        rel_a.append(np.column_stack([aa[:, 0] / m, aa[:, 1]]))
    ref = float(np.mean(mids))
    bids, asks = np.vstack(rel_b), np.vstack(rel_a)
    bids[:, 0] *= ref
    asks[:, 0] *= ref
    bids = bids[np.argsort(-bids[:, 0], kind="stable")]
    asks = asks[np.argsort(asks[:, 0], kind="stable")]
    return bids, asks


def mid_price(bids: np.ndarray, asks: np.ndarray) -> float:
    return (bids[0, 0] + asks[0, 0]) / 2


def depth_within(bids: np.ndarray, asks: np.ndarray, band: float, mid: float | None = None) -> tuple[float, float]:
    """USD notional resting within +/- `band` of mid: (bid_depth, ask_depth)."""
    mid = mid_price(bids, asks) if mid is None else mid
    b = bids[bids[:, 0] >= mid * (1 - band)]
    a = asks[asks[:, 0] <= mid * (1 + band)]
    return float((b[:, 0] * b[:, 1]).sum()), float((a[:, 0] * a[:, 1]).sum())


def walk_book(levels: np.ndarray, qty: float) -> tuple[float, bool]:
    """Fill `qty` base units against price levels (best first).

    Returns (volume-weighted average fill price, exhausted). If the visible
    book runs out before the order is filled, returns (nan, True): we do NOT
    extrapolate beyond what the data shows.
    """
    if qty <= 0:
        raise ValueError("qty must be positive")
    sizes = levels[:, 1]
    cum = np.cumsum(sizes)
    if cum[-1] < qty:
        return float("nan"), True
    k = int(np.searchsorted(cum, qty))          # first level where cumulative size >= qty
    filled_before = cum[k - 1] if k > 0 else 0.0
    notional = float((levels[:k, 0] * sizes[:k]).sum() + levels[k, 0] * (qty - filled_before))
    return notional / qty, False


def slippage_bps(bids: np.ndarray, asks: np.ndarray, notional_usd: float,
                 mid: float | None = None) -> dict:
    """Cost vs mid of a market order of `notional_usd`, for each side, in bps.

    Order size in base units = notional / mid, so the buy and the sell are the
    same size. Slippage includes the half-spread (it is measured from mid).
    """
    mid = mid_price(bids, asks) if mid is None else mid
    qty = notional_usd / mid
    buy_px, buy_x = walk_book(asks, qty)
    sell_px, sell_x = walk_book(bids, qty)
    return {
        "buy_bps": (buy_px / mid - 1) * 1e4 if not buy_x else float("nan"),
        "sell_bps": (1 - sell_px / mid) * 1e4 if not sell_x else float("nan"),
        "exhausted": bool(buy_x or sell_x),
    }


def liquidity_profile(bids: np.ndarray, asks: np.ndarray, bands, sizes) -> dict:
    """All book-derived metrics for one token in one flat dict."""
    mid = mid_price(bids, asks)
    out = {"mid": mid, "spread_bps": (asks[0, 0] - bids[0, 0]) / mid * 1e4}
    for band in bands:
        bd, ad = depth_within(bids, asks, band, mid)
        tag = f"{band * 100:g}pct"
        out[f"depth_bid_{tag}_usd"] = bd
        out[f"depth_ask_{tag}_usd"] = ad
        out[f"depth_{tag}_usd"] = bd + ad
    for n in sizes:
        s = slippage_bps(bids, asks, n, mid)
        tag = _size_tag(n)
        out[f"slip_buy_{tag}_bps"] = s["buy_bps"]
        out[f"slip_sell_{tag}_bps"] = s["sell_bps"]
        # headline = the worse side; nan if the visible book cannot absorb it
        out[f"slip_{tag}_bps"] = (float("nan") if s["exhausted"]
                                  else max(s["buy_bps"], s["sell_bps"]))
    return out


def _size_tag(n: float) -> str:
    return f"{n / 1e6:g}m" if n >= 1e6 else f"{n / 1e3:g}k"
