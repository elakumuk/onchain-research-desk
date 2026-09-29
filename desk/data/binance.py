"""Binance public data archive (data.binance.vision): daily spot klines, keyless.

    https://data.binance.vision/data/spot/monthly/klines/<SYMBOL>/1d/<SYMBOL>-1d-<YYYY-MM>.zip
    https://data.binance.vision/data/spot/daily/klines/<SYMBOL>/1d/<SYMBOL>-1d-<YYYY-MM-DD>.zip

Why this source, and what it is used for
----------------------------------------
Coin Metrics' keyless API publishes a daily *price* for only ~140 assets, and
from 2021 on several of the largest assets by volume (SOL, LUNA, ATOM, ...)
are not among them (see longrun.coverage). Leaving them out would make the
2021-26 universe "the top 20 among assets Coin Metrics happens to price for
free", which is not a universe a fund could have chosen.

The Binance archive keeps the full daily history of every spot pair Binance
ever listed, *including delisted ones* (LUNAUSDT up to the Terra collapse,
FTTUSDT, SRMUSDT). It is a static file host, reachable from the US (the
Binance REST API is not). It is used for one thing only: the daily closing
price of assets that Coin Metrics does not price. Eligibility, ranking and
the liquidity caps always use Coin Metrics' reported volume, so the volume
definition stays the same for every asset in the universe. Binance's own
single-venue volume is stored only to validate the ticker mapping.

Prices are in USDT. They are converted to USD with Coin Metrics' USDT
PriceUSD for the same day (USDT traded as low as about $0.95 in May 2022).

The store has the same layout as the Coin Metrics one (see coinmetrics.py):
data/history/binance/<YYYY>.csv[.gz] + manifest.json, append-only, with the
recent days re-downloaded on every live run.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from desk.config import LONGRUN, ROOT
from desk.data.cache import DataUnavailable
from desk.data.coinmetrics import HistoryStore

log = logging.getLogger(__name__)

ARCHIVE = "https://data.binance.vision"
LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
BINANCE_DIR = ROOT / "data" / "history" / "binance"
_KEY = re.compile(r"<Key>([^<]+\.zip)</Key>")
_session = requests.Session()
_session.headers["User-Agent"] = "onchain-research-desk/0.2 (research; public endpoints only)"


def _get(url: str, params: dict | None = None, retries: int = 5) -> requests.Response:
    last = None
    for attempt in range(retries):
        try:
            r = _session.get(url, params=params, timeout=30)
            if r.status_code == 404:
                return r
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(f"{r.status_code} for {url}")
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last = e
            import time
            time.sleep(2 * 2 ** attempt)
    raise DataUnavailable(f"gave up on {url}: {last}")


def list_keys(prefix: str) -> list[str]:
    keys, marker = [], ""
    while True:
        r = _get(LISTING, {"delimiter": "/", "prefix": prefix, "marker": marker})
        page = _KEY.findall(r.text)
        keys += page
        if "<IsTruncated>true</IsTruncated>" not in r.text or not page:
            return sorted(keys)
        marker = page[-1]


def parse_klines(raw: bytes) -> pd.DataFrame:
    """One zipped kline CSV -> (date, close, quote_volume). Handles ms and (2025+) us timestamps."""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        text = z.read(z.namelist()[0]).decode()
    df = pd.read_csv(io.StringIO(text), header=None)
    if not str(df.iloc[0, 0]).isdigit():            # some files carry a header row
        df = df.iloc[1:]
    t = pd.to_numeric(df[0])
    unit = "us" if t.max() > 1e14 else "ms"
    return pd.DataFrame({"date": pd.to_datetime(t, unit=unit).dt.normalize(),
                         "close_usdt": pd.to_numeric(df[4]).values,
                         "quote_volume_usdt": pd.to_numeric(df[7]).values})


class BinanceStore(HistoryStore):
    """Daily close and quote volume per Binance symbol, same append-only layout as Coin Metrics."""
    COLUMNS = ("close_usdt", "quote_volume_usdt")
    SIGS = (8, 5)

    def __init__(self, symbols: list[str], directory: Path = BINANCE_DIR, **kw):
        super().__init__({s: set() for s in symbols}, directory=directory, **kw)
        self.symbols = sorted(symbols)

    def _fetch_symbol(self, sym: str, since: pd.Timestamp | None) -> tuple[pd.DataFrame, list[str]]:
        all_monthly = list_keys(f"data/spot/monthly/klines/{sym}/1d/")
        mkeys = [k for k in all_monthly if since is None or k[-11:-4] >= f"{since:%Y-%m}"]
        last_month = max((k[-11:-4] for k in all_monthly), default=None)
        # Daily files exist only for the month(s) not yet packed into a monthly file, so look at the
        # current and the previous month only. A pair delisted long ago has no daily files to find.
        now = pd.Timestamp.utcnow().tz_localize(None)
        recent = [m for m in pd.date_range((now - pd.offsets.MonthBegin(2)).normalize(), now, freq="MS")
                  if last_month is None or f"{m:%Y-%m}" > last_month]
        dkeys = []
        for m in recent:
            dkeys += list_keys(f"data/spot/daily/klines/{sym}/1d/{sym}-1d-{m:%Y-%m}")
        if since is not None:
            dkeys = [k for k in dkeys if k[-14:-4] >= f"{since:%Y-%m-%d}"]
        frames = []
        for k in mkeys + dkeys:
            r = _get(f"{ARCHIVE}/{k}")
            if r.status_code == 404:
                continue
            frames.append(parse_klines(r.content))
        if not frames:
            return pd.DataFrame(columns=["asset", "date", *self.COLUMNS]), mkeys + dkeys
        df = pd.concat(frames).drop_duplicates("date", keep="last").sort_values("date")
        df = df[df["date"] >= self.start]
        return df.assign(asset=sym)[["asset", "date", *self.COLUMNS]], mkeys + dkeys

    def update(self, full: bool = False) -> None:
        now = datetime.now(timezone.utc)

        def job(sym):
            m = self.manifest.get(sym) or {}
            since = None
            if not full and m.get("last"):
                since = (pd.Timestamp(m["last"]) - pd.Timedelta(days=self.overlap)).replace(day=1)
            try:
                return sym, since, *self._fetch_symbol(sym, since), None
            except DataUnavailable as e:
                return sym, since, None, [], e

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(job, self.symbols))
        for sym, since, new, keys, err in sorted(results, key=lambda x: x[0]):
            if err is not None:
                log.warning("binance %s failed (%s); keeping stored history", sym, err)
                self._served(sym, "history-store(STALE: fetch failed)")
                continue
            if new is not None and len(new):
                cut = since if since is not None else pd.Timestamp("1900-01-01")
                keep = ~((self.data["asset"] == sym) & (self.data["date"] >= cut))
                self.data = pd.concat([self.data[keep], new], ignore_index=True)
            mine = self.data[self.data["asset"] == sym]
            self.manifest[sym] = {
                "first": f"{mine['date'].min():%Y-%m-%d}" if len(mine) else None,
                "last": f"{mine['date'].max():%Y-%m-%d}" if len(mine) else None,
                "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "url": f"{ARCHIVE}/data/spot/monthly/klines/{sym}/1d/",
                "params": {"files": len(keys), "since": f"{since:%Y-%m}" if since is not None else "start"},
                "listed": bool(len(mine)),
            }
            self._served(sym, "network")
        self.data = self.data.sort_values(["asset", "date"]).reset_index(drop=True)
        written = self._write() if len(self.data) else []
        log.info("binance store: rewrote %s", written or "nothing")

    def _served(self, sym: str, origin: str) -> None:
        super()._served(sym, origin)
        self.served[-1]["source"] = "binance"
