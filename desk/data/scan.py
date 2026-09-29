"""Which assets could ever have been in the universe? A one-off, rule-based scan.

    python -m desk.data.scan            # writes data/history/universe_scan.csv

The long backtest ranks assets by reported volume at every month start. An
asset that never comes near the top of that ranking can never be selected, so
the desk only needs full histories for assets that did. This scan finds them
without choosing by hand:

  1. List every asset for which the keyless Coin Metrics Community API
     publishes daily reported spot volume (GET /v4/catalog/assets, ~3,200).
  2. Pull that volume for all of them since 2015 (batched requests; the
     payloads go to the gitignored data/raw/scan/ and are not committed).
  3. Drop fiat currencies, stablecoins, wrapped, staked and bridged
     representations (name and ticker rules below, config.HISTORY_EXCLUDED,
     and a short MANUAL_EXCLUDE list with a reason for each entry).
  4. At every month start, rank the rest by median reported volume over the
     previous `rank_window_days`, and keep every asset that was ever in the
     top SCAN_TOP_N.

Keeping "ever in the top N" is a filter on *what to download*, not on what
the backtest may hold: an asset that died after being large (LUNA, FTT) was
large, so it is kept. N = 50 leaves room for the K = 30 sensitivity run and
for top-ranked assets that have no price anywhere.

The result is written to data/history/universe_scan.csv (committed, for
audit) and copied into config.py by hand, so the candidate list is frozen and
readable. Re-run the scan to extend it.
"""
from __future__ import annotations

import argparse
import re
import sys
import time

import pandas as pd

from desk.config import HISTORY_DIR, HISTORY_EXCLUDED, LONGRUN, RAW_DIR
from desk.data.cache import http_get_json

CM = "https://community-api.coinmetrics.io/v4"
SCAN_TOP_N = 50
BATCH = 40

NOT_RISK_ASSET = re.compile(
    r"usd|dollar|tether|euro|\beur|wrapped|staked|bridged|binance-peg|\(pos\)|liquid staking|restaked|"
    r"savings|\bbtcb\b|pegged|synthetic|tokenized stock|xstock", re.I)
# Coin Metrics also reports volume for fiat currencies (the fiat leg of exchange pairs).
FIAT = set("""aed ars aud brl cad chf clp cny cop czk dkk eur gbp hkd hrk huf idr ils inr jpy kes krw kzt mad mxn
myr ngn nok nzd php pkr pln ron rub sar sek sgd thb top try twd uah usd ves vnd zar""".split())
FIAT_NAME = re.compile(
    r"\b(won|yen|rupiah|lira|rand|pound|krona|krone|peso|real|baht|rupee|naira|yuan|ringgit|franc|shilling|"
    r"zloty|kuna|dirham|koruna|hryvnia|ruble|riyal|forint|bol[ií]var|dong|pa.?anga|leu|lev|dinar|cedi|tenge|"
    r"lari|taka|quetzal|stables?)$", re.I)
# Other non-risk assets the rules above do not catch, found by reading the scan output.
MANUAL_EXCLUDE = {
    "qc": "stablecoin (Qcash, pegged to the Chinese yuan)",
    "ron": "id collides with the Romanian leu; its volume history starts years before the Ronin token",
}
CHAIN_SUFFIX = re.compile(r"_(eth|trx|avaxc|omni|sol|bsc|arb|op|base|eos|polygon|lido|bnb)$")
NOT_RISK_TICKER = re.compile(
    r"^(w|st|wst|cb|r|s|m|t|h|ren|sw|eth|bb|lb|ez|we|os|ank|sfrx|frx)?(btc|eth|sol|bnb|avax|matic)$"
    r"|usd|^dai$|^sai$|^tusd|^frax|^lusd|^gho$|^eurc|^eurs|^xaut$|^paxg$|^pax$|^husd$|^busd$|^gusd$|^usdk$|^ust$", re.I)


def is_risk_asset(asset: str, name: str) -> tuple[bool, str]:
    base = asset.split("_")[0]
    if asset in HISTORY_EXCLUDED:
        return False, HISTORY_EXCLUDED[asset]
    if asset in MANUAL_EXCLUDE:
        return False, MANUAL_EXCLUDE[asset]
    if asset in FIAT or FIAT_NAME.search(name or ""):
        return False, "fiat currency (or a stablecoin named like one)"
    if CHAIN_SUFFIX.search(asset):
        return False, "chain-specific representation (the base id carries the volume)"
    if base not in ("btc", "eth", "sol", "bnb", "avax", "matic") and NOT_RISK_TICKER.search(base):
        return False, "stablecoin / wrapped / staked ticker"
    if NOT_RISK_ASSET.search(name or ""):
        return False, "stablecoin / wrapped / staked name"
    return True, ""


