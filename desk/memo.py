"""Deterministic token memos, rendered from facts.json only.

    python -m desk.memo                         # write reports/memos/<SYMBOL>.md for every token
    python -m desk.memo --symbols AAVE HYPE     # just these
    python -m desk.memo --render draft.md       # render {{fact:id}} tokens in any file (used by the memo agent)

How a memo is made:
  1. A *template* is built in code for each token. It contains prose and
     citation tokens such as {{fact:liq.AAVE.slip_1m_bps}}, never a typed
     number. Sections the data cannot support say "Requires analyst research"
     instead of guessing.
  2. `render()` replaces each token with the fact's value, formatted to 3
     significant figures in the fact's unit, followed by a footnote marker:
     `371 bps[^liq.AAVE.slip_1m_bps]`. It appends a Sources section with one
     footnote per cited fact (value, as-of, API endpoint, cache file).
  3. The memo header records the sha256 of the facts.json it was rendered
     from, so the verifier can refuse a memo checked against the wrong run.

The memo structure follows the questions institutional tokenomics frameworks
ask (Fidelity Digital Assets' public research on token supply, demand and
value accrual is one reference): supply, utility and demand, incentives and
value accrual, governance, and liquidity/technical risk, plus what the data
cannot tell us. It is the desk's own format, not affiliated with any firm.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

from desk.config import REPORTS_DIR
from desk.facts import Fact, Registry, load_registry
from desk.verify import CITE_RE, HEADER_RE, TOKEN_RE, footnote_definition

MEMO_DIR = REPORTS_DIR / "memos"
COMMENTARY_DIR = REPORTS_DIR / "commentary"
COMMENTARY_HEADER_RE = re.compile(
    r"<!--\s*commentary\s+symbol=(?P<symbol>\S+)\s+facts_sha256=(?P<sha>[0-9a-f]{64})[^>]*-->\s*")
SOURCES_HEADING = "## Sources"
ANALYST = "*Requires analyst research.*"

# thresholds for the words the template chooses (kept out of the text on purpose:
# they choose an adjective, they are never quoted as a number)
MOST, MINORITY = 50.0, 10.0          # holder share of fees, percent
ACCEL, DECEL = 1.25, 0.8              # run-rate / trailing
LOW_FLOAT, FULL_FLOAT = 50.0, 99.5    # circulating / fully diluted, percent
HIGH_CORR = 0.7                       # correlation with BTC


class RenderError(ValueError):
    pass


# ---------------------------------------------------------------- number formatting
def _fmt_num(v: float, sig: int = 3) -> str:
    """3 significant figures, but never round inside the integer part (84005.7 -> '84,006')."""
    if v == 0:
        return "0"
    a = abs(v)
    if a >= 1000:
        s = f"{round(v):,}"
    else:
        decimals = max(0, sig - 1 - math.floor(math.log10(a)))
        s = f"{v:.{decimals}f}"
        if "." in s:
            short = s.rstrip("0").rstrip(".")
            if float(short) == v:          # drop zeros only when nothing is lost ("1.00" for exactly 1)
                s = short
    return s


def _compact(v: float) -> str:
    a = abs(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if a >= div:
            mant = v / div
            return _fmt_num(v / div) + suf
    return _fmt_num(v)


def format_value(value: float, unit: str) -> str:
    v = float(value)
    neg = "-" if v < 0 else ""
    a = abs(v)
    if unit == "usd":
        return neg + "$" + (_compact(a) if a >= 1e6 else _fmt_num(a))
    if unit == "count":
        return neg + (_compact(a) if a >= 1e6 else _fmt_num(a))
    s = neg + _fmt_num(a)
    return s + {"pct": "%", "bps": " bps", "x": "x", "days": " days", "number": ""}[unit]


def cite(fact: Fact) -> str:
    return f"{format_value(fact.value, fact.unit)}[^{fact.id}]"


# ---------------------------------------------------------------- render
def render(md: str, reg: Registry, symbol: str | None = None) -> str:
    """Replace {{fact:id}} tokens, rebuild the Sources section and the registry header.

    Idempotent: rendering an already-rendered memo gives the same text, and a
    memo with a mix of rendered citations and new tokens comes out consistent.
    """
    if SOURCES_HEADING in md:
        md = md[: md.index(SOURCES_HEADING)].rstrip() + "\n"

    def sub(m):
        fid = m.group("id")
        if fid not in reg:
            raise RenderError(f"unknown fact id in template: {fid}")
        return cite(reg[fid])
    md = TOKEN_RE.sub(sub, md)

    hdr = HEADER_RE.search(md)
    symbol = symbol or (hdr.group("symbol") if hdr else "UNKNOWN")
    header = f"<!-- desk-memo symbol={symbol} facts_sha256={reg.sha256} -->"
    md = HEADER_RE.sub(header, md, count=1) if hdr else header + "\n" + md

    ids = list(dict.fromkeys(m.group("id") for m in CITE_RE.finditer(md)))
    unknown = [i for i in ids if i not in reg]
    if unknown:
        raise RenderError(f"citations to unknown fact ids: {unknown}")
    lines = [md.rstrip(), "", SOURCES_HEADING, "",
             "Each number above links to one entry below: the fact id in `reports/facts.json`, its value, "
             "when the data was fetched, and the API endpoint and cached file it came from.", ""]
    lines += [footnote_definition(reg[i], reg) for i in ids]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- template
class _T:
    """Template helper: `t(id)` gives a citation token, or None if the fact does not exist."""

    def __init__(self, reg: Registry, sym: str):
        self.reg, self.sym = reg, sym

    def __call__(self, fid: str) -> str | None:
        fid = fid.replace("{S}", self.sym)
        return f"{{{{fact:{fid}}}}}" if fid in self.reg else None

    def v(self, fid: str) -> float | None:
        fid = fid.replace("{S}", self.sym)
        return float(self.reg[fid].value) if fid in self.reg else None

    def s(self, text: str, **ids: str) -> str | None:
        """Fill `text` with tokens for the given fact ids, or return None if any fact is missing."""
        toks = {k: self(v) for k, v in ids.items()}
        if any(v is None for v in toks.values()):
            return None
        return text.format(**toks)


def _digit_free(s: str | None) -> str | None:
    return s if s and not re.search(r"\d", s) else None


def build_template(sym: str, reg: Registry) -> str:
    """The memo template for one token: prose + {{fact:id}} tokens, no typed numbers.

    Every sentence that needs facts is built with `t.s(...)`, which returns None
    (and the sentence is dropped) if any fact it needs is missing. A sentence
    therefore either appears complete or not at all; there is no path by which
    a missing value can turn into a guessed one.
    """
    t = _T(reg, sym)
    lab = reg.labels["tokens"][sym]
    flag = lab.get("accrual_flag") or ""
    name = _digit_free(lab.get("llama_name")) or sym
    venues = " + ".join(v.capitalize() for v in (lab.get("venues") or "none").split("+"))
    as_of = (reg.labels.get("market_as_of") or "").replace("T", " ").replace("Z", " UTC")
    out: list[str | None] = []
    add = out.extend

    ptype = _digit_free(lab.get("protocol_type"))
    add([f"# {sym}: token research memo", "",
         f"**Deterministic draft.** Generated by `desk.memo` from `reports/facts.json`; market data as of "
         f"{as_of}. Descriptive research only, not investment advice. Every number carries a footnote to "
         "its fact id, and `python -m desk.verify` checks each one against the registry.", "",
         "| | |", "|---|---|",
         f"| Segment | {lab.get('kind')}" + (f" (DefiLlama: {name}, {ptype})" if ptype else "") + " |",
         f"| Venues in the liquidity study | {venues} |",
         f"| Value-accrual flag | {flag or 'none'} |", "",
         "## Key figures", "",
         "| Metric | Value |", "|---|---|",
         t.s("| Price | {v} |", v="mkt.{S}.price_usd"),
         t.s("| Circulating market cap | {v} |", v="mkt.{S}.mcap_usd"),
         t.s("| Fully diluted valuation | {v} |", v="mkt.{S}.fdv_usd"),
         t.s("| Fees, trailing year | {v} |", v="fund.{S}.fees_365d_usd"),
         t.s("| Holders revenue, trailing year | {v} |", v="fund.{S}.holders_revenue_365d_usd"),
         t.s("| Holder share of fees | {v} |", v="fund.{S}.holder_share_365d_pct"),
         t.s("| Circulating market cap / holders revenue | {v} |", v="fund.{S}.p_holders_rev_mcap_x"),
         t.s("| Cost of a {o} market order | {v} |", o="param.order_size_1m_usd", v="liq.{S}.slip_1m_bps"),
         t.s("| Median daily volume | {v} |", v="liq.{S}.adv_30d_usd"),
         t.s("| Annualized volatility | {v} |", v="risk.{S}.vol_90d_ann_pct"), ""])

    # ------------------------------------------------------------ 1. supply
    add(["## 1. Supply model", ""])
    circ = t.s("CoinGecko reports a circulating supply of {c} tokens", c="mkt.{S}.circulating_supply_count")
    if circ:
        circ += t.s(" out of a total supply of {v}", v="mkt.{S}.total_supply_count") or ""
        circ += (t.s(" and a maximum supply of {v}.", v="mkt.{S}.max_supply_count") or
                 ". It reports no maximum supply: issuance is either uncapped or the cap is not published.")
        add([circ, ""])
    fr = t.s("Circulating market cap is {v} of fully diluted valuation.", v="fund.{S}.float_ratio_pct")
    fr_v = t.v("fund.{S}.float_ratio_pct")
    if fr:
        if fr_v < LOW_FLOAT:
            fr += (" Most of the fully diluted supply is not yet circulating, so scheduled unlocks are a "
                   "material supply overhang and the fully diluted multiples below matter more than the "
                   "circulating ones.")
        elif fr_v >= FULL_FLOAT:
            fr += " Essentially all of the fully diluted supply already circulates, so there is no unlock overhang to model."
        add([fr, ""])
    add([f"{ANALYST} Issuance or emission schedule and the resulting inflation rate; the unlock calendar "
         "(cliff dates and sizes); the initial allocation between team, investors, treasury and community; "
         "and any supply sinks other than those DefiLlama counts as holders revenue.", ""])

    # ------------------------------------------------------------ 2. utility / demand
    add(["## 2. Utility and demand drivers", ""])
    fees = t.s("Fees are the desk's proxy for demand: what users paid to use the protocol. Over the trailing "
               "{yr} users paid {f}; the last {rr} annualize to {r}",
               yr="param.fees_trailing_window_days", f="fund.{S}.fees_365d_usd",
               rr="param.runrate_window_days", r="fund.{S}.fees_runrate_usd")
    if fees:
        ratio_v = t.v("fund.{S}.fees_runrate_vs_365d_x")
        word = ("faster than" if ratio_v is not None and ratio_v > ACCEL else
                "slower than" if ratio_v is not None and ratio_v < DECEL else "in line with")
        fees += (t.s(", {x} the trailing figure, so current usage is running " + word + " the past year.",
                     x="fund.{S}.fees_runrate_vs_365d_x") or ".")
        add([fees, ""])
        cov = t.v("fund.{S}.fees_coverage_days")
        if cov is not None and cov < t.v("param.fees_trailing_window_days"):
            add([t.s("The fee series covers only {c} of the trailing window; the annual figure is scaled up "
                     "from those days and should be read with that in mind.", c="fund.{S}.fees_coverage_days"), ""])
        add(["DefiLlama fees include payments that go to the supply side (liquidity providers, lenders, "
             "stakers or validators), so fees measure activity, not the protocol's earnings.", ""])
    else:
        add(["The desk has no fee data for this token, so it has no quantitative demand measure.", ""])
    trade = t.s("Trading demand: median daily volume across all venues is {a}", a="liq.{S}.adv_30d_usd")
    if trade:
        add([trade + (t.s(" (last reported day: {v}).", v="mkt.{S}.volume_24h_usd") or "."), ""])
    add([f"{ANALYST} What the token is needed for (gas, collateral, staking, governance only, fee "
         "discounts); whether using the protocol requires holding or buying the token; who the users are "
         "and what alternatives they have.", ""])

    # ------------------------------------------------------------ 3. incentives / value accrual
    add(["## 3. Incentive and value-accrual mechanisms", ""])
    add([t.s("Of the {f} in trailing fees, the protocol kept {r} as revenue (a take rate of {k}).",
             f="fund.{S}.fees_365d_usd", r="fund.{S}.revenue_365d_usd", k="fund.{S}.take_rate_365d_pct"), ""])
    share_v = t.v("fund.{S}.holder_share_365d_pct")
    if lab.get("holders_revenue_reported") is False:
        add(["**DefiLlama does not report holders revenue for this token.** The desk therefore cannot say "
             "how much of the fee stream reaches holders. "
             + ("For Bitcoin this is by design: transaction fees go to miners, and the token has no "
                "protocol-level cash flow to holders." if sym == "BTC" else
                "This is an unknown, not a zero: the mechanism has to be established from the protocol's "
                "documentation and on-chain data."), ""])
    elif share_v is not None:
        word = ("Most of the fee stream reaches token holders" if share_v >= MOST else
                "A minority of the fee stream reaches token holders" if share_v >= MINORITY else
                "Only a small part of the fee stream reaches token holders" if share_v > 0 else
                "No measurable part of the fee stream reaches token holders")
        add([t.s(word + ": holders revenue was {h} over the trailing year, {p} of fees.",
                 h="fund.{S}.holders_revenue_365d_usd", p="fund.{S}.holder_share_365d_pct"), ""])
        pace = t.s("At the pace of the last {rr} it runs at {v} a year",
                   rr="param.runrate_window_days", v="fund.{S}.holders_revenue_runrate_usd")
        hrr_v = t.v("fund.{S}.holders_rev_runrate_vs_365d_x")
        if pace and hrr_v is not None:
            tail = (" A step up this large usually means a mechanism changed (for example a fee switch or "
                    "buyback turning on), which the trailing number does not yet show." if hrr_v > ACCEL else
                    " Holder cash flow has slowed or stopped recently; the trailing number overstates the "
                    "current pace." if hrr_v < DECEL else
                    " The recent pace is consistent with the year.")
            add([pace + t.s(", {x} the trailing figure.", x="fund.{S}.holders_rev_runrate_vs_365d_x") + tail, ""])
        elif pace:
            add([pace + ("; nothing reached holders over the past year either."
                         if t.v("fund.{S}.holders_revenue_runrate_usd") == 0 else "."), ""])
        method = _digit_free(lab.get("holders_revenue_method"))
        if method:
            add([f"DefiLlama's stated holders-revenue method: \"{method}\".", ""])
    if "ZERO HOLDER ACCRUAL" in flag:
        add([t.s("**Flag:** the protocol earns fees above the desk's {m} threshold but holders receive less "
                 "than {p} of them. Usage and token value are disconnected unless a mechanism not captured "
                 "by DefiLlama exists.", m="param.min_fee_for_flag_usd", p="param.holder_accrual_flag_pct"), ""])
    rows = [(lbl, t(f"fund.{{S}}.{k}_mcap_x"), t(f"fund.{{S}}.{k}_fdv_x"))
            for lbl, k in (("fees", "p_fees"), ("revenue", "p_revenue"), ("holders revenue", "p_holders_rev"))]
    rows = [r for r in rows if r[1] or r[2]]
    if rows:
        add(["Valuation multiples (market value divided by the trailing cash flow). Price-to-fees is a "
             "usage multiple; price-to-holders-revenue is the closest analogue to a P/E:", "",
             "| Cash flow | on circulating market cap | on fully diluted valuation |", "|---|---|---|",
             *[f"| {lbl} | {a or 'n/a'} | {b or 'n/a'} |" for lbl, a, b in rows], "",
             t.s("Holders revenue as a yield on circulating market cap: {v}.", v="fund.{S}.holder_yield_mcap_pct"),
             ""])
    add([f"{ANALYST} The mechanism behind holders revenue (buyback, burn, fee switch, staking "
         "distribution) and who exactly receives it; whether buybacks are programmatic or discretionary; "
         "token emissions paid out as incentives, which are a cost that DefiLlama's fee and revenue series "
         "do not net out; treasury size and spending policy.", ""])

    # ------------------------------------------------------------ 4. governance
    add(["## 4. Governance", "",
         f"{ANALYST} The desk has no governance data source. To research: who can change fees, emissions and "
         "treasury policy (token vote, delegates, a foundation or core team); recent or pending proposals "
         "that affect value accrual; admin keys, upgrade powers and timelocks; the legal wrapper and its "
         "obligations to token holders.", ""])

    # ------------------------------------------------------------ 5. liquidity / risk
    add(["## 5. Liquidity, market and technical risk", ""])
    add([t.s(f"On the consolidated {venues} USD book the spread is {{sp}}.", sp="liq.{S}.spread_bps"),
         t.s(" Resting depth is {a} within ±{b1} of mid and {c} within ±{b2}.",
             a="liq.{S}.depth_1pct_usd", b1="param.depth_band_1_pct",
             c="liq.{S}.depth_2pct_usd", b2="param.depth_band_2_pct")])
    out[-2:] = ["".join(x for x in out[-2:] if x) or None]
    sizes = [("param.order_size_100k_usd", "liq.{S}.slip_100k_bps"), ("param.order_size_1m_usd", "liq.{S}.slip_1m_bps"),
             ("param.order_size_10m_usd", "liq.{S}.slip_10m_bps")]
    filled = [t.s("{c} for {o}", c=c, o=o) for o, c in sizes]
    missing = [t(o) for (o, c), f in zip(sizes, filled) if f is None and t(o)]
    if any(filled) or missing:
        add([""])
        s = ""
        if any(filled):
            s = ("Estimated cost of a market order, worse of buy and sell, half-spread included: "
                 + "; ".join(f for f in filled if f) + ". ")
        if missing:
            tail = t.s("The visible book could not fill SIZES within ±{b} of mid, so no cost is reported for "
                       "that size: the desk does not extrapolate depth it cannot see.", b="param.book_keep_band_pct")
            s += tail.replace("SIZES", " or ".join(missing)) if tail else ""   # tokens never pass through format()
        add([s.strip()])
    add(["", t.s("Volume tells a different story from depth: the median day trades {a} across all venues "
                 "(CoinGecko), and Amihud illiquidity is {am} of price move per {o} traded.",
                 a="liq.{S}.adv_30d_usd", am="liq.{S}.amihud_bps_per_1m", o="param.order_size_1m_usd"),
         t.s("Exiting a {p} position at {r} of daily volume takes {d}. Reported volume includes venues of "
             "uneven quality, so these volume-based figures are optimistic.",
             p="param.liquidation_position_usd", r="param.participation_10_pct",
             d="liq.{S}.days_to_liq_10m_at_10pct_days")])
    out[-2:] = [" ".join(x for x in out[-2:] if x) or None]
    risk = t.s("Market risk: annualized volatility over the last {w} is {v}.",
               w="param.est_window_days", v="risk.{S}.vol_90d_ann_pct")
    if risk:
        risk += t.s(" Over the price history the token returned {r} with a maximum drawdown of {d}.",
                    r="risk.{S}.return_365d_pct", d="risk.{S}.max_drawdown_365d_pct") or ""
        cv = t.v("risk.{S}.corr_btc_90d")
        corr = t.s(" Correlation of daily returns with BTC is {c}", c="risk.{S}.corr_btc_90d")
        if corr:
            risk += corr + (", so much of its movement is market-wide rather than token-specific."
                            if cv >= HIGH_CORR else ".")
        add(["", risk])
    add(["", f"{ANALYST} Smart-contract and technical risk: audits, exploit history, upgradeability, oracle "
         "and bridge dependencies, validator or sequencer concentration; custody support; regulatory status "
         "of the token in the jurisdictions that matter to the fund.", ""])

    # ------------------------------------------------------------ 6. portfolio context
    add(["## 6. Portfolio context", ""])
    pos = t.s("With positions capped at {c} of median daily volume, the largest position the desk would hold "
              "is {m}.", c="param.max_adv_fraction_pct", m="port.{S}.max_position_at_cap_usd")
    if pos and lab.get("in_portfolio", True):
        pos += t.s(" Current target weights: {e} equal weight, {i} inverse volatility, {m} minimum variance.",
                   e="port.{S}.weight_equal_pct", i="port.{S}.weight_inverse_vol_pct",
                   m="port.{S}.weight_min_var_pct") or ""
        pos += t.s(" At its inverse-volatility weight, the cap starts to bind once the fund exceeds {a}.",
                   a="port.{S}.max_aum_before_cap_inverse_vol_usd") or ""
        binding = [k for k, v in (reg.labels.get("first_binding_name") or {}).items() if v == sym]
        if binding:
            names = {"equal": "equal-weight", "inverse_vol": "inverse-volatility", "min_var": "minimum-variance"}
            pos += (" It is the first name to hit its cap in the " + " and ".join(names.get(b, b) for b in binding)
                    + (" schemes" if len(binding) > 1 else " scheme")
                    + ", so it sets the capacity of the whole portfolio there.")
        add([pos])
    else:
        add(["This token is not in the portfolio analysis."])
    add([""])

    # ------------------------------------------------------------ commentary slot
    add(["## Analyst commentary", "", "{{COMMENTARY}}", ""])

    # ------------------------------------------------------------ limits
    add(["## What the data can't tell us", "",
         "- **Intent and execution.** Roadmaps, team quality, governance dynamics and competitive threats are "
         "outside any of the desk's data sources.",
         f"- **The whole market's liquidity.** Depth comes from {venues} only, in one snapshot. Offshore exchanges "
         "and on-chain pools usually hold more depth, and depth in a stress event can be a fraction of a "
         "calm-day snapshot.",
         "- **Whether DefiLlama's classification is current.** Fee, revenue and holders-revenue labels are "
         "DefiLlama's methodology and can lag a governance change. Verify on-chain before relying on them.",
         "- **Causes.** A change in fees or holder revenue is measured, not explained."])
    if lab.get("holders_revenue_reported") is False:
        add(["- **How much value reaches holders.** Holders revenue is not reported for this token."])
    if lab.get("slippage_na"):
        add(["- **Execution cost at size.** Larger orders exceed the visible book, and the desk does not "
             "extrapolate beyond it."])
    if not lab.get("in_portfolio"):
        add(["- **Portfolio fit.** The token lacked a complete price history for the portfolio test."])
    add([""])
    text = "\n".join(x for x in out if x is not None)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if re.search(r"\bNone\b", text):
        raise RenderError(f"{sym}: template contains 'None' (a missing fact leaked into a sentence)")
    return text


def load_commentary(sym: str, reg: Registry, directory: Path = COMMENTARY_DIR) -> str:
    """Commentary is included only if it was written against *this* registry."""
    f = directory / f"{sym}.md"
    if not f.exists():
        return ("*No commentary yet. This memo is the deterministic draft; narrative commentary is added by the "
                "memo agent (agents/MEMO_AGENT.md) and must pass the verifier before it is committed.*")
    text = f.read_text(encoding="utf-8")
    m = COMMENTARY_HEADER_RE.match(text)
    if not m or m.group("symbol") != sym:
        raise RenderError(f"{f}: missing or wrong '<!-- commentary symbol={sym} facts_sha256=... -->' header")
    if m.group("sha") != reg.sha256:
        return ("*Commentary withheld: it was written against a previous facts registry, so its reading of the "
                "numbers may no longer hold. The memo agent must redraft it against the current data.*")
    body = text[m.end():].strip()
    if SOURCES_HEADING in body:
        body = body[: body.index(SOURCES_HEADING)].rstrip()
    body = re.sub(r"(?m)^\[\^[^\]]+\]:.*$\n?", "", body).strip()   # renderer rebuilds all footnotes
    return ("*Drafted by the memo agent and gated by `desk.verify`: every number below is checked against "
            "the registry; the reasoning is not.*\n\n" + body)


def build_memo(sym: str, reg: Registry, commentary_dir: Path = COMMENTARY_DIR) -> str:
    tpl = build_template(sym, reg).replace("{{COMMENTARY}}", load_commentary(sym, reg, commentary_dir))
    return render(tpl, reg, symbol=sym)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render deterministic token memos from facts.json.")
    ap.add_argument("--facts", default=str(REPORTS_DIR / "facts.json"))
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--out", default=str(MEMO_DIR))
    ap.add_argument("--commentary", default=str(COMMENTARY_DIR))
    ap.add_argument("--render", metavar="FILE", help="render {{fact:id}} tokens in FILE (writes FILE, or --render-out)")
    ap.add_argument("--render-out", metavar="FILE")
    args = ap.parse_args(argv)
    reg = load_registry(args.facts)

    if args.render:
        src = Path(args.render)
        text = render(src.read_text(encoding="utf-8"), reg)
        Path(args.render_out or src).write_text(text, encoding="utf-8")
        print(f"rendered {src} -> {args.render_out or src}")
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    syms = args.symbols or sorted(reg.labels["tokens"])
    for old in out.glob("*.md"):
        if not args.symbols:
            old.unlink()          # a token that left the universe must not leave a stale memo behind
    for sym in syms:
        (out / f"{sym}.md").write_text(build_memo(sym, reg, Path(args.commentary)), encoding="utf-8")
    print(f"memo: wrote {len(syms)} memos to {out} (facts {reg.sha256[:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
