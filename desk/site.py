"""Build the static research site from reports/ (no JavaScript, no framework).

    python -m desk.site               # writes site/
    python -m desk.site --out /tmp/x  # somewhere else

Pages:
    index.html           a research note: findings, four figures, the token table
    memos/<SYM>.html     each memo, with numbered source footnotes
    methodology.html     ARCHITECTURE.md, rendered
    track-record.html    every dated snapshot in reports/history/, re-verified at build time
    facts.json           the current registry, for download

The site shows only what is already in reports/: every value it prints is read
from facts.json and formatted by the same function the memos use, so there is
no second place where a number could be produced. The figures are inline SVG
drawn from those same facts (not the matplotlib PNGs in reports/), so they use
the page's type and follow its light/dark theme.
"""
from __future__ import annotations

import argparse
import html
import math
import shutil
import sys
from pathlib import Path

from desk.config import REPORTS_DIR, ROOT
from desk.facts import Registry, load_registry
from desk.memo import format_value
from desk.mdhtml import convert
from desk.verify import verify_text

SITE_TITLE = "Onchain Research Desk"
AUTHOR_URL = "https://elakumuk.github.io/"
REPO_URL = "https://github.com/elakumuk/onchain-research-desk"
FONTS = ("https://fonts.googleapis.com/css2?family=Bodoni+Moda:ital,opsz,wght@0,6..96,400;"
         "0,6..96,500;1,6..96,400&family=IBM+Plex+Mono:wght@400;500&family=Karla:ital,wght@0,400;"
         "0,500;0,700;1,400&display=swap")

