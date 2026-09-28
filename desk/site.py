"""Build the static research site from reports/ (no JavaScript, no framework).

    python -m desk.site               # writes site/
    python -m desk.site --out /tmp/x  # somewhere else

Pages:
    index.html           token table (every value read from facts.json), verification status, charts
    memos/<SYM>.html     each memo, with numbered source footnotes
    methodology.html     ARCHITECTURE.md, rendered
    track-record.html    every dated snapshot in reports/history/, re-verified at build time
    facts.json           the current registry, for download

The site shows only what is already in reports/: it computes nothing new, so
there is no second place where a number could be produced.
"""
from __future__ import annotations

import argparse
import html
import shutil
import sys
from pathlib import Path

from desk.config import REPORTS_DIR, ROOT
from desk.facts import Registry, load_registry
from desk.memo import format_value
from desk.mdhtml import convert
from desk.verify import verify_text

SITE_TITLE = "Onchain Research Desk"
CHARTS = [("liquidity_slippage.png", "Cost of a market order by size, Coinbase + Kraken consolidated book"),
          ("value_accrual.png", "How much of each protocol's fees reaches token holders"),
          ("capacity.png", "How far the liquidity cap pushes each portfolio off target as AUM grows"),
          ("backtest.png", "Walk-forward backtest, out-of-sample only")]

CSS = """\
:root {
  --bg: #fbfbfa; --surface: #ffffff; --fg: #1b1b1a; --muted: #5f5e5a; --border: #e3e2de;
  --accent: #1f5fae; --accent-soft: #e8f0fa; --ok: #1d7a4f; --ok-soft: #e6f4ec; --bad: #a3321f; --bad-soft: #fbeae6;
  --flag: #8a5a00; --flag-soft: #fdf3dd;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #121212; --surface: #1a1a19; --fg: #e9e8e4; --muted: #a3a29d; --border: #2e2d2b;
    --accent: #7fb0ec; --accent-soft: #1c2a3b; --ok: #6fcf9c; --ok-soft: #16291f; --bad: #f0907c; --bad-soft: #33201b;
    --flag: #e9bf64; --flag-soft: #2e2615;
  }
  img.chart { filter: brightness(0.92); }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, "Helvetica Neue", Arial, sans-serif; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 0 16px; }
header.site { border-bottom: 1px solid var(--border); background: var(--surface); }
header.site .wrap { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px 24px; padding-top: 14px; padding-bottom: 14px; }
.brand { font-weight: 650; color: var(--fg); text-decoration: none; letter-spacing: -0.01em; }
nav a { color: var(--muted); text-decoration: none; margin-right: 18px; font-size: 15px; }
nav a:hover, nav a[aria-current] { color: var(--accent); }
main { padding-top: 28px; padding-bottom: 48px; }
h1 { font-size: 30px; line-height: 1.2; letter-spacing: -0.02em; margin: 0 0 8px; }
h2 { font-size: 21px; margin: 36px 0 10px; letter-spacing: -0.01em; }
h3 { font-size: 17px; margin: 24px 0 8px; }
p, li { max-width: 72ch; }
a { color: var(--accent); }
.lede { color: var(--muted); max-width: 72ch; margin-top: 0; }
code { font: 13.5px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background: var(--accent-soft); padding: 1px 4px; border-radius: 4px; overflow-wrap: anywhere; }
pre { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; overflow-x: auto; }
pre code { background: none; padding: 0; }
.table-wrap { overflow-x: auto; margin: 12px 0 20px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }
table { border-collapse: collapse; width: 100%; font-size: 14.5px; }
th, td { padding: 8px 12px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }
th { font-weight: 600; color: var(--muted); font-size: 13px; white-space: nowrap; }
tbody tr:last-child td { border-bottom: 0; }
td:first-child { white-space: nowrap; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
th.num { text-align: right; }
.status { border-radius: 8px; padding: 12px 16px; margin: 18px 0 8px; font-size: 15px; }
.status.ok { background: var(--ok-soft); color: var(--ok); }
.status.bad { background: var(--bad-soft); color: var(--bad); }
.tag { display: inline-block; font-size: 12px; line-height: 1.4; padding: 1px 7px; border-radius: 10px; background: var(--flag-soft); color: var(--flag); }
td .tag { min-width: 9em; }
.muted { color: var(--muted); }
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }
figure { margin: 0; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
figure img { width: 100%; height: auto; display: block; }
figcaption { font-size: 13.5px; color: var(--muted); margin-top: 8px; }
sup.fn a { text-decoration: none; font-size: 11px; padding: 0 1px; }
section.footnotes { border-top: 1px solid var(--border); margin-top: 16px; padding-top: 8px; font-size: 13px; color: var(--muted); }
section.footnotes li { max-width: none; margin-bottom: 4px; overflow-wrap: anywhere; }
section.footnotes li:target { background: var(--accent-soft); }
blockquote { margin: 12px 0; padding: 4px 16px; border-left: 3px solid var(--border); color: var(--muted); }
footer { border-top: 1px solid var(--border); padding-top: 16px; padding-bottom: 32px; font-size: 13.5px; color: var(--muted); }
"""


