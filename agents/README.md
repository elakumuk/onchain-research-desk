# Agents: the LLM writes, the verifier gates

This folder holds instructions for an LLM agent. The repository itself never calls an LLM API.
Everything in `desk/` is deterministic: the same data produces the same memos, byte for byte.

## The design in one picture

```
 public APIs --> desk.run --> reports/facts.json  (registry: id, value, unit, as-of, source)
                                   |
                     +-------------+----------------+
                     |                              |
               desk.memo (code)              memo agent (LLM, optional)
        deterministic sections from         writes reports/commentary/<SYM>.md
        {{fact:id}} templates               citing numbers only as {{fact:id}}
                     |                              |
                     +------> reports/memos/<SYM>.md <------+
                                   |
                              desk.verify   <-- the gate: exit 1 blocks the commit,
                                   |            the history archive and the site
                        history + site + commit (only if it passed)
```

## Why split writing and checking this way

**An LLM is good at prose and bad at being a source of numbers.** It will round inconsistently,
mix up units (a slippage in bps reported as a percent), carry a figure over from an earlier draft
or from its training data, or produce a plausible number that was never computed. None of these
failures looks wrong on the page. That is what makes them dangerous in an investment memo.

**So no number may originate in the LLM.** Every number comes from the facts registry, and the
LLM can only point at it with an id. A separate program, the verifier, then checks the finished
text. It does not trust the renderer either: it re-parses the markdown and checks every numeric
string it finds, whoever wrote it.

**Why a deterministic verifier and not a second LLM "checker"?** A second model has the same
failure modes as the first and gives no guarantee: it can miss a wrong digit, and it can be
argued with. A program gives the same answer every time, can be unit-tested against adversarial
cases (see `tests/test_verify.py`), and its rules can be read and challenged. The trade-off is
that it only checks what can be checked mechanically. See the limits below.

**Why a registry and not "check against the CSVs"?** A CSV cell has no stable name, no unit, no
timestamp and no source. The registry gives each number an id (`liq.AAVE.slip_1m_bps`), a unit
that is also spelled in the id, the time its data was fetched, and the exact endpoint and cached
file it came from. A citation therefore answers "which number, in what unit, how old, from
where" in one lookup.

## What the gate catches

| Failure | Example | Verifier result |
|---|---|---|
| Fabricated value | `171 bps[^liq.AAVE.slip_1m_bps]` when the fact is 371 | `value_mismatch` |
| Fabricated fact | `[^liq.AAVE.slip_5m_bps]` (no such id) | `unknown_fact` |
| Uncited number | "fees tripled to 12%" | `uncited` |
| Unit confusion | `3.71%` citing a bps fact, even though the conversion is right | `unit_mismatch` |
| Sign error | `74.6%` for a drawdown of -74.6% | `value_mismatch` |
| Lazy rounding | `$1B` for $964M | `imprecise` (under 2 significant figures) |
| Stale data | a fact fetched days before the rest of the run (cache fallback) | `stale` |
| Wrong run | a memo rendered against last week's registry | `wrong_registry` |
| Edited source line | a footnote pointing at a different endpoint | `footnote` |
| Broken template | `{{fact:...}}` left in, or mangled to `{fact:...}` | `unrendered_token` |
| Recommendation language | "price target", "we recommend", "guaranteed", "undervalued" | `banned` |
| Number smuggled in words | "five million" | `banned` |

Rounding that is correct passes: `371 bps`, `371.4 bps` and `371.39bps` all match 371.385.

## What it does not catch

- **Meaning.** A correct number attached to the wrong claim, or a direction word ("rose") that the
  numbers do not support. The verifier checks that `$X[^id]` equals fact `id`, not that the
  sentence around it is true.
- **Small numbers written as words** ("three venues"). Only magnitude words are banned.
- **Reasoning and emphasis.** A memo can be misleading with every number correct.

For these the agent works on a branch and a human reviews the pull request. The verifier removes
the class of error a reviewer is worst at spotting, a wrong digit among hundreds, so the reviewer's
attention goes to the reasoning.

## Why the agent's commentary lives in a separate file

`reports/commentary/<SYM>.md` carries the sha256 of the registry it was written against.
`desk.memo` inserts it only if that hash matches the current `facts.json`. Otherwise the memo says
the commentary was withheld. The `{{fact:id}}` tokens would re-render with new values every week, but the *sentences* around
them were written for last week's values, so an old interpretation must not sit next to new data.

## Why no LLM call in the code

- **Reproducibility.** `python -m desk.run --mode snapshot` and `python -m desk.memo` reproduce the
  committed outputs exactly, offline, with no key and no cost.
- **Testability.** The tests run in seconds and never depend on a model's mood.
- **Replaceability.** The agent is an operator following `MEMO_AGENT.md`. Any model, or a person,
  can follow the same instructions, and the guarantee comes from the verifier either way.
