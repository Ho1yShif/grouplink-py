"""grouplink.rebuild — read the people and their links from Notion, enrich them,
render one page each, commit the pages that changed, deploy, and say so in Slack.

Every `await ctx.run(...)` below is a separate durable run on its own instance, with
that package's retry policy, tracked in the dashboard. The per-URL stages fan out
through `_map_in_batches`, so the run opens at most BATCH_SIZE of them at a time.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypedDict, cast

from render import TaskContext
from render_lab_tasks_github.commit_files import commit_files
from render_lab_tasks_github.get_file_contents import get_file_contents
from render_lab_tasks_github.list_tree import list_tree
from render_lab_tasks_github.types import CommitFilesFileInput
from render_lab_tasks_http.request import request
from render_lab_tasks_notion.query_database import query_database
from render_lab_tasks_render.await_deploy import await_deploy
from render_lab_tasks_render.trigger_deploy import trigger_deploy
from render_lab_tasks_render_kv.get import get as kv_get
from render_lab_tasks_render_kv.set import set as kv_set
from render_lab_tasks_scrape.extract_metadata import extract_metadata
from render_lab_tasks_slack.post_message import post_message

from grouplink.app import app
from grouplink.config import RebuildInput, assert_writable, load_config
from grouplink.links import (
    LinkRow,
    PersonPage,
    SkippedRow,
    card_description,
    favicon_url,
    group_by_person,
    meta_cache_key,
    page_path,
    skipped_rows,
    to_link_rows,
    to_person_rows,
    unique_urls,
    visible_rows,
)
from grouplink.page import LinkCard, PageModel, render_page

log = logging.getLogger(__name__)


class CachedMeta(TypedDict):
    """The subset of scrape.extractMetadata we cache and use."""

    description: str


EMPTY_META: CachedMeta = {"description": ""}

# Fan-out width for the per-URL stages. Without it a 100-row database opens 100
# concurrent runs per stage and hits every linked site at once.
BATCH_SIZE = 10

# Statuses that mean the URL resolved but refused an unadorned GET. X answers 403 and
# LinkedIn answers 999 for a request with no browser fingerprint, so neither is dead.
REFUSED_STATUSES = {401, 403, 405, 429, 999}


class SkippedRowDTO(TypedDict):
    title: str
    url: str
    reason: str


class RebuildResult(TypedDict):
    #: People rendered. The root page is a second copy, not another page.
    pageCount: int
    #: Distinct card URLs across every page.
    linkCount: int
    cacheHits: int
    #: Link rows read from Notion that render on no page, and why.
    skipped: list[SkippedRowDTO]
    deadLinks: list[str]
    committed: bool
    #: Paths in the commit. Empty when nothing changed.
    changedPaths: list[str]
    commitSha: str | None
    deployId: str | None
    siteUrl: str
    dryRun: bool


class SiteFile(TypedDict):
    path: str
    content: str


@app.task(name="grouplink.rebuild")
async def rebuild(ctx: TaskContext, input: RebuildInput | None = None) -> RebuildResult:
    try:
        return await _run_rebuild(ctx, input or {})
    except Exception as error:
        # The webhook receiver only dispatches the run, so a failure would otherwise
        # show up nowhere but the dashboard.
        await _report_failure(ctx, error)
        raise


async def _run_rebuild(ctx: TaskContext, input: RebuildInput) -> RebuildResult:
    cfg = load_config(input)

    # 1) Parallel fan-out: read both databases. A link's `People` relation holds the
    #    Notion page ids of its People rows, which is how the two join.
    link_pages, people_pages = await asyncio.gather(
        ctx.run(query_database, {"databaseId": cfg.database_id, "limit": cfg.limit}),
        ctx.run(query_database, {"databaseId": cfg.people_database_id, "limit": cfg.limit}),
    )

    people = to_person_rows(people_pages)
    if not any(person.slug == cfg.default_slug for person in people):
        raise ValueError(
            f'SITE_DEFAULT_SLUG is "{cfg.default_slug}", '
            "which matches no Slug in the People database"
        )
    pages = group_by_person(visible_rows(to_link_rows(link_pages)), people)

    # A row you added in Notion that never reaches a page is otherwise invisible.
    skipped = skipped_rows(link_pages, people)
    for row in skipped:
        log.info('skipped "%s": %s', row.title, row.reason)

    # A link on three pages is one URL to look up, scrape, and health-check.
    rows = [row for page in pages for row in page.rows]
    card_urls = unique_urls(rows)

    # 2) Batched fan-out: look for each card's metadata in Key Value first.
    meta_by_url: dict[str, CachedMeta] = {}
    cached = await _map_in_batches(
        card_urls, lambda url, _i: ctx.run(kv_get, {"key": meta_cache_key(url)})
    )
    for url, entry in zip(card_urls, cached, strict=True):
        hit = _read_cached(entry.get("value"))
        if hit is not None:
            meta_by_url[url] = hit

    # 3) Batched fan-out: scrape only the misses.
    miss_urls = [url for url in card_urls if url not in meta_by_url]
    scraped = await _map_in_batches(
        miss_urls, lambda url, _i: ctx.run(extract_metadata, {"url": url})
    )
    scraped_meta: list[CachedMeta] = [
        {"description": card_description(result or {})} for result in scraped
    ]
    meta_by_url.update(zip(miss_urls, scraped_meta, strict=True))

    # 4) Batched fan-out: write the fresh metadata back with a TTL.
    await _map_in_batches(
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
    checks = await _map_in_batches(
        card_urls, lambda url, _i: ctx.run(request, {"method": "GET", "url": url})
    )
    dead_links = [
        f"{url} ({check['status'] if check else 'no response'})"
        for url, check in zip(card_urls, checks, strict=True)
        if _unreachable(check)
    ]

    # 6) Render one file per person, plus a second copy of the default person's page
    #    at the site root, so `/` and `/<default slug>` serve the same thing.
    files: list[SiteFile] = []
    for page in pages:
        content = render_page(_to_model(page, meta_by_url))
        files.append({"path": page_path(cfg.site_dir, page.person.slug), "content": content})
        if page.person.slug == cfg.default_slug:
            files.append({"path": page_path(cfg.site_dir, ""), "content": content})

    result: RebuildResult = {
        "pageCount": len(pages),
        "linkCount": len(card_urls),
        "cacheHits": len(card_urls) - len(miss_urls),
        "skipped": [_to_skipped_dto(row) for row in skipped],
        "deadLinks": dead_links,
        "committed": False,
        "changedPaths": [],
        "commitSha": None,
        "deployId": None,
        "siteUrl": cfg.site_url,
        "dryRun": cfg.dry_run,
    }

    # Dry-run lives here in the caller, not in the packs.
    if cfg.dry_run:
        return result
    assert_writable(cfg)

    # 7) Chained run, then a batched fan-out: compare each page against what the
    #    branch already holds, so a quiet day produces no commit and no deploy.
    #    listTree comes first because getFileContents throws a 404 on a path that
    #    doesn't exist yet, and a new person's page never does.
    repo = f"{cfg.repo_owner}/{cfg.repo_name}"
    tree = await ctx.run(list_tree, {"repo": repo, "ref": cfg.branch})
    on_branch = set(tree["paths"])
    existing = [file for file in files if file["path"] in on_branch]
    currents = await _map_in_batches(
        existing,
        lambda file, _i: ctx.run(
            get_file_contents, {"repo": repo, "path": file["path"], "ref": cfg.branch}
        ),
    )
    current_by_path = {
        file["path"]: current["content"]
        for file, current in zip(existing, currents, strict=True)
    }

    changed = [
        file
        for file in files
        if file["path"] not in current_by_path
        or current_by_path[file["path"]] != file["content"]
    ]
    result["changedPaths"] = [file["path"] for file in changed]

    if not changed:
        # A quiet day is not worth a Slack message. A broken link is.
        if dead_links:
            await _notify(
                ctx, "grouplink is unchanged, but some links are unreachable.", dead_links
            )
        return result

    # 8) Chained run: every changed page in one commit, so one deploy covers them all.
    commit = await ctx.run(
        commit_files,
        {
            "owner": cfg.repo_owner,
            "repo": cfg.repo_name,
            "branch": cfg.branch,
            "message": (
                f"chore(site): rebuild {len(changed)} page(s) ({len(card_urls)} links)"
            ),
            "files": cast(list[CommitFilesFileInput], changed),
        },
    )
    result["committed"] = True
    result["commitSha"] = commit["commitSha"]

    # 9) Chained runs: deploy the static site and wait for it to go live.
    deploy = await ctx.run(
        trigger_deploy, {"serviceId": cfg.static_site_id, "commitId": commit["commitSha"]}
    )
    result["deployId"] = deploy["deployId"]
    await ctx.run(
        await_deploy, {"serviceId": cfg.static_site_id, "deployId": deploy["deployId"]}
    )

    # 10) Chained run: post the outcome.
    await _notify(
        ctx,
        f"grouplink is live with {len(card_urls)} links across {len(pages)} pages. "
        f"{cfg.site_url}",
        dead_links,
    )
    return result


async def _report_failure(ctx: TaskContext, error: BaseException) -> None:
    """Never masks the error it is reporting: a failed Slack post is logged and dropped."""
    try:
        await _notify(ctx, f"grouplink.rebuild failed: {error}", [])
    except Exception:
        log.exception("could not post the failure to Slack")


async def _map_in_batches[T, R](
    items: list[T], fn: Callable[[T, int], Awaitable[R]]
) -> list[R]:
    """asyncio.gather in fixed-size batches, in input order."""
    results: list[R] = []
    for start in range(0, len(items), BATCH_SIZE):
        batch = items[start : start + BATCH_SIZE]
        results.extend(
            await asyncio.gather(*(fn(item, start + i) for i, item in enumerate(batch)))
        )
    return results


def _unreachable(check: Mapping[str, Any] | None) -> bool:
    if not check:
        return True
    return not check["ok"] and int(check["status"]) not in REFUSED_STATUSES


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


def _to_model(page: PersonPage, meta_by_url: dict[str, CachedMeta]) -> PageModel:
    return PageModel(
        name=page.person.name,
        tagline=page.person.tagline,
        cards=[_to_card(row, meta_by_url.get(row.url)) for row in page.rows],
    )


def _to_card(row: LinkRow, meta: CachedMeta | None) -> LinkCard:
    return LinkCard(
        title=row.title,
        url=row.url,
        description=(meta or EMPTY_META)["description"],
        icon_url=favicon_url(row.url),
    )


async def _notify(ctx: TaskContext, text: str, dead_links: list[str]) -> None:
    body = f"{text}\nUnreachable: {', '.join(dead_links)}" if dead_links else text
    await ctx.run(post_message, {"text": body})