CSS = """\
/* Tokens match elakumuk.github.io: charcoal on paper (light), paper on charcoal (dark). */
:root {
  --paper:#E6E3DC; --surface:#EFEDE8; --surface-2:#DEDAD1;
  --ink:#16151A; --ink-soft:#3A3840; --muted:#6B6770;
  --rule:#C9C5BC; --rule-soft:#D6D2CA; --grid:#D3CFC6;
  --accent:#2D3E5E; --accent-dim:#5C6B87;
  --s1:#2E5FA8; --s2:#BE4B1E; --s3:#7E3D96;
  --ok:#2F6B43; --bad:#A8341F; --flag:#8A5A12;
  --f-display:"Bodoni Moda","Didot","Times New Roman",serif;
  --f-body:"Karla","Helvetica Neue",Arial,sans-serif;
  --f-mono:"IBM Plex Mono","SFMono-Regular",Menlo,monospace;
  --measure:64ch;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper:#131317; --surface:#1A1A1F; --surface-2:#212127;
    --ink:#E8E5DE; --ink-soft:#C6C2BC; --muted:#8E8A93;
    --rule:#33323A; --rule-soft:#26262C; --grid:#2B2A32;
    --accent:#94ADDC; --accent-dim:#6C82AC;
    --s1:#5D96DE; --s2:#D46A3E; --s3:#A87BE0;
    --ok:#63B37F; --bad:#E0745A; --flag:#D9A857;
  }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--f-body);
  font-size:1rem;line-height:1.62;-webkit-font-smoothing:antialiased}
/* paper tooth, as on the portfolio site */
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:90;opacity:.35;
  mix-blend-mode:overlay;background-image:url("data:image/svg+xml;utf8,\\
<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'>\\
<filter id='g'><feTurbulence type='fractalNoise' baseFrequency='0.82' numOctaves='3' stitchTiles='stitch'/>\\
<feColorMatrix type='saturate' values='0'/></filter><rect width='160' height='160' filter='url(%23g)'/></svg>")}
a{color:inherit;text-decoration-color:var(--rule);text-underline-offset:3px}
a:hover{color:var(--accent);text-decoration-color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.wrap{max-width:1120px;margin:0 auto;padding:0 clamp(16px,4vw,48px)}
.eyebrow{font-family:var(--f-mono);font-size:.6875rem;letter-spacing:.13em;text-transform:uppercase;color:var(--muted)}
code{font-family:var(--f-mono);font-size:.84em;overflow-wrap:anywhere}

/* masthead */
header.mast{border-bottom:1px solid var(--rule)}
header.mast .wrap{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;
  gap:10px 28px;padding-top:22px;padding-bottom:18px}
.brand{font-family:var(--f-display);font-size:1.35rem;text-decoration:none;letter-spacing:.01em}
nav{display:flex;flex-wrap:wrap;gap:6px 22px}
nav a{font-family:var(--f-mono);font-size:.6875rem;letter-spacing:.13em;text-transform:uppercase;
  color:var(--muted);text-decoration:none;padding-bottom:3px;border-bottom:1px solid transparent}
nav a:hover{color:var(--ink)}
nav a[aria-current]{color:var(--ink);border-bottom-color:var(--ink)}
main{padding-top:clamp(28px,5vw,64px);padding-bottom:72px}

/* note */
.hero{max-width:52rem}
.hero h1{font-family:var(--f-display);font-weight:400;font-size:clamp(2.1rem,5.2vw,3.6rem);
  line-height:1.06;letter-spacing:-.01em;margin:14px 0 22px;text-wrap:balance}
.hero h1 em{color:var(--accent)}
.deck{font-size:1.125rem;color:var(--ink-soft);max-width:var(--measure);margin:0}
.verified{display:flex;gap:10px;align-items:baseline;margin:26px 0 0;font-family:var(--f-mono);
  font-size:.75rem;letter-spacing:.02em;color:var(--muted);max-width:var(--measure)}
.verified b{color:var(--ok);font-weight:500}
.verified.bad b{color:var(--bad)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));margin:48px 0 8px;
  border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.tile{padding:20px 20px 22px 0}
.tile+.tile{padding-left:20px;border-left:1px solid var(--rule)}
.tile .n{display:block;font-family:var(--f-display);font-size:2.3rem;line-height:1.05}
.tile .n small{font-size:.5em;color:var(--muted);margin-left:2px}
.tile .l{display:block;margin-top:8px;font-size:.875rem;color:var(--ink-soft);line-height:1.45}
@media (max-width:700px){.tile+.tile{padding-left:0;border-left:0;border-top:1px solid var(--rule)}}

section.part{margin-top:clamp(56px,8vw,96px);display:grid;grid-template-columns:13rem minmax(0,1fr);gap:0 40px}
section.part>.side{padding-top:6px}
section.part>.side .num{font-family:var(--f-mono);font-size:.75rem;color:var(--accent)}
section.part>.side .eyebrow{display:block;margin-top:6px}
section.part h2{font-family:var(--f-display);font-weight:400;font-size:clamp(1.6rem,3vw,2.15rem);
  line-height:1.15;margin:0 0 14px;text-wrap:balance}
section.part p{max-width:var(--measure);margin:0 0 14px;color:var(--ink-soft)}
@media (max-width:820px){section.part{grid-template-columns:minmax(0,1fr)}section.part>.side{margin-bottom:10px}}

figure.fig{margin:26px 0 0;border:1px solid var(--rule);background:var(--surface)}
.fig__bar{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;
  padding:12px 16px;border-bottom:1px solid var(--rule)}
.fig__body{padding:14px 16px 10px;overflow-x:auto}
.fig__body svg{display:block;width:100%;height:auto;min-width:560px}
figure.fig figcaption{padding:10px 16px 14px;border-top:1px solid var(--rule-soft);font-size:.8125rem;
  color:var(--muted);max-width:none}
.key{display:flex;flex-wrap:wrap;gap:4px 16px;font-family:var(--f-mono);font-size:.6875rem;color:var(--muted)}
.key i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px}
svg text{font-family:var(--f-mono);font-size:11px;fill:var(--muted)}
svg text.lab{fill:var(--ink-soft)}
svg text.val{fill:var(--ink)}
svg text.inv{fill:var(--paper)}
svg .grid{stroke:var(--grid);stroke-width:1}
svg .axis{stroke:var(--rule);stroke-width:1}
svg .zero{stroke:var(--ink-soft);stroke-width:1}
svg .span{stroke:var(--rule);stroke-width:2}
svg .c1{fill:var(--s1)} svg .c2{fill:var(--s2)} svg .c3{fill:var(--s3)}
svg .l1{stroke:var(--s1)} svg .l2{stroke:var(--s2)} svg .l3{stroke:var(--s3)}
svg .bar{fill:var(--s1)}
svg .bar.dim{fill:var(--accent-dim);opacity:.5}
svg .whisker{stroke:var(--ink-soft);stroke-width:1.5}
svg .dot{fill:var(--ink)}
svg .ring{stroke:var(--surface);stroke-width:2}
svg .whisker.l1{stroke:var(--s1)} svg .whisker.l2{stroke:var(--s2)} svg .whisker.l3{stroke:var(--s3)}
svg .hit{fill:transparent}
svg .band{fill:var(--rule-soft);opacity:.55}
svg .rowrule{stroke:var(--rule-soft);stroke-width:1}
svg text.note{font-size:10px;fill:var(--muted)}
svg text.strong{fill:var(--ink);font-weight:500}
svg .dot.em{fill:var(--s2)}

/* tables */
.table-wrap{overflow-x:auto;margin:22px 0 10px;border-top:1px solid var(--ink)}
table{border-collapse:collapse;width:100%;font-size:.875rem}
th,td{padding:10px 14px 10px 0;border-bottom:1px solid var(--rule);text-align:left;vertical-align:baseline}
th{font-family:var(--f-mono);font-weight:400;font-size:.6875rem;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);white-space:nowrap}
td:first-child{white-space:nowrap}
td.num,th.num{text-align:right}
td.num{font-family:var(--f-mono);font-size:.8125rem;white-space:nowrap}
td a{font-weight:500;text-decoration:none;border-bottom:1px solid var(--rule)}
td a:hover{border-bottom-color:var(--accent)}
.seg{font-family:var(--f-mono);font-size:.6875rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.flag{font-family:var(--f-mono);font-size:.6875rem;color:var(--flag);white-space:nowrap}
.flag::before{content:"\\25CF";margin-right:6px;font-size:.6rem}
.flag.nr{color:var(--muted)}
.flag.nr::before{content:"\\25CB"}
.muted{color:var(--muted)}
.note{font-size:.8125rem;color:var(--muted);max-width:var(--measure)}

/* long-form pages: memos, methodology */
.doc{max-width:46rem}
.doc .back{display:inline-block;margin-bottom:26px;font-family:var(--f-mono);font-size:.6875rem;
  letter-spacing:.13em;text-transform:uppercase;color:var(--muted);text-decoration:none}
.doc .back:hover{color:var(--ink)}
.doc h1{font-family:var(--f-display);font-weight:400;font-size:clamp(2rem,4.5vw,2.9rem);line-height:1.1;
  margin:0 0 18px;text-wrap:balance}
.doc h2{font-family:var(--f-display);font-weight:400;font-size:1.55rem;line-height:1.2;
  margin:48px 0 12px;padding-top:18px;border-top:1px solid var(--rule)}
.doc h3{font-size:1.02rem;font-weight:700;margin:28px 0 8px}
.doc p,.doc li{color:var(--ink-soft)}
.doc p{margin:0 0 14px}
.doc ul,.doc ol{padding-left:1.25rem}
.doc em{color:var(--muted)}
.doc strong{color:var(--ink);font-weight:700}
.doc pre{background:var(--surface);border:1px solid var(--rule);padding:14px 16px;overflow-x:auto;
  font-size:.8125rem;line-height:1.5}
.doc pre code{font-size:inherit}
.doc blockquote{margin:16px 0;padding:2px 0 2px 18px;border-left:2px solid var(--accent);color:var(--ink-soft)}
.doc hr{border:0;border-top:1px solid var(--rule);margin:48px 0}
.doc td,.doc th{font-size:.875rem}
.doc td{font-family:var(--f-body)}
sup.fn a{font-family:var(--f-mono);font-size:.625rem;text-decoration:none;color:var(--accent-dim);
  border:0;padding:0 1px}
sup.fn a:hover{color:var(--accent)}
section.footnotes{margin-top:48px;padding-top:14px;border-top:1px solid var(--ink);font-family:var(--f-mono);
  font-size:.6875rem;line-height:1.7;color:var(--muted)}
section.footnotes li{margin-bottom:4px;overflow-wrap:anywhere}
section.footnotes li:target{background:var(--surface-2);color:var(--ink)}

footer.foot{border-top:1px solid var(--rule)}
footer.foot .wrap{display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px 28px;
  padding-top:22px;padding-bottom:36px;font-size:.8125rem;color:var(--muted)}
footer.foot p{margin:0;max-width:var(--measure)}
footer.foot .links{font-family:var(--f-mono);font-size:.6875rem;letter-spacing:.1em;text-transform:uppercase;
  display:flex;gap:18px}
footer.foot .links a{text-decoration:none}
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}">
<link rel="stylesheet" href="{root}assets/style.css">
</head>
<body>
<header class="mast"><div class="wrap">
<a class="brand" href="{root}index.html">{SITE_TITLE}</a>
<nav>{nav("index.html", "Research note")}{nav("methodology.html", "Methodology")}{nav("track-record.html", "Track record")}</nav>
</div></header>
<main class="wrap">
{body}
</main>
<footer class="foot"><div class="wrap">
<p>Descriptive research generated from public APIs, not investment advice. Every number on a memo page is
checked against <a href="{root}facts.json">facts.json</a> by <code>desk.verify</code> before publication.</p>
<div class="links"><a href="{REPO_URL}">Code</a><a href="{AUTHOR_URL}">Ela Kumuk</a></div>
</div></footer>
</body>
</html>
"""


