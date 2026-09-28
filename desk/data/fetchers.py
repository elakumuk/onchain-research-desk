"""Source-specific fetchers. Each returns tidy pandas objects.

Sources (all free, keyless, public):
  * CoinGecko   /coins/markets, /coins/{id}/market_chart   -- prices, volumes, market caps
  * DefiLlama   /summary/fees/{slug}?dataType=...           -- fees, revenue, holders revenue
  * Coinbase    /products/{pair}/book?level=2               -- live L2 order book
  * Kraken      /0/public/Depth?pair=...                    -- live L2 order book (top 500 levels)

Large raw responses are trimmed *before* caching (see `transform=`), and the
trim is documented in ARCHITECTURE.md.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from desk.config import PARAMS
from desk.data.cache import Cache, DataUnavailable

log = logging.getLogger(__name__)

CG = "https://api.coingecko.com/api/v3"
LLAMA = "https://api.llama.fi"
COINBASE = "https://api.exchange.coinbase.com"
KRAKEN = "https://api.kraken.com/0/public"

CG_PAUSE = 6.5   # CoinGecko's keyless tier allows roughly 10 calls/minute


# ---------------------------------------------------------------- CoinGecko
def cg_markets(cache: Cache, cg_ids: list[str]) -> pd.DataFrame:
    payload = cache.get("coingecko", "markets", f"{CG}/coins/markets",
                        {"vs_currency": "usd", "ids": ",".join(sorted(cg_ids)),
                         "per_page": 250, "page": 1}, pause=CG_PAUSE)
    df = pd.DataFrame(payload)
    keep = ["id", "symbol", "current_price", "market_cap", "fully_diluted_valuation",
            "total_volume", "circulating_supply", "total_supply", "max_supply", "last_updated"]
    return df[keep].set_index("id")


def cg_history(cache: Cache, cg_id: str, days: int = 365) -> pd.DataFrame:
    """Daily close price, 24h volume and market cap, indexed by UTC date.

    CoinGecko returns one point per day at 00:00 UTC plus a final intraday
    point for "now". The intraday point is dropped so every row is a
    completed day (no partial-day look-ahead).
    """
    payload = cache.get("coingecko", f"history_{cg_id}", f"{CG}/coins/{cg_id}/market_chart",
                        {"vs_currency": "usd", "days": days, "interval": "daily"}, pause=CG_PAUSE)
    frames = {}
    for field, col in (("prices", "price"), ("total_volumes", "volume_usd"), ("market_caps", "mcap")):
        s = pd.DataFrame(payload[field], columns=["ts", col])
        s["ts"] = pd.to_datetime(s["ts"], unit="ms", utc=True)
        frames[col] = s.set_index("ts")[col]
    df = pd.concat(frames, axis=1)
    df = df[(df.index.hour == 0) & (df.index.minute == 0)]           # completed days only
    df.index = df.index.normalize().tz_localize(None)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


# ---------------------------------------------------------------- DefiLlama
_LLAMA_KEEP = ("name", "slug", "symbol", "gecko_id", "category", "protocolType", "methodology",
               "total24h", "total30d", "total1y", "totalAllTime")


def _trim_llama(payload: dict) -> dict:
    """Keep metadata + the daily total series; drop the (large) per-chain breakdown."""
    out = {k: payload.get(k) for k in _LLAMA_KEEP}
    out["totalDataChart"] = payload.get("totalDataChart") or []
    return out


def llama_fee_series(cache: Cache, slug: str, data_type: str) -> tuple[pd.Series, dict]:
    """Daily USD series for dataType in {dailyFees, dailyRevenue, dailyHoldersRevenue}."""
    payload = cache.get("defillama", f"{slug}_{data_type}", f"{LLAMA}/summary/fees/{slug}",
                        {"dataType": data_type}, transform=_trim_llama)
    chart = payload.get("totalDataChart") or []
    s = pd.Series({pd.Timestamp(int(t), unit="s"): float(v) for t, v in chart}, dtype=float)
    s = s.sort_index()
    meta = {k: payload.get(k) for k in _LLAMA_KEEP}
    return s, meta


# ---------------------------------------------------------------- order books
def _levels(rows) -> np.ndarray:
    """[[price, size, ...], ...] -> float array of shape (n, 2)."""
    if not rows:
        return np.empty((0, 2))
    return np.array([[float(r[0]), float(r[1])] for r in rows])


def _trim_book(bids: np.ndarray, asks: np.ndarray, band: float) -> tuple[np.ndarray, np.ndarray]:
    mid = (bids[0, 0] + asks[0, 0]) / 2
    return bids[bids[:, 0] >= mid * (1 - band)], asks[asks[:, 0] <= mid * (1 + band)]


def coinbase_book(cache: Cache, pair: str) -> dict:
    def transform(p):
        bids, asks = _levels(p["bids"]), _levels(p["asks"])
        # coverage = how far from mid the *venue's full book* extends, recorded before trimming
        mid = (bids[0, 0] + asks[0, 0]) / 2
        cov = float(min(1 - bids[-1, 0] / mid, asks[-1, 0] / mid - 1))
        b, a = _trim_book(bids, asks, PARAMS.book_keep_band)
        return {"bids": b.tolist(), "asks": a.tolist(), "coverage": cov, "venue_time": p.get("time")}
    return cache.get("coinbase", f"book_{pair}", f"{COINBASE}/products/{pair}/book",
                     {"level": 2}, transform=transform, pause=0.35)


def kraken_book(cache: Cache, pair: str) -> dict:
    def transform(p):
        if p.get("error"):
            raise DataUnavailable(f"kraken error {p['error']}")
        v = next(iter(p["result"].values()))
        bids, asks = _levels(v["bids"]), _levels(v["asks"])
        mid = (bids[0, 0] + asks[0, 0]) / 2
        cov = float(min(1 - bids[-1, 0] / mid, asks[-1, 0] / mid - 1))
        b, a = _trim_book(bids, asks, PARAMS.book_keep_band)
        return {"bids": b.tolist(), "asks": a.tolist(), "coverage": cov}
    return cache.get("kraken", f"book_{pair}", f"{KRAKEN}/Depth",
                     {"pair": pair, "count": 500}, transform=transform, pause=0.35)
