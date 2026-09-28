"""The facts registry: every citable number from a run, with a stable id.

After each run, `desk.run` writes `reports/facts.json`. It is the single source
of truth for any number that appears in a memo:

    {"schema_version": 1,
     "registry": {...run metadata...},
     "labels":   {...non-numeric context per token, e.g. the accrual flag...},
     "facts": [
       {"id": "liq.AAVE.slip_1m_bps", "value": 371.357, "unit": "bps",
        "as_of": "2026-09-28T18:57:20Z", "description": "...",
        "source": ["coinbase:book_AAVE-USD", "kraken:book_AAVEUSD"]},
       ...one fact per line...],
     "sources": {"coinbase:book_AAVE-USD": {"api": "Coinbase Exchange",
                   "endpoint": "GET /products/AAVE-USD/book?level=2",
                   "cache_file": "data/snapshot/coinbase/book_AAVE-USD.json",
                   "fetched_at": "2026-09-28T18:57:20Z"}, ...},
     "source_groups": {"all_price_histories": ["coingecko:history_bitcoin", ...]}}

Provenance is normalized: each fact lists source *ids*, and the `sources`
table says which API endpoint and which cached file each id refers to.
Portfolio-level facts depend on every token's price history, so they cite the
group `group:all_price_histories` instead of repeating twenty ids.

Design rules (see ARCHITECTURE.md section 8):
  * This module does no analytics. It only reads the tables the run already
    computed, converts ratios to percent, and attaches provenance. A number
    that is not in a report table cannot become a fact.
  * An id ends in its unit (`_usd`, `_bps`, `_pct`, `_x`, `_days`, `_count`),
    except dimensionless statistics such as a Sharpe ratio (unit "number").
  * Shares and returns are stored in PERCENT (75.49 means 75.49%), never as a
    0-1 ratio, so a reader and the verifier see the same unit.
  * Values are rounded to 6 significant figures. That is far more precision
    than any memo uses, and it keeps the file identical across machines whose
    floating point differs in the last bits.
  * Only finite values are facts. "Not available" is carried by `labels`
    (and by the memo text), never by a number.
  * `as_of` is when the underlying data was fetched (the oldest fetch among a
    fact's sources). Parameters from config.py have no as_of: they do not age.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

SCHEMA_VERSION = 1
UNITS = {
    "usd": "US dollars",
    "pct": "percent (75.5 means 75.5%)",
    "bps": "basis points (1 bps = 0.01%)",
    "x": "multiple (28.7 means 28.7x)",
    "days": "days",
    "count": "a count (tokens, names, observations)",
    "number": "dimensionless statistic (Sharpe ratio, correlation, condition number)",
}
API_NAMES = {"coingecko": "CoinGecko", "defillama": "DefiLlama",
             "coinbase": "Coinbase Exchange", "kraken": "Kraken"}
SIG_FIGS = 6


# ---------------------------------------------------------------- data model
@dataclass(frozen=True)
class Fact:
    id: str
    value: float
    unit: str
    as_of: str | None
    description: str
    source: tuple = field(default_factory=tuple)   # source ids, e.g. ("coinbase:book_AAVE-USD",)

    def to_dict(self) -> dict:
        return {"id": self.id, "value": self.value, "unit": self.unit, "as_of": self.as_of,
                "description": self.description, "source": list(self.source)}


@dataclass
class Registry:
    facts: dict[str, Fact]
    meta: dict
    labels: dict
    sha256: str
    path: Path | None = None
    sources: dict = field(default_factory=dict)
    source_groups: dict = field(default_factory=dict)

    def expand_sources(self, fact: Fact) -> list[dict]:
        """Source descriptors for a fact, with groups expanded."""
        out = []
        for sid in fact.source:
            ids = self.source_groups.get(sid[len("group:"):], []) if sid.startswith("group:") else [sid]
            out.extend({"id": i, **self.sources.get(i, {})} for i in ids)
        return out

    def __contains__(self, fid: str) -> bool:
        return fid in self.facts

    def __getitem__(self, fid: str) -> Fact:
        return self.facts[fid]


# ---------------------------------------------------------------- helpers
def round_sig(v: float, sig: int = SIG_FIGS) -> float | int:
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return int(v)
    v = float(v)
    if v == 0:
        return 0
    r = float(f"{v:.{sig}g}")
    return int(r) if r.is_integer() and abs(r) < 1e15 else r


def iso_z(ts: str | None) -> str | None:
    """'2026-09-28T18:56:25.034221+00:00' -> '2026-09-28T18:56:25Z' (UTC, second precision)."""
    if not ts:
        return None
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _finite(v) -> bool:
    try:
        return v is not None and not isinstance(v, bool) and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _endpoint(url: str | None, params: dict | None) -> str:
    if not url:
        return "unknown"
    path = urlparse(url).path
    parts = []
    for k in sorted(params or {}):
        v = str(params[k])
        if k == "ids" and "," in v:
            v = f"<{len(v.split(','))} ids>"
        parts.append(f"{k}={v}")
    return "GET " + path + ("?" + "&".join(parts) if parts else "")


def size_tag(n: float) -> str:
    """1e5 -> '100k', 1e6 -> '1m', 1e9 -> '1b' (stable pieces for fact ids)."""
    for div, suf in ((1e9, "b"), (1e6, "m"), (1e3, "k")):
        if n >= div:
            return f"{n / div:g}{suf}"
    return f"{n:g}"


def pct_tag(frac: float) -> str:
    """0.005 -> '0p5pct', 0.02 -> '2pct' (no dots inside an id segment)."""
    return f"{frac * 100:g}".replace(".", "p") + "pct"


def scheme_slug(name: str) -> str:
    """'Equal weight' -> 'equal', 'Min-var (LW)' -> 'min_var', 'Inverse vol, capped @ $1B' -> 'inverse_vol_capped_1b'."""
    s = slug(name)
    s = s.replace("equal_weight", "equal").replace("min_var_lw", "min_var")
    return s


def slug(name: str) -> str:
    out = []
    for ch in name.lower():
        out.append(ch if ch.isalnum() else "_")
    s = "".join(out)
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_")


# ---------------------------------------------------------------- builder
class _Builder:
    def __init__(self, provenance: dict):
        self.provenance = provenance    # (source, key) -> {"api","endpoint","cache_file","fetched_at"}
        self.facts: dict[str, Fact] = {}
        self.used: dict[str, dict] = {}          # source id -> descriptor (only sources some fact cites)
        self.groups: dict[str, list[str]] = {}

    def src(self, *keys) -> tuple:
        """Source ids for (source, key) pairs that were actually served this run."""
        out = []
        for k in keys:
            rec = self.provenance.get(k)
            sid = f"{k[0]}:{k[1]}"
            if rec is not None and sid not in out:
                out.append(sid)
                self.used[sid] = rec
        return tuple(out)

    def group(self, name: str, ids: tuple) -> tuple:
        self.groups[name] = list(ids)
        return (f"group:{name}",)

    def _stamps(self, source: tuple) -> list[str]:
        out = []
        for sid in source:
            ids = self.groups.get(sid[len("group:"):], []) if sid.startswith("group:") else [sid]
            out.extend(self.used[i]["fetched_at"] for i in ids if self.used.get(i, {}).get("fetched_at"))
        return out

    def add(self, fid: str, value, unit: str, description: str, source: tuple = (), *,
            scale: float = 1.0, timeless: bool = False):
        if not _finite(value):
            return
        if unit not in UNITS:
            raise ValueError(f"{fid}: unknown unit {unit!r}")
        if fid in self.facts:
            raise ValueError(f"duplicate fact id {fid}")
        v = float(value) * scale
        value = int(round(v)) if unit == "count" else round_sig(v)
        stamps = self._stamps(source)
        as_of = None if timeless or not stamps else min(stamps)
        self.facts[fid] = Fact(fid, value, unit, as_of, description, tuple(source))


def provenance_index(served: list[dict], snapshot_paths: dict | None = None) -> dict:
    """Map (source, key) -> source descriptor, from Cache.served.

    `snapshot_paths` (optional) maps (source, key) -> path of the snapshot copy;
    it is used after `--save-snapshot`, so the registry points at committed
    files rather than the gitignored raw cache.
    """
    out = {}
    for rec in served:
        if not rec.get("file"):
            continue
        key = (rec["source"], rec["key"])
        path = (snapshot_paths or {}).get(key, rec["file"])
        out[key] = {"api": API_NAMES.get(rec["source"], rec["source"]),
                    "endpoint": _endpoint(rec.get("url"), rec.get("params")),
                    "cache_file": str(path),
                    "fetched_at": iso_z(rec.get("fetched_at"))}
    return out


def _param_facts(b: _Builder, params) -> None:
    def cfg(name):
        sid = f"config:{name}"
        b.used[sid] = {"api": "config", "endpoint": f"desk/config.py Params.{name}",
                       "cache_file": None, "fetched_at": None}
        return (sid,)
    add = lambda fid, v, unit, desc, name, scale=1.0: b.add(fid, v, unit, desc, cfg(name), scale=scale, timeless=True)
    add("param.amihud_window_days", params.amihud_window_days, "days", "Trailing window for the Amihud illiquidity average", "amihud_window_days")
    add("param.adv_window_days", params.adv_window_days, "days", "Trailing window for median daily volume (ADV)", "adv_window_days")
    for band in params.depth_bands:
        add(f"param.depth_band_{pct_tag(band)[:-3]}_pct", band, "pct", "Order-book depth band, +/- this far from mid", "depth_bands", 100)
    for n in params.order_sizes_usd:
        add(f"param.order_size_{size_tag(n)}_usd", n, "usd", "Market-order size used for the slippage walk", "order_sizes_usd")
    add("param.book_keep_band_pct", params.book_keep_band, "pct", "Book levels kept within +/- this far from mid; larger orders report n/a", "book_keep_band", 100)
    for p in params.participation_rates:
        add(f"param.participation_{int(round(p * 100))}_pct", p, "pct", "Share of ADV traded per day when liquidating", "participation_rates", 100)
    add("param.liquidation_position_usd", params.liquidation_position_usd, "usd", "Position size for days-to-liquidate", "liquidation_position_usd")
    add("param.fees_trailing_window_days", 365, "days", "Trailing window for annual fees/revenue (complete days)", "(fixed in desk/fundamentals.py)")
    add("param.runrate_window_days", params.runrate_window_days, "days", "Window for the current run-rate (x 365/N)", "runrate_window_days")
    add("param.min_fee_for_flag_usd", params.min_fee_usd_for_flag, "usd", "Minimum annual fees before the accrual flag can fire", "min_fee_usd_for_flag")
    add("param.holder_accrual_flag_pct", params.holder_accrual_flag_ratio, "pct", "Holders revenue below this share of fees triggers the flag", "holder_accrual_flag_ratio", 100)
    add("param.est_window_days", params.est_window_days, "days", "Lookback for volatility/covariance estimates", "est_window_days")
    add("param.rebalance_every_days", params.rebalance_every_days, "days", "Rebalance interval in the backtest", "rebalance_every_days")
    add("param.max_adv_fraction_pct", params.max_adv_fraction, "pct", "Position cap as a share of 30-day median ADV", "max_adv_fraction", 100)
    for a in params.aum_scenarios:
        add(f"param.aum_{size_tag(a)}_usd", a, "usd", "AUM scenario for the capacity analysis", "aum_scenarios")
    add("param.cost_assumption_bps", params.cost_bps_assumption, "bps", "Flat assumed cost per unit of turnover (backtest)", "cost_bps_assumption")
    add("param.periods_per_year_count", params.periods_per_year, "count", "Annualization periods (crypto trades every calendar day)", "periods_per_year")


def build_facts(*, params, universe, markets, liq, fund, risk, capacity, stats, diag,
                status_df, provenance: dict, market_as_of) -> tuple[list[Fact], dict]:
    """Assemble every citable number from the run's tables. Returns (facts, labels)."""
    b = _Builder(provenance)
    tok = {t.symbol: t for t in universe}
    _param_facts(b, params)

    b.add("universe.candidates_count", len(status_df), "count", "Tokens in the candidate universe",
          b.src(("coingecko", "markets")))
    all_hist = b.group("all_price_histories", b.src(*[("coingecko", f"history_{t.cg_id}") for t in universe]))

    labels: dict = {"tokens": {}}
    for sym in status_df.index:
        t = tok[sym]
        mk = b.src(("coingecko", "markets"))
        hist = b.src(("coingecko", f"history_{t.cg_id}"))
        books = b.src(*([("coinbase", f"book_{t.cb_pair}")] if t.cb_pair else []),
                      *([("kraken", f"book_{t.kraken_pair}")] if t.kraken_pair else []))
        llama = b.src(*[("defillama", f"{t.llama_slug}_{dt}")
                        for dt in ("dailyFees", "dailyRevenue", "dailyHoldersRevenue")]) if t.llama_slug else ()

        # -- market data (CoinGecko /coins/markets)
        if t.cg_id in markets.index:
            m = markets.loc[t.cg_id]
            b.add(f"mkt.{sym}.price_usd", m["current_price"], "usd", "Spot price (CoinGecko)", mk)
            b.add(f"mkt.{sym}.mcap_usd", m["market_cap"], "usd", "Circulating market cap", mk)
            b.add(f"mkt.{sym}.fdv_usd", m["fully_diluted_valuation"], "usd", "Fully diluted valuation", mk)
            b.add(f"mkt.{sym}.volume_24h_usd", m["total_volume"], "usd", "24h volume, all venues (CoinGecko)", mk)
            b.add(f"mkt.{sym}.circulating_supply_count", m["circulating_supply"], "count", "Circulating supply (tokens)", mk)
            b.add(f"mkt.{sym}.total_supply_count", m["total_supply"], "count", "Total supply (tokens)", mk)
            b.add(f"mkt.{sym}.max_supply_count", m["max_supply"], "count", "Maximum supply (tokens); absent = uncapped or unreported", mk)

        # -- liquidity
        if sym in liq.index:
            r = liq.loc[sym]
            b.add(f"liq.{sym}.mid_usd", r["mid"], "usd", "Consolidated book mid price", books)
            b.add(f"liq.{sym}.spread_bps", r["spread_bps"], "bps", "Consolidated bid-ask spread", books)
            for band in params.depth_bands:
                tag = f"{band * 100:g}pct"
                b.add(f"liq.{sym}.depth_{pct_tag(band)}_usd", r[f"depth_{tag}_usd"], "usd",
                      f"Resting bid+ask notional within +/-{band * 100:g}% of mid", books)
            for n in params.order_sizes_usd:
                tag = size_tag(n)
                b.add(f"liq.{sym}.slip_{tag}_bps", r[f"slip_{tag}_bps"], "bps",
                      f"Cost of a ${_h(tag)} market order vs mid, worse of buy/sell (half-spread included)", books)
            b.add(f"liq.{sym}.venue_mid_gap_bps", r["venue_mid_gap_bps"], "bps", "Gap between the venues' mids at snapshot time", books)
            b.add(f"liq.{sym}.min_venue_coverage_pct", r["min_venue_coverage_pct"], "pct",
                  "How far from mid the shallowest venue's returned book reaches", books)
            if "coinbase_share_of_2pct_depth" in r:
                b.add(f"liq.{sym}.coinbase_share_2pct_depth_pct", r["coinbase_share_of_2pct_depth"], "pct",
                      "Coinbase share of consolidated +/-2% depth", books, scale=100)
            b.add(f"liq.{sym}.amihud_bps_per_1m", r["amihud_bps_per_1m"], "bps",
                  f"Amihud illiquidity: bps of price move per $1M traded, {params.amihud_window_days}d mean", hist)
            b.add(f"liq.{sym}.adv_30d_usd", r["adv_30d_usd"], "usd",
                  f"Median daily volume over {params.adv_window_days}d (CoinGecko, all venues)", hist)
            for p in params.participation_rates:
                pos = size_tag(params.liquidation_position_usd)
                col = f"days_to_liq_{params.liquidation_position_usd / 1e6:g}m_at_{int(p * 100)}pct"
                b.add(f"liq.{sym}.days_to_liq_{pos}_at_{int(p * 100)}pct_days", r[col], "days",
                      f"Days to exit a ${_h(pos)} position trading {int(p * 100)}% of ADV", hist)

        # -- value accrual
        if sym in fund.index:
            f = fund.loc[sym]
            both = llama + mk
            for layer, dt in (("fees", "dailyFees"), ("revenue", "dailyRevenue"),
                              ("holders_revenue", "dailyHoldersRevenue")):
                one = b.src(("defillama", f"{t.llama_slug}_{dt}"))
                b.add(f"fund.{sym}.{layer}_365d_usd", f[f"{layer}_ann"], "usd",
                      f"DefiLlama {layer.replace('_', ' ')}, trailing 365 complete days", one)
                b.add(f"fund.{sym}.{layer}_runrate_usd", f[f"{layer}_runrate"], "usd",
                      f"DefiLlama {layer.replace('_', ' ')}, last 30 days x 365/30", one)
                if f[f"{layer}_coverage_days"] and f[f"{layer}_coverage_days"] > 0:
                    b.add(f"fund.{sym}.{layer}_coverage_days", f[f"{layer}_coverage_days"], "days",
                          f"Days of {layer.replace('_', ' ')} data in the trailing window", one)
            b.add(f"fund.{sym}.take_rate_365d_pct", f["take_rate"], "pct", "Revenue / fees (trailing 365d)", llama, scale=100)
            b.add(f"fund.{sym}.holder_share_365d_pct", f["holder_accrual_ratio"], "pct",
                  "Holders revenue / fees (trailing 365d)", llama, scale=100)
            b.add(f"fund.{sym}.float_ratio_pct", f["float_ratio"], "pct", "Circulating market cap / FDV", mk, scale=100)
            for cap in ("mcap", "fdv"):
                b.add(f"fund.{sym}.p_fees_{cap}_x", f[f"p_fees_{cap}"], "x", f"{cap.upper()} / trailing fees", both)
                b.add(f"fund.{sym}.p_revenue_{cap}_x", f[f"p_revenue_{cap}"], "x", f"{cap.upper()} / trailing revenue", both)
                b.add(f"fund.{sym}.p_holders_rev_{cap}_x", f[f"p_holders_rev_{cap}"], "x",
                      f"{cap.upper()} / trailing holders revenue", both)
            b.add(f"fund.{sym}.holder_yield_mcap_pct", f["holder_yield_mcap"], "pct",
                  "Trailing holders revenue / circulating market cap", both, scale=100)
            b.add(f"fund.{sym}.fees_runrate_vs_365d_x", f["fees_runrate_vs_ann"], "x",
                  "Fees run-rate / trailing 365d (above 1 = accelerating)", llama)
            b.add(f"fund.{sym}.holders_rev_runrate_vs_365d_x", f["holders_revenue_runrate_vs_ann"], "x",
                  "Holders revenue run-rate / trailing 365d (above 1 = accelerating)", llama)
            b.add(f"mkt.{sym}.circulating_share_of_total_pct", f["circulating_share_of_total"], "pct",
                  "Circulating supply / total supply", mk, scale=100)

        # -- market risk and sizing
        if sym in risk.index:
            k = risk.loc[sym]
            b.add(f"risk.{sym}.vol_{params.est_window_days}d_ann_pct", k["vol_ann"], "pct",
                  f"Annualized volatility of daily log returns, last {params.est_window_days}d", hist, scale=100)
            b.add(f"risk.{sym}.return_365d_pct", k["return_total"], "pct", "Price return over the history window", hist, scale=100)
            b.add(f"risk.{sym}.max_drawdown_365d_pct", k["max_drawdown"], "pct", "Worst peak-to-trough fall over the history window", hist, scale=100)
            b.add(f"risk.{sym}.history_days", k["history_days"], "days", "Days of daily price history used", hist)
            b.add(f"risk.{sym}.corr_btc_{params.est_window_days}d", k["corr_btc"], "number",
                  f"Correlation of daily log returns with BTC, last {params.est_window_days}d",
                  tuple(dict.fromkeys(hist + b.src(("coingecko", "history_bitcoin")))))
            b.add(f"port.{sym}.max_position_at_cap_usd", k["max_position_at_cap_usd"], "usd",
                  "Largest position allowed by the ADV cap", hist)
            for s in ("equal", "inverse_vol", "min_var"):
                b.add(f"port.{sym}.weight_{s}_pct", k[f"weight_{s}"], "pct", f"Current target weight, {s.replace('_', ' ')} scheme", all_hist, scale=100)
                b.add(f"port.{sym}.max_aum_before_cap_{s}_usd", k[f"max_aum_before_cap_{s}"], "usd",
                      f"AUM at which this token's {s.replace('_', ' ')} weight hits its ADV cap", all_hist)

        labels["tokens"][sym] = {
            "kind": t.kind,
            "coingecko_id": t.cg_id,
            "defillama_slug": t.llama_slug,
            "venues": str(liq.loc[sym, "venues"]) if sym in liq.index else "none",
            "llama_name": _s(fund.loc[sym, "llama_name"]) if sym in fund.index else None,
            "protocol_type": _s(fund.loc[sym, "protocol_type"]) if sym in fund.index else None,
            "holders_revenue_reported": bool(fund.loc[sym, "holders_revenue_reported"]) if sym in fund.index else None,
            "holders_revenue_method": _s(fund.loc[sym, "llama_holders_revenue_method"]) if sym in fund.index else None,
            "accrual_flag": _s(fund.loc[sym, "accrual_flag"]) if sym in fund.index else None,
            "slippage_na": [size_tag(n) for n in params.order_sizes_usd
                            if sym in liq.index and not _finite(liq.loc[sym, f"slip_{size_tag(n)}_bps"])],
            "in_portfolio": bool(status_df.loc[sym, "in_portfolio"]),
            "notes": _s(status_df.loc[sym, "notes"]),
        }

    # -- capacity (portfolio-level)
    for _, r in capacity.iterrows():
        s = scheme_slug(r["scheme"])
        a = size_tag(r["aum"])
        base = f"cap.{s}.aum_{a}"
        b.add(f"{base}.investable_pct", r["investable_fraction"], "pct", f"Share of ${_h(a)} AUM investable under the cap", all_hist, scale=100)
        b.add(f"{base}.names_at_cap_count", r["names_at_cap"], "count", f"Tokens at their ADV cap at ${_h(a)} AUM", all_hist)
        b.add(f"{base}.weight_moved_pct", r["weight_moved"], "pct", f"Weight pushed off target by the cap at ${_h(a)} AUM", all_hist, scale=100)
        b.add(f"{base}.effective_n_capped", r["effective_n_capped"], "number", f"Effective number of positions (1/sum w^2) at ${_h(a)} AUM", all_hist)
        b.add(f"{base}.btc_eth_sol_weight_pct", r["btc_eth_sol_weight"], "pct", f"BTC+ETH+SOL combined weight at ${a} AUM", all_hist, scale=100)
        if f"cap.{s}.effective_n_target" not in b.facts:
            b.add(f"cap.{s}.effective_n_target", r["effective_n_target"], "number", "Effective number of positions before caps", all_hist)
            b.add(f"cap.{s}.max_aum_before_any_cap_usd", r["max_aum_before_any_cap_binds"], "usd", "AUM at which the first ADV cap binds", all_hist)
    labels["first_binding_name"] = {scheme_slug(r["scheme"]): r["first_binding_name"] for _, r in capacity.iterrows()}

    # -- backtest
    for name, r in stats.iterrows():
        s = scheme_slug(name)
        base = f"bt.{s}"
        b.add(f"{base}.total_return_pct", r["total_return"], "pct", f"{name}: total out-of-sample return", all_hist, scale=100)
        b.add(f"{base}.ann_vol_pct", r["ann_vol"], "pct", f"{name}: annualized volatility", all_hist, scale=100)
        b.add(f"{base}.sharpe", r["sharpe_rf0"], "number", f"{name}: Sharpe ratio, 0% risk-free rate", all_hist)
        b.add(f"{base}.sharpe_se", r["sharpe_se"], "number", f"{name}: approximate standard error of the Sharpe ratio", all_hist)
        b.add(f"{base}.max_drawdown_pct", r["max_drawdown"], "pct", f"{name}: maximum drawdown", all_hist, scale=100)
        b.add(f"{base}.total_return_net_pct", r["total_return_net_of_cost"], "pct", f"{name}: total return net of the assumed cost", all_hist, scale=100)
    if len(stats):
        b.add("bt.oos_days", stats["days"].iloc[0], "days", "Out-of-sample days in the walk-forward test", all_hist)
    b.add("port.assets_count", diag["n_assets"], "count", "Assets in the portfolio analysis", all_hist)
    b.add("port.shrinkage_intensity", diag["shrinkage_intensity"], "number", "Ledoit-Wolf shrinkage intensity", all_hist)
    b.add("port.cond_sample_cov", diag["cond_sample_cov"], "number", "Condition number of the sample covariance", all_hist)
    b.add("port.cond_lw_cov", diag["cond_lw_cov"], "number", "Condition number of the Ledoit-Wolf covariance", all_hist)
    labels["backtest_window"] = {"oos_start": diag["oos_start"], "oos_end": diag["oos_end"]}
    labels["market_as_of"] = iso_z(str(market_as_of.tz_localize("UTC")) if market_as_of.tzinfo is None else str(market_as_of))

    labels["_sources"] = dict(sorted(b.used.items()))
    labels["_source_groups"] = b.groups
    facts = sorted(b.facts.values(), key=lambda f: f.id)
    return facts, labels


