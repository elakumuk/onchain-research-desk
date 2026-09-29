"""v2: the long-history, point-in-time research layer.

Pure functions: DataFrames in, DataFrames and numbers out. No network, no files.
`desk.run` feeds them the Coin Metrics history store and writes the results.

The questions
-------------
1. With a universe chosen the way it could have been chosen at the time, and
   with assets that later died still in it, how did the v1 weighting schemes
   behave from 2016 to now, overall and per market regime?
2. When did crypto become deep enough for an institutional fund? For every
   month: the AUM at which the liquidity cap first binds, and how many
   effective positions survive at $10M / $100M / $1B.
3. How has liquidity changed over the long run (rolling Amihud, BTC and ETH)?
4. What does the keyless price universe miss (the coverage diagnostic)?

Point in time
-------------
At a rebalance date t every eligibility input is computed from rows strictly
before t, on the raw data: price history length, a complete estimation window,
trading on the day before t, and the median reported volume that ranks the
assets. The tests change every row on or after t and check that the universe
at t, and every portfolio return before t, are unchanged.

Deaths
------
An asset has *died* when its last day with both a price and positive reported
volume (its last traded day) falls more than `death_grace_days` before the
end of the sample. Its prices after that day are removed (set missing), and
the engine sells any holding on the first missing day at the last traded
price times (1 - exit_haircut). Everything before the last traded day stays
in the returns. The date of death is known only after the fact, but it
changes nothing before that date: it only says that the market never
reopened, and the haircut is the stated assumption about what a holder could
recover. A stress run sets the haircut to 100%.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from desk import portfolio as P


# ====================================================================== panels
def first_segment(s: pd.Series, gap_days: int) -> pd.Series:
    """The series up to its first gap of `gap_days` or more missing days; NaN after that.

    Binance reuses tickers: LUNAUSDT was Terra's LUNA until the May 2022
    collapse and later a different token. Using only the first continuous
    segment of a pair's history never splices two tokens together (and treats
    a delisted-then-relisted pair as ended at the delisting, conservatively).
    """
    v = s.dropna()
    if v.empty:
        return s
    gaps = v.index.to_series().diff().dt.days
    breaks = gaps[gaps >= gap_days]
    if len(breaks):
        end = v.index[v.index.get_loc(breaks.index[0]) - 1]
        s = s.where(s.index <= end)
    return s


def load_panels(cm, bn, candidates, params) -> dict:
    """Prices (USD) and Coin Metrics reported volumes by symbol, lifetimes, death-masked prices.

    Prices come from Coin Metrics PriceUSD where it exists; otherwise from the
    Binance archive (<SYM>USDT close x Coin Metrics' USDT/USD price), used
    only up to the pair's first long gap and only if the ticker mapping passes
    the volume check below; otherwise the asset has volume but no price and
    can never be held (it still counts in the coverage diagnostic).

    Ticker check: Binance is one of the venues inside Coin Metrics' reported
    volume, so Binance's own USD volume divided by the all-venue figure should
    be a fraction. A median ratio outside params.binance_volume_ratio means
    the Binance ticker is probably a different token, and the price is not used.
    """
    syms = [h.symbol for h in candidates]
    vol = cm.panel("volume_usd", [h.volume_asset for h in candidates])
    vol.columns = syms
    cmc = [h for h in candidates if h.price_source == "coinmetrics"]
    price = cm.panel("price_usd", [h.price_asset for h in cmc])
    price.columns = [h.symbol for h in cmc]
    idx = vol.index.union(price.index)
    # The sample ends on the last day every price source covers: the newest BTC close, capped by the
    # newest day in the Binance archive (which publishes a day later). Otherwise every Binance-priced
    # holding would look as if it stopped trading on the final day.
    end = price["BTC"].last_valid_index()
    if bn is not None and len(bn.data):
        end = min(end, bn.data["date"].max())
    idx = idx[(idx >= pd.Timestamp(params.history_start)) & (idx <= end)]
    vol = vol.reindex(idx)
    cols = {s: price[s].reindex(idx) for s in price.columns}   # built as a dict: one frame at the end
    usdt = cm.panel("price_usd", ["usdt"])["usdt"].reindex(idx)
    bnc = [h for h in candidates if h.price_source == "binance"]
    checks = {}
    if bnc:
        pairs = sorted({h.price_asset for h in bnc})      # two ids can map to one ticker; the check decides
        close = bn.panel("close_usdt", pairs).reindex(idx)
        qv = bn.panel("quote_volume_usdt", pairs).reindex(idx)
        lo, hi = params.binance_volume_ratio
        for h in bnc:
            c = first_segment(close[h.price_asset], params.binance_gap_days)
            both = (qv[h.price_asset] * usdt / vol[h.symbol]).where(c.notna() & (vol[h.symbol] > 0)).dropna()
            ratio = float(both.median()) if len(both) >= 30 else np.nan
            ok = bool(np.isfinite(ratio) and lo <= ratio <= hi)
            checks[h.symbol] = {"binance_volume_ratio": ratio, "mapping_ok": ok,
                                "binance_first": c.first_valid_index(), "binance_last": c.last_valid_index()}
            if ok:
                cols[h.symbol] = c * usdt
    price = pd.DataFrame({s: cols.get(s, pd.Series(np.nan, index=idx)) for s in syms}, index=idx)
    meta = {h.symbol: h for h in candidates}
    out = {"price_raw": price, "volume_raw": vol, "sample_end": end, "usdt": usdt,
           **mask_deaths(price, vol, end, params.death_grace_days, meta)}
    life = out["lifetimes"]
    life["price_source"] = [meta[s].price_source if (meta[s].price_source != "binance" or
                            checks.get(s, {}).get("mapping_ok")) else "none" for s in life.index]
    for k in ("binance_volume_ratio", "mapping_ok", "binance_first", "binance_last"):
        life[k] = [checks.get(s, {}).get(k) for s in life.index]
    return out


def mask_deaths(price: pd.DataFrame, volume: pd.DataFrame, end, grace_days: int, meta=None) -> dict:
    raw = price.to_numpy()
    P_ = raw.copy()
    traded = ~np.isnan(P_) & (np.nan_to_num(volume.to_numpy(), nan=0.0) > 0)
    idx = price.index
    rows = []
    for j, s in enumerate(price.columns):
        tj = np.flatnonzero(traded[:, j])
        pj = np.flatnonzero(~np.isnan(raw[:, j]))
        last_t = idx[tj[-1]] if len(tj) else None
        died = last_t is not None and last_t < end - pd.Timedelta(days=grace_days)
        if died:
            P_[tj[-1] + 1:, j] = np.nan
        h = (meta or {}).get(s)
        rows.append({"symbol": s, "price_id": getattr(h, "price_asset", None),
                     "volume_id": getattr(h, "volume_asset", None),
                     "first_price": idx[pj[0]] if len(pj) else None, "last_price": idx[pj[-1]] if len(pj) else None,
                     "last_traded": last_t, "died": bool(died), "note": getattr(h, "note", "")})
    life = pd.DataFrame(rows).set_index("symbol")
    masked = pd.DataFrame(P_, index=idx, columns=price.columns)
    return {"price": masked, "volume": volume.where(masked.notna()), "lifetimes": life}


def month_starts(first: str, end) -> list[pd.Timestamp]:
    return list(pd.date_range(pd.Timestamp(first), end, freq="MS"))


# ====================================================================== universe
def eligibility(t, price: pd.DataFrame, volume: pd.DataFrame, *, top_k: int, min_history_days: int,
                est_window: int, rank_window: int, floor_usd: float) -> pd.DataFrame:
    """Who is eligible at rebalance date t, using only rows strictly before t.

    An asset is a contender if, before t, it has at least `min_history_days` of
    prices, a price on every day of the estimation window (plus one, for the
    first return), a price and positive reported volume on the day before t,
    and a median reported volume over the last `rank_window` days (missing days
    count as zero) of at least `floor_usd`. The contenders are ranked by that
    median (ties by symbol) and the top `top_k` are eligible.
    """
    p, v = price.loc[price.index < t], volume.loc[volume.index < t]
    hist_days = p.notna().sum()
    window_ok = p.tail(est_window + 1).notna().all() if len(p) > est_window else pd.Series(False, index=p.columns)
    last = p.index.max() if len(p) else None
    traded_prev = (p.loc[last].notna() & (v.loc[last].fillna(0) > 0)) if last is not None \
        else pd.Series(False, index=p.columns)
    med = v.tail(rank_window).fillna(0.0).median() if len(v) else pd.Series(0.0, index=p.columns)
    df = pd.DataFrame({"history_days": hist_days, "window_complete": window_ok, "traded_prev_day": traded_prev,
                       "median_volume_usd": med})
    df["contender"] = (df["history_days"] >= min_history_days) & df["window_complete"] & df["traded_prev_day"] \
        & (df["median_volume_usd"] >= floor_usd)
    order = df[df["contender"]].reset_index(names="symbol").sort_values(
        ["median_volume_usd", "symbol"], ascending=[False, True])
    df["rank"] = np.nan
    df.loc[order["symbol"], "rank"] = np.arange(1, len(order) + 1)
    df["eligible"] = df["rank"] <= top_k
    reason = np.where(df["eligible"], "eligible",
             np.where(df["history_days"] < min_history_days, "history",
             np.where(~df["window_complete"], "gap in window",
             np.where(~df["traded_prev_day"], "not trading",
             np.where(df["median_volume_usd"] < floor_usd, "below floor", "outside top K")))))
    df["reason"] = reason
    return df


def universe_table(dates, price, volume, **kw) -> pd.DataFrame:
    """Eligibility for every rebalance date, long format (date, symbol, ...)."""
    frames = []
    for t in dates:
        e = eligibility(t, price, volume, **kw)
        e = e[e["history_days"] > 0]                      # assets not yet listed are not rows
        frames.append(e.assign(date=t).reset_index(names="symbol"))
    out = pd.concat(frames, ignore_index=True)
    return out[["date", "symbol", "eligible", "rank", "median_volume_usd", "history_days", "reason"]]


# ====================================================================== backtests
SCHEMES = {
    "Equal weight": lambda h: P.equal_weight(h.columns),
    "Inverse vol": P.inverse_vol,
    "Min-var (LW)": lambda h: P.min_variance(P.ledoit_wolf_cov(h)[0]),
}


def run_backtests(price, volume, univ: pd.DataFrame, dates, *, est_window, adv_window, aum_scenarios,
                  max_adv_fraction, exit_haircut) -> dict:
    members = {t: g.loc[g["eligible"], "symbol"].tolist() for t, g in univ.groupby("date")}
    ufn = lambda t: members[t]
    # An asset never eligible can never be held: dropping it changes no number, only the run time.
    ever = [c for c in price.columns if c in set(univ.loc[univ["eligible"], "symbol"])]
    price, volume = price[ever], volume[ever]
    kw = dict(est_window=est_window, every=None, adv_window=adv_window, rebalance_on=dates,
              universe_fn=ufn, exit_haircut=exit_haircut)
    runs = {name: P.backtest(price, volume, fn, **kw) for name, fn in SCHEMES.items()}
    for aum in aum_scenarios:
        runs[f"Inverse vol, capped @ {_usd(aum)}"] = P.backtest(
            price, volume, P.inverse_vol, aum=aum, max_adv_fraction=max_adv_fraction, **kw)
    return runs


def btc_returns(price, index) -> pd.Series:
    return P.simple_returns(price[["BTC"]])["BTC"].reindex(index)


def net_of_cost(run: dict, cost_bps: float) -> pd.Series:
    """Daily returns after a flat cost per unit of turnover, charged on each rebalance day."""
    r = run["returns"].copy()
    c = run["turnover"].reindex(r.index).fillna(0.0) * cost_bps / 1e4
    return (1 + r) * (1 - c) - 1


def sharpe_diff(r1: pd.Series, r2: pd.Series, periods: int) -> tuple[float, float]:
    """Sharpe(r1) - Sharpe(r2), annualized, and its standard error.

    Jobson-Korkie test with Memmel's (2003) correction, iid case, written in the
    same annual-observation form as P.perf_stats' Lo (2002) standard error, so
    the two are on one footing:
        Var = (2 - 2 rho + (S1^2 + S2^2 - 2 S1 S2 rho^2) / 2) / years
    with S the annualized Sharpe ratios and rho the correlation of the returns.
    """
    both = pd.concat([r1, r2], axis=1).dropna()
    a, b = both.iloc[:, 0], both.iloc[:, 1]
    k = np.sqrt(periods)
    s1, s2 = a.mean() / a.std(ddof=1) * k, b.mean() / b.std(ddof=1) * k
    rho = float(a.corr(b))
    years = len(both) / periods
    var = (2 - 2 * rho + 0.5 * (s1 ** 2 + s2 ** 2 - 2 * s1 * s2 * rho ** 2)) / years
    return float(s1 - s2), float(np.sqrt(max(var, 0.0)))


def block_bootstrap_sharpe(returns: pd.DataFrame, periods: int, *, n: int = 2000, block: int = 20,
                           seed: int = 7, bench: str | None = None) -> pd.DataFrame:
    """Circular block bootstrap 95% intervals for each column's Sharpe (and its gap to `bench`).

    Resamples blocks of consecutive days (keeping short-range serial correlation
    and fat tails, which the iid formula ignores). One set of resampled dates is
    shared by all columns, so differences are resampled jointly. Fixed seed:
    the result is deterministic.
    """
    x = returns.dropna().to_numpy()
    T = len(x)
    rng = np.random.default_rng(seed)
    nb = -(-T // block)
    sh = []
    for lo in range(0, n, 500):
        m = min(500, n - lo)
        starts = rng.integers(0, T, size=(m, nb))
        idx = ((starts[:, :, None] + np.arange(block)) % T).reshape(m, -1)[:, :T]
        s = x[idx]                                             # (m, T, k)
        sh.append(s.mean(axis=1) / s.std(axis=1, ddof=1) * np.sqrt(periods))
    sh = np.vstack(sh)
    cols = list(returns.columns)
    out = {"boot_lo": np.percentile(sh, 2.5, axis=0), "boot_hi": np.percentile(sh, 97.5, axis=0)}
    if bench is not None:
        d = sh - sh[:, [cols.index(bench)]]
        out["boot_diff_lo"] = np.percentile(d, 2.5, axis=0)
        out["boot_diff_hi"] = np.percentile(d, 97.5, axis=0)
    return pd.DataFrame(out, index=cols)


def stats_table(series: dict[str, pd.Series], periods: int, bench: str = "BTC buy & hold") -> pd.DataFrame:
    rows = {}
    for name, r in series.items():
        st = P.perf_stats(r, periods)
        if name != bench and bench in series:
            st["sharpe_diff_vs_btc"], st["sharpe_diff_se"] = sharpe_diff(r, series[bench], periods)
        rows[name] = st
    return pd.DataFrame(rows).T.rename_axis("strategy")


def regime_table(series: dict[str, pd.Series], regimes, periods: int) -> pd.DataFrame:
    rows = []
    for slug, label, a, b in regimes:
        for name, r in series.items():
            part = r.loc[(r.index >= pd.Timestamp(a)) & (r.index <= pd.Timestamp(b))]
            if len(part) < 30:
                continue
            st = P.perf_stats(part, periods)
            rows.append({"regime": slug, "label": label, "start": str(part.index[0].date()),
                         "end": str(part.index[-1].date()), "strategy": name, **st})
    return pd.DataFrame(rows)


# ====================================================================== capacity over time
def capacity_over_time(targets: list[dict], aum_scenarios, max_adv_fraction: float) -> pd.DataFrame:
    """For each rebalance of the uncapped run: where the cap binds and what survives it."""
    rows = []
    for rec in targets:
        w, adv = rec["target"], rec["adv"]
        w = w[w > 0]
        row = {"date": rec["date"], "eligible_count": int(len(w)), "effective_n_target": P.effective_n(w),
               "max_aum_before_any_cap_usd": P.max_uncapped_aum(w, adv, max_adv_fraction),
               "first_binding_name": (max_adv_fraction * adv.reindex(w.index) / w).idxmin()}
        for aum in aum_scenarios:
            caps = P.capacity_caps(adv.reindex(w.index), aum, max_adv_fraction)
            wc = P.apply_caps(w, caps)
            tag = _tag(aum)
            row[f"effective_n_{tag}"] = P.effective_n(wc) if wc.sum() > 0 else 0.0
            row[f"names_at_cap_{tag}"] = int((wc >= caps - 1e-12).sum())
            row[f"weight_moved_{tag}"] = P.weight_moved(w, wc)
            row[f"investable_{tag}"] = float(wc.sum())
        rows.append(row)
    return pd.DataFrame(rows).set_index("date")


def first_sustained(series: pd.Series, threshold: float) -> pd.Timestamp | None:
    """First date from which the series stays at or above `threshold` for every later date."""
    ok = series >= threshold
    if not ok.iloc[-1]:
        return None
    bad = ok[~ok]
    later = ok.index[ok.index > bad.index.max()] if len(bad) else ok.index
    return later[0] if len(later) else None


# ====================================================================== liquidity trend
def rolling_amihud(price: pd.Series, volume: pd.Series, window: int, min_obs: int) -> pd.Series:
    """Rolling mean of |simple return| / dollar volume, in bps of price move per $1M traded.

    Same definition as liquidity.amihud (Amihud 2002), on a rolling window.
    Days with zero or missing volume are skipped, as there.
    """
    r = price.pct_change().abs()
    ratio = (r / volume.where(volume > 0)) * 1e6 * 1e4
    return ratio.rolling(window, min_periods=min_obs).mean()


def liquidity_trend(price_raw, volume_raw, symbols, dates, window, min_obs) -> pd.DataFrame:
    rows = {}
    for s in symbols:
        a = rolling_amihud(price_raw[s], volume_raw[s], window, min_obs)
        v = volume_raw[s].rolling(window, min_periods=min_obs).median()
        # value on the day before each month start: the trailing year that ends there
        rows[f"{s}_amihud_bps_per_1m"] = a.shift(1).reindex(dates)
        rows[f"{s}_median_volume_usd"] = v.shift(1).reindex(dates)
    return pd.DataFrame(rows, index=pd.DatetimeIndex(dates, name="date"))


# ====================================================================== coverage diagnostic
def coverage(dates, price, volume, *, top_k, min_history_days, rank_window, floor_usd) -> pd.DataFrame:
    """How much of the top of the market (by reported volume) the priced universe cannot hold.

    At each date, every candidate with at least `min_history_days` of reported
    volume and a median volume above the floor is ranked by that median (the
    same window as the universe). Of the top K, count the names that have no
    usable price history yet (fewer than `min_history_days` of prices before
    the date): the universe could not have held them for lack of a price.
    """
    rows = []
    for t in dates:
        v = volume.loc[volume.index < t]
        p = price.loc[price.index < t]
        med = v.tail(rank_window).fillna(0).median()
        ok = ((v.fillna(0) > 0).sum() >= min_history_days) & (med >= floor_usd)
        pool = med[ok].rename("m").to_frame()
        pool["priced"] = p.notna().sum().reindex(pool.index) >= min_history_days
        pool = pool.reset_index(names="symbol").sort_values(["m", "symbol"], ascending=[False, True]).head(top_k)
        miss = pool[~pool["priced"]]
        rows.append({"date": t, "top_k_size": len(pool), "unpriced_count": len(miss),
                     "unpriced_volume_share": float(miss["m"].sum() / pool["m"].sum()) if len(pool) else np.nan,
                     "unpriced_names": " ".join(miss["symbol"])})
    return pd.DataFrame(rows).set_index("date")


# ====================================================================== per-token long-run context
def token_context(sym: str, price: pd.Series, volume: pd.Series, univ: pd.DataFrame, periods: int) -> dict:
    p = price.dropna()
    lr = np.log(p / p.shift(1)).dropna()
    dd = p / p.cummax() - 1
    trough = dd.idxmin()
    peak = p.loc[:trough].idxmax()
    first_year = slice(p.index[0], p.index[0] + pd.Timedelta(days=364))
    last_year = slice(p.index[-1] - pd.Timedelta(days=364), p.index[-1])
    a = (p.pct_change().abs() / volume.where(volume > 0)) * 1e10
    u = univ[univ["symbol"] == sym]
    return {"symbol": sym, "history_start": str(p.index[0].date()), "history_end": str(p.index[-1].date()),
            "history_days": int(len(p)), "max_drawdown": float(dd.min()),
            "max_drawdown_peak": str(peak.date()), "max_drawdown_trough": str(trough.date()),
            "drawdown_now": float(dd.iloc[-1]), "vol_ann": float(lr.std(ddof=1) * np.sqrt(periods)),
            "amihud_first_year": float(a.loc[first_year].mean()), "amihud_last_year": float(a.loc[last_year].mean()),
            "median_volume_first_year": float(volume.loc[first_year].median()),
            "median_volume_last_year": float(volume.loc[last_year].median()),
            "months_in_universe": int(u["eligible"].sum()),
            "months_listed": int(len(u))}


# ====================================================================== helpers
def _usd(x):
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(x) >= div:
            return f"${x / div:g}{suf}"
    return f"${x:g}"


def _tag(n: float) -> str:
    """1e7 -> '10m', 1e9 -> '1b' (same as facts.size_tag)."""
    for div, suf in ((1e9, "b"), (1e6, "m"), (1e3, "k")):
        if n >= div:
            return f"{n / div:g}{suf}"
    return f"{n:g}"


# ====================================================================== orchestration
STRATEGY_ORDER = ["Equal weight", "Inverse vol", "Min-var (LW)", "Inverse vol, capped @ $10M",
                  "Inverse vol, capped @ $100M", "Inverse vol, capped @ $1B", "BTC buy & hold"]


def analyze(cm, bn, candidates, P_, LR, memo_map: dict) -> dict:
    """Everything the long-run layer reports, as tables. P_ = config.PARAMS, LR = config.LONGRUN."""
    pan = load_panels(cm, bn, candidates, LR)
    price, volume, end = pan["price"], pan["volume"], pan["sample_end"]
    dates = month_starts(LR.first_rebalance, end)
    ukw = dict(min_history_days=LR.min_history_days, est_window=P_.est_window_days,
               rank_window=LR.rank_window_days, floor_usd=LR.liquidity_floor_usd)
    univ = universe_table(dates, pan["price_raw"], pan["volume_raw"], top_k=LR.top_k, **ukw)
    bkw = dict(est_window=P_.est_window_days, adv_window=P_.adv_window_days,
               max_adv_fraction=P_.max_adv_fraction)
    runs = run_backtests(price, volume, univ, dates, aum_scenarios=P_.aum_scenarios,
                         exit_haircut=LR.exit_haircut, **bkw)
    idx = runs["Equal weight"]["returns"].index
    series = {k: runs[k]["returns"] for k in STRATEGY_ORDER if k in runs}
    series["BTC buy & hold"] = btc_returns(price, idx)
    stats = stats_table(series, P_.periods_per_year)
    for k, r in runs.items():
        net = net_of_cost(r, P_.cost_bps_assumption)
        ns = P.perf_stats(net, P_.periods_per_year)
        stats.loc[k, "ann_return_net"] = ns["ann_return"]
        stats.loc[k, "sharpe_net"] = ns["sharpe_rf0"]
        stats.loc[k, "avg_turnover_per_rebalance"] = r["avg_turnover"]
        stats.loc[k, "avg_invested"] = r["avg_invested"]
        stats.loc[k, "exits"] = len(r["exits"])
    boot = block_bootstrap_sharpe(pd.DataFrame(series), P_.periods_per_year, n=LR.bootstrap_reps,
                                  block=LR.bootstrap_block_days, seed=LR.bootstrap_seed, bench="BTC buy & hold")
    stats = stats.join(boot)
    stats = stats.loc[[k for k in STRATEGY_ORDER if k in stats.index]]
    regimes = regime_table(series, LR.regimes, P_.periods_per_year)

    # sensitivity of the headline scheme (inverse vol, uncapped) to the universe and exit rules
    sens = [{"variant": f"K = {LR.top_k} (base)", "top_k": LR.top_k, "exit_haircut": LR.exit_haircut,
             **P.perf_stats(runs["Inverse vol"]["returns"], P_.periods_per_year),
             "exits": len(runs["Inverse vol"]["exits"])}]
    for k in LR.k_sensitivity:
        u = universe_table(dates, pan["price_raw"], pan["volume_raw"], top_k=k, **ukw)
        r = run_backtests(price, volume, u, dates, aum_scenarios=(), exit_haircut=LR.exit_haircut, **bkw)
        sens.append({"variant": f"K = {k}", "top_k": k, "exit_haircut": LR.exit_haircut,
                     **P.perf_stats(r["Inverse vol"]["returns"], P_.periods_per_year),
                     "exits": len(r["Inverse vol"]["exits"])})
    stress = P.backtest(price, volume, P.inverse_vol, every=None, rebalance_on=dates,
                        universe_fn=lambda t, m={t: g.loc[g["eligible"], "symbol"].tolist()
                                                  for t, g in univ.groupby("date")}: m[t],
                        exit_haircut=LR.exit_haircut_stress, est_window=P_.est_window_days,
                        adv_window=P_.adv_window_days)
    sens.append({"variant": f"exit haircut {LR.exit_haircut_stress:.0%}", "top_k": LR.top_k,
                 "exit_haircut": LR.exit_haircut_stress, **P.perf_stats(stress["returns"], P_.periods_per_year),
                 "exits": len(stress["exits"])})
    sens = pd.DataFrame(sens).set_index("variant")

    capacity = capacity_over_time(runs["Inverse vol"]["targets"], P_.aum_scenarios, P_.max_adv_fraction)
    capsum = capacity_summary(capacity, P_.aum_scenarios, LR.deep_enough_moved)
    liq = liquidity_trend(pan["price_raw"], pan["volume_raw"], ["BTC", "ETH"], dates,
                          LR.amihud_roll_days, LR.amihud_min_obs)
    cov = coverage(dates, pan["price_raw"], pan["volume_raw"], top_k=LR.top_k, **{
        k: v for k, v in ukw.items() if k != "est_window"})
    tokens = {}
    for sym, hs in memo_map.items():
        if hs not in pan["price_raw"]:
            continue
        p = pan["price_raw"][hs]
        if p.notna().sum() >= LR.min_memo_history_days:
            tokens[sym] = {**token_context(hs, p, pan["volume_raw"][hs], univ, P_.periods_per_year),
                           "price_source": pan["lifetimes"].loc[hs, "price_source"]}
    tokens = pd.DataFrame(tokens).T.rename_axis("symbol") if tokens else pd.DataFrame()
    exits = pd.DataFrame([{"strategy": k, **e} for k, r in runs.items() for e in r["exits"]]
                         + [{"strategy": "stress: inverse vol", **e} for e in stress["exits"]])

    life = pan["lifetimes"].copy()
    elig = univ[univ["eligible"]]
    life["months_eligible"] = elig.groupby("symbol").size().reindex(life.index).fillna(0).astype(int)
    life["first_eligible"] = elig.groupby("symbol")["date"].min().reindex(life.index)
    life["last_eligible"] = elig.groupby("symbol")["date"].max().reindex(life.index)
    equity = pd.DataFrame({k: (1 + v).cumprod() for k, v in series.items()})
    return {"panels": pan, "dates": dates, "universe": univ, "runs": runs, "series": series, "stats": stats,
            "regimes": regimes, "sensitivity": sens, "capacity": capacity, "liquidity": liq, "coverage": cov,
            "tokens": tokens, "exits": exits, "lifetimes": life, "equity": equity, "capacity_summary": capsum}


def capacity_summary(cap: pd.DataFrame, aum_scenarios, moved_max: float) -> dict:
    """When was each fund size 'deep enough' (cap moves <= moved_max of the design)?

    first_ok_<aum>         first month it held
    sustained_from_<aum>   first month from which it held in every later month ("never" if the
                           latest month fails)
    ok_share_<aum>_pct     share of all months in which it held
    ok_share_last36_<aum>_pct  the same over the last 36 months
    """
    out = {}
    for a in aum_scenarios:
        tag = _tag(a)
        ok = (cap[f"weight_moved_{tag}"] <= moved_max + 1e-12).astype(float)
        first = ok[ok > 0].index.min() if ok.any() else None
        sus = first_sustained(ok, 1.0)
        out[f"first_ok_{tag}"] = f"{first:%Y-%m-%d}" if first is not None else "never"
        out[f"sustained_from_{tag}"] = f"{sus:%Y-%m-%d}" if sus is not None else "never"
        out[f"ok_share_{tag}_pct"] = 100 * float(ok.mean())
        out[f"ok_share_last36_{tag}_pct"] = 100 * float(ok.tail(36).mean())
    return out
