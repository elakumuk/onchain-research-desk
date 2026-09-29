# onchain-research-desk

A reproducible research pipeline for crypto tokens. It answers three questions an analyst has to
answer before a token goes into an investment memo or a portfolio, and then writes the memo:

1. **Can we trade it at size?** Liquidity profile: Amihud illiquidity, live order-book depth,
   slippage for $100k / $1M / $10M market orders, and days to exit a position.
2. **Does the token capture the value the protocol creates?** Fees vs revenue vs holders
   revenue, annualized, with price-to-fees and price-to-holders-revenue multiples on circulating
   and fully diluted market cap, and a flag for tokens whose protocol earns fees but whose holders
   receive close to nothing.
3. **How does liquidity limit a portfolio as assets under management (AUM) grow?** Equal weight,
   inverse volatility and minimum variance (Ledoit-Wolf covariance) allocations, capped so that no position
   exceeds 10% of average daily volume, tested walk-forward with no look-ahead.

**v2** adds a fourth question, answered over ten years instead of one: **how would these portfolios
have behaved from 2016 to now, with a universe chosen the way it could have been chosen at the time
and with the assets that later died still in it, and when did the market become deep enough for a
fund to hold them?** (See "v2: the long run" below and ARCHITECTURE.md section 13.)

Every number comes from free public APIs. Nothing is typed in by hand. The data used for the results
below is committed in `data/snapshot/` (weekly snapshot) and `data/history/` (long history), so the
report can be reproduced exactly without network access.

## v1: an LLM may write prose, but a deterministic verifier gates every number

```
 APIs -> desk.run -> reports/facts.json -> desk.memo -> reports/memos/<SYM>.md -> desk.verify -> archive, site, commit
                     (2,968 facts, each     (every number is a     (+ optional LLM        (exit 1 = nothing
                      with id, unit,         citation token)         commentary)            ships)
                      as-of, source)
```

- **`reports/facts.json`**: every citable number from a run, with a stable id
  (`liq.AAVE.slip_1m_bps`), unit, as-of time and source (API, endpoint, cached file). The single
  source of truth.
- **`reports/memos/<SYMBOL>.md`**: a deterministic memo per token, structured as an institutional
  tokenomics review (supply, utility and demand, incentives and value accrual, governance,
  liquidity and technical risk, portfolio context, and "what the data can't tell us"). Every number
  is written as `371 bps[^liq.AAVE.slip_1m_bps]` with a footnote to its source. Anything the data
  cannot support says "*Requires analyst research*".
- **`desk/verify.py`**: parses any memo, including one written or edited by an LLM, and fails unless
  every number cites a fact whose value matches within rounding, in the same unit, from fresh data,
  with no recommendation language. It was built and tested against adversarial memos.
- **`agents/MEMO_AGENT.md`**: instructions for a scheduled Claude agent that adds commentary on
  top, citing fact ids only, and may commit only after the verifier passes. The code itself never
  calls an LLM.
- **`.github/workflows/`**: a weekly job (fetch, compute, render, verify, archive, commit) and a
  Pages job that publishes a static site with the memos, the methodology and a track record of
  dated snapshots.

ARCHITECTURE.md (sections 7 to 12) explains why each piece exists and what the verifier does and
does not catch. agents/README.md covers the LLM side.

---

## Run it

Requires Python 3.10+.

```bash
pip install -r requirements.txt

python -m desk.run --mode snapshot   # reproduce the committed results exactly, no network (~80 s)
python -m desk.run                   # live: pull fresh data (cached 12h), recompute everything
python -m desk.run --mode offline    # recompute from whatever is in the local cache
python -m desk.run --refresh-history # live, and re-pull the whole long history (not just recent days)
python -m desk.data.scan             # re-derive the long-run candidate list (one-off, ~20 min)

python -m desk.memo                  # render reports/memos/<SYMBOL>.md from reports/facts.json
python -m desk.verify                # check every number in every memo; exit 1 on any failure
python -m desk.history               # archive facts.json + memos to reports/history/<data date>/
python -m desk.site                  # build the static site into site/ (open site/index.html)

python -m pytest                     # 125 tests
```

