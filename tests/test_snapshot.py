"""The snapshot the Workflow writes to Key Value and the web service reads back."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from grouplink.page import LinkCard, PageModel
from grouplink.snapshot import SCHEMA, build_snapshot, parse_snapshot

BUILT_AT = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)

SHIFRA = PageModel(
    name="Shifra Williams",
    tagline="Developer relations at Render.",
    cards=[
        LinkCard(
            title="Docs",
            url="https://render.com/docs",
            description="Guides",
            icon_url="https://render.com/favicon.ico",
            icon="info",
        )
    ],
)
ALEX = PageModel(name="Alex Rivera", tagline="Engineer at Render.", cards=[])


def build(**overrides: object) -> str:
    args: dict[str, object] = {
        "pages": {"shifra": SHIFRA, "alex": ALEX},
        "default_slug": "shifra",
        "built_at": BUILT_AT,
    }
    args.update(overrides)
    return build_snapshot(**args)


def test_writes_the_documented_shape() -> None:
    doc = json.loads(build())

    assert doc["schema"] == SCHEMA == 1
    assert doc["builtAt"] == "2026-09-30T18:00:00Z"
    assert doc["defaultSlug"] == "shifra"
    assert doc["pages"][0] == {
        "slug": "shifra",
        "name": "Shifra Williams",
        "tagline": "Developer relations at Render.",
        "cards": [
            {
                "title": "Docs",
                "url": "https://render.com/docs",
                "description": "Guides",
                "iconUrl": "https://render.com/favicon.ico",
                "icon": "info",
            }
        ],
    }
    assert len(doc["hash"]) == 64


def test_reads_back_what_it_wrote() -> None:
    snapshot = parse_snapshot(build())

    assert snapshot.default_slug == "shifra"
    assert list(snapshot.pages) == ["shifra", "alex"]
    assert snapshot.pages["shifra"] == SHIFRA
    assert snapshot.pages["alex"] == ALEX


def test_hash_ignores_the_build_time() -> None:
    later = datetime(2027, 1, 1, tzinfo=UTC)
    assert parse_snapshot(build()).hash == parse_snapshot(build(built_at=later)).hash


def test_hash_changes_with_the_pages_or_the_default_slug() -> None:
    base = parse_snapshot(build()).hash
    assert parse_snapshot(build(pages={"shifra": SHIFRA})).hash != base
    assert parse_snapshot(build(default_slug="alex")).hash != base


def test_refuses_a_schema_it_does_not_know() -> None:
    doc = json.loads(build())
    doc["schema"] = 2
    with pytest.raises(ValueError, match="schema"):
        parse_snapshot(json.dumps(doc))


def test_refuses_a_value_that_is_not_a_snapshot() -> None:
    for raw in ("not json", "[]", json.dumps({"schema": 1}), json.dumps({"schema": 1, "pages": 3})):
        with pytest.raises(ValueError):
            parse_snapshot(raw)


def test_refuses_a_default_slug_with_no_page() -> None:
    with pytest.raises(ValueError, match="defaultSlug"):
        parse_snapshot(build(default_slug="nobody"))


def test_draws_the_default_icon_for_an_icon_this_build_does_not_know() -> None:
    doc = json.loads(build())
    doc["pages"][0]["cards"][0]["icon"] = "rocket"
    snapshot = parse_snapshot(json.dumps(doc))
    assert snapshot.pages["shifra"].cards[0].icon == "arrow"
