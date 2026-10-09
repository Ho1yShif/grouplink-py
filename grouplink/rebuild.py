"""grouplink.rebuild — read the profiles and their links from Notion, enrich them,
write the site to Key Value for the web service to serve, and log the outcome.

Every `await ctx.run(...)` below is a separate durable run on its own instance, with
that package's retry policy, tracked in the dashboard. The per-URL stages fan out
through `map_in_batches`, so the run opens at most batch.py's BATCH_SIZE at a time.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, TypedDict

from render import TaskContext
from render_lab_tasks_http.request import request
from render_lab_tasks_notion.types import PageDTO
from render_lab_tasks_render_kv.get import get as kv_get
from render_lab_tasks_render_kv.set import set as kv_set
from render_lab_tasks_scrape.extract_metadata import extract_metadata

from grouplink.app import app
from grouplink.batch import map_in_batches
from grouplink.config import RebuildInput, load_config
from grouplink.icons import DEFAULT_ICON
from grouplink.links import (
    ProfilePage,
    ProfileRow,
    SkippedRow,
    card_description,
    fetchable_urls,
    meta_cache_key,
    skipped_rows,
    to_card,
    unique_urls,
    unknown_icons,
    unservable_profiles,
)
from grouplink.page import PageModel
from grouplink.read_notion import read_notion_site
from grouplink.snapshot import SITE_KEY, build_snapshot, parse_snapshot

log = logging.getLogger(__name__)


class CachedMeta(TypedDict):
    """The subset of scrape.extractMetadata we cache and use."""

    description: str


EMPTY_META: CachedMeta = {"description": ""}

# Statuses that mean the URL resolved but refused an unadorned GET. X answers 403 and
# LinkedIn answers 999 for a request with no browser fingerprint, so neither is dead.
REFUSED_STATUSES = {401, 403, 405, 429, 999}


class SkippedRowDTO(TypedDict):
    title: str
    url: str
    reason: str


class RebuildResult(TypedDict):
    #: Profiles in the snapshot.
    pageCount: int
    #: Distinct card URLs across every page.
    linkCount: int
    cacheHits: int
    #: Link rows read from Notion that render on no page, and why.
    skipped: list[SkippedRowDTO]
    deadLinks: list[str]
    #: sha256 of the snapshot's pages and default slug.
    hash: str
    #: Whether the run wrote a new snapshot to Key Value.
    published: bool
    siteUrl: str
    dryRun: bool


@app.task(name="grouplink.rebuild")
async def rebuild(ctx: TaskContext, input: RebuildInput | None = None) -> RebuildResult:
    cfg = load_config(input or {})

    # 1) Parallel fan-out: read both databases.
    site = await read_notion_site(ctx, cfg)
    pages = site.pages

    skipped = _report_notion_problems(site.link_pages, site.profiles)

    # A link on three pages is one URL to look up, scrape, and health-check. A
    # mailto: card renders from its Notion row alone, so it is a card URL but not a
    # web URL and skips stages 2 through 5.
    rows = [row for page in pages for row in page.rows]
    card_urls = unique_urls(rows)
    web_urls = fetchable_urls(card_urls)

    # 2) Batched fan-out: look for each card's metadata in Key Value first.
    meta_by_url: dict[str, CachedMeta] = {}
    cached = await map_in_batches(
        web_urls, lambda url, _i: ctx.run(kv_get, {"key": meta_cache_key(url)})
    )
    for url, entry in zip(web_urls, cached, strict=True):
        hit = _read_cached(entry.get("value"))
        if hit is not None:
            meta_by_url[url] = hit

    # 3) Batched fan-out: scrape only the misses.
    miss_urls = [url for url in web_urls if url not in meta_by_url]
    scraped = await map_in_batches(
        miss_urls, lambda url, _i: ctx.run(extract_metadata, {"url": url})
    )
    scraped_meta: list[CachedMeta] = [
        {"description": card_description(result or {})} for result in scraped
    ]
    meta_by_url.update(zip(miss_urls, scraped_meta, strict=True))

    # 4) Batched fan-out: write the fresh metadata back with a TTL.
    await map_in_batches(
        miss_urls,
        lambda url, i: ctx.run(
            kv_set,
            {
                "key": meta_cache_key(url),
                # Separators match the JSON the TypeScript implementation writes, so
                # an entry looks the same in Key Value whichever one wrote it.
                "value": json.dumps(scraped_meta[i], separators=(",", ":")),
                "ttlSeconds": cfg.cache_ttl_seconds,
            },
        ),
    )

    # 5) Batched fan-out: health-check every link. tasks-http has no HEAD method, so
    #    this is a GET whose body we discard.
    checks = await map_in_batches(
        web_urls, lambda url, _i: ctx.run(request, {"method": "GET", "url": url})
    )
    dead_links = [
        f"{url} ({check['status'] if check else 'no response'})"
        for url, check in zip(web_urls, checks, strict=True)
        if _unreachable(check)
    ]

    # 6) Build the snapshot the web service serves: every profile's page model and
    #    the slug the root page shows.
    models = {page.profile.slug: _to_model(page, meta_by_url) for page in pages}
    snapshot = build_snapshot(models, cfg.default_slug, datetime.now(UTC))
    snapshot_hash = parse_snapshot(snapshot).hash

    result: RebuildResult = {
        "pageCount": len(pages),
        "linkCount": len(card_urls),
        "cacheHits": len(web_urls) - len(miss_urls),
        "skipped": [_to_skipped_dto(row) for row in skipped],
        "deadLinks": dead_links,
        "hash": snapshot_hash,
        "published": False,
        "siteUrl": cfg.site_url,
        "dryRun": cfg.dry_run,
    }

    # Dry-run lives here in the caller, not in the packs.
    if cfg.dry_run:
        return result

    # 7) Chained runs: read the published snapshot, and write the new one only when
    #    its hash differs. One SET, so a reader never sees half of a rebuild. No TTL,
    #    so Key Value never evicts it. The page is live when the SET returns.
    current = await ctx.run(kv_get, {"key": SITE_KEY})
    if _snapshot_hash(current.get("value")) == snapshot_hash:
        if dead_links:
            _log_outcome("grouplink is unchanged, but some links are unreachable.", dead_links)
        return result

    await ctx.run(kv_set, {"key": SITE_KEY, "value": snapshot})
    result["published"] = True

    _log_outcome(
        f"grouplink is live with {len(card_urls)} links across {len(pages)} pages. {cfg.site_url}",
        dead_links,
    )
    return result


def _report_notion_problems(
    link_pages: list[PageDTO], profiles: list[ProfileRow]
) -> list[SkippedRow]:
    """Logs what you can see in Notion but the site does not show: a row that
    reaches no page, and an Icon option no file matches. Both are otherwise silent.
    Returns the skipped rows, which the run reports as part of its result.
    """
    skipped = skipped_rows(link_pages, profiles) + unservable_profiles(profiles)
    for row in skipped:
        log.info('skipped "%s": %s', row.title, row.reason)
    for name in unknown_icons(link_pages):
        log.info('unknown Icon "%s", using %s', name, DEFAULT_ICON)
    return skipped


def _unreachable(check: Mapping[str, Any] | None) -> bool:
    if not check:
        return True
    return not check["ok"] and int(check["status"]) not in REFUSED_STATUSES


def _snapshot_hash(value: str | None) -> str | None:
    """The hash of the published snapshot. None when there is none this build can read."""
    if not value:
        return None
    try:
        return parse_snapshot(value).hash
    except ValueError:
        return None


def _read_cached(value: str | None) -> CachedMeta | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        # A malformed cache entry is a miss, not a failure.
        return None
    if isinstance(parsed, dict) and "description" in parsed:
        return {"description": str(parsed["description"] or "")}
    return None


def _to_skipped_dto(row: SkippedRow) -> SkippedRowDTO:
    return {"title": row.title, "url": row.url, "reason": row.reason}


def _to_model(page: ProfilePage, meta_by_url: dict[str, CachedMeta]) -> PageModel:
    return PageModel(
        name=page.profile.name,
        tagline=page.profile.tagline,
        cards=[
            to_card(row, meta_by_url.get(row.url, EMPTY_META)["description"]) for row in page.rows
        ],
    )


def _log_outcome(text: str, dead_links: list[str]) -> None:
    body = f"{text}\nUnreachable: {', '.join(dead_links)}" if dead_links else text
    log.info(body)
