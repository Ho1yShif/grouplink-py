"""Build the snapshot from the real Notion databases and write it to a local Redis,
so a local web service serves the same pages as production.

Run with `uv run python -m scripts.preview`.

This is the read half of grouplink.rebuild: Notion and the scrape, with no metadata
cache, no health check, and no Slack post.
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv
from render_lab_tasks_scrape.extract_metadata import extract_metadata
from render_lab_test_utils import local_ctx

import grouplink.notion_relation  # noqa: F401  (imported for its side effect)
from grouplink.batch import map_in_batches
from grouplink.config import load_config
from grouplink.links import card_description, fetchable_urls, to_card, unique_urls
from grouplink.page import PageModel
from grouplink.read_notion import read_notion_site
from scripts.local_site import REPO_ROOT, write_snapshot


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    ctx = local_ctx()
    cfg = load_config({"dryRun": True})

    pages = (await read_notion_site(ctx, cfg)).pages
    # Only the http(s) cards are scraped. A mailto: card has no page, and httpx
    # refuses the URL, which is what grouplink.rebuild does too.
    card_urls = fetchable_urls(unique_urls([row for page in pages for row in page.rows]))

    scraped = await map_in_batches(
        card_urls, lambda url, _i: ctx.run(extract_metadata, {"url": url})
    )
    descriptions = {
        url: card_description(result or {}) for url, result in zip(card_urls, scraped, strict=True)
    }

    write_snapshot(
        {
            page.profile.slug: PageModel(
                name=page.profile.name,
                tagline=page.profile.tagline,
                cards=[to_card(row, descriptions.get(row.url, "")) for row in page.rows],
            )
            for page in pages
        },
        cfg.default_slug,
    )


if __name__ == "__main__":
    asyncio.run(main())
