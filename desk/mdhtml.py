"""A small Markdown-to-HTML converter for the memos and docs this repo writes.

Why not a library? The site build should need nothing beyond the standard
library, and the Markdown here is a known, small subset: headings,
paragraphs, lists, tables, fenced code, blockquotes, bold/italic/code/links,
and footnotes ([^fact.id]). Everything is HTML-escaped first, so text from a
memo can never inject markup into the site.
"""
from __future__ import annotations

import html
import re

_FOOTREF = re.compile(r"\[\^([A-Za-z0-9_.\-]+)\]")
_FOOTDEF = re.compile(r"^\[\^([A-Za-z0-9_.\-]+)\]:\s?(.*)$")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITAL = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_COMMENT = re.compile(r"<!--.*?-->", re.S)


def slugify(text: str) -> str:
    s = re.sub(r"<[^>]+>", "", text).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "section"


class _Ctx:
    def __init__(self, link_map):
        self.foot_order: list[str] = []
        self.link_map = link_map or (lambda u: u)

    def foot_num(self, fid: str) -> int:
        if fid not in self.foot_order:
            self.foot_order.append(fid)
        return self.foot_order.index(fid) + 1


def _inline(text: str, ctx: _Ctx) -> str:
    codes: list[str] = []

    def keep_code(m):
        codes.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        return f"\x00{len(codes) - 1}\x00"
    text = _CODE.sub(keep_code, text)
    text = html.escape(text, quote=False)

    def link(m):
        url = html.unescape(m.group(2))
        if url.lower().startswith("javascript:"):
            url = "#"
        return f'<a href="{html.escape(ctx.link_map(url), quote=True)}">{m.group(1)}</a>'
    text = _LINK.sub(link, text)

    def foot(m):
        fid = m.group(1)
        n = ctx.foot_num(fid)
        return (f'<sup class="fn"><a href="#fn-{html.escape(fid, quote=True)}" '
                f'title="{html.escape(fid, quote=True)}">{n}</a></sup>')
    text = _FOOTREF.sub(foot, text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITAL.sub(r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def convert(md: str, link_map=None) -> tuple[str, list[tuple[int, str, str]]]:
    """Markdown -> (html, headings). headings = [(level, id, text)] for a table of contents."""
    md = _COMMENT.sub("", md)
    ctx = _Ctx(link_map)
    lines = md.split("\n")
    out: list[str] = []
    defs: dict[str, str] = {}
    headings: list[tuple[int, str, str]] = []
    used_ids: set[str] = set()
    i = 0
    para: list[str] = []

    def flush_para():
        if para:
            out.append("<p>" + _inline(" ".join(para), ctx) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        fd = _FOOTDEF.match(line)
        if fd:
            flush_para()
            defs[fd.group(1)] = fd.group(2)
            i += 1
            continue
        if not stripped:
            flush_para()
            i += 1
            continue
        if stripped.startswith("```"):
            flush_para()
            j = i + 1
            code = []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                code.append(lines[j])
                j += 1
            out.append("<pre><code>" + html.escape("\n".join(code), quote=False) + "</code></pre>")
            i = j + 1
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if h:
            flush_para()
            level = len(h.group(1))
            inner = _inline(h.group(2).strip(), ctx)
            hid = slugify(inner)
            base, k = hid, 2
            while hid in used_ids:
                hid, k = f"{base}-{k}", k + 1
            used_ids.add(hid)
            headings.append((level, hid, re.sub(r"<[^>]+>", "", inner)))
            out.append(f'<h{level} id="{hid}">{inner}</h{level}>')
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,})$", stripped):
            flush_para()
            out.append("<hr>")
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{3,}", lines[i + 1].strip()):
            flush_para()
            header = _split_row(stripped)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            t = ['<div class="table-wrap"><table>', "<thead><tr>"]
            t += [f"<th>{_inline(c, ctx)}</th>" for c in header]
            t += ["</tr></thead>", "<tbody>"]
            for r in rows:
                r = (r + [""] * len(header))[: len(header)]
                t.append("<tr>" + "".join(f"<td>{_inline(c, ctx)}</td>" for c in r) + "</tr>")
            t += ["</tbody></table></div>"]
            out.append("".join(t))
            continue
        if stripped.startswith(">"):
            flush_para()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            out.append("<blockquote><p>" + _inline(" ".join(quote), ctx) + "</p></blockquote>")
            continue
        lm = re.match(r"^(\s*)([-*]|\d+[.)])\s+(.*)$", line)
        if lm:
            flush_para()
            ordered = lm.group(2)[0].isdigit()
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while i < len(lines):
                m2 = re.match(r"^(\s*)([-*]|\d+[.)])\s+(.*)$", lines[i])
                if m2 and m2.group(2)[0].isdigit() == ordered:
                    items.append(m2.group(3))
                elif lines[i].strip() and not m2 and (lines[i].startswith("  ") or lines[i].startswith("\t")) and items:
                    items[-1] += " " + lines[i].strip()          # continuation line
                else:
                    break
                i += 1
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(it, ctx)}</li>" for it in items) + f"</{tag}>")
            continue
        para.append(stripped)
        i += 1
    flush_para()

    if ctx.foot_order or defs:
        order = ctx.foot_order + [d for d in defs if d not in ctx.foot_order]
        out.append('<section class="footnotes"><ol>')
        for fid in order:
            body = defs.get(fid, "<em>missing source entry</em>")
            body = _inline(body, ctx) if fid in defs else body
            out.append(f'<li id="fn-{html.escape(fid, quote=True)}">{body}</li>')
        out.append("</ol></section>")
    return "\n".join(out), headings
