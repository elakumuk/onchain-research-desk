import numpy as np
import pandas as pd
import pytest

from desk import fundamentals as FU

AS_OF = pd.Timestamp("2026-01-01 15:00")


def _daily(value, days, end="2026-01-01"):
    idx = pd.date_range(end=end, periods=days, freq="D")
    return pd.Series(float(value), index=idx)


def test_trailing_sum_excludes_partial_current_day():
    s = _daily(1.0, 400)          # includes 2026-01-01, the partial day
    total, cov = FU.trailing_sum(s, AS_OF, 365)
    assert total == 365.0 and cov == 365


def test_annualize_scales_short_history_and_reports_coverage():
    s = _daily(2.0, 101)          # 100 complete days + the partial one
    a = FU.annualize(s, AS_OF, 30)
    assert a["coverage_days"] == 100
    assert a["trailing_365d"] == pytest.approx(2.0 * 365)
    assert a["runrate"] == pytest.approx(2.0 * 365)


def test_flag_fees_but_zero_holder_accrual():
    series = {"fees": _daily(10_000, 400), "revenue": _daily(1_000, 400), "holders_revenue": _daily(0, 400)}
    row = FU.value_accrual_row(series, mcap=1e9, fdv=2e9, as_of=AS_OF, runrate_days=30,
                               min_fee_usd=1e6, flag_ratio=0.01)
    assert row["fees_ann"] == pytest.approx(3.65e6)
    assert row["p_fees_mcap"] == pytest.approx(1e9 / 3.65e6)
    assert row["p_fees_fdv"] == pytest.approx(2e9 / 3.65e6)
    assert np.isnan(row["p_holders_rev_mcap"])      # no earnings -> multiple undefined, not infinite
    assert row["accrual_flag"] == "FEES, ~ZERO HOLDER ACCRUAL"


def test_no_flag_when_holders_capture_value():
    series = {"fees": _daily(10_000, 400), "revenue": _daily(5_000, 400), "holders_revenue": _daily(4_000, 400)}
    row = FU.value_accrual_row(series, 1e9, 1e9, AS_OF, 30, 1e6, 0.01)
    assert row["holder_accrual_ratio"] == pytest.approx(0.4)
    assert row["holder_yield_mcap"] == pytest.approx(4_000 * 365 / 1e9)
    assert row["accrual_flag"] == ""


def test_unreported_holders_revenue_is_flagged_distinctly():
    series = {"fees": _daily(10_000, 400), "revenue": _daily(0, 400), "holders_revenue": None}
    row = FU.value_accrual_row(series, 1e9, 1e9, AS_OF, 30, 1e6, 0.01)
    assert row["accrual_flag"] == "FEES, holder revenue not reported"
