# Architecture, and why it is built this way

This document explains the structure of the code and the reasoning behind each decision,
including the alternatives and what each choice gives up. It is written so every choice can be
defended in a technical interview.

---

## 1. The shape of the pipeline

```
          config.py  (universe + every parameter, in one place)
              |
   data/fetchers.py  --->  data/cache.py  --->  data/raw/<source>/<key>/<timestamp>.json
              |                                    data/snapshot/<source>/<key>.json
              v
   +----------------+------------------+-------------------+
   | liquidity.py   | fundamentals.py  | portfolio.py      |   pure functions:
   | (books, volume)| (fees, mcap)     | (prices, volume)  |   DataFrames in, numbers out
   +----------------+------------------+-------------------+
              |
           run.py  --->  reports/*.csv, *.png, summary.md
```

**Why three layers (data -> analytics -> report)?**
The analytics modules never make network calls and never read files. They take pandas or numpy
objects and return numbers. This matters for three reasons:

1. **Testability.** `walk_book` can be tested on a 3-level synthetic book with a hand-computed answer
   (see `tests/test_liquidity.py`). A function that also fetched data could not be tested that way.
2. **Reproducibility.** Every input the analytics see went through the cache first, so any result
   can be recomputed from files on disk.
3. **Replaceability.** Adding a data source (e.g. Binance depth) only touches `fetchers.py`, and the
   slippage math does not change.

*Trade-off:* the code has more files than a single notebook would. For a one-off analysis a notebook
is faster to write. This is meant to be re-run and extended, so the separation earns its keep.

**Why is every parameter in `config.py`?** Analysts disagree about window lengths and thresholds
(90-day Amihud or 30-day? a cap at 10% of ADV or 20%?). Putting them in one frozen dataclass means
each one is visible, can be changed in one place, and is printed at the bottom of `summary.md`, so a
reader always knows which assumptions produced the numbers.

---

## 2. Data layer: caching, retries, snapshots

**What happens on every API call** (`desk/data/cache.py`):

1. Look for a cached response younger than 12 hours. If one exists, use it.
2. Otherwise call the API with **exponential backoff**: wait 2s, 4s, 8s... on network errors, HTTP 429
   (rate limit) or 5xx. If the server sends a `Retry-After` header, wait that long instead.
3. Save the response as `data/raw/<source>/<key>/<UTC timestamp>.json`, inside an envelope that
   records `fetched_at`, the URL and the parameters. Nothing is ever overwritten.
4. If the network fails completely, fall back to the latest cached file, then to the committed
   snapshot. Every fallback is logged to `reports/data_provenance.csv`.

**Why cache at all?**
- *Reproducibility:* crypto data changes by the second. Without a saved copy, a number in the README
  could never be re-derived. With the cache, `--mode snapshot` reproduces the committed report
  byte-for-byte (verified: the summary tables are identical across runs).
- *Rate limits:* CoinGecko's keyless tier allows roughly 10 calls per minute. Re-running the analysis
  while iterating on a chart should not spend any of them.
- *Auditability:* the planned v1 verifier agent needs to trace a number in a memo back to a raw file.
  Timestamped files make that possible.

**Why timestamped files instead of overwriting one file per key?** History is free this way. Two
pulls a week apart can be compared (e.g. did depth collapse?) without any extra code. *Trade-off:*
disk use grows, which is why `data/raw/` is gitignored.

**Why a separate committed snapshot?** A fresh clone has no raw cache. The snapshot contains exactly
the files the committed run used (120 files, about 3.7 MB). `Cache.save_snapshot()` copies only what
was actually served, so it holds no dead weight.

**Why cache "HTTP 400" answers too?** DefiLlama returns 400 when it does not track holders revenue
for a protocol (BTC, ARB, OP, ENA). That answer is information in its own right, and it differs from
"the file went missing". Caching the negative result keeps the two distinguishable in offline runs.
A 4xx is also never masked with stale data, because it means "this does not exist", not "try again later".

**Why trim some responses before caching?**
- Coinbase returns the *entire* BTC book, about 1 MB and 40k levels, some at prices 99% away from mid.
  Only levels within ±15% of mid are kept. Nothing beyond that is relevant to a market-order
  estimate, and the size trimmed away is recorded as `coverage`.
- DefiLlama responses include a per-chain breakdown the analysis does not use, so only the daily
  total series and metadata are kept.
- *Trade-off:* the cache is a documented subset of the raw response, not the raw response itself.
  If v1 needs per-chain fees, the transform has to change and the data has to be re-pulled.

**Completed days only.** CoinGecko's last daily point is "now", a partial day. DefiLlama's last point
is today, also partial. Both are dropped, so every row is a finished day. Including a half-day would
understate volume and fees and bias Amihud upward.

