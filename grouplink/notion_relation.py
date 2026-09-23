"""Teach render-lab-tasks-notion 0.1.0 to simplify a relation property.

`simplify_property` in the published pack handles title, rich_text, checkbox,
select, status, multi_select, date, people, number, url, email, phone_number,
created_time, and last_edited_time, and returns None for everything else. A link
row's `Profiles` relation therefore arrives as None, and every link row without
`Everyone` checked renders on no page.

This is the gap render-lab/render-tasks#28 fixed for TypeScript. Delete this module
once the same fix ships in a render-lab-tasks-notion release, and drop the import
from grouplink/main.py, grouplink/rebuild.py, and scripts/preview.py.

Replacing the module attribute is enough: `page()` looks `simplify_property` up as a
module global on every property it reads.
"""

from __future__ import annotations

from typing import Any

import render_lab_tasks_notion.client as notion_client
from render_lab_tasks_notion.types import PropertyValue

_base = notion_client.simplify_property


def _with_relation(prop: Any) -> PropertyValue:
    if prop.get("type") == "relation":
        return [item["id"] for item in prop.get("relation") or []]
    return _base(prop)


def install() -> None:
    """Idempotent, so importing this from more than one entry point is safe."""
    notion_client.simplify_property = _with_relation


install()
