"""Read both Notion databases and build each profile's page model.

Shared so the workflow and the preview script query and order the rows the same way.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from render import TaskContext
from render_lab_tasks_notion.query_database import query_database
from render_lab_tasks_notion.types import PageDTO

from grouplink.config import RebuildConfig
from grouplink.links import (
    LINK_SORTS,
    ProfilePage,
    ProfileRow,
    assert_default_slug,
    group_by_profile,
    to_link_rows,
    to_profile_rows,
    unnumbered_last,
    visible_rows,
)


@dataclass(frozen=True)
class NotionSite:
    """What one read of the two Notion databases gives the rebuild and the preview."""

    #: The raw link rows, which the skip report reads.
    link_pages: list[PageDTO]
    profiles: list[ProfileRow]
    #: One page per profile. Each page holds its visible rows, in `Order`.
    pages: list[ProfilePage]


async def read_notion_site(ctx: TaskContext, cfg: RebuildConfig) -> NotionSite:
    """Two runs in parallel.

    A link's `Profiles` relation holds the Notion page ids of its Profiles rows, which
    is how the two join. Notion sorts the links by `Order`. The profiles need no sort.
    """
    link_pages, profile_pages = await asyncio.gather(
        ctx.run(
            query_database,
            {"databaseId": cfg.database_id, "sorts": LINK_SORTS, "limit": cfg.limit},
        ),
        ctx.run(query_database, {"databaseId": cfg.profiles_database_id, "limit": cfg.limit}),
    )

    profiles = to_profile_rows(profile_pages)
    assert_default_slug(profiles, cfg.default_slug)
    pages = group_by_profile(visible_rows(unnumbered_last(to_link_rows(link_pages))), profiles)

    return NotionSite(link_pages=link_pages, profiles=profiles, pages=pages)