# ------------------------------------------------------------------ values
def _v(reg: Registry, fid: str) -> str:
    if fid not in reg:
        return '<span class="muted">n/a</span>'
    f = reg[fid]
    return f'<span title="{html.escape(fid)}">{html.escape(format_value(f.value, f.unit))}</span>'


def _fmt(reg: Registry, fid: str) -> str:
    f = reg[fid]
    return html.escape(format_value(f.value, f.unit))


def _val(reg: Registry, fid: str):
    return reg[fid].value if fid in reg else None


def _flag_html(flag: str) -> str:
    if not flag:
        return ""
    if "not reported" in flag:
        return '<span class="flag nr">holder revenue not reported</span>'
    if "ZERO" in flag:
        return '<span class="flag">~zero holder accrual</span>'
    return f'<span class="flag">{html.escape(flag)}</span>'


def _verify_dir(reg: Registry, memo_dir: Path) -> tuple[int, int, int, int]:
    memos = sorted(memo_dir.glob("*.md"))
    res = [verify_text(m.read_text(encoding="utf-8"), reg, path=m.name) for m in memos]
    return sum(r.ok for r in res), len(res), sum(r.n_verified for r in res), sum(r.n_claims for r in res)


def _status(ok_memos, n_memos, ok_claims, n_claims, sha) -> str:
    good = ok_memos == n_memos and n_memos > 0
    return (f'<p class="verified{"" if good else " bad"}"><b>{"&#10003;" if good else "&#10007;"}</b>'
            f'<span>{"Verified" if good else "Verification FAILED"}: {ok_memos} of {n_memos} memos pass; '
            f'{ok_claims} of {n_claims} numeric claims match facts.json <code>{sha[:12]}</code>.</span></p>')


def _memo_page(md_path: Path, root: str, title_suffix: str = "", back: str = "index.html") -> str:
    body, _ = convert(md_path.read_text(encoding="utf-8"), link_map=lambda u: u)
    doc = f'<article class="doc"><a class="back" href="{root}{back}">&larr; All tokens</a>\n{body}</article>'
    return _page(f"{md_path.stem} memo{title_suffix} | {SITE_TITLE}", doc, root)


# ------------------------------------------------------------------ figures (inline SVG from facts)
def _t(x, y, s, cls="", anchor="start", fid=None, dy=4):
    title = f"<title>{html.escape(fid)}</title>" if fid else ""
    c = f' class="{cls}"' if cls else ""
    return f'<text x="{x:.1f}" y="{y + dy:.1f}" text-anchor="{anchor}"{c}>{title}{html.escape(s)}</text>'


def _log_ticks(lo: float, hi: float):
    return [10 ** e for e in range(int(math.floor(math.log10(lo))), int(math.ceil(math.log10(hi))) + 1)]


def _tick_label(v: float) -> str:
    return f"{v:g}" if v < 1e4 else f"{v:.0e}"


def fig_slippage(reg: Registry, order: list[str]) -> str:
    sizes = [("100k", "c1"), ("1m", "c2"), ("10m", "c3")]
    rows = []
    for s in order:
        vals = {k: _val(reg, f"liq.{s}.slip_{k}_bps") for k, _ in sizes}
        rows.append((s, vals))
    rows.sort(key=lambda r: (r[1]["1m"] is None, r[1]["1m"] or 0, r[1]["100k"] or 0))
    allv = [v for _, vals in rows for v in vals.values() if v and v > 0]
    lo, hi = min(allv), max(allv)
    ticks = _log_ticks(lo, hi)
    L, R, T, rh, W = 70, 150, 26, 22, 720
    x0, x1 = L, W - R
    lx0, lx1 = math.log10(ticks[0]), math.log10(ticks[-1])
    X = lambda v: x0 + (math.log10(v) - lx0) / (lx1 - lx0) * (x1 - x0)
    H = T + rh * len(rows) + 30
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Estimated cost of a market order of $100k, $1M and $10M for each token, log scale, in basis points">']
    for tv in ticks:
        out.append(f'<line class="grid" x1="{X(tv):.1f}" y1="{T - 8}" x2="{X(tv):.1f}" y2="{H - 26}"/>')
        out.append(_t(X(tv), H - 12, _tick_label(tv), anchor="middle"))
    out.append(_t(x1 + 12, T - 16, "cannot fill", anchor="start"))
    for i, (s, vals) in enumerate(rows):
        y = T + rh * i + rh / 2
        out.append(_t(L - 12, y, s, "lab", "end"))
        present = [v for v in vals.values() if v]
        if len(present) > 1:
            out.append(f'<line class="span" x1="{X(min(present)):.1f}" y1="{y:.1f}" x2="{X(max(present)):.1f}" y2="{y:.1f}"/>')
        missing = []
        for k, c in sizes:
            v = vals[k]
            if v:
                out.append(f'<circle class="{c}" cx="{X(v):.1f}" cy="{y:.1f}" r="4.5"><title>liq.{s}.slip_{k}_bps = '
                           f'{_fmt(reg, f"liq.{s}.slip_{k}_bps")}</title></circle>')
            else:
                missing.append({"100k": "$100k", "1m": "$1M", "10m": "$10M"}[k])
        if missing:
            out.append(_t(x1 + 12, y, " / ".join(missing)))
    out.append("</svg>")
    return "".join(out)


def fig_accrual(reg: Registry, order: list[str]) -> str:
    rows = []
    for s in order:
        fees = _val(reg, f"fund.{s}.fees_365d_usd")
        if fees is None:
            continue
        rows.append((s, _val(reg, f"fund.{s}.holder_share_365d_pct")))
    rows.sort(key=lambda r: (r[1] is None, -(r[1] or 0)))
    L, R, T, rh, W = 70, 150, 12, 22, 720
    x0, x1 = L, W - R
    X = lambda p: x0 + p / 100 * (x1 - x0)
    H = T + rh * len(rows) + 30
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Share of each protocol\'s trailing-year fees that reached token holders">']
    for p in (0, 25, 50, 75, 100):
        out.append(f'<line class="grid" x1="{X(p):.1f}" y1="{T}" x2="{X(p):.1f}" y2="{H - 26}"/>')
        out.append(_t(X(p), H - 12, f"{p}%", anchor="middle"))
    for i, (s, share) in enumerate(rows):
        y = T + rh * i + rh / 2
        out.append(_t(L - 12, y, s, "lab", "end"))
        fees_id = f"fund.{s}.fees_365d_usd"
        if share is None:
            out.append(_t(X(0) + 6, y, "holder revenue not reported"))
        else:
            w = max(X(share) - X(0), 1.5)
            cls = "bar" if share >= 1 else "bar dim"
            out.append(f'<rect class="{cls}" x="{X(0):.1f}" y="{y - 6:.1f}" width="{w:.1f}" height="12">'
                       f'<title>fund.{s}.holder_share_365d_pct = {_fmt(reg, f"fund.{s}.holder_share_365d_pct")}</title></rect>')
            txt = format_value(reg[f"fund.{s}.holder_share_365d_pct"].value, "pct")
            fid = f"fund.{s}.holder_share_365d_pct"
            if share >= 80:
                out.append(_t(X(0) + w - 6, y, txt, "val inv", "end", fid=fid))
            else:
                out.append(_t(X(0) + w + 8, y, txt, "val", fid=fid))
        out.append(_t(x1 + 12, y, "fees " + format_value(reg[fees_id].value, "usd"), fid=fees_id))
    out.append("</svg>")
    return "".join(out)