---

## 3. Liquidity: why both Amihud *and* order-book depth

They measure different things, and each covers the other's blind spot:

| | Amihud illiquidity | Order-book walk |
|---|---|---|
| Question | How much did price move per dollar traded, on average? | What would a market order of size X pay right now? |
| Data | Daily price and volume, any history length | Live L2 book, one instant |
| Strength | Comparable over time and across assets; academically standard (Amihud 2002) | Exact execution cost for the stated size, including the spread |
| Blind spot | Uses reported volume, which can be inflated; ignores intraday execution | Single snapshot; ignores hidden liquidity and how the book refills |

When the two disagree, the disagreement is a finding in its own right. AAVE has a large ADV
(~$256M/day on CoinGecko) but only about $1.6M within ±2% on Coinbase + Kraken. The volume happens
mostly elsewhere, or is less "real" than it looks. A memo should say which.

**Amihud details.** `ILLIQ = mean(|R_t| / DollarVolume_t)` over 90 days. It uses *simple* returns, as in
the original paper, and is reported in bps of price move per $1M traded. Zero-volume days are skipped
because the ratio is undefined there.

**ADV as a median, not a mean.** Crypto volume spikes on single days (listings, liquidation
cascades). A mean would overstate the volume available on a normal day, so the 30-day median is used.

**Merging the two venues' books.** The obvious approach, stacking both books by raw price, produced
*negative spreads* for BTC, LINK, PENDLE and MORPHO in the first run, because the two books are
snapshotted a moment apart and can be offset by a few bps (up to 14 bps for PENDLE and MORPHO; see
`venue_mid_gap_bps`). A crossed book implies free trades that do not exist. The fix is to express each
venue's levels *relative to its own mid*, then place them on a common reference mid (the average of
the venue mids). This keeps each venue's shape, i.e. how much size sits how far from mid, which is
what slippage depends on. The unit test `test_merge_books_never_crossed_when_venues_are_offset`
pins this behaviour down. *Trade-off:* the cross-venue price gap disappears from the slippage number,
though it is still reported separately. The assumption is that the gap is timing noise that
arbitrageurs close, not a persistent basis.

**Slippage definition.** Order size in coins = notional / mid, the same for buy and sell. The code
walks the asks (buy) or bids (sell) level by level and computes the volume-weighted fill price.
Cost = distance from mid in bps, so the half-spread is included. The headline number is the *worse*
of the two sides, to stay conservative. **If the visible book runs out, the answer is `n/a`, not an
extrapolation.** Inventing depth beyond the data would be exactly the kind of made-up number this
project avoids.

**Days to liquidate** = position / (participation x ADV). At a 10% participation rate you are at most
10% of the day's volume, a common institutional rule of thumb for limiting market impact. Both 10%
and 20% are reported.

---

## 4. Value accrual

DefiLlama splits cash flow into nested layers: **fees** (everything users pay), then **revenue** (what
the protocol keeps), then **holders revenue** (what reaches token holders through buybacks, burns,
fee switches or staking distributions). The investment question is how much of the fee stream the
*token* captures, which is `holders revenue / fees`.

**Two annualizations, on purpose.**
- `_ann`: the actual sum over the last 365 complete days. Stable, but slow to reflect changes.
- `_runrate`: the last 30 days x 365/30. Current, but noisy.
When they diverge, something changed. UNI's holders revenue is $52M trailing but $191M at the
current run-rate, consistent with a fee switch turning on. A memo that quoted only one of the two
would mislead.

**Missing days are not zero-filled.** If a series is younger than 365 days, the available sum is
scaled up to a year and the coverage (e.g. "BTC 339d") is reported. Zero-filling would silently
understate young protocols.

**Multiples on circulating *and* fully diluted cap.** Circulating market cap prices today's float.
Fully diluted valuation (FDV) prices the float after all scheduled unlocks. The ratio
(`float_ratio`) is itself a supply-overhang signal: HYPE's circulating cap is 23% of its FDV, so its
price-to-holders-revenue is 28.7x on circulating cap and 123x fully diluted.

**Undefined, not infinite.** Price-to-holders-revenue for a token with $0 holders revenue is left
blank (`n/a`), not reported as infinity. The flag column carries that information instead.

**The flag** fires when fees are at least $1M a year (to skip dust) and holders revenue is below 1% of
fees, or when DefiLlama does not report holders revenue at all. These are two distinct labels,
because "zero" and "unknown" are different findings.

---

## 5. Portfolio construction

**Returns conventions** (stated in the `portfolio.py` docstring):
- *Log returns* for estimating each asset's volatility and covariance. They add up over time, which
  a volatility estimate over a window implicitly assumes.
