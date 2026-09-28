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
              |
           facts.py  --->  reports/facts.json      (v1: every citable number, with id, unit, as-of, source)
              |
           memo.py   --->  reports/memos/<SYM>.md  (deterministic draft; optional LLM commentary slot)
              |
           verify.py --->  pass / fail             (the gate: nothing below runs on a failure)
              |
   history.py + site.py + git commit               (dated archive, static site, weekly automation)
```

Sections 1 to 6 describe the analytics (v0). Sections 7 to 12 describe the v1 research pipeline
built on top of it: how a number gets from an API into a sentence without anyone, human or LLM,
being able to change it on the way.

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
- *Auditability:* the v1 facts registry traces every number in a memo back to the cached file it
  was computed from (section 7). Timestamped files make that possible.

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
  If a later version needs per-chain fees, the transform has to change and the data has to be re-pulled.

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

The tests target the places where a silent error would produce a plausible-looking wrong number.
v0 had 27; v1 brings the suite to 105.

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
| snapshot save after a partial network outage | deleting snapshot files the reports still need |
| facts: id/unit suffix rule, provenance resolves to committed files, agrees with the CSVs | a registry that says something the tables do not |
| verifier: adversarial memos (section 9) | each way a memo can misstate a number |
| memo: 400 random values rendered then verified | the renderer and verifier disagreeing about rounding |
| memo: missing facts, unreported holder revenue, stale commentary | guessing, or zero standing in for unknown |
| site: HTML nesting, unique ids, every local link and footnote anchor resolves, escaping | a broken or injectable site |

Tests that read the committed outputs derive their expectations from `facts.json` rather than
hard-coding this week's values, so they keep passing after the weekly job commits new data.

---

## 7. The facts registry (`desk/facts.py`, `reports/facts.json`)

After each run, every citable number is written to one file, one fact per line:

```
{"id": "liq.AAVE.slip_1m_bps", "value": 371.385, "unit": "bps", "as_of": "2026-09-28T18:57:20Z",
 "description": "Cost of a $1M market order vs mid, worse of buy/sell (half-spread included)",
 "source": ["coinbase:book_AAVE-USD", "kraken:book_AAVEUSD"]}