SCHEMES = [("equal", "Equal weight", "l1", "c1"), ("inverse_vol", "Inverse vol", "l2", "c2"),
           ("min_var", "Min variance", "l3", "c3")]
AUMS = [("10m", "$10M"), ("100m", "$100M"), ("1b", "$1B")]


def fig_capacity(reg: Registry) -> str:
    W, H, L, R, T, B = 720, 300, 60, 150, 20, 40
    x0, x1, y0, y1 = L, W - R, H - B, T
    ymax = 20
    X = lambda i: x0 + i / (len(AUMS) - 1) * (x1 - x0)
    Y = lambda v: y0 - v / ymax * (y0 - y1)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Effective number of positions after the liquidity cap, by fund size">']
    for v in (0, 5, 10, 15, 20):
        out.append(f'<line class="grid" x1="{x0}" y1="{Y(v):.1f}" x2="{x1}" y2="{Y(v):.1f}"/>')
        out.append(_t(x0 - 12, Y(v), str(v), anchor="end"))
    for i, (_, lab) in enumerate(AUMS):
        out.append(_t(X(i), H - 14, lab, "lab", "middle"))
    ends = []
    for key, name, lc, cc in SCHEMES:
        pts = [(X(i), _val(reg, f"cap.{key}.aum_{a}.effective_n_capped")) for i, (a, _) in enumerate(AUMS)]
        if any(v is None for _, v in pts):
            continue
        d = " ".join(f"{'M' if j == 0 else 'L'}{x:.1f},{Y(v):.1f}" for j, (x, v) in enumerate(pts))
        out.append(f'<path class="{lc}" d="{d}" fill="none" stroke-width="2"/>')
        for i, (x, v) in enumerate(pts):
            fid = f"cap.{key}.aum_{AUMS[i][0]}.effective_n_capped"
            out.append(f'<circle class="{cc}" cx="{x:.1f}" cy="{Y(v):.1f}" r="4"><title>{fid} = {_fmt(reg, fid)}</title></circle>')
        ends.append([Y(pts[-1][1]), f"{name}  {_fmt(reg, f'cap.{key}.aum_1b.effective_n_capped')}"])
    ends.sort()
    for j in range(1, len(ends)):          # keep end labels at least 15px apart
        ends[j][0] = max(ends[j][0], ends[j - 1][0] + 15)
    for y, txt in ends:
        out.append(_t(x1 + 12, y, txt, "lab"))
    out.append("</svg>")
    return "".join(out)


STRATS = [("equal", "Equal weight"), ("inverse_vol", "Inverse vol"), ("min_var", "Min variance"),
          ("inverse_vol_capped_10m", "Inverse vol, capped at $10M"),
          ("inverse_vol_capped_100m", "Inverse vol, capped at $100M"),
          ("inverse_vol_capped_1b", "Inverse vol, capped at $1B"), ("btc_buy_hold", "BTC buy and hold")]


def fig_sharpe(reg: Registry) -> str:
    rows = [(k, n, _val(reg, f"bt.{k}.sharpe"), _val(reg, f"bt.{k}.sharpe_se")) for k, n in STRATS]
    rows = [r for r in rows if r[2] is not None and r[3] is not None]
    lo = math.floor(min(s - 1.96 * se for _, _, s, se in rows))
    hi = math.ceil(max(s + 1.96 * se for _, _, s, se in rows))
    W, L, R, T, rh = 720, 210, 130, 12, 26
    H = T + rh * len(rows) + 30
    x0, x1 = L, W - R
    X = lambda v: x0 + (v - lo) / (hi - lo) * (x1 - x0)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Sharpe ratio of each strategy with a 95 percent interval from its standard error">']
    for v in range(lo, hi + 1):
        out.append(f'<line class="{"zero" if v == 0 else "grid"}" x1="{X(v):.1f}" y1="{T}" x2="{X(v):.1f}" y2="{H - 26}"/>')
        out.append(_t(X(v), H - 12, str(v), anchor="middle"))
    for i, (k, name, s, se) in enumerate(rows):
        y = T + rh * i + rh / 2
        out.append(_t(L - 14, y, name, "lab", "end"))
        out.append(f'<line class="whisker" x1="{X(s - 1.96 * se):.1f}" y1="{y:.1f}" x2="{X(s + 1.96 * se):.1f}" y2="{y:.1f}"/>')
        out.append(f'<circle class="dot{" em" if k == "btc_buy_hold" else ""}" cx="{X(s):.1f}" cy="{y:.1f}" r="4.5">'
                   f'<title>bt.{k}.sharpe = {_fmt(reg, f"bt.{k}.sharpe")}</title></circle>')
        out.append(_t(x1 + 14, y, f"{_fmt(reg, f'bt.{k}.sharpe')} ± {_fmt(reg, f'bt.{k}.sharpe_se')}", "val"))
    out.append("</svg>")
    return "".join(out)


# ------------------------------------------------------------------ v2 long-run figures
LR_STRATS = [("inverse_vol", "Inverse vol", "s1"), ("inverse_vol_capped_1b", "Inverse vol, capped at $1B", "s2"),
             ("btc_buy_hold", "BTC buy and hold", "s3")]
LR_TABLE_STRATS = [("equal", "Equal weight"), ("inverse_vol", "Inverse vol"), ("min_var", "Min variance"),
                   ("inverse_vol_capped_10m", "Inverse vol, capped at $10M"),
                   ("inverse_vol_capped_100m", "Inverse vol, capped at $100M"),
                   ("inverse_vol_capped_1b", "Inverse vol, capped at $1B"), ("btc_buy_hold", "BTC buy and hold")]


def _month_date(tag: str):
    import datetime as _dt
    return _dt.date(int(tag[1:5]), int(tag[6:8]), 1)


def _time_axis(months, x0, x1):
    d0, d1 = _month_date(months[0]), _month_date(months[-1])
    span = (d1 - d0).days or 1
    return lambda tag: x0 + (_month_date(tag) - d0).days / span * (x1 - x0)


def _year_ticks(out, months, X, y_top, y_bot, H):
    for m in months:
        if m.endswith("_01"):
            yr = int(m[1:5])
            out.append(f'<line class="grid" x1="{X(m):.1f}" y1="{y_top}" x2="{X(m):.1f}" y2="{y_bot}"/>')
            if yr % 2 == 0:
                out.append(_t(X(m), H - 12, str(yr), anchor="middle"))