def _page(title: str, body: str, root: str, current: str = "") -> str:
    def nav(href, label):
        cur = ' aria-current="page"' if href == current else ""
        return f'<a href="{root}{href}"{cur}>{label}</a>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="{root}assets/style.css">
</head>
<body>
<header class="site"><div class="wrap">
<a class="brand" href="{root}index.html">{SITE_TITLE}</a>
<nav>{nav("index.html", "Tokens")}{nav("methodology.html", "Methodology")}{nav("track-record.html", "Track record")}</nav>
</div></header>
<main class="wrap">
{body}
</main>
<footer class="wrap">Descriptive research generated from public APIs, not investment advice. Every number on a memo
page is checked against <a href="{root}facts.json">facts.json</a> by <code>desk.verify</code> before publication.</footer>
</body>
</html>
"""


def _v(reg: Registry, fid: str) -> str:
    if fid not in reg:
        return '<span class="muted">n/a</span>'
    f = reg[fid]
    return f'<span title="{html.escape(fid)}">{html.escape(format_value(f.value, f.unit))}</span>'


def _short_flag(flag: str) -> str:
    if "not reported" in flag:
        return "holder revenue not reported"
    if "ZERO" in flag:
        return "~zero holder accrual"
    return flag


def _verify_dir(reg: Registry, memo_dir: Path) -> tuple[int, int, int, int]:
    memos = sorted(memo_dir.glob("*.md"))
    res = [verify_text(m.read_text(encoding="utf-8"), reg, path=m.name) for m in memos]
    return sum(r.ok for r in res), len(res), sum(r.n_verified for r in res), sum(r.n_claims for r in res)


def _status(ok_memos, n_memos, ok_claims, n_claims, sha) -> str:
    good = ok_memos == n_memos and n_memos > 0
    return (f'<div class="status {"ok" if good else "bad"}">'
            f'{"Verified" if good else "Verification FAILED"}: {ok_memos} of {n_memos} memos pass; '
            f'{ok_claims} of {n_claims} numeric claims match facts.json <code>{sha[:12]}</code>.</div>')


def _memo_page(md_path: Path, root: str, title_suffix: str = "") -> str:
    body, _ = convert(md_path.read_text(encoding="utf-8"),
                      link_map=lambda u: u)
    return _page(f"{md_path.stem} memo{title_suffix} | {SITE_TITLE}", body, root)


def build(out: Path, reports: Path = REPORTS_DIR) -> Path:
    reg = load_registry(reports / "facts.json")
    if out.exists():
        shutil.rmtree(out)
    (out / "assets" / "charts").mkdir(parents=True)
    (out / "memos").mkdir()
    (out / "assets" / "style.css").write_text(CSS, encoding="utf-8")
    shutil.copy2(reports / "facts.json", out / "facts.json")
    (out / ".nojekyll").write_text("")

    # ---- memos
    memo_dir = reports / "memos"
    for m in sorted(memo_dir.glob("*.md")):
        (out / "memos" / f"{m.stem}.html").write_text(_memo_page(m, "../"), encoding="utf-8")

    # ---- index
    ok_m, n_m, ok_c, n_c = _verify_dir(reg, memo_dir)
    as_of = (reg.labels.get("market_as_of") or "").replace("T", " ").replace("Z", " UTC")
    toks = reg.labels["tokens"]
    order = sorted(toks, key=lambda s: -(reg[f"mkt.{s}.mcap_usd"].value if f"mkt.{s}.mcap_usd" in reg else 0))
    rows = []
    for s in order:
        lab = toks[s]
        flag = lab.get("accrual_flag") or ""
        memo_link = f'<a href="memos/{s}.html">{s}</a>' if (memo_dir / f"{s}.md").exists() else s
        rows.append(
            f"<tr><td>{memo_link}</td><td>{html.escape(lab.get('kind') or '')}</td>"
            f'<td class="num">{_v(reg, f"mkt.{s}.price_usd")}</td>'
            f'<td class="num">{_v(reg, f"mkt.{s}.mcap_usd")}</td>'
            f'<td class="num">{_v(reg, f"fund.{s}.fees_365d_usd")}</td>'
            f'<td class="num">{_v(reg, f"fund.{s}.holder_share_365d_pct")}</td>'
            f'<td class="num">{_v(reg, f"liq.{s}.slip_1m_bps")}</td>'
            f'<td class="num">{_v(reg, f"liq.{s}.adv_30d_usd")}</td>'
            f"<td>{f'<span class=tag>{html.escape(_short_flag(flag))}</span>' if flag else ''}</td></tr>")
    charts = []
    for fname, cap in CHARTS:
        if (reports / fname).exists():
            shutil.copy2(reports / fname, out / "assets" / "charts" / fname)
            charts.append(f'<figure><img class="chart" src="assets/charts/{fname}" alt="{html.escape(cap)}" '
                          f'loading="lazy"><figcaption>{html.escape(cap)}</figcaption></figure>')
    index = f"""<h1>Token research memos</h1>