```

and each source id resolves, in a `sources` table in the same file, to the API, the endpoint with
its parameters, the cached file on disk and the fetch time. This run has 1,152 facts.

**Why a registry at all?** A memo needs to cite numbers, and the CSVs are the wrong thing to cite. A
CSV cell has no stable name ("row 6, column 17" changes when a column is added), no unit, no
timestamp and no source. The registry gives each number:

- **a stable id** that reads like a sentence: segment, token, metric, unit (`fund.HYPE.holder_share_365d_pct`);
- **a unit**, which is also the id's suffix (`_usd`, `_pct`, `_bps`, `_x`, `_days`, `_count`). A human
  reading `3.71%[^liq.AAVE.slip_1m_bps]` can see the mismatch before any program does;
- **an as-of time**: the *oldest* fetch among the data it depends on. A value is only as fresh as its
  stalest input;
- **its provenance**: API, endpoint, and the committed cache file, so any number can be re-derived.

It is the single source of truth: the memo generator and the site read only this file, and the
verifier checks against only this file.

**Design choices and their trade-offs**

- *facts.py does no analytics.* It reads the report tables and converts units. The few new metrics
  v1 needed (per-token volatility, drawdown, BTC correlation, cap-limited position size, run-rate
  vs trailing ratios) were added to `portfolio.py` and `fundamentals.py` and written to CSV first.
  So every fact also exists in a table a human can open, and there is exactly one place per metric
  where the math lives. *Cost:* a new metric touches two files instead of one.
- *Percent, never 0-1 ratios.* `holder_share_365d_pct = 75.49` means 75.49%. Storing 0.7549 invites
  exactly the unit confusion the verifier exists to catch.
- *Six significant figures.* Far more than any memo shows, and it makes the file byte-identical
  across machines whose floating point differs in the last digits. *Cost:* the registry is not full
  float precision; for a research memo that is irrelevant.
- *Only finite values are facts.* "Not available" is information (the book could not fill a $10M
  order), but it is not a number, so it lives in `labels` and in the memo's words. There is no NaN
  for a template to print by accident.
- *Parameters are facts too* (`param.order_size_1m_usd`, `param.runrate_window_days`). A memo that says
  "over the last 30 days" is stating a number. Making the assumptions citable means *every* digit
  in a memo is checked, with no list of "harmless" numbers to maintain.
- *Normalized provenance.* The first version embedded full source records in every fact and came
  to 1.5 MB. Referencing a `sources` table by id (and a group id for the twenty price histories
  behind every portfolio fact) brought it to about 300 KB, about 25 KB compressed.
- *Determinism.* The file holds no wall-clock time, only data timestamps, so `--mode snapshot`
  reproduces it byte for byte, and its sha256 identifies the run.

---

## 8. The deterministic memo (`desk/memo.py`)

For each token, code builds a *template*: prose plus citation tokens such as
`{{fact:liq.AAVE.slip_1m_bps}}`, never a typed number. `render()` replaces each token with the value
formatted in the fact's unit, followed by a footnote marker, `371 bps[^liq.AAVE.slip_1m_bps]`, and
appends a Sources section with one canonical footnote per cited fact. The header records the
sha256 of the registry it was rendered from.

**Structure.** The sections follow the questions institutional tokenomics frameworks ask. Fidelity
Digital Assets' public research on token supply, demand and value accrual is one reference. The
memo is the desk's own format, not affiliated with any firm:

1. Supply model: circulating, total and maximum supply; circulating share of fully diluted value.
2. Utility and demand drivers: fees as a usage proxy, trailing vs current pace, trading volume.
3. Incentive and value-accrual mechanisms: revenue, holders revenue, holder share, multiples, flags.
4. Governance.
5. Liquidity, market and technical risk: spread, depth, slippage, Amihud, days to exit, volatility.
6. Portfolio context: cap-limited position size, current weights, the AUM at which the cap binds.
7. Analyst commentary (the LLM slot, section 10), then "What the data can't tell us", then Sources.

**Only what the data supports.** Every section ends with a "*Requires analyst research.*" list of
what the desk's data cannot answer (unlock calendars, the mechanism behind holders revenue,
governance, audits). Governance is *entirely* that list, because the desk has no governance data.
Writing plausible governance prose without data is exactly the failure this project exists to
prevent.

**A missing fact drops the sentence, never fills it.** Each sentence is built by a helper that
returns nothing if any fact it needs is absent. A test builds a memo for a token with almost no
data and checks that it verifies and says "requires analyst research" rather than inventing.
Building that test surfaced a real bug: an early version joined tokens into a Python format
string, which silently turned `{{fact:x}}` into `{fact:x}`. No digit was visible, so the first
verifier passed it. The renderer was fixed, and the verifier now rejects any token-like leftover.

**Words from numbers, not numbers in words.** Some sentences choose an adjective from a value
(holder share above one half reads "most of the fee stream reaches holders"; a run-rate above 1.25x
the trailing figure reads "a mechanism may have changed"). The thresholds live in code and are
never quoted, so they need no citation, and the cited number sits next to the adjective so a reader
can judge it.

**Formatting rule:** 3 significant figures, but never rounding inside the integer part
(`$84,006`, `20,080x`), and trailing zeros dropped only when that loses nothing (`$1M` for exactly
1,000,000, but `$1.00M` for 1,004,000). A property test renders 400 random values in every unit and
checks the verifier accepts all of them. That test guarantees the two modules agree on rounding.

---

## 9. The verifier (`desk/verify.py`): why it exists and what it checks

**The problem.** An LLM (or a tired analyst) writing about 1,152 numbers will occasionally round
inconsistently, swap units, carry over last week's figure, or produce a plausible number that was
never computed. In prose none of these look wrong. In an investment memo any of them is a
credibility failure.

**The rule.** Every number a reader sees must be a *claim* of the form
`<number><unit>[^<fact id>]`, and the claim must match the registry. The verifier re-parses the
finished markdown, whoever wrote it, and checks:

| Check | Rule | Why this rule |
|---|---|---|
| citation | the number is immediately followed by `[^id]` | adjacency is unambiguous; "somewhere in the sentence" is not |
| existence | the id is in `facts.json` | catches invented or renamed facts |
| unit | the written unit is the fact's unit ($ = usd, %, bps, x, days; a bare number may cite a count, a statistic or days) | 3.71% for 371 bps is a correct conversion, but a reader comparing two memos needs one unit per fact, so conversions are rejected |
| value | within half a unit of the last written digit | exactly the definition of "correctly rounded": 371, 371.4 and 371.39 all pass for 371.385; 370 does not |
| precision | at least 2 significant figures unless exact | stops "$1B" for $964M, which is a correct rounding to one digit but not an honest one |
| freshness | no fact more than 48h older than the newest data in the run; for live runs, none more than 24h (CI) old | catches the cache's stale-fallback path mixing old data into a live run |
| binding | the memo header's sha256 equals the registry's | a memo verified against the wrong week is not verified |
| sources | each footnote is byte-identical to what the registry generates | nobody can quietly edit where a number "came from" |
| leftovers | no `{{fact:...}}` or mangled `{fact:...}` remains | a template that failed to render |
| language | no guarantees, price targets, buy/sell/hold calls, "undervalued", "will rise", "risk-free" (except "risk-free rate"), or "million/billion" spelled out | the desk describes; recommendations are the analyst's job, and spelled-out magnitudes would dodge the digit check |

It **fails closed**: anything that looks like a number and is not a verified claim is an error,
including an innocent-looking "3 times" or "30-day". A false alarm costs an edit. A false pass
costs credibility. Dates in ISO form, ordered-list markers, numbered headings, link targets and
digits inside fact ids are the only exemptions, each by an explicit pattern.

**Why a deterministic program instead of trusting the LLM, or asking a second LLM to check?**

- Same input, same verdict, every time. It can be unit-tested against adversarial cases
  (`tests/test_verify.py`: a fabricated value, a fabricated id, a stale fact, an uncited number,
  percent vs bps, a sign error, lazy rounding, an edited footnote, a wrong registry, banned
  phrases, and rounded-but-correct numbers that must pass).
- A second LLM shares the first one's failure modes, cannot promise it checked every digit, and its
  verdict cannot be reproduced next week.
- Its rules are short enough to read and argue with, which is what a compliance reviewer or an
  interviewer will do.

**What it does not catch, stated plainly:**

- *Meaning.* It checks that `$X[^id]` equals fact `id`, not that the sentence around it is true or
  that `id` is the right fact for the sentence. "Fees rose to $X" passes if $X is right even when
  fees fell. Comparatives are not checked either: a draft commentary written while testing this
  pipeline called HYPE's holder cash flow "the strongest in the universe", which the verifier
  cannot evaluate.
- *Small numbers written as words* ("three venues"). Only magnitude words are banned.
- *Reasoning and emphasis.* A memo can mislead with every number correct.

That is why LLM output goes to a human-reviewed branch (section 10). The verifier takes away the
class of error people are worst at spotting, one wrong digit among hundreds, so the reviewer can
spend attention on the argument.

---

## 10. Where the LLM fits (`agents/`)

The code never calls an LLM API. `agents/MEMO_AGENT.md` is a procedure for a scheduled Claude
agent (or a person): read `facts.json` and the memo, write commentary to
`reports/commentary/<SYM>.md` citing numbers only as `{{fact:id}}` tokens, run `desk.memo` and
`desk.verify`, revise until verification passes (at most five tries per token, then drop the
commentary for that token), and commit to a review branch, never to main.

- **Commentary is bound to one registry.** Its header carries the registry's sha256, and
  `desk.memo` inserts it only when that matches. The tokens would re-render with new values every
  week, but sentences written about last week's values must not appear next to this week's.
- **No LLM in the code** keeps every run reproducible offline, the tests fast and free, and the
  guarantee independent of which model (or person) writes the prose.

`agents/README.md` has the full reasoning and the failure table.

---

## 11. Automation and publication

**`weekly.yml`** (Monday cron, plus manual dispatch): tests, then a live run with `--save-snapshot`,
memo render, verification with a 24-hour freshness limit, tests again on the new outputs, history
archive, a site smoke build, then a commit. If verification fails the job fails and nothing is
committed. The gate is the job's exit code, not a convention.

*Why `--save-snapshot` every week?* The invariant "`--mode snapshot` reproduces the committed
reports" must hold for every commit, not only the first. *Cost:* the snapshot is about 3.8 MB
(about 1.2 MB compressed) and changes every week, so git history grows by roughly that much a week.
If that becomes a problem, the snapshot can move to release assets or Git LFS without changing the
code.

**`reports/history/YYYY-MM-DD/`** keeps only `facts.json` and the memos (about 60 KB compressed a
week). The folder is named after the data date, not the clock, so re-running is idempotent, and
`desk.history` refuses to archive a memo the verifier rejects. Everything else can be regenerated
from that commit's snapshot.

**`pages.yml`** builds `site/` with `python -m desk.site` and deploys it to GitHub Pages. It triggers
after a successful weekly run through `workflow_run`, because a push made with the Actions token
does not trigger `push` workflows. It re-runs the verifier (without the clock check, so a doc-only
push weeks later still deploys) and needs only the standard library.

**The site** has no JavaScript and no framework: an index table whose every value is read from
`facts.json` (hover shows the fact id), memo pages with numbered source footnotes, this document as
the methodology page, and a track record listing each archived snapshot, *re-verified against its
own registry at build time*. The Markdown converter is about 200 lines of standard library code,
escapes all input, and is tested for well-formed HTML and resolving links. *Trade-off:* it supports
only the Markdown subset this repo writes.

---

## 12. What is deliberately not here

- No LLM API calls in code, for the reasons in section 10.
- No numeric comparison with earlier snapshots in memos. History registries are archived but not
  citable, so "fees are up X% week over week" cannot be written yet. The natural next step is a
  diff module that writes `delta.*` facts from two registries, so changes become citable facts too.
- No offshore or DEX liquidity yet. The two-venue limitation is disclosed and not hidden.
- No check of the *meaning* of a sentence. That is the human reviewer's job, by design.