def _inflated_band(out, months, X, y_top, y_bot):
    """Shade the months before 2019: reported volume heavily inflated by wash trading."""
    pre = [m for m in months if m < "m2019_01"]
    if not pre:
        return
    x_end = X("m2019_01") if "m2019_01" in months else X(pre[-1])
    out.append(f'<rect class="band" x="{X(months[0]):.1f}" y="{y_top}" width="{x_end - X(months[0]):.1f}" '
               f'height="{y_bot - y_top}"/>')
    out.append(_t(X(months[0]), y_top - 9, "shaded: before 2019, reported volume heavily inflated", "note"))


def fig_lr_regimes(reg: Registry) -> str:
    lab = reg.labels.get("longrun") or {}
    rows = [("", "Full sample", f"{lab.get('sample_start', '')} to {lab.get('sample_end', '')}")]
    for slug, r in (lab.get("regimes") or {}).items():
        rows.append((slug, r["label"], ""))
    def fid(slug, k, what):
        return f"lr.bt.{k}.{what}" if not slug else f"lr.regime.{slug}.{k}.{what}"
    vals = [(slug, k, _val(reg, fid(slug, k, "sharpe")), _val(reg, fid(slug, k, "sharpe_se")))
            for slug, _, _ in rows for k, _, _ in LR_STRATS]
    vals = [v for v in vals if v[2] is not None and v[3] is not None]
    lo = math.floor(min(s - 1.96 * se for *_, s, se in vals))
    hi = math.ceil(max(s + 1.96 * se for *_, s, se in vals))
    W, L, R, T, rh = 720, 250, 24, 14, 46
    H = T + rh * len(rows) + 30
    x0, x1 = L, W - R
    X = lambda v: x0 + (v - lo) / (hi - lo) * (x1 - x0)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Sharpe ratio with 95 percent interval, full sample '
           f'and each market regime, for inverse volatility, inverse volatility capped at one billion dollars, and '
           f'bitcoin buy and hold">']
    step = 1 if hi - lo <= 8 else 2
    for v in range(math.ceil(lo / step) * step, hi + 1, step):     # multiples of step, so 0 is a tick
        out.append(f'<line class="{"zero" if v == 0 else "grid"}" x1="{X(v):.1f}" y1="{T}" x2="{X(v):.1f}" y2="{H - 26}"/>')
        out.append(_t(X(v), H - 12, str(v), anchor="middle"))
    for i, (slug, name, sub) in enumerate(rows):
        yc = T + rh * i + rh / 2
        if i:
            out.append(f'<line class="rowrule" x1="12" y1="{T + rh * i:.1f}" x2="{x1}" y2="{T + rh * i:.1f}"/>')
        out.append(_t(L - 14, yc - (6 if sub else 0), name, "lab" + (" strong" if not slug else ""), "end"))
        if sub:
            out.append(_t(L - 14, yc + 8, sub, "", "end"))
        for j, (k, _, cls) in enumerate(LR_STRATS):
            s_, se = _val(reg, fid(slug, k, "sharpe")), _val(reg, fid(slug, k, "sharpe_se"))
            if s_ is None or se is None:
                continue
            y = yc + (j - 1) * 11
            f_s, f_se = fid(slug, k, "sharpe"), fid(slug, k, "sharpe_se")
            out.append(f'<line class="whisker l{cls[-1]}" x1="{X(s_ - 1.96 * se):.1f}" y1="{y:.1f}" '
                       f'x2="{X(s_ + 1.96 * se):.1f}" y2="{y:.1f}"/>')
            out.append(f'<circle class="c{cls[-1]} ring" cx="{X(s_):.1f}" cy="{y:.1f}" r="4.5"><title>{f_s} = '
                       f'{_fmt(reg, f_s)}; {f_se} = {_fmt(reg, f_se)}</title></circle>')
    out.append("</svg>")
    return "".join(out)


def fig_lr_capacity(reg: Registry) -> str:
    months = (reg.labels.get("longrun") or {}).get("months") or []
    W, H, L, R, T, B = 720, 316, 52, 110, 32, 40
    x0, x1, y0, y1 = L, W - R, H - B, T
    X = _time_axis(months, x0, x1)
    Y = lambda v: y0 - v / 100 * (y0 - y1)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Share of the inverse volatility portfolio that the '
           f'liquidity cap forces elsewhere, each month since 2016, for funds of ten million, one hundred million '
           f'and one billion dollars">']
    _inflated_band(out, months, X, y1, y0)
    for v in (0, 25, 50, 75, 100):
        out.append(f'<line class="grid" x1="{x0}" y1="{Y(v):.1f}" x2="{x1}" y2="{Y(v):.1f}"/>')
        out.append(_t(x0 - 10, Y(v), f"{v}%", anchor="end"))
    _year_ticks(out, months, X, y1, y0, H)
    ends = []
    for (tag, name), cls in zip(AUMS, ("1", "2", "3")):
        pts = [(m, _val(reg, f"lr.cap.{m}.weight_moved_{tag}_pct")) for m in months]
        pts = [(m, v) for m, v in pts if v is not None]
        if not pts:
            continue
        d = " ".join(f"{'M' if j == 0 else 'L'}{X(m):.1f},{Y(v):.1f}" for j, (m, v) in enumerate(pts))
        out.append(f'<path class="l{cls}" d="{d}" fill="none" stroke-width="2"/>')
        for m, v in pts:          # invisible hover targets carrying the fact id
            f = f"lr.cap.{m}.weight_moved_{tag}_pct"
            out.append(f'<circle class="hit" cx="{X(m):.1f}" cy="{Y(v):.1f}" r="5"><title>{f} = {_fmt(reg, f)}</title></circle>')
        m, v = pts[-1]
        ends.append([Y(v), f"{name} fund", f"lr.cap.{m}.weight_moved_{tag}_pct"])
    ends.sort()
    for j in range(1, len(ends)):
        ends[j][0] = max(ends[j][0], ends[j - 1][0] + 14)
    for y, txt, f in ends:
        out.append(_t(x1 + 10, y, txt, "lab", fid=f))
    out.append("</svg>")
    return "".join(out)