<p class="lede">Liquidity, value accrual and portfolio capacity for {len(toks)} tokens, from free public APIs.
Market data as of {html.escape(as_of)}. Each memo is generated from a registry of facts; an LLM may add
commentary, but a deterministic verifier checks every number against the registry before anything is published.</p>
{_status(ok_m, n_m, ok_c, n_c, reg.sha256)}
<h2>Tokens</h2>
<div class="table-wrap"><table>
<thead><tr><th>Token</th><th>Segment</th><th class="num">Price</th><th class="num">Circ. market cap</th>
<th class="num">Fees, trailing year</th><th class="num">Holder share of fees</th><th class="num">Cost of $1M order</th>
<th class="num">Median daily volume</th><th>Flag</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody></table></div>
<p class="muted">n/a means the data does not exist (for example, the visible order book cannot fill a $1M order),
never that a value was estimated. Hover a value to see its fact id.</p>
<h2>Charts</h2>
<div class="charts">
{chr(10).join(charts)}
</div>
"""
    (out / "index.html").write_text(_page(SITE_TITLE, index, "", "index.html"), encoding="utf-8")

    # ---- methodology
    parts = []
    for doc in ("ARCHITECTURE.md", "agents/README.md"):
        p = ROOT / doc
        if p.exists():
            body, _ = convert(p.read_text(encoding="utf-8"), link_map=_doc_link)
            parts.append(body)
    meth = ('<p class="lede">How the desk produces its numbers and why each design choice was made. '
            'Rendered from <code>ARCHITECTURE.md</code> and <code>agents/README.md</code>.</p>\n'
            + "\n<hr>\n".join(parts))
    (out / "methodology.html").write_text(_page(f"Methodology | {SITE_TITLE}", meth, "", "methodology.html"),
                                          encoding="utf-8")

    # ---- track record
    hist = reports / "history"
    snaps = sorted([d for d in hist.iterdir() if d.is_dir() and (d / "facts.json").exists()],
                   reverse=True) if hist.exists() else []
    trows = []
    for d in snaps:
        hreg = load_registry(d / "facts.json")
        dest = out / "history" / d.name
        dest.mkdir(parents=True)
        shutil.copy2(d / "facts.json", dest / "facts.json")
        links = []
        for m in sorted((d / "memos").glob("*.md")):
            (dest / f"{m.stem}.html").write_text(_memo_page(m, "../../", f" ({d.name})"), encoding="utf-8")
            links.append(f'<a href="history/{d.name}/{m.stem}.html">{m.stem}</a>')
        ok_m, n_m, ok_c, n_c = _verify_dir(hreg, d / "memos")
        status = (f'<span class="tag" style="background:var(--ok-soft);color:var(--ok)">verified</span>'
                  if ok_m == n_m and n_m else '<span class="tag">verification failed</span>')
        trows.append(f"<tr><td>{d.name}</td><td>{html.escape(hreg.meta.get('data_as_of') or '')}</td>"
                     f'<td class="num">{len(hreg.facts)}</td><td class="num">{ok_c} / {n_c}</td><td>{status}</td>'
                     f'<td><a href="history/{d.name}/facts.json">facts.json</a></td>'
                     f"<td>{' '.join(links)}</td></tr>")
    track = f"""<h1>Track record</h1>
<p class="lede">Every weekly run is archived with its facts registry and memos, unchanged. A past claim can be
checked against the exact data that produced it: each snapshot is re-verified against its own
<code>facts.json</code> when this site is built.</p>
<div class="table-wrap"><table>
<thead><tr><th>Snapshot</th><th>Data as of</th><th class="num">Facts</th><th class="num">Claims verified</th>
<th>Status</th><th>Registry</th><th>Memos</th></tr></thead>
<tbody>
{chr(10).join(trows) if trows else '<tr><td colspan="7" class="muted">No snapshots archived yet.</td></tr>'}
</tbody></table></div>
"""
    (out / "track-record.html").write_text(_page(f"Track record | {SITE_TITLE}", track, "", "track-record.html"),
                                           encoding="utf-8")
    return out


def _doc_link(url: str) -> str:
    if url.startswith(("http://", "https://", "#")):
        return url
    if url.endswith("ARCHITECTURE.md") or url.endswith("agents/README.md"):
        return "methodology.html"
    return "methodology.html"      # other repo files are not part of the site


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the static research site.")
    ap.add_argument("--out", default=str(ROOT / "site"))
    args = ap.parse_args(argv)
    out = build(Path(args.out))
    n = sum(1 for _ in out.rglob("*.html"))
    print(f"site: {n} HTML pages -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
