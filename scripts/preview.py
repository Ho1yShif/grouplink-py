"""Render every profile's page from the real Notion databases and write them into
site/, so a local static server serves the same HTML the workflow commits.

Run with `uv run python -m scripts.preview`.

This is the read half of grouplink.rebuild: Notion and the scrape, no Key Value, no
GitHub, no deploy. It overwrites the tracked files under site/ — `git checkout --
site && git clean -fd site` puts them back.
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv
from render_lab_tasks_scrape.extract_metadata import extract_metadata
from render_lab_test_utils import local_ctx

import grouplink.notion_relation  # noqa: F401  (imported for its side effect)
from grouplink.batch import map_in_batches
from grouplink.config import load_config
from grouplink.links import card_description, to_card, unique_urls
from grouplink.page import PageModel
from grouplink.read_notion import read_notion_site
from scripts.write_pages import REPO_ROOT, write_pages


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    ctx = local_ctx()
    cfg = load_config({"dryRun": True})

    pages = (await read_notion_site(ctx, cfg)).pages
    card_urls = unique_urls([row for page in pages for row in page.rows])

    scraped = await map_in_batches(
        card_urls, lambda url, _i: ctx.run(extract_metadata, {"url": url})
    )
    descriptions = {
        url: card_description(result or {}) for url, result in zip(card_urls, scraped, strict=True)
    }

    for page in pages:
        write_pages(
            PageModel(
                name=page.profile.name,
                tagline=page.profile.tagline,
                cards=[to_card(row, descriptions.get(row.url, "")) for row in page.rows],
            ),
            site_dir=cfg.site_dir,
            slug=page.profile.slug,
            default_slug=cfg.default_slug,
        )


if __name__ == "__main__":
    asyncio.run(main())