A live run takes about 12 minutes: about 5 waiting between calls to stay within CoinGecko's keyless
rate limit, and about 7 appending the latest days to the long-history stores. `desk.memo`, `desk.verify` and `desk.site` take seconds and use only
the standard library. Outputs are written to `reports/`:

| File | Contents |
|---|---|
| `facts.json` | The facts registry: every citable number with id, unit, as-of and source |
| `memos/<SYMBOL>.md` | One research memo per token, every number footnoted to a fact id |
| `verification.json` | The verifier's report: claims checked per memo, and any issues |
| `history/YYYY-MM-DD/` | Dated archive of `facts.json` + memos, one folder per weekly run |
| `summary.md` | Headline tables and findings, generated from the CSVs |
| `liquidity.csv`, `liquidity_slippage.png` | Liquidity profile per token |
| `fundamentals.csv`, `value_accrual.png` | Value accrual table per token |
| `token_risk.csv` | Per-token volatility, drawdown, BTC correlation, cap-limited position size |
| `capacity.csv`, `capacity.png` | How far the liquidity cap pushes each scheme off target, by AUM |
| `backtest_stats.csv`, `backtest.png` | Walk-forward performance |
| `current_weights.csv` | Today's target weights per scheme |
| `universe.csv` | Which tokens entered which table, with notes on missing data |
| `data_provenance.csv` | Every payload used, where it came from (network, cache, snapshot), its URL and when it was fetched |
| `longrun.md` | v2: the long-run layer's tables and findings, generated from the CSVs below |
| `longrun_universe.csv` | Point-in-time universe: every month's members, rank, median volume, price source |
| `longrun_candidates.csv` | All 373 candidates: price source, first/last traded day, died or not, months eligible |
| `longrun_backtest_stats.csv`, `longrun_regimes.csv`, `longrun_sensitivity.csv` | Long backtest: full sample, per regime, K and exit-haircut sensitivity |
| `longrun_capacity.csv` | Per month: AUM at which the cap first binds, effective positions and weight moved at $10M/$100M/$1B |
| `longrun_liquidity.csv`, `longrun_coverage.csv` | Rolling Amihud and median volume (BTC, ETH); top names the universe could not price |
| `longrun_tokens.csv`, `longrun_exits.csv`, `longrun_provenance.csv` | Memo long-run context, forced exits, and every history source used |

**Automation.** `.github/workflows/weekly.yml` runs every Monday (and on demand): tests, live run
(saved as the new snapshot), recompute from that snapshot, memos, verification against the clock,
tests again, archive, commit. If verification fails, the job fails and
nothing is committed. `.github/workflows/pages.yml` then re-verifies and publishes `site/` to GitHub
Pages. (One-time setup: Settings, Pages, Source: GitHub Actions.)

---

## Headline findings from the committed run

Data as of **2026-09-28 18:54 UTC**, universe of 20 tokens. The numbers below are copied from
`reports/summary.md`. Treat them as a snapshot, not a recommendation.

**Liquidity on US-regulated USD venues (Coinbase + Kraken consolidated book)**

- Only **BTC, ETH and SOL** could absorb a $10M market order within 15% of mid. Estimated cost:
  6.2 / 14.9 / 46.4 bps.
- For a **$1M** order, costs range from **0.2 bps (BTC)** to **371 bps (AAVE)**. Ten of the 20 books
  (SKY, LDO, ARB, OP, PENDLE, JUP, COMP, AERO, MORPHO, JTO) cannot fill $1M within 15% of mid.
- Order-book depth and daily volume tell different stories. AAVE trades a median $256M a day
  (CoinGecko, all venues), yet only about $1.6M rests within ±2% on these two venues. That gap is
  the reason the tool measures both.

**Value accrual (DefiLlama, trailing 365 complete days)**

- Largest fee generators: UNI $964M, HYPE $911M, AAVE $740M, LDO $627M.
- Share of fees that reaches token holders varies from 0% to 94%. HYPE sends 75.5% to holders
  ($688M, a 3.5% yield on circulating market cap), LINK 93.6% and AERO 71.7%. AAVE sends 2.8% and
  LDO 1.1%.
