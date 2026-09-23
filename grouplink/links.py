"""Notion rows in, page model out. Pure functions — no network, no ctx."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from render_lab_tasks_notion.types import PageDTO, PropertyValue

from grouplink.icons import DEFAULT_ICON, IconName, is_icon_name
from grouplink.jsurl import parse as parse_url
from grouplink.page import LinkCard


@dataclass(frozen=True)
class LinkRow:
    title: str
    url: str
    visible: bool
    #: Renders on every profile's page, whatever `profile_ids` holds.
    everyone: bool
    #: Notion page ids of the Profiles rows this link belongs to.
    profile_ids: list[str]
    #: Which file under site/assets/link-icons the card draws.
    icon: IconName


class CardSource(Protocol):
    """The three fields a card needs from a row, so the seed links fit too."""

    @property
    def title(self) -> str: ...

    @property
    def url(self) -> str: ...

    @property
    def icon(self) -> IconName: ...


@dataclass(frozen=True)
class ProfileRow:
    """One row of the Profiles database. `id` is what a link's relation points at."""

    id: str
    name: str
    slug: str
    tagline: str


@dataclass(frozen=True)
class ProfilePage:
    """A profile and the links that relate to it, in the order Notion returned them."""

    profile: ProfileRow
    rows: list[LinkRow]


@dataclass(frozen=True)
class SkippedRow:
    """A link row read from Notion that renders on no page, and the check it failed."""

    title: str
    url: str
    reason: str


def _read_string(value: PropertyValue | None) -> str:
    return value.strip() if isinstance(value, str) else ""


def _read_icon_option(value: PropertyValue | None) -> str:
    """Notion flattens a select property to the option name, or None when empty."""
    return _read_string(value).lower()


def to_icon_name(value: PropertyValue | None) -> IconName:
    """The icon a link row draws, whatever its Icon cell holds."""
    name = _read_icon_option(value)
    return name if is_icon_name(name) else DEFAULT_ICON


def _profile_ids(value: PropertyValue | None) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def read_link_row(page: PageDTO) -> LinkRow:
    """The link columns of one Notion page.

    Both the row builder and the skip report read rows through here, so the two cannot
    disagree about what a column means.

    Notion's queryDatabase hoists the title column to `page["title"]` and leaves every
    column in `page["properties"]`. `page["url"]` is the Notion page itself, not the
    link — the link lives in the `URL` property.
    """
    props = page["properties"]
    return LinkRow(
        title=_read_string(page["title"]) or _read_string(props.get("Title")),
        url=_read_string(props.get("URL")),
        visible=props.get("Visible") is not False,
        everyone=props.get("Everyone") is True,
        profile_ids=_profile_ids(props.get("Profiles")),
        icon=to_icon_name(props.get("Icon")),
    )


def to_link_rows(pages: list[PageDTO]) -> list[LinkRow]:
    """Every row that has both a title and a URL, in the order Notion returned them."""
    rows = [read_link_row(page) for page in pages]
    return [row for row in rows if row.url and row.title]


def _skip_reason(row: LinkRow, known_ids: frozenset[str]) -> str:
    """Why this row renders nowhere, or "" when it renders. Checks run in read order."""
    if not row.url:
        return "no URL"
    if not row.title:
        return "no Title"
    if not row.visible:
        return "Visible is unchecked"
    if row.everyone or any(profile_id in known_ids for profile_id in row.profile_ids):
        return ""
    if not row.profile_ids:
        return "no Profiles relation and Everyone is unchecked"
    return "its Profiles relation points at no row in the Profiles database"


def skipped_rows(pages: list[PageDTO], profiles: list[ProfileRow]) -> list[SkippedRow]:
    """Why a row you can see in Notion is missing from the site.

    Reads every page, so it can name the rows `to_link_rows` drops as well as the ones
    no page claims.
    """
    known_ids = frozenset(profile.id for profile in profiles)
    skipped: list[SkippedRow] = []

    for page in pages:
        row = read_link_row(page)
        reason = _skip_reason(row, known_ids)
        if reason:
            skipped.append(SkippedRow(row.title or row.url or page["id"], row.url, reason))

    return skipped


def unknown_icons(pages: list[PageDTO]) -> list[str]:
    """Icon options Notion holds that no file matches.

    Those rows render the default, so the value is otherwise invisible. Reads every
    row, including hidden ones, because an option with no file is a Notion mistake
    either way. Distinct, first-seen order.
    """
    raw = (_read_icon_option(page["properties"].get("Icon")) for page in pages)
    return list(dict.fromkeys(name for name in raw if name and not is_icon_name(name)))


def to_profile_rows(pages: list[PageDTO]) -> list[ProfileRow]:
    """Profile rows.

    A row without a name or a slug is skipped, because neither the page heading nor
    its path can be built without both.
    """
    rows: list[ProfileRow] = []

    for page in pages:
        props = page["properties"]
        name = _read_string(page["title"]) or _read_string(props.get("Name"))
        slug = _read_string(props.get("Slug")).lower()
        if not name or not slug:
            continue

        rows.append(
            ProfileRow(
                id=page["id"], name=name, slug=slug, tagline=_read_string(props.get("Tagline"))
            )
        )

    return rows


def group_by_profile(rows: list[LinkRow], profiles: list[ProfileRow]) -> list[ProfilePage]:
    """One bundle per profile.

    A link related to two profiles appears in both, and one with `everyone` set appears
    on every page. The two are a union, so a row with both set is redundant rather
    than contradictory.
    """
    return [
        ProfilePage(
            profile=profile,
            rows=[row for row in rows if row.everyone or profile.id in row.profile_ids],
        )
        for profile in profiles
    ]


def unique_urls(rows: list[LinkRow]) -> list[str]:
    """Distinct URLs, first-seen order. A link on three pages is fetched once."""
    return list(dict.fromkeys(row.url for row in rows))


def page_path(site_dir: str, slug: str) -> str:
    """The default profile is the root page; every other profile lives under its slug."""
    return f"{site_dir}/{slug}/index.html" if slug else f"{site_dir}/index.html"


def page_paths_for(site_dir: str, slug: str, default_slug: str) -> list[str]:
    """Every path one profile's page is written to.

    The default profile gets a second copy at the site root, so `/` and
    `/<default slug>` serve the same bytes.
    """
    paths = [page_path(site_dir, slug)]
    if slug == default_slug:
        paths.append(page_path(site_dir, ""))
    return paths


def assert_default_slug(profiles: list[ProfileRow], default_slug: str) -> None:
    """Raise unless one of the profiles carries the default slug.

    A slug that matches no profile would publish a site with no root page, so every
    caller that renders pages checks it before it renders anything.
    """
    if not any(profile.slug == default_slug for profile in profiles):
        raise ValueError(
            f'SITE_DEFAULT_SLUG is "{default_slug}", which matches no Slug in the Profiles database'
        )


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


def to_card(row: CardSource, description: str) -> LinkCard:
    """One card, from the Notion row and whatever description the scrape found.

    Takes the row's fields rather than a LinkRow, so the seed links in scripts/ fit
    too.
    """
    return LinkCard(
        title=row.title,
        url=row.url,
        description=description,
        icon_url=favicon_url(row.url),
        icon=row.icon,
    )


def meta_cache_key(url: str) -> str:
    """Cache key for one URL's scraped metadata. `v1` lets a shape change invalidate."""
    return f"gl:meta:v1:{url}"