def fig_lr_liquidity(reg: Registry) -> str:
    months = (reg.labels.get("longrun") or {}).get("months") or []
    series = [(sym, cls, [(m, _val(reg, f"lr.liq.{sym}.{m}.amihud_bps_per_1m")) for m in months])
              for sym, cls in (("BTC", "1"), ("ETH", "2"))]
    series = [(s, c, [(m, v) for m, v in pts if v is not None and v > 0]) for s, c, pts in series]
    allv = [v for _, _, pts in series for _, v in pts]
    if not allv:
        return ""
    ticks = _log_ticks(min(allv), max(allv))
    W, H, L, R, T, B = 720, 316, 60, 110, 32, 40
    x0, x1, y0, y1 = L, W - R, H - B, T
    X = _time_axis(months, x0, x1)
    lg0, lg1 = math.log10(ticks[0]), math.log10(ticks[-1])
    Y = lambda v: y0 - (math.log10(v) - lg0) / (lg1 - lg0) * (y0 - y1)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Trailing-year Amihud illiquidity of bitcoin and '
           f'ether, basis points of price move per one million dollars of reported volume, log scale">']
    _inflated_band(out, months, X, y1, y0)
    for tv in ticks:
        out.append(f'<line class="grid" x1="{x0}" y1="{Y(tv):.1f}" x2="{x1}" y2="{Y(tv):.1f}"/>')
        out.append(_t(x0 - 10, Y(tv), _tick_label(tv), anchor="end"))
    _year_ticks(out, months, X, y1, y0, H)
    ends = []
    for sym, cls, pts in series:
        if not pts:
            continue
        d = " ".join(f"{'M' if j == 0 else 'L'}{X(m):.1f},{Y(v):.1f}" for j, (m, v) in enumerate(pts))
        out.append(f'<path class="l{cls}" d="{d}" fill="none" stroke-width="2"/>')
        for m, v in pts:
            f = f"lr.liq.{sym}.{m}.amihud_bps_per_1m"
            out.append(f'<circle class="hit" cx="{X(m):.1f}" cy="{Y(v):.1f}" r="5"><title>{f} = {_fmt(reg, f)}</title></circle>')
        m, v = pts[-1]
        f = f"lr.liq.{sym}.{m}.amihud_bps_per_1m"
        ends.append([Y(v), f"{sym}  {_fmt(reg, f)}", f])
    ends.sort()
    for j in range(1, len(ends)):
        ends[j][0] = max(ends[j][0], ends[j - 1][0] + 14)
    for y, txt, f in ends:
        out.append(_t(x1 + 10, y, txt, "lab", fid=f))
    out.append("</svg>")
    return "".join(out)


def _figure(label: str, svg: str, caption: str, key: str = "") -> str:
    return (f'<figure class="fig"><div class="fig__bar"><span class="eyebrow">{label}</span>{key}</div>'
            f'<div class="fig__body">{svg}</div><figcaption>{caption}</figcaption></figure>')


def _key(items) -> str:
    return '<span class="key">' + "".join(
        f'<span><i style="background:var(--{c})"></i>{html.escape(t)}</span>' for c, t in items) + "</span>"


def _part(num: str, eyebrow: str, title: str, body: str) -> str:
    return (f'<section class="part"><div class="side"><span class="num">{num}</span>'
            f'<span class="eyebrow">{eyebrow}</span></div><div>'
            f"<h2>{title}</h2>{body}</div></section>")


