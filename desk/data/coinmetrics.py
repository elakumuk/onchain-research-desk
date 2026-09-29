"""Coin Metrics Community API: long daily history, kept in an append-only store in the repo.

    GET https://community-api.coinmetrics.io/v4/timeseries/asset-metrics
        ?assets=<id>&metrics=PriceUSD,volume_reported_spot_usd_1d&frequency=1d
        &start_time=<date>&page_size=10000&paging_from=start

Keyless. PriceUSD is Coin Metrics' daily reference price (the close at 00:00 UTC
at the end of the stamped day); volume_reported_spot_usd_1d is spot volume as
*reported* by the exchanges Coin Metrics covers (all venues, wash trading
included). Both are in US dollars.

Why a store instead of the request cache used for the snapshot data
-------------------------------------------------------------------
The v1 cache keeps one file per API call, which suits payloads that are
re-pulled whole every week. A ten-year history is different: re-downloading
and re-committing 90 assets x 4,000 days every week would grow git by
megabytes a week. The store instead keeps

    data/history/coinmetrics/<YYYY>.csv.gz   one file per closed calendar year, all assets (long format)
    data/history/coinmetrics/<YYYY>.csv      the open (current) year, plain text
    data/history/coinmetrics/manifest.json   per asset: metrics, first/last day, when and from where it was fetched

and each live run re-pulls only the last `refetch_overlap_days` for each asset
and replaces those rows. Completed years therefore never change and only the
open year's plain-text file changes each week, which git stores as a small
delta. Files are written
only when their content changes, with a fixed gzip header (mtime 0), so an
unchanged year is byte-identical on every machine that has not rewritten it.

Only completed days are kept: a row stamped D is kept if D + 1 day is not
after the fetch time. A row older than the overlap window is never re-pulled,
so a late revision by Coin Metrics to old data is not picked up;
`python -m desk.run --refresh-history` re-pulls everything.
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from desk.config import HISTORY_DIR, LONGRUN, ROOT
from desk.data.cache import DataUnavailable, http_get_json

log = logging.getLogger(__name__)

CM = "https://community-api.coinmetrics.io/v4"
ENDPOINT = f"{CM}/timeseries/asset-metrics"
PRICE, VOLUME = "PriceUSD", "volume_reported_spot_usd_1d"
COLS = {PRICE: "price_usd", VOLUME: "volume_usd"}
CM_PAUSE = 0.7          # the documented community limit is about 10 requests per 6 seconds


def _fmt(v, sig):
    return "" if pd.isna(v) else f"{float(v):.{sig}g}"


def _year_text(df: pd.DataFrame, columns=("price_usd", "volume_usd"), sigs=(8, 5)) -> str:
    """Canonical CSV text for one year: sorted, fixed float formats, so it is deterministic."""
    df = df.sort_values(["asset", "date"])
    lines = ["asset,date," + ",".join(columns)]
    for a, d, p, v in zip(df["asset"], df["date"], df[columns[0]], df[columns[1]]):
        lines.append(f"{a},{d:%Y-%m-%d},{_fmt(p, sigs[0])},{_fmt(v, sigs[1])}")
    return "\n".join(lines) + "\n"


class HistoryStore:
    """Append-only daily history for a set of Coin Metrics asset ids.

    needs: {asset_id: set of metrics} -- which metrics to keep for which id.
    """
    COLUMNS = ("price_usd", "volume_usd")
    SIGS = (8, 5)

    def __init__(self, needs: dict[str, set], directory: Path = HISTORY_DIR,
                 start: str = LONGRUN.history_start, overlap_days: int = LONGRUN.refetch_overlap_days):
        self.needs = {a: tuple(m for m in (PRICE, VOLUME) if m in ms) for a, ms in needs.items()}
        self.dir = Path(directory)
        self.start = pd.Timestamp(start)
        self.overlap = overlap_days
        self.manifest_path = self.dir / "manifest.json"
        self.manifest: dict = json.loads(self.manifest_path.read_text()) if self.manifest_path.exists() else {}
        self.data = self._read()
        self.served: list[dict] = []

    # ------------------------------------------------------------ disk
    def _read(self) -> pd.DataFrame:
        files = sorted(self.dir.glob("*.csv.gz")) + sorted(self.dir.glob("*.csv"))
        frames = [pd.read_csv(f, parse_dates=["date"]) for f in files]
        if not frames:
            return pd.DataFrame({"asset": pd.Series(dtype=str), "date": pd.Series(dtype="datetime64[ns]"),
                                 **{c: pd.Series(dtype=float) for c in self.COLUMNS}})
        df = pd.concat(frames, ignore_index=True)
        df["asset"] = df["asset"].astype(str)
        return df

    def _write(self) -> list[str]:
        """Write each year's file only if its content changed. Returns the files written.

        Closed years are gzipped (they never change again). The open year, the
        one holding the newest data, is plain CSV: it changes every week, and git
        stores a week's change to a text file as a small delta, whereas every
        rewrite of a gzip file would be stored in full.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        written = []
        open_year = int(self.data["date"].max().year) if len(self.data) else None
        for year, part in self.data.groupby(self.data["date"].dt.year):
            text = _year_text(part, self.COLUMNS, self.SIGS)
            gz, plain = self.dir / f"{year}.csv.gz", self.dir / f"{year}.csv"
            if year == open_year:
                f, other, blob = plain, gz, text.encode()
            else:
                f, other, blob = gz, plain, None
            if other.exists():
                other.unlink()
            current = None
            if f.exists():
                raw = f.read_bytes()
                current = raw.decode() if f is plain else gzip.decompress(raw).decode()
            if current == text:
                continue
            f.write_bytes(blob if blob is not None else gzip.compress(text.encode(), compresslevel=9, mtime=0))
            written.append(f.name)
        self.manifest_path.write_text(json.dumps(self.manifest, indent=1, sort_keys=True) + "\n")
        return written

    # ------------------------------------------------------------ network
    def _fetch(self, assets: list[str], metrics: tuple, start: pd.Timestamp, now: datetime) -> pd.DataFrame:
        """One (paged) request for several assets that need the same metrics from the same day."""
        params = {"assets": ",".join(assets), "metrics": ",".join(metrics), "frequency": "1d",
                  "start_time": f"{start:%Y-%m-%d}", "page_size": 10000, "paging_from": "start"}
        rows, url, p = [], ENDPOINT, params
        while url:
            payload = http_get_json(url, p)
            rows += payload.get("data") or []
            url, p = payload.get("next_page_url"), None       # next_page_url already carries the query
            time.sleep(CM_PAUSE)
        df = pd.DataFrame(rows)
        if df.empty:
            return pd.DataFrame(columns=["asset", "date", "price_usd", "volume_usd"])
        df["date"] = pd.to_datetime(df["time"].str[:10])
        now_utc = pd.Timestamp(now).tz_convert(None)
        df = df[df["date"] + pd.Timedelta(days=1) <= now_utc]      # completed days only
        out = pd.DataFrame({"asset": df["asset"].astype(str), "date": df["date"]})
        for m, col in COLS.items():
            out[col] = pd.to_numeric(df[m], errors="coerce") if m in df else float("nan")
        return out.reset_index(drop=True)

    def _plan(self, full: bool, now: datetime) -> dict:
        """Group assets into batched requests: {(metrics, start): [assets]}.

        An asset whose stored data is recent shares a common start date (the
        last 45 days) with every other recent asset, so a weekly run needs a
        handful of requests instead of one per asset. An asset seen for the
        first time, or whose data stopped long ago, gets its own start date.
        """
        recent = (pd.Timestamp(now).tz_convert(None) - pd.Timedelta(days=45)).normalize()
        groups: dict = {}
        for asset, metrics in sorted(self.needs.items()):
            m = self.manifest.get(asset)
            same_metrics = m is not None and tuple(m.get("metrics", ())) == metrics
            if full or not same_metrics or not m.get("last"):
                start = self.start
            else:
                start = max(self.start, pd.Timestamp(m["last"]) - pd.Timedelta(days=self.overlap))
                start = min(start, recent) if start >= recent else start
            groups.setdefault((metrics, start), []).append(asset)
        return groups

    def update(self, full: bool = False, batch: int = 40) -> None:
        """Live mode: pull the tail (or everything) for every asset and merge it in."""
        now = datetime.now(timezone.utc)
        for (metrics, start), assets in sorted(self._plan(full, now).items(), key=lambda kv: (kv[0][1], kv[0][0])):
            for i in range(0, len(assets), batch):
                chunk = assets[i:i + batch]
                try:
                    new = self._fetch(chunk, metrics, start, now)
                    got = {a: new[new["asset"] == a] for a in chunk}
                except (DataUnavailable, requests.HTTPError) as e:
                    got = {}
                    for a in chunk:                      # isolate the asset that fails
                        try:
                            got[a] = self._fetch([a], metrics, start, now)
                        except (DataUnavailable, requests.HTTPError) as e1:
                            log.warning("coinmetrics %s failed (%s); keeping stored history", a, e1)
                            self._served(a, "history-store(STALE: fetch failed)")
                for asset, rows in got.items():
                    self._merge(asset, rows, metrics, start, now, full)
        self.data = self.data.sort_values(["asset", "date"]).reset_index(drop=True)
        written = self._write()
        log.info("coinmetrics store: rewrote %s", written or "nothing")

    def _merge(self, asset, new, metrics, start, now, full) -> None:
        keep = ~((self.data["asset"] == asset) & (self.data["date"] >= start))
        if new.empty and not full:
            keep[:] = True                                      # nothing new: never delete stored history
        self.data = pd.concat([self.data[keep], new], ignore_index=True)
        mine = self.data[self.data["asset"] == asset]
        self.manifest[asset] = {
            "metrics": list(metrics),
            "first": f"{mine['date'].min():%Y-%m-%d}" if len(mine) else None,
            "last": f"{mine['date'].max():%Y-%m-%d}" if len(mine) else None,
            "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "url": ENDPOINT,
            "params": {"assets": asset, "metrics": ",".join(metrics), "frequency": "1d"},
        }
        self._served(asset, "network")

    def load(self) -> None:
        """Offline / snapshot mode: use the committed store as is."""
        for asset in sorted(self.needs):
            if asset not in self.manifest:
                raise DataUnavailable(f"coinmetrics: {asset} is not in the history store; run live once")
            self._served(asset, "history-store")

    def _served(self, asset: str, origin: str) -> None:
        m = self.manifest.get(asset, {})
        self.served.append({"source": "coinmetrics", "key": asset, "origin": origin,
                            "fetched_at": m.get("fetched_at"),
                            "file": str(self.manifest_path.relative_to(ROOT)) if self.manifest_path.is_relative_to(ROOT)
                            else str(self.manifest_path),
                            "url": m.get("url"), "params": m.get("params") or {}})

    # ------------------------------------------------------------ access
    def panel(self, column: str, assets: list[str]) -> pd.DataFrame:
        """Wide daily frame (date x asset) of one column, on a complete calendar index."""
        d = self.data[self.data["asset"].isin(assets)]
        wide = d.pivot(index="date", columns="asset", values=column)
        if wide.empty:
            return wide
        idx = pd.date_range(max(wide.index.min(), self.start), wide.index.max(), freq="D")
        return wide.reindex(idx).reindex(columns=assets)


def needs_for(candidates) -> dict[str, set]:
    """Which Coin Metrics ids to keep and which metrics for each (plus USDT's price, for Binance closes)."""
    needs: dict[str, set] = {"usdt": {PRICE}}
    for h in candidates:
        if h.price_source == "coinmetrics":
            needs.setdefault(h.price_asset, set()).add(PRICE)
        needs.setdefault(h.volume_asset, set()).add(VOLUME)
    return needs


def open_store(mode: str, full_refresh: bool = False, **kw) -> HistoryStore:
    from desk.config import HISTORY_CANDIDATES
    store = HistoryStore(needs_for(HISTORY_CANDIDATES), **kw)
    if mode == "live":
        store.update(full=full_refresh)
    else:
        store.load()
    return store
