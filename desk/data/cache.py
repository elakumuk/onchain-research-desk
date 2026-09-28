"""HTTP fetching with retry + a timestamped on-disk cache.

Every successful API response is written to
    data/raw/<source>/<key>/<UTC timestamp>.json
wrapped in an envelope that records *when* and *from where* it was pulled.
Nothing is ever overwritten, so any past run can be re-analysed exactly.

Lookup order depends on the mode:
    live      fresh raw cache (< TTL)  -> network -> stale raw cache -> snapshot
    offline   latest raw cache -> snapshot            (never touches the network)
    snapshot  committed snapshot only                 (reproduces the README numbers)
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from desk.config import RAW_DIR, SNAPSHOT_DIR, PARAMS

log = logging.getLogger(__name__)

MODES = ("live", "offline", "snapshot")
_USER_AGENT = "onchain-research-desk/0.1 (research; public endpoints only)"


class DataUnavailable(RuntimeError):
    """Raised when neither the network nor any cache can supply a payload."""


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _parse_stamp(s: str) -> datetime:
    return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def _latest(dirpath: Path) -> Path | None:
    files = sorted(dirpath.glob("*.json"))
    return files[-1] if files else None


def http_get_json(url: str, params: dict | None = None, *, retries: int = 5,
                  base_sleep: float = 2.0, timeout: float = 30.0):
    """GET a JSON endpoint with exponential backoff.

    Retries on network errors, HTTP 429 (rate limit) and 5xx. Honors a
    Retry-After header when the server sends one. 4xx other than 429 is a
    real "no such resource" answer and is raised immediately.
    """
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After") or base_sleep * 2 ** attempt)
                wait = min(wait, 90.0)
                log.warning("HTTP %s from %s -- retry %d in %.0fs", r.status_code, url, attempt + 1, wait)
                last_err = requests.HTTPError(f"{r.status_code} for {url}")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.HTTPError:
            raise
        except (requests.ConnectionError, requests.Timeout, ValueError) as e:
            last_err = e
            wait = base_sleep * 2 ** attempt
            log.warning("%s on %s -- retry %d in %.0fs", type(e).__name__, url, attempt + 1, wait)
            time.sleep(wait)
    raise DataUnavailable(f"gave up on {url}: {last_err}")


class Cache:
    """Cache-aware fetcher. One instance per run; it remembers what it served."""

    def __init__(self, mode: str = "live", ttl_hours: float = PARAMS.cache_ttl_hours,
                 raw_dir: Path = RAW_DIR, snapshot_dir: Path = SNAPSHOT_DIR):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        self.ttl_s = ttl_hours * 3600
        self.raw_dir = raw_dir
        self.snapshot_dir = snapshot_dir
        self.served: list[dict] = []   # provenance log -> reports/data_provenance.csv

    # -- paths -----------------------------------------------------------
    def _raw_path(self, source: str, key: str) -> Path:
        return self.raw_dir / source / key

    def _snap_file(self, source: str, key: str) -> Path:
        return self.snapshot_dir / source / f"{key}.json"

    # -- public API --------------------------------------------------------
    def get(self, source: str, key: str, url: str, params: dict | None = None,
            transform=None, pause: float = 0.0):
        """Return the payload for (source, key), honoring the cache mode.

        `transform` (optional) is applied to the raw JSON *before* caching,
        used to trim very large responses (e.g. full order books) to the part
        the analysis uses. `pause` is a politeness sleep after a network hit.
        """
        now = datetime.now(timezone.utc)
        raw_dir = self._raw_path(source, key)

        if self.mode == "snapshot":
            return self._from_snapshot(source, key)

        latest = _latest(raw_dir) if raw_dir.exists() else None
        if self.mode == "offline":
            if latest is not None:
                return self._load(latest, source, key, "raw-cache")
            return self._from_snapshot(source, key)

        # live
        if latest is not None and (now - _parse_stamp(latest.stem)).total_seconds() < self.ttl_s:
            return self._load(latest, source, key, "raw-cache(fresh)")
        try:
            payload = http_get_json(url, params)
            if transform is not None:
                payload = transform(payload)
            raw_dir.mkdir(parents=True, exist_ok=True)
            env = {"fetched_at": now.isoformat(), "url": url, "params": params or {},
                   "payload": payload}
            out = raw_dir / f"{_stamp(now)}.json"
            out.write_text(json.dumps(env))
            self.served.append({"source": source, "key": key, "origin": "network",
                                "fetched_at": env["fetched_at"], "file": str(out)})
            if pause:
                time.sleep(pause)
            return payload
        except requests.HTTPError as e:
            # A definite "not found / bad request" answer. It is cached as a
            # *negative* result (payload None + status) so offline/snapshot runs
            # can tell "the API said this does not exist" from "file missing".
            code = e.response.status_code if e.response is not None else None
            raw_dir.mkdir(parents=True, exist_ok=True)
            env = {"fetched_at": now.isoformat(), "url": url, "params": params or {},
                   "payload": None, "http_status": code}
            out = raw_dir / f"{_stamp(now)}.json"
            out.write_text(json.dumps(env))
            self.served.append({"source": source, "key": key, "origin": f"network http-{code}",
                                "fetched_at": env["fetched_at"], "file": str(out)})
            raise DataUnavailable(str(e)) from e
        except DataUnavailable as e:
            log.warning("network failed for %s/%s (%s); falling back to cache", source, key, e)
            if latest is not None:
                return self._load(latest, source, key, "raw-cache(STALE fallback)")
            return self._from_snapshot(source, key)

    # -- helpers ---------------------------------------------------------
    def _load(self, path: Path, source: str, key: str, origin: str):
        env = json.loads(path.read_text())
        status = env.get("http_status")
        self.served.append({"source": source, "key": key,
                            "origin": origin if status is None else f"{origin} http-{status}",
                            "fetched_at": env.get("fetched_at"), "file": str(path)})
        if status is not None:
            raise DataUnavailable(f"{source}/{key}: cached HTTP {status} from {env.get('url')}")
        return env["payload"]

    def _from_snapshot(self, source: str, key: str):
        f = self._snap_file(source, key)
        if not f.exists():
            self.served.append({"source": source, "key": key, "origin": "missing",
                                "fetched_at": None, "file": None})
            raise DataUnavailable(f"no snapshot for {source}/{key}")
        return self._load(f, source, key, "snapshot")

    def save_snapshot(self) -> int:
        """Copy the exact raw files served in this run into data/snapshot/.

        Only files actually used are copied, so the snapshot is the minimal set
        that reproduces this run's report with `--mode snapshot`.
        """
        if self.snapshot_dir.exists():
            shutil.rmtree(self.snapshot_dir)
        n = 0
        for rec in self.served:
            f = rec.get("file")
            if not f or rec["origin"] == "snapshot":
                continue
            src = Path(f)
            dst = self._snap_file(rec["source"], rec["key"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
        return n