# ------------------------------------------------------------------ build
def build(out: Path, reports: Path = REPORTS_DIR) -> Path:
    reg = load_registry(reports / "facts.json")
    if out.exists():
        shutil.rmtree(out)
    (out / "assets").mkdir(parents=True)
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

    tiles = []
    for fid, small, label in [
        ("cap.inverse_vol.aum_1b.names_at_cap_count", f"of {len(toks)}", "positions hit the liquidity cap in a $1B inverse-vol fund"),
        ("cap.inverse_vol.aum_1b.weight_moved_pct", "", "of that portfolio has to move to where the market can absorb it"),
        ("cap.inverse_vol.aum_1b.effective_n_capped", "positions",
         "effectively left at $1B, down from " + _v(reg, "cap.inverse_vol.aum_10m.effective_n_capped") + " at $10M"),
    ]:
        if fid in reg:
            tiles.append(f'<div class="tile"><span class="n">{_v(reg, fid)}'
                         f'{f"<small>{small}</small>" if small else ""}</span><span class="l">{label}</span></div>')
    tiles.append(f'<div class="tile"><span class="n">{n_c:,}<small>/ {n_c:,}</small></span>'
                 f'<span class="l">numbers in the memos traced to a source before publishing</span></div>')

    rows = []
    for s in order:
        lab = toks[s]
        memo_link = f'<a href="memos/{s}.html">{s}</a>' if (memo_dir / f"{s}.md").exists() else s
        rows.append(
            f'<tr><td>{memo_link}</td><td class="seg">{html.escape(lab.get("kind") or "")}</td>'
            f'<td class="num">{_v(reg, f"mkt.{s}.price_usd")}</td>'
            f'<td class="num">{_v(reg, f"mkt.{s}.mcap_usd")}</td>'
            f'<td class="num">{_v(reg, f"fund.{s}.fees_365d_usd")}</td>'
            f'<td class="num">{_v(reg, f"fund.{s}.holder_share_365d_pct")}</td>'
            f'<td class="num">{_v(reg, f"liq.{s}.slip_1m_bps")}</td>'
            f'<td class="num">{_v(reg, f"liq.{s}.adv_30d_usd")}</td>'
            f'<td>{_flag_html(lab.get("accrual_flag") or "")}</td></tr>')

    bt = reg.labels.get("backtest_window") or {}
    ivs = [(reg[f"bt.{k}.sharpe"].value, reg[f"bt.{k}.sharpe_se"].value) for k, _ in STRATS
           if f"bt.{k}.sharpe" in reg and f"bt.{k}.sharpe_se" in reg]
    spans_zero = all(abs(sh) < 1.96 * se for sh, se in ivs)
    bt_title = ("Does any of it show up in returns? Not distinguishably, yet." if spans_zero
                else "Does any of it show up in returns?")
    bt_text = ("With this much history every interval spans zero, so the desk reports these and does not rank them."
               if spans_zero else "Intervals are wide; compare them before comparing the point estimates.")
    parts = [
        _part("01", "Liquidity", "What does it cost to actually trade?",
              "<p>Daily volume is the number most screens show. The order book is what a fund pays. Each row walks "
              "the live Coinbase and Kraken books for a $100k, $1M and $10M market order; where the visible book "
              "runs out, the desk says so instead of extrapolating.</p>"
              + _figure("Market-order cost, basis points from mid, log scale", fig_slippage(reg, order),
                        "Worse of buy and sell. Consolidated Coinbase + Kraken USD books at snapshot time; two US "
                        "venues only, so costs are an upper bound for a fund that can route offshore.",
                        _key([("s1", "$100k"), ("s2", "$1M"), ("s3", "$10M")]))),
        _part("02", "Value accrual", "Who captures the fees?",
              "<p>A protocol can earn a great deal while its token receives almost none of it. This is the share "
              "of each protocol's trailing-year fees that reached token holders through buybacks, burns, fee "
              "switches or staking distributions.</p>"
              + _figure("Holder share of fees, trailing 365 days", fig_accrual(reg, order),
                        "Source: DefiLlama fees and holders revenue. Fees include payments to liquidity providers, lenders "
                        "and stakers, so a low share is not by itself a low margin. Faded bars are under 1%.")),
        _part("03", "Capacity", "How much can a fund own before the market stops absorbing it?",
              "<p>A weight is not a trade. Each position is capped at a share of its median daily volume and the "
              "excess moves to names that can take it. As assets grow, the portfolio stops being the one that "
              "was designed and concentrates in the few tokens deep enough to hold size.</p>"
              + _figure("Effective number of positions after the cap", fig_capacity(reg),
                        "Effective positions = 1 / sum of squared weights. Min variance starts concentrated because "
                        "the alts are highly correlated with each other.",
                        _key([("s1", "Equal weight"), ("s2", "Inverse vol"), ("s3", "Min variance")]))),
        _part("04", "Backtest", bt_title,
              f"<p>Walk-forward, out of sample only ({html.escape(bt.get('oos_start', ''))} to "
              f"{html.escape(bt.get('oos_end', ''))}). {bt_text}</p>"
              + _figure("Sharpe ratio with 95% interval", fig_sharpe(reg),
                        "Interval = Sharpe &plusmn; 1.96 &times; its standard error (Lo 2002, iid case). Value shown is "
                        "Sharpe &plusmn; one standard error.")),
    ]

    if reg.labels.get("longrun") and "lr.bt.btc_buy_hold.sharpe" in reg:
        parts.append(_longrun_part(reg))
        parts[3] = parts[3].replace("<p>Walk-forward, out of sample only", "<p>The current tokens, over the one "
                                    "year of history the keyless CoinGecko endpoint returns. Section 05 repeats the test "
                                    "on a long, point-in-time universe. Walk-forward, out of sample only", 1)

    index = f"""<div class="hero">
<p class="eyebrow">Weekly research note &middot; {len(toks)} tokens &middot; data as of {html.escape(as_of)}</p>
<h1>Reported volume says one thing. <em>The order book says another.</em></h1>
<p class="deck">What a fund can actually trade, how much of each protocol&rsquo;s fees reaches the people who
hold its token, and how much a portfolio can own before the market stops absorbing it. Every memo is written from a
registry of facts; a language model may add commentary, but a deterministic verifier decides whether it ships.</p>
{_status(ok_m, n_m, ok_c, n_c, reg.sha256)}
</div>
<div class="tiles">{"".join(tiles)}</div>
{"".join(parts)}
<section class="part"><div class="side"><span class="num">{len(parts) + 1:02d}</span><span class="eyebrow">Memos</span></div><div>
<h2>All tokens</h2>
<p>Ordered by circulating market cap. Each symbol opens its memo, where every number is footnoted to its source.</p>
<div class="table-wrap"><table>
<thead><tr><th>Token</th><th>Segment</th><th class="num">Price</th><th class="num">Market cap</th>
<th class="num">Fees, 1y</th><th class="num">Holder share</th><th class="num">$1M order</th>
<th class="num">Daily volume</th><th>Flag</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody></table></div>
<p class="note">n/a means the data does not exist (for example, the visible order book cannot fill a $1M order),
never that a value was estimated. Hover a value to see its fact id.</p>
</div></section>
"""
    (out / "index.html").write_text(_page(SITE_TITLE, index, "", "index.html"), encoding="utf-8")

    # ---- methodology
    parts_md = []
    for doc in ("ARCHITECTURE.md", "agents/README.md"):
        p = ROOT / doc
        if p.exists():
            body, _ = convert(p.read_text(encoding="utf-8"), link_map=_doc_link)
            parts_md.append(body)
    meth = ('<article class="doc"><p class="eyebrow">Methodology</p>'
            '<p class="deck">How the desk produces its numbers and why each design choice was made. '
            'Rendered from <code>ARCHITECTURE.md</code> and <code>agents/README.md</code>.</p>\n'
            + "\n<hr>\n".join(parts_md) + "</article>")
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
            (dest / f"{m.stem}.html").write_text(
                _memo_page(m, "../../", f" ({d.name})", back="track-record.html"), encoding="utf-8")
            links.append(f'<a href="history/{d.name}/{m.stem}.html">{m.stem}</a>')
        ok_m, n_m, ok_c, n_c = _verify_dir(hreg, d / "memos")
        status = ('<span class="flag" style="color:var(--ok)">verified</span>' if ok_m == n_m and n_m
                  else '<span class="flag">verification failed</span>')
        trows.append(f"<tr><td>{d.name}</td><td>{html.escape(hreg.meta.get('data_as_of') or '')}</td>"
                     f'<td class="num">{len(hreg.facts)}</td><td class="num">{ok_c} / {n_c}</td><td>{status}</td>'
                     f'<td><a href="history/{d.name}/facts.json">facts.json</a></td>'
                     f"<td>{' '.join(links)}</td></tr>")
    track = f"""<div class="hero">
<p class="eyebrow">Track record</p>
<h1>Every week, <em>kept as it was.</em></h1>
<p class="deck">Each weekly run is archived with its facts registry and memos, unchanged. A past claim can be
checked against the exact data that produced it: every snapshot is re-verified against its own
<code>facts.json</code> when this site is built.</p>
</div>
<div class="table-wrap" style="margin-top:48px"><table>
<thead><tr><th>Snapshot</th><th>Data as of</th><th class="num">Facts</th><th class="num">Claims verified</th>
<th>Status</th><th>Registry</th><th>Memos</th></tr></thead>
<tbody>
{chr(10).join(trows) if trows else '<tr><td colspan="7" class="muted">No snapshots archived yet.</td></tr>'}
</tbody></table></div>
"""
    (out / "track-record.html").write_text(_page(f"Track record | {SITE_TITLE}", track, "", "track-record.html"),
                                           encoding="utf-8")
    return out


def longrun_headline(sharpes: dict, diffs: dict, names: dict) -> tuple[str, str]:
    """Pick the long-run headline from the intervals: (sharpe, se) per strategy, (gap, se) to BTC.

    'not distinguishable from zero' only if every Sharpe interval spans zero;
    'no scheme distinguishable from Bitcoin' only if every gap interval spans zero;
    otherwise name the schemes whose gap to BTC is outside its interval.
    """
    if all(abs(s) < 1.96 * se for s, se in sharpes.values()):
        return ("Even over the long sample, not distinguishable from zero.",
                "Every Sharpe interval spans zero, so the desk reports these and does not rank them.")
    differ = [k for k, (d, se) in diffs.items() if abs(d) >= 1.96 * se]
    if not differ:
        return ("Over the long run, no scheme is distinguishable from holding Bitcoin.",
                "Sharpe ratios are estimated well enough to tell some apart from zero, but not from one another: "
                "every scheme's gap to BTC buy and hold lies within 1.96 standard errors of zero.")
    return ("Over the long run, some schemes differ from holding Bitcoin.",
            "The Sharpe gap to BTC buy and hold is more than 1.96 standard errors from zero for "
            + ", ".join(f"{names.get(k, k)} ({'below' if diffs[k][0] < 0 else 'above'} BTC)" for k in differ)
            + "; every other scheme's gap is within its interval.")