- *Simple returns* for the portfolio. A portfolio's simple return is the weighted sum of its
  assets' simple returns. Log returns do **not** add across assets, and using them for portfolio
  P&L is a common mistake.
- Annualization uses **365** periods a year, because crypto trades every calendar day. Using 252 (the
  equity convention) would understate annualized volatility by about 17%.

**Why Ledoit-Wolf shrinkage?** With 20 assets and a 90-day window, the sample covariance has 210
parameters estimated from 90 observations. It is noisy and nearly singular. In this run its
condition number is **520**. A minimum-variance optimizer inverts that matrix, so it amplifies the
noise and puts heavy bets on estimation error. Ledoit-Wolf blends the sample covariance with a
structured target by a data-chosen amount (intensity **0.09** here), and the condition number falls
to **87**. The test `test_ledoit_wolf_is_better_conditioned_than_sample` checks this property.
*Trade-off:* shrinkage adds a little bias in exchange for much less variance. With a longer window or
fewer assets it matters less.

**Why three weighting schemes?**
- *Equal weight:* no estimation at all, so no estimation error. The honest baseline.
- *Inverse vol:* uses only each asset's own volatility (the diagonal of the covariance matrix), so it
  is robust, and it stops the most volatile tokens from dominating the risk.
- *Min-variance (long-only, LW):* uses the full covariance and is the scheme that shows why shrinkage
  matters. In practice it concentrates heavily in BTC/ETH/SOL (effective N = 2.4), which is itself
  a useful finding about how correlated the alts are.

**Why capacity caps?** A weight is not a trade. On a $1B fund, a 5% weight in a token with $13M daily
volume is a $50M position, almost four days of the *entire market's* volume. The cap
`position_i <= 10% x ADV_i` turns "what should we own" into "what can we actually own and still exit
in about a day". `apply_caps` clips capped names and hands the excess to uncapped names
in proportion to their weight, repeating until nothing breaches. If every name is capped, the
remainder sits in cash. That shortfall is the point where capacity is fully exhausted.

**How capacity is reported.** "Share of AUM investable" stays at 100% until very large AUM, because
BTC and ETH absorb the excess. The informative measure is **how far the cap pushes the portfolio from
its target**: `weight_moved = sum|w_capped - w_target| / 2`, together with the effective number of
positions `1 / sum(w^2)`. At $1B the inverse-vol portfolio moves 48% of its weight and collapses from
18 effective positions to 7. The capped portfolio at that size is effectively a different strategy.

**No look-ahead** (`backtest()`): at each rebalance date *t*, weights use returns and volumes from
days strictly before *t*. They are then held and allowed to drift with prices until the next
rebalance, as a real portfolio would. The test `test_backtest_has_no_look_ahead` rewrites all prices
after a cut-off date and asserts that every portfolio return up to that date is unchanged. If any
future data leaked into an earlier decision, that test would fail.

**Reporting performance honestly.**
- Only the out-of-sample period is shown (the first 90 days are used for estimation, not scored).
- Sharpe comes with its standard error, `sqrt((1 + SR^2/2) / years)` (Lo 2002, iid case). With 0.75
  years of data the SE is 1.2 to 1.5, larger than most of the Sharpe ratios themselves. The summary
  says so directly.
- The net-of-cost line uses a flat assumed cost, labelled as an assumption. The measured slippage is
  *not* applied historically, because it is today's snapshot and applying it to past trades would be
  a form of look-ahead.

---

## 6. Testing strategy

The tests (27) target the places where a silent error would produce a plausible-looking wrong number:

| Test | Guards against |
|---|---|
| `walk_book` VWAP on a 3-level book | off-by-one in the partial fill of the last level |
| exhausted book returns `nan` | extrapolating depth that does not exist |
| merged book never crossed | the negative-spread artifact described above |
| Amihud on 3 toy prices (hand-computed 750 bps/$1M) | unit/scale mistakes |
| cap binding, cascading caps, cash when exhausted | weights summing above 1 or breaching caps |
| max uncapped AUM | the binding name and threshold |
| no look-ahead in the backtest | future data leaking into past weights |
| partial current day excluded, short-history scaling | biased annualized fees |
| zero vs not-reported holders revenue | conflating two different findings |
| cache: fresh hit, offline never touches network, snapshot fallback, cached 4xx | irreproducible runs |

---

## 7. What deliberately is not here (v0 scope)

- No LLM calls. The memo-writer and verifier agents are v1. Building them before the numbers are
  trustworthy would only automate mistakes.
- No scheduler or GitHub Actions yet. The pipeline has to be correct before it runs unattended.
- No offshore or DEX liquidity yet. The two-venue limitation is disclosed and not hidden.
