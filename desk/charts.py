"""Static PNG charts (matplotlib). One chart = one question.

Palette: a colour-vision-deficiency-validated categorical order
(blue, orange, aqua, ...), neutral grey for benchmarks, recessive grid.
"""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import matplotlib.dates as mdates  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#4a3aa7"]
NEUTRAL = "#8a8985"
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


def _style(ax, title: str, subtitle: str | None = None):
    ax.set_facecolor(SURFACE)
    ax.figure.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9)
    ax.grid(True, color=GRID, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    ax.set_title(title, loc="left", fontsize=12, color=INK, pad=22, fontweight="bold")
    if subtitle:
        ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9, color=INK2, va="bottom")


def _usd(x, _=None):
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(x) >= div:
            return f"${x / div:g}{suf}"
    return f"${x:g}"


def slippage_dotplot(liq: pd.DataFrame, sizes_tags: list[str], path):
    """Per token: estimated market-order cost at three order sizes (log x)."""
    # most liquid at the top: rank by $1M cost, then by $100k cost where $1M could not fill
    d = liq.sort_values([f"slip_{sizes_tags[1]}_bps", f"slip_{sizes_tags[0]}_bps"],
                        ascending=False, na_position="first")
    fig, ax = plt.subplots(figsize=(8, 0.34 * len(d) + 1.6))
    y = np.arange(len(d))
    labels = {"100k": "$100k", "1m": "$1M", "10m": "$10M"}
    for i, tag in enumerate(sizes_tags):
        v = d[f"slip_{tag}_bps"].values
        ax.scatter(v, y, s=42, color=SERIES[i], edgecolor=SURFACE, linewidth=1.5,
                   zorder=3, label=f"{labels.get(tag, tag)} order")
    # tokens whose visible book could not fill the largest order get a marker in the label
    big = d[f"slip_{sizes_tags[-1]}_bps"].isna()
    ax.set_yticks(y, [f"{s} *" if b else s for s, b in zip(d.index, big)])
    ax.set_xscale("log")
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:g}"))
    ax.set_xlabel("Estimated cost vs mid, bps (worse of buy/sell; log scale)", color=INK2, fontsize=9)
    _style(ax, "Market-order slippage walking the live consolidated book",
           "Coinbase + Kraken USD books at snapshot time. * = visible book (within 15% of mid) cannot fill $10M.")
    ax.legend(frameon=False, fontsize=9, loc="lower right", labelcolor=INK2)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def accrual_bars(fund: pd.DataFrame, path):
    """Annual fees vs annual holders revenue per token (log x): the accrual gap."""
    d = fund.dropna(subset=["fees_ann"]).sort_values("fees_ann")
    fig, ax = plt.subplots(figsize=(8, 0.38 * len(d) + 1.6))
    y = np.arange(len(d))
    h = 0.38
    floor = 1e5
    fees = d["fees_ann"].clip(lower=floor)
    hold = d["holders_revenue_ann"].fillna(0).clip(lower=floor)
    ax.barh(y + h / 2, fees, height=h, color=SERIES[0], label="Fees (trailing 365d)", zorder=3,
            edgecolor=SURFACE, linewidth=1)
    ax.barh(y - h / 2, hold, height=h, color=SERIES[1], label="Holders revenue (trailing 365d)",
            zorder=3, edgecolor=SURFACE, linewidth=1)
    for yy, (sym, row) in zip(y, d.iterrows()):
        if row["accrual_flag"].startswith("FEES"):
            ax.text(floor * 1.3, yy - h / 2, "no holder accrual" if "ZERO" in row["accrual_flag"] else "not reported",
                    va="center", fontsize=7.5, color=INK2, zorder=4)
    ax.set_yticks(y, d.index)
    ax.set_xscale("log")
    ax.set_xlim(left=floor)
    ax.xaxis.set_major_formatter(FuncFormatter(_usd))
    _style(ax, "Who captures the fees? Fees vs holders revenue",
           "DefiLlama, trailing 365 complete days, USD, log scale.")
    ax.legend(frameon=False, fontsize=9, loc="lower right", labelcolor=INK2)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def capacity_curves(curves: pd.DataFrame, marks, path):
    """Share of the target portfolio the ADV cap forces to be reallocated, vs AUM."""
    fig, ax = plt.subplots(figsize=(8, 4.6))
    for i, col in enumerate(curves.columns):
        ax.plot(curves.index, curves[col] * 100, color=SERIES[i], linewidth=2, zorder=3, label=col)
    ax.legend(frameon=False, fontsize=9, loc="upper left", labelcolor=INK2)
    for m in marks:
        ax.axvline(m, color=NEUTRAL, linewidth=0.8, linestyle=(0, (3, 3)), zorder=2)
        ax.text(m, 101, _usd(m), fontsize=8, color=INK2, ha="center", va="bottom")
    ax.set_xscale("log")
    ax.xaxis.set_major_formatter(FuncFormatter(_usd))
    ax.set_ylim(0, 108)
    ax.set_xlim(curves.index[0], curves.index[-1])
    ax.set_ylabel("% of portfolio reallocated by the cap", color=INK2, fontsize=9)
    ax.set_xlabel("Fund AUM", color=INK2, fontsize=9)
    _style(ax, "Capacity: how far the liquidity cap pushes the portfolio off target",
           "Each position capped at 10% of its 30-day median ADV (CoinGecko); excess moved to uncapped names.")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def equity_curves(curves: pd.DataFrame, benchmark: pd.Series, path):
    fig, ax = plt.subplots(figsize=(8, 4.6))
    ax.plot(benchmark.index, benchmark.values, color=NEUTRAL, linewidth=1.5, zorder=2)
    ax.text(benchmark.index[-1], benchmark.iloc[-1], "  BTC", color=INK2, fontsize=9, va="center")
    for i, col in enumerate(curves.columns[:4]):
        ax.plot(curves.index, curves[col], color=SERIES[i], linewidth=2, zorder=3)
        ax.text(curves.index[-1], curves[col].iloc[-1], "  " + col, color=INK2, fontsize=9, va="center")
    ax.axhline(1.0, color=INK2, linewidth=0.8, zorder=2)
    ax.set_ylabel("Growth of $1 (gross of costs)", color=INK2, fontsize=9)
    _style(ax, "Walk-forward backtest, out-of-sample period only",
           "Weights set from the prior 90 days, rebalanced every 30 days; one sample path, short history.")
    ax.set_xlim(right=curves.index[-1] + (curves.index[-1] - curves.index[0]) * 0.22)
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