def _longrun_part(reg: Registry) -> str:
    """Section 05: the v2 long-run layer. The headline is chosen from the intervals, never written ahead."""
    lab = reg.labels["longrun"]
    strats = [k for k, _ in LR_TABLE_STRATS if f"lr.bt.{k}.sharpe" in reg]
    others = [k for k in strats if k != "btc_buy_hold"]
    iv = lambda f: (reg[f"{f}"].value, reg[f"{f}_se"].value)
    names = dict(LR_TABLE_STRATS)
    title, verdict = longrun_headline({k: iv(f"lr.bt.{k}.sharpe") for k in strats},
                                      {k: iv(f"lr.bt.{k}.sharpe_diff_btc") for k in others
                                       if f"lr.bt.{k}.sharpe_diff_btc_se" in reg}, names)
    rows = []
    for k in strats:
        d = (f'{_v(reg, f"lr.bt.{k}.sharpe_diff_btc")} &plusmn; {_v(reg, f"lr.bt.{k}.sharpe_diff_btc_se")}'
             if f"lr.bt.{k}.sharpe_diff_btc" in reg else '<span class="muted">benchmark</span>')
        rows.append(f"<tr><td>{html.escape(names[k])}</td>"
                    f'<td class="num">{_v(reg, f"lr.bt.{k}.ann_return_pct")}</td>'
                    f'<td class="num">{_v(reg, f"lr.bt.{k}.ann_vol_pct")}</td>'
                    f'<td class="num">{_v(reg, f"lr.bt.{k}.sharpe")} &plusmn; {_v(reg, f"lr.bt.{k}.sharpe_se")}</td>'
                    f'<td class="num">{_v(reg, f"lr.bt.{k}.sharpe_boot_lo")} to {_v(reg, f"lr.bt.{k}.sharpe_boot_hi")}</td>'
                    f'<td class="num">{d}</td>'
                    f'<td class="num">{_v(reg, f"lr.bt.{k}.max_drawdown_pct")}</td></tr>')
    table = ('<div class="table-wrap"><table><thead><tr><th>Strategy</th><th class="num">Return</th>'
             '<th class="num">Vol</th><th class="num">Sharpe &plusmn; SE</th><th class="num">Bootstrap 95%</th>'
             '<th class="num">vs BTC &plusmn; SE</th><th class="num">Max DD</th></tr></thead><tbody>'
             + "".join(rows) + "</tbody></table></div>")
    months = lab.get("months") or []
    jan = [m for m in months if m.endswith("_01")]
    crow = []
    for m in jan:
        crow.append(f"<tr><td>{m[1:5]}</td>"
                    f'<td class="num">{_v(reg, f"lr.cap.{m}.max_aum_uncapped_usd")}</td>'
                    f'<td class="num">{_v(reg, f"lr.cap.{m}.weight_moved_100m_pct")}</td>'
                    f'<td class="num">{_v(reg, f"lr.cap.{m}.weight_moved_1b_pct")}</td>'
                    f'<td class="num">{_v(reg, f"lr.cap.{m}.eff_n_1b")}</td>'
                    f'<td class="num">{_v(reg, f"lr.liq.BTC.{m}.amihud_bps_per_1m")}</td>'
                    f'<td class="num">{_v(reg, f"lr.cov.{m}.unpriced_count")}</td></tr>')
    ctable = ('<div class="table-wrap"><table><thead><tr><th>January</th><th class="num">First cap binds</th>'
              '<th class="num">Moved, $100M</th><th class="num">Moved, $1B</th><th class="num">Eff. N, $1B</th>'
              '<th class="num">BTC Amihud, bps</th><th class="num">Unpriced in top K</th></tr></thead><tbody>'
              + "".join(crow) + "</tbody></table></div>")
    body = (
        f"<p>The same schemes, rebuilt on a universe chosen the way it could have been chosen at the time: each "
        f"month from {html.escape(lab.get('sample_start', ''))}, the top {_v(reg, 'param.lr_top_k_count')} assets by "
        f"reported volume among those with at least {_v(reg, 'param.lr_min_history_days')} of prices, from "
        f"{_v(reg, 'lr.universe.candidates_count')} candidates that include assets which later collapsed or were "
        f"delisted. {_v(reg, 'lr.universe.ever_eligible_count')} assets were eligible at some point, "
        f"{_v(reg, 'lr.universe.ever_eligible_died_count')} of which later stopped trading; each stays in the "
        f"returns for every day it traded. {verdict}</p>"
        + _figure("Sharpe ratio with 95% interval, full sample and by regime", fig_lr_regimes(reg),
                  "Interval = Sharpe &plusmn; 1.96 &times; its standard error (Lo 2002). Regime boundaries are Bitcoin "
                  "cycle turning points chosen with hindsight: they describe, and no portfolio decision uses them. "
                  "Hover a point for its fact ids.",
                  _key([(c, n) for _, n, c in LR_STRATS]))
        + table
        + "<p>When did the market become deep enough for a fund? Each month each position of the inverse-vol "
          f"portfolio is capped at {_v(reg, 'param.max_adv_fraction_pct')} of the asset's median reported "
          f"volume over the previous {_v(reg, 'param.adv_window_days')}, and the chart shows how much of the designed "
          "portfolio the cap forces into other names or cash. Zero means the market absorbs the whole design.</p>"
        + _figure("Weight the liquidity cap forces off the inverse-vol design", fig_lr_capacity(reg),
                  "Monthly, inverse volatility over the point-in-time universe. The shaded years use exchange-reported "
                  "volume that was heavily inflated by wash trading, so capacity there is overstated. Early 2016 holds "
                  "one or two assets, which is why even a small fund is capped.",
                  _key([("s1", "$10M fund"), ("s2", "$100M fund"), ("s3", "$1B fund")]))
        + _figure("Liquidity trend: trailing-year Amihud illiquidity, log scale", fig_lr_liquidity(reg),
                  "Basis points of price move per $1M of reported volume; lower is more liquid. Reported volume "
                  "flatters the early years most: if later volume is less inflated, the true improvement is larger "
                  "than it looks here.",
                  _key([("s1", "BTC"), ("s2", "ETH")]))
        + ctable
        + '<p class="note">Values in January of each year; every month is in <code>reports/longrun_capacity.csv</code>. '
          '&ldquo;Unpriced in top K&rdquo; counts assets in the top K by reported volume that no keyless source prices; '
          'the universe could not hold them.</p>')
    return _part("05", "Long run", title, body)


def _doc_link(url: str) -> str:
    if url.startswith(("http://", "https://", "#")):
        return url
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