def _s(v):
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
    except TypeError:
        pass
    s = str(v)
    return s if s and s.lower() != "nan" else None


# ---------------------------------------------------------------- I/O
def write_registry(path: Path, facts: list[Fact], labels: dict, mode: str) -> str:
    """Write facts.json (one fact per line, for readable diffs). Returns its sha256."""
    stamps = [f.as_of for f in facts if f.as_of]
    meta = {"mode": mode, "data_as_of": max(stamps) if stamps else None,
            "oldest_fact_as_of": min(stamps) if stamps else None,
            "n_facts": len(facts), "units": UNITS,
            "note": "Generated by desk.run. Every number in a memo must cite one of these ids."}
    labels = dict(labels)
    sources = labels.pop("_sources", {})
    groups = labels.pop("_source_groups", {})
    head = json.dumps({"schema_version": SCHEMA_VERSION, "registry": meta, "labels": labels},
                      sort_keys=True, ensure_ascii=False)
    lines = [json.dumps(f.to_dict(), sort_keys=True, ensure_ascii=False) for f in facts]
    src_lines = [json.dumps(k) + ": " + json.dumps(v, sort_keys=True) for k, v in sorted(sources.items())]
    text = (head[:-1] + ',\n"facts": [\n' + ",\n".join(lines) + '\n],\n"sources": {\n'
            + ",\n".join(src_lines) + '\n},\n"source_groups": ' + json.dumps(groups, sort_keys=True) + "}\n")
    json.loads(text)  # sanity: the hand-assembled file must be valid JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_registry(path: Path | str) -> Registry:
    path = Path(path)
    raw = path.read_bytes()
    doc = json.loads(raw)
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{path}: unsupported schema_version {doc.get('schema_version')}")
    facts = {}
    for d in doc["facts"]:
        facts[d["id"]] = Fact(d["id"], d["value"], d["unit"], d.get("as_of"), d.get("description", ""),
                              tuple(d.get("source") or ()))
    return Registry(facts, doc.get("registry", {}), doc.get("labels", {}),
                    hashlib.sha256(raw).hexdigest(), path,
                    doc.get("sources", {}), doc.get("source_groups", {}))


def _h(tag: str) -> str:
    """'1m' -> '1M' for human-readable descriptions."""
    return tag[:-1] + tag[-1].upper() if tag[-1] in "mb" else tag
