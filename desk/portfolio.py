"""Liquidity-constrained portfolio construction and a walk-forward backtest.

Returns conventions (stated once, used everywhere):
  * log returns   ln(P_t / P_{t-1})  -- for per-asset risk estimation (vol,
                  covariance); they add up over time, which is what a
                  volatility estimate over a window assumes.
  * simple returns P_t / P_{t-1} - 1  -- for portfolio aggregation; a
                  portfolio's simple return is the weighted sum of its assets'
                  simple returns (log returns do NOT aggregate across assets).
Annualisation uses 365 periods/year because crypto trades every calendar day.

No look-ahead: at every rebalance date t, weights use only data from days
strictly before t, and they are then held (drifting with prices) until the
next rebalance.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from sklearn.covariance import LedoitWolf


# ------------------------------------------------------------------ estimators
def log_returns(prices: pd.DataFrame) -> pd.DataFrame:
    return np.log(prices / prices.shift(1)).iloc[1:]


def simple_returns(prices: pd.DataFrame) -> pd.DataFrame:
    return (prices / prices.shift(1) - 1).iloc[1:]


def ledoit_wolf_cov(rets: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """Ledoit-Wolf shrunk covariance (daily units) and the shrinkage intensity.

    With ~90 daily observations and ~20 assets the sample covariance is
    noisy and badly conditioned; LW pulls it toward a scaled identity by a
    data-determined amount (0 = sample, 1 = fully shrunk).
    """
    lw = LedoitWolf().fit(rets.values)
    return pd.DataFrame(lw.covariance_, index=rets.columns, columns=rets.columns), float(lw.shrinkage_)


# ------------------------------------------------------------------ weighting schemes
def equal_weight(cols) -> pd.Series:
    n = len(cols)
    return pd.Series(1.0 / n, index=cols)


def inverse_vol(rets: pd.DataFrame) -> pd.Series:
    vol = rets.std(ddof=1)
    w = 1.0 / vol
    return w / w.sum()


def min_variance(cov: pd.DataFrame) -> pd.Series:
    """Long-only, fully invested minimum-variance weights (SLSQP)."""
    n = cov.shape[0]
    c = cov.values
    res = minimize(lambda w: w @ c @ w, np.full(n, 1.0 / n),
                   jac=lambda w: 2 * c @ w, method="SLSQP",
                   bounds=[(0.0, 1.0)] * n,
                   constraints=[{"type": "eq", "fun": lambda w: w.sum() - 1.0}],
                   options={"ftol": 1e-12, "maxiter": 500})
    if not res.success:
        raise RuntimeError(f"min-variance optimisation failed: {res.message}")
    w = np.clip(res.x, 0, None)
    return pd.Series(w / w.sum(), index=cov.index)


# ------------------------------------------------------------------ liquidity cap
def capacity_caps(adv_usd: pd.Series, aum: float, max_adv_fraction: float) -> pd.Series:
    """Max weight per asset so that position_i <= max_adv_fraction x ADV_i."""
    return (max_adv_fraction * adv_usd / aum).clip(lower=0.0).fillna(0.0)


def apply_caps(base: pd.Series, caps: pd.Series, tol: float = 1e-12) -> pd.Series:
    """Cap weights and redistribute the excess pro-rata to uncapped names.

    Iterates until no weight exceeds its cap. If the caps together sum to
    less than 100%, everything ends up at its cap and the remainder is held
    as cash (returned weights sum to < 1): that shortfall *is* the capacity
    constraint binding.
    """
    caps = caps.reindex(base.index).fillna(0.0)
    w = base.copy().astype(float)
    free = pd.Series(True, index=base.index)
    for _ in range(len(base) + 1):
        over = free & (w > caps + tol)
        if not over.any():
            break
        w[over] = caps[over]
        free &= ~over
        budget = 1.0 - w[~free].sum()
        if not free.any() or budget <= 0:
            break
        w[free] = base[free] / base[free].sum() * budget
    return w.clip(upper=caps)


def max_uncapped_aum(weights: pd.Series, adv_usd: pd.Series, max_adv_fraction: float) -> float:
    """Largest AUM at which no position breaches its ADV cap (the binding name sets it)."""
    w = weights[weights > 0]
    return float((max_adv_fraction * adv_usd.reindex(w.index) / w).min())


def weight_moved(target: pd.Series, capped: pd.Series) -> float:
    """Share of the portfolio the cap forced away from the target: sum|w_c - w_t| / 2.

    0 = the cap did nothing; 0.3 = 30% of the book had to be reallocated
    (to other names or to cash). Counts cash as a position.
    """
    diff = (capped.reindex(target.index).fillna(0) - target).abs().sum()
    cash_diff = abs((1 - capped.sum()) - (1 - target.sum()))
    return float((diff + cash_diff) / 2)


def effective_n(w: pd.Series) -> float:
    """Effective number of positions, 1 / sum(w_i^2) on the invested weights."""
    w = w[w > 0] / w[w > 0].sum()
    return float(1.0 / (w ** 2).sum())


# ------------------------------------------------------------------ backtest
def rebalance_dates(index: pd.DatetimeIndex, first_valid: int, every: int) -> list[pd.Timestamp]:
    return list(index[first_valid::every])


def backtest(prices: pd.DataFrame, dollar_volume: pd.DataFrame, weight_fn,
             est_window: int, every: int, adv_window: int,
             aum: float | None = None, max_adv_fraction: float | None = None) -> dict:
    """Walk-forward backtest with drifting weights between rebalances.

    weight_fn(log_returns_window) -> target weights (sums to 1).
    If `aum` is given, target weights are capped at max_adv_fraction x ADV / aum,
    with ADV = median dollar volume over the `adv_window` days *before* the
    rebalance date. Uninvested weight earns 0 (cash, no yield assumed).
    """
    simple = simple_returns(prices)
    logr = log_returns(prices)
    dates = simple.index
    rb = set(rebalance_dates(dates, est_window, every))
    w = pd.Series(0.0, index=prices.columns)
    port, invested, turnover, shrink = [], [], [], []
    for t in dates[est_window:]:
        if t in rb:
            hist = logr.loc[logr.index < t].tail(est_window)
            target = weight_fn(hist)
            if aum is not None:
                adv_t = dollar_volume.loc[dollar_volume.index < t].tail(adv_window).median()
                target = apply_caps(target, capacity_caps(adv_t, aum, max_adv_fraction))
            turnover.append(float((target - w).abs().sum()))
            w = target
        r_t = simple.loc[t].fillna(0.0)
        port_r = float((w * r_t).sum())
        port.append((t, port_r))
        invested.append(float(w.sum()))
        # drift: each position grows with its own return; cash stays put
        gross = w * (1 + r_t)
        total = gross.sum() + (1 - w.sum())
        w = gross / total
    s = pd.Series(dict(port)).sort_index()
    return {"returns": s, "avg_invested": float(np.mean(invested)),
            "avg_turnover": float(np.mean(turnover)) if turnover else 0.0,
            "n_rebalances": len(turnover)}


def perf_stats(r: pd.Series, periods: int, turnover_per_rebalance: float = 0.0,
               n_rebalances: int = 0, cost_bps: float = 0.0) -> dict:
    """Honest summary stats for a daily simple-return series.

    Sharpe uses a 0% risk-free rate (stated in the report) and comes with its
    approximate standard error, SE ~ sqrt((1 + SR^2/2) / years) (Lo, 2002, iid
    case), so a short sample's Sharpe is shown with how little it pins down.
    """
    n = len(r)
    years = n / periods
    total = float((1 + r).prod() - 1)
    ann_ret = (1 + total) ** (1 / years) - 1 if years > 0 else np.nan
    ann_vol = float(r.std(ddof=1) * np.sqrt(periods))
    sharpe = float(r.mean() / r.std(ddof=1) * np.sqrt(periods)) if r.std() > 0 else np.nan
    se = float(np.sqrt((1 + 0.5 * sharpe ** 2) / years)) if years > 0 else np.nan
    wealth = (1 + r).cumprod()
    mdd = float((wealth / wealth.cummax() - 1).min())
    # cost drag: turnover x cost at each rebalance, spread over the sample
    cost_total = turnover_per_rebalance * n_rebalances * cost_bps / 1e4
    return {"days": n, "total_return": total, "ann_return": ann_ret, "ann_vol": ann_vol,
            "sharpe_rf0": sharpe, "sharpe_se": se, "max_drawdown": mdd,
            "total_return_net_of_cost": (1 + total) * (1 - cost_total) - 1}


# ---------------------------------------------------------------- per-token risk
def token_risk(price: pd.Series, btc_price: pd.Series | None, window: int, periods: int) -> dict:
    """Market-risk descriptors for one token, from its daily close series.

    vol_ann        annualized std of the last `window` daily *log* returns (x sqrt(periods))
    return_total   simple return from the first to the last available close
    max_drawdown   worst peak-to-trough fall of the close series (a negative number)
    corr_btc       correlation of the last `window` daily log returns with BTC's
    """
    p = price.dropna()
    lr = np.log(p / p.shift(1)).dropna()
    recent = lr.tail(window)
    out = {"vol_ann": float(recent.std(ddof=1) * np.sqrt(periods)) if len(recent) > 1 else np.nan,
           "return_total": float(p.iloc[-1] / p.iloc[0] - 1) if len(p) > 1 else np.nan,
           "max_drawdown": float((p / p.cummax() - 1).min()) if len(p) else np.nan,
           "history_days": int(len(p)),
           "corr_btc": np.nan}
    if btc_price is not None:
        b = btc_price.dropna()
        blr = np.log(b / b.shift(1)).dropna()
        both = pd.concat([recent, blr], axis=1, join="inner").dropna()
        if len(both) > 2:
            out["corr_btc"] = float(both.iloc[:, 0].corr(both.iloc[:, 1]))
    return out
