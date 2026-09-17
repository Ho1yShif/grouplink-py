"""Live integration test. Opt-in only — gated behind RUN_LIVE=1 and real secrets.

Requires NOTION_TOKEN, NOTION_LINKS_DATABASE_ID, NOTION_PEOPLE_DATABASE_ID,
SITE_DEFAULT_SLUG, and REDIS_URL. Runs in dry-run, so it reads Notion, scrapes,
caches, and health-checks without committing or deploying anything.

Run it with `RUN_LIVE=1 uv run pytest tests/test_rebuild_live.py --enable-socket`.
"""

from __future__ import annotations

import os

import pytest
from render_lab_test_utils import local_ctx

from grouplink.app import app  # noqa: F401  (composes the packs the run dispatches)
from grouplink.rebuild import rebuild

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_LIVE"), reason="set RUN_LIVE=1 to run against the real databases"
)


async def test_reads_the_real_notion_database_and_enriches_every_link() -> None:
    assert os.environ.get("NOTION_TOKEN"), "set NOTION_TOKEN"
    assert os.environ.get("NOTION_LINKS_DATABASE_ID"), "set NOTION_LINKS_DATABASE_ID"
    assert os.environ.get("REDIS_URL"), "set REDIS_URL to a Key Value instance"

    result = await rebuild.func(local_ctx(), {"dryRun": True})

    assert result["dryRun"] is True
    assert result["committed"] is False
    assert result["linkCount"] > 0
    assert result["deadLinks"] == [], "every link should resolve"


async def test_serves_the_second_run_from_the_key_value_cache() -> None:
    assert os.environ.get("REDIS_URL"), "set REDIS_URL to a Key Value instance"

    await rebuild.func(local_ctx(), {"dryRun": True})
    second = await rebuild.func(local_ctx(), {"dryRun": True})

    assert second["cacheHits"] == second["linkCount"]
