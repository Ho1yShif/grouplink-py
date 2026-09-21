from __future__ import annotations

from typing import Any

import pytest
from render_lab_tasks_notion.types import PageDTO

from grouplink.links import (
    assert_default_slug,
    favicon_url,
    group_by_person,
    meta_cache_key,
    page_path,
    page_paths_for,
    skipped_rows,
    to_icon_name,
    to_link_rows,
    to_person_rows,
    unique_urls,
    unknown_icons,
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

PEOPLE = [
    page({"Slug": "shifra", "Tagline": "DevRel"}, "Shifra", id="person-shifra"),
    page({"Slug": "Alex", "Tagline": ""}, "Alex", id="person-alex"),
]


class TestToLinkRows:
    def test_reads_the_url_property_not_the_notion_page_url(self) -> None:
        assert to_link_rows(PAGES)[0].url == "https://b.example"

    def test_skips_rows_with_no_url(self) -> None:
        assert "No URL" not in [row.title for row in to_link_rows(PAGES)]

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


class TestToPersonRows:
    def test_lowercases_the_slug(self) -> None:
        assert [person.slug for person in to_person_rows(PEOPLE)] == ["shifra", "alex"]

    def test_skips_a_row_with_no_slug(self) -> None:
        assert to_person_rows([page({"Tagline": "x"}, "Nameless")]) == []


class TestGroupByPerson:
    people = to_person_rows(PEOPLE)
    rows = to_link_rows(
        [
            page({"URL": "https://shared.example", "People": ["person-shifra"]}, "Shifra only"),
            page({"URL": "https://all.example", "Everyone": True}, "Everyone"),
            page(
                {"URL": "https://both.example", "Everyone": True, "People": ["person-shifra"]},
                "Everyone and related",
            ),
            page({"URL": "https://orphan.example"}, "Related to nobody"),
        ]
    )

    def titles_for(self, slug: str) -> list[str]:
        grouped = group_by_person(self.rows, self.people)
        return [row.title for p in grouped if p.person.slug == slug for row in p.rows]

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
        people = to_person_rows(PEOPLE)
        pages = [
            page({"URL": "https://ok.example", "People": ["person-shifra"]}, "Fine"),
            page({"URL": "", "People": ["person-shifra"]}, "No URL"),
            page({"URL": "https://x.example"}, ""),
            page({"URL": "https://h.example", "Visible": False, "Everyone": True}, "Hidden"),
            page({"URL": "https://o.example"}, "Orphan"),
            page({"URL": "https://g.example", "People": ["person-ghost"]}, "Ghost"),
        ]
        assert [(row.title, row.reason) for row in skipped_rows(pages, people)] == [
            ("No URL", "no URL"),
            ("https://x.example", "no Title"),
            ("Hidden", "Visible is unchecked"),
            ("Orphan", "no People relation and Everyone is unchecked"),
            ("Ghost", "its People relation points at no row in the People database"),
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


class TestFaviconUrl:
    def test_points_at_the_origin_root(self) -> None:
        assert favicon_url("https://render.com/tutorials/x") == "https://render.com/favicon.ico"

    def test_returns_empty_for_a_url_that_will_not_parse(self) -> None:
        assert favicon_url("not a url") == ""


def test_page_path_puts_the_default_person_at_the_root() -> None:
    assert page_path("site", "") == "site/index.html"
    assert page_path("site", "alex") == "site/alex/index.html"


class TestPagePathsFor:
    def test_writes_the_default_person_twice(self) -> None:
        assert page_paths_for("site", "shifra", "shifra") == [
            "site/shifra/index.html",
            "site/index.html",
        ]

    def test_writes_everyone_else_once(self) -> None:
        assert page_paths_for("site", "alex", "shifra") == ["site/alex/index.html"]


class TestAssertDefaultSlug:
    def test_passes_when_a_person_has_the_slug(self) -> None:
        assert_default_slug(to_person_rows(PEOPLE), "shifra")

    def test_names_the_slug_that_matches_nobody(self) -> None:
        with pytest.raises(ValueError, match="SITE_DEFAULT_SLUG"):
            assert_default_slug(to_person_rows(PEOPLE), "nobody")


def test_meta_cache_key_is_versioned() -> None:
    assert meta_cache_key("https://a.example") == "gl:meta:v1:https://a.example"
