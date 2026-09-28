# onchain-research-desk

A small, reproducible research toolkit for crypto tokens. It answers three questions an analyst
has to answer before a token goes into an investment memo or a portfolio:

1. **Can we trade it at size?** Liquidity profile: Amihud illiquidity, live order-book depth,
   slippage for $100k / $1M / $10M market orders, and days to exit a position.
2. **Does the token capture the value the protocol creates?** Fees vs revenue vs holders
   revenue, annualized, with price-to-fees and price-to-holders-revenue multiples on circulating
   and fully diluted market cap, and a flag for tokens whose protocol earns fees but whose holders
   receive close to nothing.
3. **How does liquidity limit a portfolio as assets under management (AUM) grow?** Equal weight,
   inverse volatility and minimum variance (Ledoit-Wolf covariance) allocations, capped so that no position
   exceeds 10% of average daily volume, tested walk-forward with no look-ahead.

Every number comes from free public APIs. Nothing is typed in by hand. The data used for the results
below is committed in `data/snapshot/`, so the report can be reproduced exactly without network access.

This is v0. It covers the quantitative inputs to a memo and does not write the memo itself.
See "Next: v1".

---

## Run it

Requires Python 3.10+.

```bash
pip install -r requirements.txt

python -m desk.run --mode snapshot   # reproduce the committed results exactly, no network
python -m desk.run                   # live: pull fresh data (cached 12h), recompute everything
python -m desk.run --mode offline    # recompute from whatever is in the local cache
python -m pytest                     # 27 unit tests
```

A live run takes about 5 minutes, mostly because it waits between calls to stay within
CoinGecko's keyless rate limit. Outputs are written to `reports/`:

| File | Contents |
|---|---|
| `summary.md` | Headline tables and findings, generated from the CSVs |
| `liquidity.csv`, `liquidity_slippage.png` | Liquidity profile per token |
| `fundamentals.csv`, `value_accrual.png` | Value accrual table per token |
| `capacity.csv`, `capacity.png` | How far the liquidity cap pushes each scheme off target, by AUM |
| `backtest_stats.csv`, `backtest.png` | Walk-forward performance |
| `current_weights.csv` | Today's target weights per scheme |
| `universe.csv` | Which tokens entered which table, with notes on missing data |
| `data_provenance.csv` | Every payload used, where it came from (network, cache, snapshot) and when it was fetched |

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

---

## Data sources

| Source | Endpoint | Used for | Status in this run |
|---|---|---|---|
| CoinGecko (keyless) | `/coins/markets`, `/coins/{id}/market_chart?days=365&interval=daily` | prices, 24h volume, circulating mcap, FDV | worked; rate-limited (HTTP 429) twice, recovered by backoff |
| DefiLlama | `/summary/fees/{slug}?dataType=dailyFees / dailyRevenue / dailyHoldersRevenue` | fees, revenue, holders revenue | worked; returns HTTP 400 for holders revenue on BTC, ARB, OP, ENA (not tracked). Cached as a negative result. |
| Coinbase Exchange | `/products/{pair}/book?level=2` | live L2 order book | worked; JUP-USD is delisted, so JUP uses Kraken only |
| Kraken | `/0/public/Depth?count=500` | live L2 order book | worked; 500-level cap (see limitations) |

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
  liquidate and the capacity caps are therefore optimistic. A venue-level volume series is on the v1 list.
- **DefiLlama definitions are DefiLlama's.** "Holders revenue" counts burns (ETH, SOL), buybacks and
  fee-switch distributions. Classifications can lag protocol changes, so verify on-chain before
  relying on one in a memo. "Fees" include supply-side payouts (LP fees, staking rewards, interest
  paid to lenders), which makes price-to-fees a usage multiple, not an earnings multiple.
- **Short sample, one regime.** 365 days of daily prices (the keyless CoinGecko limit), 90 of which
  are used to estimate, leaves 274 out-of-sample days. Sharpe standard errors are reported for this reason.
- **Survivorship and selection bias.** The universe is today's liquid tokens with fee data. Tokens
  that failed or delisted during the year are not in it.
- **Costs are an assumption in the backtest** (flat 10 bps per unit of turnover). The measured book
  slippage is not used historically, because it is a single present-day snapshot.
- **Risk-free rate is 0%** in the Sharpe ratio.

## Next: v1 (planned, not built)

- **Memo-writer agent.** An LLM drafts a token memo (technical design, security, value accrual,
  liquidity, sizing) from the CSVs this pipeline produces.
- **Verifier agent.** A second pass that checks every number in the draft traces back to a cell in
  `reports/*.csv` and from there to a cached raw file. Any claim without a trace gets rejected.
- **Scheduled automation.** A GitHub Actions workflow runs the pipeline on a schedule, commits a
  dated snapshot and flags material changes, such as a fee switch turning on or depth collapsing.
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
  portfolio.py       returns, Ledoit-Wolf, EW / inverse-vol / min-var, ADV caps, walk-forward backtest
  charts.py          the four PNGs
  run.py             entrypoint: python -m desk.run
tests/               27 pytest tests
data/snapshot/       the exact raw files behind the committed reports (~3.7 MB)
reports/             outputs of the committed run
```

See **ARCHITECTURE.md** for why the code is structured this way and what each choice trades off.
