"""The published site: every profile's page model in one JSON document.

grouplink.rebuild writes it to Key Value under SITE_KEY, and the web service reads
it on each request and renders it with page.py. The two deploy separately, so the
document carries a schema number, and a reader refuses a schema it does not know.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from grouplink.icons import DEFAULT_ICON, is_icon_name
from grouplink.page import LinkCard, PageModel

#: One key for the whole site, so a reader never sees half of a rebuild.
SITE_KEY = "grouplink:site"

#: Increase this when the shape changes in a way an older reader cannot read.
SCHEMA = 1


@dataclass(frozen=True)
class Snapshot:
    #: sha256 of the pages and the default slug. The build time is not part of it.
    hash: str
    built_at: str
    default_slug: str
    #: Slug -> page, in Notion's profile order.
    pages: dict[str, PageModel]


def build_snapshot(pages: Mapping[str, PageModel], default_slug: str, built_at: datetime) -> str:
    """The JSON document for one rebuild. `pages` maps each slug to its page."""
    content = {
        "defaultSlug": default_slug,
        "pages": [_page_to_json(slug, page) for slug, page in pages.items()],
    }
    document = {
        "schema": SCHEMA,
        "hash": _hash(content),
        "builtAt": built_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **content,
    }
    return json.dumps(document, separators=(",", ":"))


def parse_snapshot(raw: str) -> Snapshot:
    """Raises ValueError for anything but a snapshot this build can serve."""
    try:
        doc = json.loads(raw)
    except ValueError as error:
        raise ValueError(f"snapshot is not JSON: {error}") from error
    if not isinstance(doc, dict):
        raise ValueError("snapshot is not a JSON object")
    if doc.get("schema") != SCHEMA:
        raise ValueError(f"snapshot schema {doc.get('schema')!r} is not {SCHEMA}")

    raw_pages = doc.get("pages")
    if not isinstance(raw_pages, list):
        raise ValueError("snapshot pages is not a list")
    pages = dict(_page_from_json(page) for page in raw_pages)

    default_slug = _str(doc, "defaultSlug")
    if default_slug not in pages:
        raise ValueError(f'snapshot defaultSlug "{default_slug}" has no page')

    return Snapshot(
        hash=_str(doc, "hash"),
        built_at=_str(doc, "builtAt"),
        default_slug=default_slug,
        pages=pages,
    )


def _hash(content: Mapping[str, Any]) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _page_to_json(slug: str, page: PageModel) -> dict[str, Any]:
    return {
        "slug": slug,
        "name": page.name,
        "tagline": page.tagline,
        "cards": [
            {
                "title": card.title,
                "url": card.url,
                "description": card.description,
                "iconUrl": card.icon_url,
                "icon": card.icon,
            }
            for card in page.cards
        ],
    }


def _page_from_json(page: Any) -> tuple[str, PageModel]:
    if not isinstance(page, dict):
        raise ValueError("snapshot page is not a JSON object")
    cards = page.get("cards")
    if not isinstance(cards, list):
        raise ValueError("snapshot page cards is not a list")
    return _str(page, "slug"), PageModel(
        name=_str(page, "name"),
        tagline=_str(page, "tagline"),
        cards=[_card_from_json(card) for card in cards],
    )


def _card_from_json(card: Any) -> LinkCard:
    if not isinstance(card, dict):
        raise ValueError("snapshot card is not a JSON object")
    # A Workflow deployed ahead of the web service can name an icon this build does
    # not ship. The card draws the default, as a Notion row with that icon would.
    icon = _str(card, "icon")
    return LinkCard(
        title=_str(card, "title"),
        url=_str(card, "url"),
        description=_str(card, "description"),
        icon_url=_str(card, "iconUrl"),
        icon=icon if is_icon_name(icon) else DEFAULT_ICON,
    )


def _str(obj: Mapping[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise ValueError(f"snapshot field {key} is not a string")
    return value
