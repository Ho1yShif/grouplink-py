"""Renderer tests, including the golden pages.

The golden fixtures are byte-for-byte copies of the three pages the TypeScript
build rendered from the seed links in scripts/placeholder.py. They guard the four
places a port can drift: the HTML escaping, the URL normalization, the CSP
hashes, and the whitespace.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import replace
from pathlib import Path

import pytest

from grouplink.links import to_card
from grouplink.page import LinkCard, PageModel, escape_html, render_page, safe_url
from scripts.placeholder import PEOPLE, TAGLINE, SeedPerson

GOLDEN = Path(__file__).parent / "golden"

#: The footer year the golden pages were rendered with. Pinned, so the fixtures do
#: not go stale on 1 January.
GOLDEN_YEAR = 2026

#: The fixture each seed person's page is committed as. The default person is
#: written twice, so index.html and shifra.html hold the same bytes.
GOLDEN_FIXTURES = {"shifra": ["index.html", "shifra.html"], "graham": ["graham.html"]}

MODEL = PageModel(
    name="Render",
    tagline="Cloud application hosting for developers.",
    cards=[
        LinkCard(
            "First",
            "https://example.com/a",
            "A",
            icon_url="https://example.com/favicon.ico",
            icon="arrow",
        ),
        LinkCard("Second", "https://example.com/b", "", icon_url="", icon="workflows"),
    ],
)


@pytest.mark.parametrize(
    ("person", "fixture"),
    [(person, fixture) for person in PEOPLE for fixture in GOLDEN_FIXTURES[person.slug]],
    ids=lambda value: value if isinstance(value, str) else value.slug,
)
def test_reproduces_the_committed_page_byte_for_byte(person: SeedPerson, fixture: str) -> None:
    html = render_page(
        PageModel(
            name=person.name,
            tagline=TAGLINE,
            cards=[to_card(link, link.description) for link in person.links],
        ),
        year=GOLDEN_YEAR,
    )
    assert html == (GOLDEN / fixture).read_text()


class TestRenderPage:
    def test_emits_every_card_in_model_order(self) -> None:
        html = render_page(MODEL)
        assert html.index("First") < html.index("Second")
        assert 'href="https://example.com/a"' in html
        assert 'href="https://x.com/render"' in html

    def test_omits_the_description_paragraph_when_there_is_no_description(self) -> None:
        assert len(re.findall(r'class="card__desc"', render_page(MODEL))) == 1

    def test_escapes_titles_and_descriptions(self) -> None:
        html = render_page(
            replace(
                MODEL,
                cards=[
                    LinkCard(
                        "<script>alert(1)</script>",
                        "https://example.com",
                        'a "b" & c',
                        icon_url="",
                        icon="arrow",
                    )
                ],
            )
        )
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "a &quot;b&quot; &amp; c" in html

    def test_drops_a_javascript_href(self) -> None:
        card = LinkCard("Bad", "javascript:alert(1)", "", icon_url="", icon="arrow")
        html = render_page(replace(MODEL, cards=[card]))
        assert "javascript:" not in html
        assert 'href="#"' in html

    def test_keeps_the_tagline_out_of_the_page_and_on_one_line(self) -> None:
        html = render_page(replace(MODEL, tagline="First half\nsecond half"))
        assert 'class="tagline"' not in html
        assert '<meta name="description" content="First half second half">' in html

    def test_draws_the_card_icon_the_model_names(self) -> None:
        html = render_page(MODEL)
        assert 'class="card__mark card__mark--workflows"' in html
        assert 'class="card__mark card__mark--arrow"' in html

    def test_reads_the_footer_year_from_its_argument(self) -> None:
        assert "&copy; 1999 render.com" in render_page(MODEL, year=1999)

    def test_always_renders_the_same_icon_row(self) -> None:
        assert 'class="social__icon social__icon--github"' in render_page(MODEL)

    def test_declares_both_color_schemes_and_no_bold_weight(self) -> None:
        html = render_page(MODEL)
        assert "@media (prefers-color-scheme: dark)" in html
        assert "prefers-reduced-motion" in html
        assert not re.search(r"font-weight:\s*(600|700|800|900|bold)", html)


class TestContentSecurityPolicy:
    @staticmethod
    def _inline_block(html: str, tag: str) -> str:
        match = re.search(rf"<{tag}>([\s\S]*?)</{tag}>", html)
        assert match, f"expected one inline <{tag}> block"
        return match.group(1)

    @staticmethod
    def _policy(html: str) -> str:
        match = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)">', html)
        assert match, "expected a CSP meta tag"
        return match.group(1)

    @staticmethod
    def _sha256(content: str) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).digest()
        return f"sha256-{base64.b64encode(digest).decode('ascii')}"

    def test_allows_the_inline_blocks_it_actually_emits(self) -> None:
        html = render_page(MODEL)
        csp = self._policy(html)
        assert f"style-src '{self._sha256(self._inline_block(html, 'style'))}'" in csp
        assert f"script-src '{self._sha256(self._inline_block(html, 'script'))}'" in csp

    def test_allows_the_sites_own_images(self) -> None:
        assert "img-src 'self' https:" in self._policy(render_page(MODEL))

    def test_emits_no_inline_event_handlers(self) -> None:
        assert not re.search(r"\son[a-z]+=", render_page(MODEL))


class TestEscapeHtmlAndSafeUrl:
    def test_escapes_the_five_html_significant_characters(self) -> None:
        assert escape_html("<>&\"'") == "&lt;&gt;&amp;&quot;&#39;"

    def test_passes_http_and_https_through_and_rejects_everything_else(self) -> None:
        assert safe_url("https://render.com/") == "https://render.com/"
        assert safe_url("data:text/html,x") == "#"
        assert safe_url("not a url") == "#"

    def test_normalizes_the_way_the_js_url_constructor_does(self) -> None:
        assert safe_url("https://render.com") == "https://render.com/"
        assert safe_url("HTTPS://RENDER.COM:443/a/../b") == "https://render.com/b"
        assert safe_url("https://render.com/a b") == "https://render.com/a%20b"
