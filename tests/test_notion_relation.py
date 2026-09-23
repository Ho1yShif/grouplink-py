"""The shim is what makes a link row's Profiles relation reach the page model."""

from __future__ import annotations

import render_lab_tasks_notion.client as notion_client

import grouplink.notion_relation  # noqa: F401  (imported for its side effect)


def test_a_relation_property_simplifies_to_its_page_ids() -> None:
    prop = {"type": "relation", "relation": [{"id": "profile-shifra"}, {"id": "profile-alex"}]}
    assert notion_client.simplify_property(prop) == ["profile-shifra", "profile-alex"]


def test_an_empty_relation_simplifies_to_an_empty_list() -> None:
    assert notion_client.simplify_property({"type": "relation", "relation": None}) == []


def test_every_other_property_type_is_left_to_the_pack() -> None:
    assert notion_client.simplify_property({"type": "checkbox", "checkbox": True}) is True
    assert notion_client.simplify_property({"type": "url", "url": "https://a.example"}) == (
        "https://a.example"
    )


def test_a_page_carries_the_relation_into_its_properties() -> None:
    raw = {
        "id": "p-1",
        "properties": {
            "Profiles": {"type": "relation", "relation": [{"id": "profile-shifra"}]},
        },
    }
    assert notion_client.page(raw)["properties"]["Profiles"] == ["profile-shifra"]
