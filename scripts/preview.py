"""Render every person's page from the real Notion databases and write them into
site/, so a local static server serves the same HTML the workflow commits.

Run with `uv run python scripts/preview.py`.

This is the read half of grouplink.rebuild: Notion and the scrape, no Key Value, no
GitHub, no deploy. It overwrites the tracked files under site/ — `git checkout --
site && git clean -fd site` puts them back.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv
from render_lab_tasks_notion.query_database import query_database
from render_lab_tasks_scrape.extract_metadata import extract_metadata
from render_lab_test_utils import local_ctx

import grouplink.notion_relation  # noqa: F401  (imported for its side effect)
from grouplink.config import load_config
from grouplink.links import (
    card_description,
    favicon_url,
    group_by_person,
    page_path,
    to_link_rows,
    to_person_rows,
    unique_urls,
    visible_rows,
)
from grouplink.page import LinkCard, PageModel, render_page

BATCH_SIZE = 10
REPO_ROOT = Path(__file__).resolve().parent.parent


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    ctx = local_ctx()
    cfg = load_config({"dryRun": True})

    link_pages, people_pages = await asyncio.gather(
        ctx.run(query_database, {"databaseId": cfg.database_id, "limit": cfg.limit}),
        ctx.run(query_database, {"databaseId": cfg.people_database_id, "limit": cfg.limit}),
    )

    people = to_person_rows(people_pages)
    if not any(person.slug == cfg.default_slug for person in people):
        raise SystemExit(
            f'SITE_DEFAULT_SLUG is "{cfg.default_slug}", which matches no Slug in People'
        )

    pages = group_by_person(visible_rows(to_link_rows(link_pages)), people)
    card_urls = unique_urls([row for page in pages for row in page.rows])

    descriptions: dict[str, str] = {}
    for start in range(0, len(card_urls), BATCH_SIZE):
        batch = card_urls[start : start + BATCH_SIZE]
        scraped = await asyncio.gather(
            *(ctx.run(extract_metadata, {"url": url}) for url in batch)
        )
        for url, result in zip(batch, scraped, strict=True):
            descriptions[url] = card_description(result or {})

    for page in pages:
        html = render_page(
            PageModel(
                name=page.person.name,
                tagline=page.person.tagline,
                cards=[
                    LinkCard(
                        title=row.title,
                        url=row.url,
                        description=descriptions.get(row.url, ""),
                        icon_url=favicon_url(row.url),
                    )
                    for row in page.rows
                ],
            )
        )

        paths = [page_path(cfg.site_dir, page.person.slug)]
        if page.person.slug == cfg.default_slug:
            paths.append(page_path(cfg.site_dir, ""))
        for path in paths:
            out = REPO_ROOT / path
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(html)
            print(f"wrote {path}")


if __name__ == "__main__":
    asyncio.run(main())
