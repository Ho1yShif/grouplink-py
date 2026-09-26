from __future__ import annotations

from typing import Any

import pytest
from render_lab_tasks_notion.types import PageDTO

from grouplink.links import (
    assert_default_slug,
    favicon_url,
    fetchable_urls,
    group_by_profile,
    meta_cache_key,
    page_path,
    page_paths_for,
    skipped_rows,
    to_icon_name,
    to_link_rows,
    to_profile_rows,
    unique_urls,
    unknown_icons,
    unnumbered_last,
    visible_rows,
)


def page(props: dict[str, Any], title: str, id: str = "p") -> PageDTO:
    return {
        "id": id,
        "url": "https://www.notion.so/p",
        "title": title,
        "properties": props,
        "createdTime": "",
        "lastEditedTime": "",
    }


PAGES = [
    page({"URL": "https://b.example", "Visible": True}, "B"),
    page({"URL": "https://a.example", "Visible": True}, "A"),
    page({"URL": "https://hidden.example", "Visible": False}, "Hidden"),
    page({"URL": "https://x.com/render", "Visible": True}, "X"),
    page({"URL": "", "Visible": True}, "No URL"),
]

PROFILES = [
    page({"Slug": "shifra", "Tagline": "DevRel"}, "Shifra", id="profile-shifra"),
    page({"Slug": "Alex", "Tagline": ""}, "Alex", id="profile-alex"),
]


class TestToLinkRows:
    def test_reads_the_url_property_not_the_notion_page_url(self) -> None:
        assert to_link_rows(PAGES)[0].url == "https://b.example"

    def test_skips_rows_with_no_url(self) -> None:
        assert "No URL" not in [row.title for row in to_link_rows(PAGES)]

    @pytest.mark.parametrize(
        "url",
        ["render.com/careers", "ftp://example.com", "mailto:", "javascript:alert(1)"],
    )
    def test_skips_a_url_the_site_cannot_link_to(self, url: str) -> None:
        assert to_link_rows([page({"URL": url}, "Bad")]) == []

    @pytest.mark.parametrize(
        "url",
        ["https://render.com", "http://render.com", "mailto:shifra@render.com"],
    )
    def test_keeps_an_http_or_mailto_url(self, url: str) -> None:
        assert [row.url for row in to_link_rows([page({"URL": url}, "Good")])] == [url]

    def test_keeps_the_notion_order_and_drops_hidden_rows(self) -> None:
        assert [row.title for row in visible_rows(to_link_rows(PAGES))] == ["B", "A", "X"]

    def test_falls_back_to_the_title_property(self) -> None:
        rows = to_link_rows([page({"URL": "https://a.example", "Title": " Named "}, "")])
        assert rows[0].title == "Named"

    def test_reads_the_icon_column(self) -> None:
        rows = to_link_rows([page({"URL": "https://a.example", "Icon": "Workflows"}, "A")])
        assert rows[0].icon == "workflows"

    def test_defaults_the_icon_when_the_column_is_missing(self) -> None:
        assert to_link_rows([page({"URL": "https://a.example"}, "A")])[0].icon == "arrow"


class TestOrder:
    def test_reads_a_number_as_order_and_anything_else_as_none(self) -> None:
        rows = to_link_rows(
            [
                page({"URL": "https://a.example", "Order": 10}, "A"),
                page({"URL": "https://b.example", "Order": 2.5}, "B"),
                page({"URL": "https://c.example", "Order": 0}, "C"),
                page({"URL": "https://d.example", "Order": None}, "D"),
                page({"URL": "https://e.example", "Order": "10"}, "E"),
                page({"URL": "https://f.example", "Order": True}, "F"),
                page({"URL": "https://g.example"}, "G"),
            ]
        )
        assert [row.order for row in rows] == [10, 2.5, 0, None, None, None, None]


class TestUnnumberedLast:
    def test_moves_rows_with_no_order_to_the_end_and_keeps_input_order(self) -> None:
        rows = to_link_rows(
            [
                page({"URL": "https://a.example"}, "Empty 1"),
                page({"URL": "https://b.example", "Order": 20}, "Twenty"),
                page({"URL": "https://c.example"}, "Empty 2"),
                page({"URL": "https://d.example", "Order": 10}, "Ten"),
            ]
        )
        assert [row.title for row in unnumbered_last(rows)] == [
            "Twenty",
            "Ten",
            "Empty 1",
            "Empty 2",
        ]


class TestToIconName:
    @pytest.mark.parametrize("value", ["workflows", " Workflows ", "WORKFLOWS"])
    def test_strips_and_lowercases_a_known_option(self, value: str) -> None:
        assert to_icon_name(value) == "workflows"

    @pytest.mark.parametrize("value", ["", "   ", "nonesuch", None, 7, ["arrow"]])
    def test_falls_back_for_an_empty_unknown_or_non_string_cell(self, value: Any) -> None:
        assert to_icon_name(value) == "arrow"


class TestUnknownIcons:
    def test_names_the_options_no_file_matches_in_first_seen_order(self) -> None:
        pages = [
            page({"URL": "https://a.example", "Icon": "Sparkle"}, "A"),
            page({"URL": "https://b.example", "Icon": "workflows"}, "B"),
            page({"URL": "https://c.example", "Icon": "Rocket"}, "C"),
            page({"URL": "https://d.example", "Icon": "sparkle"}, "D"),
            page({"URL": "https://e.example"}, "E"),
        ]
        assert unknown_icons(pages) == ["sparkle", "rocket"]


