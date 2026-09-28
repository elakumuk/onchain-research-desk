"""Entry point:  python -m desk.run [--mode live|offline|snapshot] [--save-snapshot]

Pulls (or loads cached) data, computes the three modules, and writes
everything to reports/:
    universe.csv              which tokens made it into which table, and why not
    liquidity.csv             liquidity profile per token
    fundamentals.csv          value-accrual table per token
    capacity.csv              investable fraction per weighting scheme x AUM
    backtest_stats.csv        walk-forward performance per strategy
    data_provenance.csv       every payload used: source, origin (network/cache/snapshot), timestamp
    *.png                     one chart per question
    summary.md                headline findings, generated from the numbers above
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import asdict

import numpy as np
import pandas as pd

from desk import charts
from desk.config import PARAMS, REPORTS_DIR, ROOT, UNIVERSE
from desk.data.cache import Cache, DataUnavailable
from desk.data import fetchers as F
from desk import fundamentals as FU
from desk import liquidity as L
from desk import portfolio as P

log = logging.getLogger("desk")


# ====================================================================== load
def load_all(cache: Cache):
    markets = F.cg_markets(cache, [t.cg_id for t in UNIVERSE])
    as_of = pd.Timestamp(pd.to_datetime(markets["last_updated"]).max()).tz_convert(None)

    status, hist, books, fees = [], {}, {}, {}
    for t in UNIVERSE:
        st = {"symbol": t.symbol, "kind": t.kind, "cg_id": t.cg_id, "llama_slug": t.llama_slug,
              "notes": []}
        # -- price / volume history
        try:
            h = F.cg_history(cache, t.cg_id)
            hist[t.symbol] = h
            st["history_days"] = len(h)
        except DataUnavailable as e:
            st["history_days"] = 0
            st["notes"].append(f"no price history ({e})")
        if t.cg_id not in markets.index:
            st["notes"].append("missing from CoinGecko markets")
        # -- order books
        venue_books = {}
        for venue, pair, fn in (("coinbase", t.cb_pair, F.coinbase_book), ("kraken", t.kraken_pair, F.kraken_book)):
            if pair is None:
                st["notes"].append(f"no {venue} USD pair")
                continue
            try:
                venue_books[venue] = fn(cache, pair)
            except DataUnavailable as e:
                st["notes"].append(f"{venue} book failed ({e})")
        books[t.symbol] = venue_books
        st["venues"] = "+".join(venue_books) or "none"
        # -- DefiLlama fees / revenue / holders revenue
        series, meta = {}, {}
        if t.llama_slug:
            for dt, layer in FU.LAYERS.items():
                try:
                    s, m = F.llama_fee_series(cache, t.llama_slug, dt)
                    series[layer] = s if not s.empty else None
                    meta = meta or m
                except DataUnavailable:
                    series[layer] = None
                    if layer == "holders_revenue":
                        st["notes"].append("DefiLlama does not report holders revenue")
                    else:
                        st["notes"].append(f"DefiLlama {layer} unavailable")
        fees[t.symbol] = (series, meta)
        status.append(st)
    return markets, as_of, status, hist, books, fees


# ====================================================================== modules
def liquidity_table(markets, hist, books) -> pd.DataFrame:
    rows = {}
    for sym, venue_books in books.items():
        if not venue_books or sym not in hist:
            continue
        bids, asks = L.merge_books(list(venue_books.values()))
        row = L.liquidity_profile(bids, asks, PARAMS.depth_bands, PARAMS.order_sizes_usd)
        row["venues"] = "+".join(venue_books)
        row["min_venue_coverage_pct"] = 100 * min(b["coverage"] for b in venue_books.values())
        vm = [(b["bids"][0][0] + b["asks"][0][0]) / 2 for b in venue_books.values()]
        row["venue_mid_gap_bps"] = (max(vm) - min(vm)) / np.mean(vm) * 1e4  # snapshot-timing / basis check
        if "coinbase" in venue_books and len(venue_books) > 1:
            cb_b, cb_a = L.merge_books([venue_books["coinbase"]])
            row["coinbase_share_of_2pct_depth"] = sum(L.depth_within(cb_b, cb_a, 0.02, row["mid"])) / row["depth_2pct_usd"]
        h = hist[sym]
        row["amihud_bps_per_1m"] = L.amihud(h["price"], h["volume_usd"], PARAMS.amihud_window_days)
        row["adv_30d_usd"] = L.adv(h["volume_usd"], PARAMS.adv_window_days)
        for p in PARAMS.participation_rates:
            row[f"days_to_liq_{PARAMS.liquidation_position_usd / 1e6:g}m_at_{int(p * 100)}pct"] = \
                L.days_to_liquidate(PARAMS.liquidation_position_usd, row["adv_30d_usd"], p)
        rows[sym] = row
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("symbol")


def fundamentals_table(markets, as_of, fees) -> pd.DataFrame:
    sym_to_cg = {t.symbol: t.cg_id for t in UNIVERSE}
    rows = {}
    for sym, (series, meta) in fees.items():
        if not series or series.get("fees") is None:
            continue
        m = markets.loc[sym_to_cg[sym]] if sym_to_cg[sym] in markets.index else None
        mcap = float(m["market_cap"]) if m is not None else np.nan
        fdv = float(m["fully_diluted_valuation"]) if m is not None and pd.notna(m["fully_diluted_valuation"]) else np.nan
        row = FU.value_accrual_row(series, mcap, fdv, as_of, PARAMS.runrate_window_days,
                                   PARAMS.min_fee_usd_for_flag, PARAMS.holder_accrual_flag_ratio)
        row["llama_name"] = meta.get("name")
        row["protocol_type"] = meta.get("protocolType")
        hr_method = (meta.get("methodology") or {}).get("HoldersRevenue") if isinstance(meta.get("methodology"), dict) else None
        row["llama_holders_revenue_method"] = hr_method
        rows[sym] = row
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("symbol")


def portfolio_analysis(hist, liq):
    syms = [s for s in liq.index if s in hist]
    prices = pd.DataFrame({s: hist[s]["price"] for s in syms}).sort_index()
    dvol = pd.DataFrame({s: hist[s]["volume_usd"] for s in syms}).sort_index()
    # keep assets with a complete price history over the common window (no back-filling)
    full = prices.columns[prices.notna().all()]
    dropped = sorted(set(prices.columns) - set(full))
    prices, dvol = prices[full], dvol[full]

    schemes = {
        "Equal weight": lambda h: P.equal_weight(h.columns),
        "Inverse vol": P.inverse_vol,
        "Min-var (LW)": lambda h: P.min_variance(P.ledoit_wolf_cov(h)[0]),
    }
    kw = dict(est_window=PARAMS.est_window_days, every=PARAMS.rebalance_every_days,
              adv_window=PARAMS.adv_window_days)
    runs = {name: P.backtest(prices, dvol, fn, **kw) for name, fn in schemes.items()}
    for aum in PARAMS.aum_scenarios:
        runs[f"Inverse vol, capped @ {charts._usd(aum)}"] = P.backtest(
            prices, dvol, P.inverse_vol, aum=aum, max_adv_fraction=PARAMS.max_adv_fraction, **kw)

    stats = {}
    for name, r in runs.items():
        stats[name] = {**P.perf_stats(r["returns"], PARAMS.periods_per_year, r["avg_turnover"],
                                      r["n_rebalances"], PARAMS.cost_bps_assumption),
                       "avg_invested": r["avg_invested"], "avg_turnover_per_rebalance": r["avg_turnover"],
                       "n_rebalances": r["n_rebalances"]}
    oos_index = runs["Equal weight"]["returns"].index
    btc = P.simple_returns(prices[["BTC"]])["BTC"].loc[oos_index] if "BTC" in prices else None
    if btc is not None:
        stats["BTC buy & hold"] = {**P.perf_stats(btc, PARAMS.periods_per_year),
                                   "avg_invested": 1.0, "avg_turnover_per_rebalance": 0.0, "n_rebalances": 0}
    stats = pd.DataFrame(stats).T.rename_axis("strategy")

    # ---- capacity snapshot with today's weights and today's ADV (no backtest involved)
    lr = P.log_returns(prices).tail(PARAMS.est_window_days)
    cov_lw, shrink = P.ledoit_wolf_cov(lr)
    cur = {"Equal weight": P.equal_weight(lr.columns), "Inverse vol": P.inverse_vol(lr),
           "Min-var (LW)": P.min_variance(cov_lw)}
    adv_now = dvol.tail(PARAMS.adv_window_days).median()
    grid = np.logspace(6, 10, 49)  # $1M .. $10B
    curves = pd.DataFrame({n: [P.weight_moved(w, P.apply_caps(w, P.capacity_caps(adv_now, a, PARAMS.max_adv_fraction)))
                               for a in grid] for n, w in cur.items()}, index=grid)
    cap_rows = []
    for n, w in cur.items():
        limit = P.max_uncapped_aum(w, adv_now, PARAMS.max_adv_fraction)
        binding_name = (PARAMS.max_adv_fraction * adv_now.reindex(w[w > 0].index) / w[w > 0]).idxmin()
        for a in PARAMS.aum_scenarios:
            caps = P.capacity_caps(adv_now, a, PARAMS.max_adv_fraction)
            wc = P.apply_caps(w, caps)
            cap_rows.append({"scheme": n, "aum": a, "investable_fraction": wc.sum(),
                             "names_at_cap": int((wc >= caps - 1e-12).sum()),
                             "weight_moved": P.weight_moved(w, wc),
                             "effective_n_target": P.effective_n(w), "effective_n_capped": P.effective_n(wc),
                             "btc_eth_sol_weight": float(wc.reindex(["BTC", "ETH", "SOL"]).fillna(0).sum()),
                             "max_aum_before_any_cap_binds": limit, "first_binding_name": binding_name})
    capacity = pd.DataFrame(cap_rows)

    sample_cov = lr.cov().values
    diag = {"shrinkage_intensity": shrink,
            "cond_sample_cov": float(np.linalg.cond(sample_cov)),
            "cond_lw_cov": float(np.linalg.cond(cov_lw.values)),
            "n_assets": len(full), "dropped_incomplete_history": dropped,
            "oos_start": str(oos_index[0].date()), "oos_end": str(oos_index[-1].date()),
            "history_start": str(prices.index[0].date())}
    weights_now = pd.DataFrame(cur).rename_axis("symbol")
    equity = pd.DataFrame({n: (1 + r["returns"]).cumprod() for n, r in runs.items()})
    btc_eq = (1 + btc).cumprod() if btc is not None else None
    return stats, capacity, curves, weights_now, equity, btc_eq, diag


# ====================================================================== report
def md_table(df: pd.DataFrame, fmt: dict, index_name: str | None = None) -> str:
    cols = list(fmt)
    head = [index_name or df.index.name or ""] + cols
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for idx, row in df.iterrows():
        cells = [str(idx)] + [fmt[c](row[c]) for c in cols]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _money(x):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return charts._usd(float(f"{float(x):.3g}"))


def _num(d=1):
    return lambda x: "n/a" if x is None or not np.isfinite(float(x)) else f"{float(x):,.{d}f}"


def _pct(d=1):
    return lambda x: "n/a" if x is None or not np.isfinite(float(x)) else f"{100 * float(x):.{d}f}%"


def write_summary(path, as_of, status_df, liq, fund, stats, capacity, diag, prov):
    tag_small, tag_mid, tag_big = [L._size_tag(n) for n in PARAMS.order_sizes_usd]
    liq_sorted = liq.sort_values(f"slip_{tag_mid}_bps")
    exhausted = liq.index[liq[f"slip_{tag_big}_bps"].isna()].tolist()
    filled_1m = liq[f"slip_{tag_mid}_bps"].dropna().sort_values()
    flagged = fund[fund["accrual_flag"].str.startswith("FEES")]
    origins = prov["origin"].value_counts().to_dict()

    out = [f"# Research desk summary -- data as of {as_of:%Y-%m-%d %H:%M} UTC", "",
           "Generated by `python -m desk.run`. Every number below is computed from the files in "
           "`data/` and written alongside this summary as CSV. Nothing here is investment advice.", "",
           f"Data origins this run: {origins}.", "",
           "## Universe", "",
           f"{len(status_df)} candidates; {len(liq)} in the liquidity table, {len(fund)} in the fundamentals "
           f"table, {diag['n_assets']} in the portfolio backtest. Exclusions and notes: `universe.csv`.", "",
           "## 1. Liquidity profile", "",
           f"Consolidated Coinbase + Kraken USD order book at snapshot time; Amihud over {PARAMS.amihud_window_days}d; "
           f"ADV = median daily volume (CoinGecko, all venues) over {PARAMS.adv_window_days}d.", "",
           md_table(liq_sorted, {
               "spread_bps": _num(2), "depth_1pct_usd": _money, "depth_2pct_usd": _money,
               f"slip_{tag_small}_bps": _num(1), f"slip_{tag_mid}_bps": _num(1), f"slip_{tag_big}_bps": _num(1),
               "amihud_bps_per_1m": lambda x: f"{float(x):.2e}", "adv_30d_usd": _money,
               "days_to_liq_10m_at_10pct": _num(2)}), "",
           f"- $1M market order: cheapest **{filled_1m.index[0]}** ({filled_1m.iloc[0]:.1f} bps), most expensive "
           f"that could still be filled **{filled_1m.index[-1]}** ({filled_1m.iloc[-1]:.1f} bps); visible book "
           f"too thin for $1M: {', '.join(liq.index[liq[f'slip_{tag_mid}_bps'].isna()]) or 'none'}.",
           f"- Visible books that could NOT absorb a $10M market order within 15% of mid: "
           f"{', '.join(exhausted) if exhausted else 'none'}.",
           "- `n/a` slippage means the order exceeds the visible book; it is not extrapolated.", "",
           "## 2. Value accrual (DefiLlama fees -> revenue -> holders revenue)", "",
           "`_ann` = sum of the last 365 complete days; `_runrate` = last 30 complete days x 365/30 (current pace). "
           "Multiples use CoinGecko circulating market cap (mcap) and fully diluted valuation (FDV). "
           "DefiLlama 'fees' include supply-side payouts (LP fees, staking rewards, interest paid to lenders), so "
           "price-to-fees is a usage multiple, not an earnings multiple; price-to-holders-revenue is the closer "
           "analogue to P/E.", "",
           md_table(fund.sort_values("fees_ann", ascending=False), {
               "fees_ann": _money, "revenue_ann": _money, "holders_revenue_ann": _money,
               "holders_revenue_runrate": _money,
               "holder_accrual_ratio": _pct(1), "p_fees_mcap": _num(1), "p_holders_rev_mcap": _num(1),
               "p_holders_rev_fdv": _num(1), "holder_yield_mcap": _pct(2), "accrual_flag": str}), "",
           f"- Flagged (fees >= {_money(PARAMS.min_fee_usd_for_flag)}/yr but holders revenue < "
           f"{PARAMS.holder_accrual_flag_ratio:.0%} of fees, or not reported): "
           f"{', '.join(f'{s} ({r})' for s, r in flagged['accrual_flag'].items()) or 'none'}.",
           "- For L1s, DefiLlama counts burned fees as holders revenue (ETH, SOL); for BTC, fees go to miners.",
           "- Series with fewer than 365 complete days in the window (sum scaled up to a year): "
           + (", ".join(f"{s} {c}d" for s, c in fund["fees_coverage_days"].items() if 0 < c < 365) or "none") + ".",
           "",
           "## 3. Portfolio construction under a liquidity cap", "",
           f"Position cap: <= {PARAMS.max_adv_fraction:.0%} of 30d median ADV. Current weights use the last "
           f"{PARAMS.est_window_days} days of log returns; covariance is Ledoit-Wolf "
           f"(shrinkage intensity {diag['shrinkage_intensity']:.2f}; condition number "
           f"{diag['cond_sample_cov']:,.0f} sample -> {diag['cond_lw_cov']:,.0f} shrunk).", "",
           md_table(capacity.set_index("scheme"), {
               "aum": _money, "investable_fraction": _pct(1), "names_at_cap": str, "weight_moved": _pct(1),
               "effective_n_target": _num(1), "effective_n_capped": _num(1), "btc_eth_sol_weight": _pct(1),
               "max_aum_before_any_cap_binds": _money, "first_binding_name": str}), "",
           f"### Walk-forward backtest ({diag['oos_start']} to {diag['oos_end']}, out-of-sample only)", "",
           md_table(stats, {
               "total_return": _pct(1), "ann_vol": _pct(1), "sharpe_rf0": _num(2), "sharpe_se": _num(2),
               "max_drawdown": _pct(1), "avg_invested": _pct(0), "avg_turnover_per_rebalance": _pct(0),
               "total_return_net_of_cost": _pct(1)}), "",
           f"- Sharpe uses a 0% risk-free rate. `sharpe_se` is its approximate standard error: with "
           f"{int(stats['days'].iloc[0])} daily observations, a Sharpe within about 2 SE of another is not "
           "statistically distinguishable from it.",
           f"- Net-of-cost line assumes a flat {PARAMS.cost_bps_assumption:g} bps per unit of turnover; "
           "it is an assumption, not a measured cost.",
           "- One year of history is a single market regime. Treat these as a demonstration of the "
           "machinery, not evidence that any scheme is better.", "",
           "## Parameters", "", "```", *(f"{k} = {v}" for k, v in asdict(PARAMS).items()), "```", ""]
    path.write_text("\n".join(out))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("live", "offline", "snapshot"), default="live",
                    help="live: cache then network (default); offline: cache only; snapshot: committed snapshot only")
    ap.add_argument("--save-snapshot", action="store_true",
                    help="copy the raw files used in this run into data/snapshot/")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    cache = Cache(args.mode)
    markets, as_of, status, hist, books, fees = load_all(cache)
    liq = liquidity_table(markets, hist, books)
    fund = fundamentals_table(markets, as_of, fees)
    stats, capacity, curves, weights_now, equity, btc_eq, diag = portfolio_analysis(hist, liq)

    status_df = pd.DataFrame(status).set_index("symbol")
    status_df["in_liquidity"] = status_df.index.isin(liq.index)
    status_df["in_fundamentals"] = status_df.index.isin(fund.index)
    status_df["in_portfolio"] = status_df.index.isin(weights_now.index)
    status_df["notes"] = status_df["notes"].map("; ".join)
    prov = pd.DataFrame(cache.served)
    prov["file"] = prov["file"].map(lambda f: str(f).replace(str(ROOT) + "/", "") if f else f)

    REPORTS_DIR.mkdir(exist_ok=True)
    status_df.to_csv(REPORTS_DIR / "universe.csv")
    liq.to_csv(REPORTS_DIR / "liquidity.csv", float_format="%.6g")
    fund.to_csv(REPORTS_DIR / "fundamentals.csv", float_format="%.6g")
    capacity.to_csv(REPORTS_DIR / "capacity.csv", index=False, float_format="%.6g")
    stats.to_csv(REPORTS_DIR / "backtest_stats.csv", float_format="%.6g")
    weights_now.to_csv(REPORTS_DIR / "current_weights.csv", float_format="%.6g")
    prov.to_csv(REPORTS_DIR / "data_provenance.csv", index=False)

    tags = [L._size_tag(n) for n in PARAMS.order_sizes_usd]
    charts.slippage_dotplot(liq, tags, REPORTS_DIR / "liquidity_slippage.png")
    charts.accrual_bars(fund, REPORTS_DIR / "value_accrual.png")
    charts.capacity_curves(curves, PARAMS.aum_scenarios, REPORTS_DIR / "capacity.png")
    eq_cols = ["Equal weight", "Inverse vol", "Min-var (LW)",
               f"Inverse vol, capped @ {charts._usd(PARAMS.aum_scenarios[-1])}"]
    charts.equity_curves(equity[eq_cols], btc_eq, REPORTS_DIR / "backtest.png")
    write_summary(REPORTS_DIR / "summary.md", as_of, status_df, liq, fund, stats, capacity, diag, prov)

    if args.save_snapshot:
        n = cache.save_snapshot()
        print(f"snapshot: copied {n} raw files to data/snapshot/")
    print(f"as of {as_of} UTC | liquidity {len(liq)} | fundamentals {len(fund)} | "
          f"portfolio {diag['n_assets']} assets | reports -> {REPORTS_DIR.relative_to(ROOT)}/")
    print(f"data origins: {prov['origin'].value_counts().to_dict()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
