"""Notion rows in, page model out. Pure functions — no network, no ctx."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from render_lab_tasks_notion.types import PageDTO, PropertyValue

from grouplink.jsurl import parse as parse_url


@dataclass(frozen=True)
class LinkRow:
    title: str
    url: str
    visible: bool
    #: Renders on every person's page, whatever `person_ids` holds.
    everyone: bool
    #: Notion page ids of the People rows this link belongs to.
    person_ids: list[str]


@dataclass(frozen=True)
class PersonRow:
    """One row of the People database. `id` is what a link's relation points at."""

    id: str
    name: str
    slug: str
    tagline: str


@dataclass(frozen=True)
class PersonPage:
    """A person and the links that relate to them, in the order Notion returned them."""

    person: PersonRow
    rows: list[LinkRow]


@dataclass(frozen=True)
class SkippedRow:
    """A link row read from Notion that renders on no page, and the check it failed."""

    title: str
    url: str
    reason: str


def _read_string(value: PropertyValue | None) -> str:
    return value.strip() if isinstance(value, str) else ""


def _person_ids(value: PropertyValue | None) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def to_link_rows(pages: list[PageDTO]) -> list[LinkRow]:
    """Link rows, in Notion order.

    Notion's queryDatabase hoists the title column to `page["title"]` and leaves every
    column in `page["properties"]`. `page["url"]` is the Notion page itself, not the
    link — the link lives in the `URL` property.
    """
    rows: list[LinkRow] = []

    for page in pages:
        props = page["properties"]
        url = _read_string(props.get("URL"))
        title = _read_string(page["title"]) or _read_string(props.get("Title"))
        if not url or not title:
            continue

        rows.append(
            LinkRow(
                title=title,
                url=url,
                visible=props.get("Visible") is not False,
                everyone=props.get("Everyone") is True,
                person_ids=_person_ids(props.get("People")),
            )
        )

    return rows


def skipped_rows(pages: list[PageDTO], people: list[PersonRow]) -> list[SkippedRow]:
    """Why a row you can see in Notion is missing from the site.

    Re-reads the raw pages so it can name rows that `to_link_rows` drops before they
    become a LinkRow.
    """
    person_ids = {person.id for person in people}
    skipped: list[SkippedRow] = []

    for page in pages:
        props = page["properties"]
        url = _read_string(props.get("URL"))
        title = _read_string(page["title"]) or _read_string(props.get("Title"))
        label = title or url or page["id"]

        if not url:
            skipped.append(SkippedRow(label, url, "no URL"))
            continue
        if not title:
            skipped.append(SkippedRow(label, url, "no Title"))
            continue
        if props.get("Visible") is False:
            skipped.append(SkippedRow(label, url, "Visible is unchecked"))
            continue
        ids = _person_ids(props.get("People"))
        if props.get("Everyone") is not True and not any(id in person_ids for id in ids):
            reason = (
                "no People relation and Everyone is unchecked"
                if not ids
                else "its People relation points at no row in the People database"
            )
            skipped.append(SkippedRow(label, url, reason))

    return skipped


def to_person_rows(pages: list[PageDTO]) -> list[PersonRow]:
    """People rows.

    A row without a name or a slug is skipped, because neither the page heading nor
    its path can be built without both.
    """
    rows: list[PersonRow] = []

    for page in pages:
        props = page["properties"]
        name = _read_string(page["title"]) or _read_string(props.get("Name"))
        slug = _read_string(props.get("Slug")).lower()
        if not name or not slug:
            continue

        rows.append(
            PersonRow(
                id=page["id"], name=name, slug=slug, tagline=_read_string(props.get("Tagline"))
            )
        )

    return rows


def group_by_person(rows: list[LinkRow], people: list[PersonRow]) -> list[PersonPage]:
    """One bundle per person.

    A link related to two people appears in both, and one with `everyone` set appears
    on every page. The two are a union, so a row with both set is redundant rather
    than contradictory.
    """
    return [
        PersonPage(
            person=person,
            rows=[row for row in rows if row.everyone or person.id in row.person_ids],
        )
        for person in people
    ]


def unique_urls(rows: list[LinkRow]) -> list[str]:
    """Distinct URLs, first-seen order. A link on three pages is fetched once."""
    return list(dict.fromkeys(row.url for row in rows))


def page_path(site_dir: str, slug: str) -> str:
    """The default person is the root page; everyone else lives under their slug."""
    return f"{site_dir}/{slug}/index.html" if slug else f"{site_dir}/index.html"


def visible_rows(rows: list[LinkRow]) -> list[LinkRow]:
    """Cards render in the order the Notion database returned them."""
    return [row for row in rows if row.visible]


def favicon_url(raw_url: str) -> str:
    """Best-effort icon. The card hides the image when this 404s."""
    parsed = parse_url(raw_url)
    if parsed is None:
        return ""
    return f"{parsed.scheme}://{parsed.authority}/favicon.ico"


def card_description(meta: Mapping[str, Any]) -> str:
    """The card blurb. Empty when the page has no meta description."""
    return _read_string(meta.get("description"))


def meta_cache_key(url: str) -> str:
    """Cache key for one URL's scraped metadata. `v1` lets a shape change invalidate."""
    return f"gl:meta:v1:{url}"