- Flagged for **fees but about zero holder accrual**: MORPHO ($205M fees), JTO ($153M) and COMP ($30M).
  Flagged for **holder revenue not reported by DefiLlama**: ENA, ARB, OP and BTC. For BTC this is by
  design, because fees go to miners.
- Trailing and current run-rates can diverge sharply, and a memo needs both. UNI holders revenue is
  $52M over the trailing year but runs at **$191M/yr** on the last 30 days, which fits a recently
  switched-on fee switch. AAVE's trailing $20M runs at $0 over the last 30 days.

**Capacity (position ≤ 10% of 30-day median ADV)**

- The inverse-vol portfolio can run up to about **$25M** before any cap binds. The first binding name is SKY.
- At **$1B**, 17 of 20 names are at their cap, **48%** of the portfolio has to be reallocated, and
  BTC+ETH+SOL go from 24% to **60%** of weight. The effective number of positions falls from 18.1 to 6.7.
- In the walk-forward test (2025-12-29 to 2026-09-28, 274 out-of-sample days), inverse vol returned
  +42.5% uncapped, +31.8% capped at $100M and +2.3% capped at $1B. BTC returned -3.9%. The Sharpe
  ratios (1.08 / 0.90 / 0.32) each carry a standard error of about 1.2 to 1.5. **Over a sample this
  short, the differences are not statistically meaningful.** The test shows that capacity reshapes
  the portfolio. It does not show that any scheme is better.

**Verification of the committed memos**

- 20 memos, 1,355 numeric claims, **1,355 verified** against 2,968 facts (v1: 1,212 claims against 1,152 facts)
  (`python -m desk.verify`; report in `reports/verification.json`). The snapshot run reproduces
  `facts.json`, the memos and the verification report byte for byte.

### v2: the long run (2016-01-01 to 2026-09-27, 3,923 days, from `reports/longrun.md`)

Universe: each month, the top 20 assets by Coin Metrics reported volume among those with at least
90 days of prices and at least $1M median daily volume, chosen with data from before the rebalance
date only. The 373 candidates come from a rule-based scan and include assets that later collapsed,
migrated or were delisted. 108 were eligible at some point, and 19 of those later stopped trading.
LUNA, for example, ranked as high as 3rd, and its collapse is in the returns.

| Strategy | Return, ann. | Sharpe ± SE | Gap to BTC ± SE | Max drawdown |
|---|---|---|---|---|
| Equal weight | 54.6% | 0.94 ± 0.37 | -0.13 ± 0.29 | -92.5% |
| Inverse vol | 60.9% | 1.00 ± 0.37 | -0.08 ± 0.28 | -91.8% |
| Min-var (LW) | 133.5% | 1.53 ± 0.45 | +0.46 ± 0.33 | -83.7% |
| Inverse vol, capped @ $100M | 25.0% | 0.68 ± 0.34 | -0.40 ± 0.27 | -91.2% |
| Inverse vol, capped @ $1B | 11.0% | 0.48 ± 0.32 | -0.59 ± 0.28 | -84.8% |
| BTC buy & hold | 63.4% | 1.08 ± 0.38 | | -83.8% |

- **With ten years, Sharpe ratios mostly separate from zero, but the uncapped schemes still cannot be
  told apart from holding BTC.** Min-var's +0.46 gap is 1.4 standard errors. The capacity cost is
  measurable: the $1B-capped portfolio trails BTC by 0.59 ± 0.28, more than 1.96 standard errors. A
  block bootstrap (2,000 resamples, 20-day blocks) gives similar intervals, for example 0.32 to 1.66
  for inverse vol.
- **Per regime** the intervals are wide (standard errors 0.5 to 1.9), so the regimes show where
  returns came from, not which scheme is better. Min-var had the least-bad Sharpe ratio in both
  drawdown regimes (2018: -0.70 vs -1.72 for BTC; 2022: -0.70 vs -1.74).
- **When was crypto deep enough?** (the cap moves at most 10% of the designed portfolio): a $10M
  diversified fund in every month since January 2018, a $100M fund since August 2020, and a $1B fund
  never for long: in 7.8% of months (best: January 2022, 4.25% moved), and in none of the last 36.
  The constraint is the 20th name's volume, not BTC's.
