"""Value accrual: who captures the cash a protocol generates?

DefiLlama splits protocol cash flow into three nested layers:
    fees             -- everything users pay to use the protocol
    revenue          -- the part the protocol keeps (treasury + token holders)
    holders revenue  -- the part that reaches token holders (buybacks, burns,
                        fee switches, staking distributions)
The gap between fees and holders revenue is the core "value accrual" question
of a token memo: a protocol can be heavily used while its token captures
little or nothing.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

LAYERS = {"dailyFees": "fees", "dailyRevenue": "revenue", "dailyHoldersRevenue": "holders_revenue"}


def complete_days(s: pd.Series, as_of: pd.Timestamp) -> pd.Series:
    """Keep only days strictly before `as_of` (the current UTC day is partial)."""
    return s[s.index < as_of.normalize()]


def trailing_sum(s: pd.Series, as_of: pd.Timestamp, days: int) -> tuple[float, int]:
    """Sum over the `days` complete days before `as_of`, plus how many days had data.

    Missing days are NOT zero-filled: the coverage count is returned so the
    caller can see when a series is too young to trust.
    """
    end = as_of.normalize()
    w = s[(s.index >= end - pd.Timedelta(days=days)) & (s.index < end)]
    return float(w.sum()), int(w.notna().sum())


def annualize(s: pd.Series | None, as_of: pd.Timestamp, runrate_days: int) -> dict:
    """Two annual views of one cash-flow series.

    trailing_365d : the actual sum of the last 365 complete days (if the series
                    has fewer days, the available sum is scaled up to 365 and
                    `coverage_days` shows how many days it rests on).
    runrate_Nd    : last N days x 365/N -- reflects the *current* pace, noisier.
    """
    if s is None or s.empty:
        return {"trailing_365d": np.nan, "runrate": np.nan, "coverage_days": 0}
    t_sum, t_cov = trailing_sum(s, as_of, 365)
    r_sum, r_cov = trailing_sum(s, as_of, runrate_days)
    trailing = t_sum * 365 / t_cov if 0 < t_cov < 365 else (t_sum if t_cov else np.nan)
    runrate = r_sum * 365 / r_cov if r_cov else np.nan
    return {"trailing_365d": trailing, "runrate": runrate, "coverage_days": t_cov}


def value_accrual_row(series: dict[str, pd.Series | None], mcap: float, fdv: float,
                      as_of: pd.Timestamp, runrate_days: int,
                      min_fee_usd: float, flag_ratio: float) -> dict:
    """One row of the fundamentals table for one token."""
    row: dict = {}
    for layer in LAYERS.values():
        a = annualize(series.get(layer), as_of, runrate_days)
        row[f"{layer}_ann"] = a["trailing_365d"]
        row[f"{layer}_runrate"] = a["runrate"]
        row[f"{layer}_coverage_days"] = a["coverage_days"]
    row["holders_revenue_reported"] = series.get("holders_revenue") is not None

    fees = row["fees_ann"]
    hr = row["holders_revenue_ann"] if row["holders_revenue_reported"] else np.nan
    row["take_rate"] = _div(row["revenue_ann"], fees)             # revenue / fees
    row["holder_accrual_ratio"] = _div(hr, fees)                  # holders revenue / fees
    row["mcap"] = mcap
    row["fdv"] = fdv
    row["float_ratio"] = _div(mcap, fdv)                          # circulating / fully diluted
    for cap_name, cap in (("mcap", mcap), ("fdv", fdv)):
        row[f"p_fees_{cap_name}"] = _div(cap, fees)
        row[f"p_revenue_{cap_name}"] = _div(cap, row["revenue_ann"])
        row[f"p_holders_rev_{cap_name}"] = _div(cap, hr)
    # holder yield = holders revenue / market cap: the cash "earnings yield" to the token
    row["holder_yield_mcap"] = _div(hr, mcap)

    ratio = row["holder_accrual_ratio"]
    if not (fees and fees >= min_fee_usd):
        flag = "fees below threshold"
    elif not row["holders_revenue_reported"]:
        flag = "FEES, holder revenue not reported"
    elif np.isnan(ratio) or ratio < flag_ratio:
        flag = "FEES, ~ZERO HOLDER ACCRUAL"
    else:
        flag = ""
    row["accrual_flag"] = flag
    return row


def _div(a, b) -> float:
    try:
        if a is None or b is None or np.isnan(a) or np.isnan(b) or b <= 0:
            return float("nan")
    except TypeError:
        return float("nan")
    return float(a) / float(b)