class TestToProfileRows:
    def test_lowercases_the_slug(self) -> None:
        assert [profile.slug for profile in to_profile_rows(PROFILES)] == ["shifra", "alex"]

    def test_skips_a_row_with_no_slug(self) -> None:
        assert to_profile_rows([page({"Tagline": "x"}, "Nameless")]) == []


class TestGroupByProfile:
    profiles = to_profile_rows(PROFILES)
    rows = to_link_rows(
        [
            page({"URL": "https://shared.example", "Profiles": ["profile-shifra"]}, "Shifra only"),
            page({"URL": "https://all.example", "Everyone": True}, "Everyone"),
            page(
                {"URL": "https://both.example", "Everyone": True, "Profiles": ["profile-shifra"]},
                "Everyone and related",
            ),
            page({"URL": "https://orphan.example"}, "Related to nobody"),
        ]
    )

    def titles_for(self, slug: str) -> list[str]:
        grouped = group_by_profile(self.rows, self.profiles)
        return [row.title for p in grouped if p.profile.slug == slug for row in p.rows]

    def test_puts_an_everyone_row_on_every_page(self) -> None:
        assert self.titles_for("alex") == ["Everyone", "Everyone and related"]

    def test_counts_a_row_that_is_both_everyone_and_related_once(self) -> None:
        assert self.titles_for("shifra") == [
            "Shifra only",
            "Everyone",
            "Everyone and related",
        ]

    def test_renders_a_row_related_to_nobody_nowhere(self) -> None:
        assert "Related to nobody" not in self.titles_for("shifra")
        assert "Related to nobody" not in self.titles_for("alex")


class TestSkippedRows:
    def test_names_the_check_each_row_failed(self) -> None:
        profiles = to_profile_rows(PROFILES)
        pages = [
            page({"URL": "https://ok.example", "Profiles": ["profile-shifra"]}, "Fine"),
            page({"URL": "", "Profiles": ["profile-shifra"]}, "No URL"),
            page({"URL": "https://x.example"}, ""),
            page({"URL": "https://h.example", "Visible": False, "Everyone": True}, "Hidden"),
            page({"URL": "https://o.example"}, "Orphan"),
            page({"URL": "https://g.example", "Profiles": ["profile-ghost"]}, "Ghost"),
            page({"URL": "render.com/careers", "Profiles": ["profile-shifra"]}, "Schemeless"),
            page({"URL": "ftp://example.com", "Profiles": ["profile-shifra"]}, "FTP"),
            page({"URL": "mailto:shifra@render.com", "Profiles": ["profile-shifra"]}, "Email"),
        ]
        assert [(row.title, row.reason) for row in skipped_rows(pages, profiles)] == [
            ("No URL", "no URL"),
            ("https://x.example", "no Title"),
            ("Hidden", "Visible is unchecked"),
            ("Orphan", "no Profiles relation and Everyone is unchecked"),
            ("Ghost", "its Profiles relation points at no row in the Profiles database"),
            ("Schemeless", "URL is neither http://, https://, nor mailto:"),
            ("FTP", "URL is neither http://, https://, nor mailto:"),
        ]


class TestUniqueUrls:
    def test_keeps_first_seen_order(self) -> None:
        rows = to_link_rows(
            [
                page({"URL": "https://b.example"}, "B"),
                page({"URL": "https://a.example"}, "A"),
                page({"URL": "https://b.example"}, "B again"),
            ]
        )
        assert unique_urls(rows) == ["https://b.example", "https://a.example"]


class TestFetchableUrls:
    def test_keeps_only_the_urls_with_a_page_to_fetch(self) -> None:
        urls = ["https://render.com", "mailto:shifra@render.com", "http://render.com"]
        assert fetchable_urls(urls) == ["https://render.com", "http://render.com"]


class TestFaviconUrl:
    def test_points_at_the_origin_root(self) -> None:
        assert favicon_url("https://render.com/tutorials/x") == "https://render.com/favicon.ico"

    def test_returns_empty_for_a_url_that_will_not_parse(self) -> None:
        assert favicon_url("not a url") == ""

    def test_returns_empty_for_a_mailto_link(self) -> None:
        assert favicon_url("mailto:shifra@render.com") == ""


def test_page_path_puts_the_default_profile_at_the_root() -> None:
    assert page_path("site", "") == "site/index.html"
    assert page_path("site", "alex") == "site/alex/index.html"


class TestPagePathsFor:
    def test_writes_the_default_profile_twice(self) -> None:
        assert page_paths_for("site", "shifra", "shifra") == [
            "site/shifra/index.html",
            "site/index.html",
        ]

    def test_writes_everyone_else_once(self) -> None:
        assert page_paths_for("site", "alex", "shifra") == ["site/alex/index.html"]


class TestAssertDefaultSlug:
    def test_passes_when_a_profile_has_the_slug(self) -> None:
        assert_default_slug(to_profile_rows(PROFILES), "shifra")

    def test_names_the_slug_that_matches_nobody(self) -> None:
        with pytest.raises(ValueError, match="SITE_DEFAULT_SLUG"):
            assert_default_slug(to_profile_rows(PROFILES), "nobody")


def test_meta_cache_key_is_versioned() -> None:
    assert meta_cache_key("https://a.example") == "gl:meta:v1:https://a.example"