- **Liquidity trend:** BTC's trailing-year Amihud illiquidity fell from 11.9 bps per $1M (to January
  2016) to 0.0106 (to January 2026), and ETH's from 65.5 (to January 2017) to 0.0273. Reported volume
  was heavily wash-traded before 2019, so the early values flatter the early market.
- **Robust to the universe rules:** K = 10 / 20 / 30 gives inverse-vol Sharpe ratios of
  1.01 / 1.00 / 0.95, and a 100% loss on every forced exit changes nothing, because the only held
  asset that died (LUNA) had already fallen to near zero within the daily returns.

**Previously published numbers are unchanged:** every v1 table, chart and fact (for example 17 of 20
names capped at $1B, AAVE's $256M ADV against $1.6M of ±2% depth, and the 1,152 v1 facts) is
byte-identical. The memos gained a "Long-run context" section for the 11 tokens with at least three
years of history, so the verified claim count is now **1,355** (the original 1,212 plus 143 new), all
verified against 2,968 facts.

---

## Data sources

| Source | Endpoint | Used for | Status in this run |
|---|---|---|---|
| CoinGecko (keyless) | `/coins/markets`, `/coins/{id}/market_chart?days=365&interval=daily` | prices, 24h volume, circulating mcap, FDV | worked; rate-limited (HTTP 429) twice, recovered by backoff |
| DefiLlama | `/summary/fees/{slug}?dataType=dailyFees / dailyRevenue / dailyHoldersRevenue` | fees, revenue, holders revenue | worked; returns HTTP 400 for holders revenue on BTC, ARB, OP, ENA (not tracked). Cached as a negative result. |
| Coinbase Exchange | `/products/{pair}/book?level=2` | live L2 order book | worked; JUP-USD is delisted, so JUP uses Kraken only |
| Kraken | `/0/public/Depth?count=500` | live L2 order book | worked; 500-level cap (see limitations) |
| Coin Metrics Community (keyless) | `/v4/timeseries/asset-metrics` (`PriceUSD`, `volume_reported_spot_usd_1d`), `/v4/catalog/assets` | v2: daily prices for 91 candidates since 2015, reported volume for all 373 | worked; a free daily price exists for only about 140 assets, hence the Binance archive |
| Binance public data archive | `data.binance.vision/data/spot/{monthly,daily}/klines/<PAIR>/1d/` | v2: daily USDT closes for 183 candidates Coin Metrics does not price, delisted pairs included | worked; 186 of 281 pairs listed; 2 ticker collisions and 2 too-recent listings rejected by the volume check |

No API keys are required. Every response is cached with its fetch timestamp (see ARCHITECTURE.md).

## Limitations (read before quoting any number)

- **Two venues, not the whole market.** Books come from Coinbase and Kraken only: US-regulated USD
  venues, which suits a US institutional desk. Offshore exchanges (Binance, OKX, Bybit) and on-chain
  DEX pools hold much more depth for most of these tokens, so slippage here is an upper bound for
  global execution.
- **One snapshot of the book.** Depth changes by the minute. A single snapshot says nothing about
  how depth behaves under stress, which is when it matters most.
- **Kraken returns at most 500 levels.** For BTC that covers only ±1.9% of mid (ETH ±2.7%), so
  Kraken's contribution to ±2% depth and to the $10M walk is slightly understated. Per-token
  coverage is in `liquidity.csv` (`min_venue_coverage_pct`).
- **ADV is CoinGecko's aggregate volume**, which includes venues with questionable volume. Days to
  liquidate and the capacity caps are therefore optimistic. A venue-level volume series is on the Next list.
- **DefiLlama definitions are DefiLlama's.** "Holders revenue" counts burns (ETH, SOL), buybacks and
  fee-switch distributions. Classifications can lag protocol changes, so verify on-chain before
  relying on one in a memo. "Fees" include supply-side payouts (LP fees, staking rewards, interest
  paid to lenders), which makes price-to-fees a usage multiple, not an earnings multiple.
- **Short sample, one regime (the v1 snapshot backtest).** 365 days of daily prices (the keyless
  CoinGecko limit), 90 of which are used to estimate, leaves 274 out-of-sample days. The v2 layer
  exists for this reason, and its own limits are listed in ARCHITECTURE.md section 13.10.
- **Survivorship and selection bias (v1 only).** The snapshot universe is today's liquid tokens with
  fee data. The v2 universe is point-in-time and keeps dead assets. Its remaining gaps: reported
  volume is wash-traded, 183 candidates are priced from one venue (Binance), and in the median month
  1 of the top 20 by volume has no usable price (8 in the worst month, October 2017).
- **Two volume definitions.** The snapshot layer uses CoinGecko's aggregate volume and the long-run
  layer Coin Metrics' reported volume (a narrower set of exchanges). Their capacity numbers are not
  comparable. For example, AAVE: $256M 30-day median on CoinGecko, $98.5M median over the last year
  on Coin Metrics.
- **Costs are an assumption in the backtest** (flat 10 bps per unit of turnover). The measured book
  slippage is not used historically, because it is a single present-day snapshot.
- **Risk-free rate is 0%** in the Sharpe ratio.

### Limits of the v1 pipeline

- **The verifier checks numbers, not meaning.** It proves that `$X[^id]` equals fact `id`. It does
  not prove the sentence around it is true ("fees rose" when they fell), that the right fact was
  cited, or that the reasoning holds. LLM commentary therefore goes to a human-reviewed branch.
- **Small numbers spelled as words** ("three venues") are not detected. Magnitude words
  ("million") are banned outright.
- **No week-over-week numbers in memos yet.** Archived registries are not citable, so changes
  cannot be quoted as numbers (see Next).
- **The weekly job commits a fresh data snapshot** (about 1.2 MB compressed) so that snapshot mode
  keeps reproducing the committed reports. Git history grows by roughly that much a week. The
  long-history stores (`data/history/`, 11 MB on disk) only append. Closed years never change, and
  the open year is plain text, so each week adds a small git delta.

## Next

- **Citable changes.** A diff module that turns two dated registries into `delta.*` facts, so a memo
  can say "holder revenue run-rate up X% week over week" and have it verified.
- **Material-change alerts.** Flag when a fee switch appears to turn on, a flag flips, or depth
  collapses between weekly snapshots.
- **Broader liquidity.** Add offshore CEX and on-chain DEX depth, a venue-level ADV, and repeated
  book snapshots, so the tool reports a distribution of depth rather than a single point.

## Layout

```
desk/
  config.py          universe + every parameter
  data/cache.py      HTTP retry/backoff + timestamped cache, 3 run modes
  data/fetchers.py   CoinGecko, DefiLlama, Coinbase, Kraken -> pandas
  liquidity.py       Amihud, ADV, book merge, depth, slippage walk, days to liquidate
  fundamentals.py    fee/revenue annualization, multiples, accrual flag
  portfolio.py       returns, Ledoit-Wolf, EW / inverse-vol / min-var, ADV caps, walk-forward backtest, token risk
  longrun.py         v2: point-in-time universe, deaths, long backtest, regimes, capacity over time, liquidity, coverage
  data/coinmetrics.py, data/binance.py   v2: append-only long-history stores (Coin Metrics, Binance archive)
  data/scan.py       v2: one-off rule-based scan that picks the long-run candidates
  charts.py          the four PNGs
  run.py             entrypoint: python -m desk.run (writes the CSVs and facts.json)
  facts.py           the facts registry: ids, units, as-of, provenance
  memo.py            deterministic memo templates + {{fact:id}} renderer
  verify.py          the verifier (the gate)
  history.py         dated archive of facts + memos
  mdhtml.py, site.py static site, standard library only
agents/              MEMO_AGENT.md (LLM agent procedure) and README.md (design)
.github/workflows/   weekly.yml (run + verify + commit), pages.yml (publish)
tests/               125 pytest tests
data/snapshot/       the exact raw files behind the committed snapshot reports (~3.8 MB)
data/history/        v2 long-history stores + universe_scan.csv (~11 MB)
reports/             outputs of the committed run
```

See **ARCHITECTURE.md** for why the code is structured this way and what each choice trades off.
