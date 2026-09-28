"""The static site: well-formed HTML, working links, no injected markup; and the history archive."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import pytest

from desk import history as H
from desk import mdhtml
from desk import site as S

VOID = {"meta", "link", "br", "hr", "img", "input", "source", "wbr", "col", "area", "base", "embed", "track"}


class Checker(HTMLParser):
    """Strict nesting check: every non-void tag closes, in order; ids unique; collect links."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors, self.ids, self.links = [], [], set(), []
        self.saw_doctype = False

    def handle_decl(self, decl):
        self.saw_doctype = decl.lower() == "doctype html"

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            if a["id"] in self.ids:
                self.errors.append(f"duplicate id {a['id']}")
            self.ids.add(a["id"])
        for k in ("href", "src"):
            if a.get(k):
                self.links.append(a[k])
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}>, open: {self.stack[-3:]}")
        else:
            self.stack.pop()


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    return S.build(tmp_path_factory.mktemp("site") / "site")


def test_site_pages_exist(site):
    for p in ["index.html", "methodology.html", "track-record.html", "facts.json", "assets/style.css",
              "memos/AAVE.html"]:
        assert (site / p).exists(), p
    assert len(list((site / "memos").glob("*.html"))) == 20


def test_every_page_is_well_formed_and_every_local_link_resolves(site):
    pages = list(site.rglob("*.html"))
    assert len(pages) >= 23
    for page in pages:
        c = Checker()
        c.feed(page.read_text())
        c.close()
        assert c.saw_doctype, page
        assert not c.errors, (page, c.errors[:3])
        assert c.stack == [], (page, c.stack)
        for link in c.links:
            u = urlparse(link)
            if u.scheme or link.startswith("#"):
                continue
            target = (page.parent / u.path).resolve()
            assert target.exists(), (page.name, link)
            if u.fragment and target.suffix == ".html":
                t = Checker()
                t.feed(target.read_text())
                assert u.fragment in t.ids, (page.name, link)


def test_index_values_come_from_facts_and_show_verification(site):
    text = (site / "index.html").read_text()
    assert 'title="liq.AAVE.slip_1m_bps">371 bps<' in text
    assert "Verified: 20 of 20 memos pass" in text
    assert "prefers-color-scheme: dark" in (site / "assets" / "style.css").read_text()


def test_memo_page_footnotes_link_to_sources(site):
    text = (site / "memos" / "AAVE.html").read_text()
    assert 'href="#fn-liq.AAVE.slip_1m_bps"' in text and 'id="fn-liq.AAVE.slip_1m_bps"' in text


def test_markdown_is_escaped():
    html_out, _ = mdhtml.convert("Hi <script>alert(1)</script> and [x](javascript:alert(1)) **b**")
    assert "<script>" not in html_out and "javascript:" not in html_out and "<strong>b</strong>" in html_out


def test_history_archive_refuses_unverified_memos(tmp_path):
    from desk.config import REPORTS_DIR
    import shutil
    rep = tmp_path / "reports"
    shutil.copytree(REPORTS_DIR / "memos", rep / "memos")
    shutil.copy2(REPORTS_DIR / "facts.json", rep / "facts.json")
    dest = H.archive(rep, rep / "history")
    assert dest.name == "2026-09-28" and len(list((dest / "memos").glob("*.md"))) == 20
    (rep / "memos" / "AAVE.md").write_text((rep / "memos" / "AAVE.md").read_text() + "\nMade up: 42%.\n")
    with pytest.raises(SystemExit, match="unverified"):
        H.archive(rep, rep / "history")