def catalog() -> pd.DataFrame:
    doc = http_get_json(f"{CM}/catalog/assets")
    rows = []
    for a in doc["data"]:
        mets = {}
        for m in a.get("metrics", []):
            for f in m["frequencies"]:
                if f["frequency"] == "1d" and f.get("community"):
                    mets[m["metric"]] = (f["min_time"][:10], f["max_time"][:10])
        rows.append({"asset": a["asset"], "name": a.get("full_name", ""),
                     "has_volume": "volume_reported_spot_usd_1d" in mets, "has_price": "PriceUSD" in mets})
    return pd.DataFrame(rows).set_index("asset")


def pull_volume(assets: list[str], start: str) -> pd.DataFrame:
    out = RAW_DIR / "scan"
    out.mkdir(parents=True, exist_ok=True)
    frames = []
    for i in range(0, len(assets), BATCH):
        chunk = assets[i:i + BATCH]
        f = out / f"volume_{i:05d}.csv.gz"
        if f.exists():
            frames.append(pd.read_csv(f, parse_dates=["date"]))
            continue
        rows, url, params = [], f"{CM}/timeseries/asset-metrics", {
            "assets": ",".join(chunk), "metrics": "volume_reported_spot_usd_1d", "frequency": "1d",
            "start_time": start, "page_size": 10000, "paging_from": "start"}
        while url:
            doc = http_get_json(url, params)
            rows += doc.get("data") or []
            url, params = doc.get("next_page_url"), None
            time.sleep(0.4)
        df = pd.DataFrame(rows, columns=["asset", "time", "volume_reported_spot_usd_1d"])
        df = pd.DataFrame({"asset": df["asset"], "date": pd.to_datetime(df["time"].str[:10]),
                           "volume_usd": pd.to_numeric(df["volume_reported_spot_usd_1d"], errors="coerce")})
        df.to_csv(f, index=False)
        frames.append(df)
        print(f"scan: {i + len(chunk)}/{len(assets)} assets", file=sys.stderr)
    return pd.concat(frames, ignore_index=True)


def scan(top_n: int = SCAN_TOP_N) -> pd.DataFrame:
    cat = catalog()
    vol_assets = sorted(cat.index[cat["has_volume"]])
    long = pull_volume(vol_assets, LONGRUN.history_start)
    wide = long.pivot_table(index="date", columns="asset", values="volume_usd")
    wide = wide.reindex(pd.date_range(wide.index.min(), wide.index.max(), freq="D"))
    ok = {a: is_risk_asset(a, cat.loc[a, "name"]) for a in wide.columns}
    risk = [a for a, (y, _) in ok.items() if y]
    med = wide[risk].fillna(0).rolling(LONGRUN.rank_window_days, min_periods=1).median().shift(1)
    dates = pd.date_range(LONGRUN.first_rebalance, wide.index.max(), freq="MS")
    ranks = med.reindex(dates).rank(axis=1, ascending=False, method="first")
    best = ranks.min()
    months = (ranks <= top_n).sum()
    first = (ranks <= top_n).idxmax().where(months > 0)
    rows = []
    for a in wide.columns:
        y, why = ok[a]
        rows.append({"asset": a, "name": cat.loc[a, "name"], "has_cm_price": bool(cat.loc[a, "has_price"]),
                     "risk_asset": y, "excluded_because": why,
                     "best_rank": best.get(a), "months_in_top_n": int(months.get(a, 0)),
                     "first_month_in_top_n": first.get(a)})
    out = pd.DataFrame(rows)
    out = out[(out["months_in_top_n"] > 0) | (~out["risk_asset"] & (wide.max() > 1e8).reindex(out["asset"]).values)]
    return out.sort_values(["risk_asset", "best_rank"], ascending=[False, True])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top-n", type=int, default=SCAN_TOP_N)
    args = ap.parse_args(argv)
    out = scan(args.top_n)
    HISTORY_DIR.parent.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR.parent / "universe_scan.csv"
    out.to_csv(path, index=False, float_format="%.0f", date_format="%Y-%m-%d")
    print(f"scan: {int(out['risk_asset'].sum())} risk assets ever in the top {args.top_n} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
