"""grouplink.rebuild — read the profiles and their links from Notion, enrich them,
write the site to Key Value for the web service, render one page each, commit the
pages that changed, deploy, and say so in Slack.

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
from render_lab_tasks_github.commit_files import commit_files
from render_lab_tasks_github.get_file_contents import get_file_contents
from render_lab_tasks_github.list_tree import list_tree
from render_lab_tasks_github.types import CommitFilesFileInput
from render_lab_tasks_http.request import request
from render_lab_tasks_notion.types import PageDTO
from render_lab_tasks_render.await_deploy import await_deploy
from render_lab_tasks_render.trigger_deploy import trigger_deploy
from render_lab_tasks_render_kv.get import get as kv_get
from render_lab_tasks_render_kv.set import set as kv_set
from render_lab_tasks_scrape.extract_metadata import extract_metadata
from render_lab_tasks_slack.post_message import post_message

from grouplink.app import app
from grouplink.batch import map_in_batches
from grouplink.config import RebuildInput, assert_writable, load_config
from grouplink.icons import DEFAULT_ICON
from grouplink.links import (
    ProfilePage,
    ProfileRow,
    SkippedRow,
    card_description,
    fetchable_urls,
    meta_cache_key,
    page_paths_for,
    reserved_profiles,
    skipped_rows,
    to_card,
    unique_urls,
    unknown_icons,
)
from grouplink.page import PageModel, render_page
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
    #: Profiles rendered. The root page is a second copy, not another page.
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
    committed: bool
    #: Paths in the commit. Empty when nothing changed.
    changedPaths: list[str]
    commitSha: str | None
    deployId: str | None
    siteUrl: str
    dryRun: bool


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

    # 6) Build the snapshot the web service serves, and render one file per profile,
    #    plus a second copy of the default profile's page at the site root, so `/` and
    #    `/<default slug>` serve the same thing.
    models = {page.profile.slug: _to_model(page, meta_by_url) for page in pages}
    snapshot = build_snapshot(models, cfg.default_slug, datetime.now(UTC))
    snapshot_hash = parse_snapshot(snapshot).hash
    files: list[CommitFilesFileInput] = []
    for slug, model in models.items():
        content = render_page(model)
        paths = page_paths_for(cfg.site_dir, slug, cfg.default_slug)
        files.extend({"path": path, "content": content} for path in paths)

    result: RebuildResult = {
        "pageCount": len(pages),
        "linkCount": len(card_urls),
        "cacheHits": len(web_urls) - len(miss_urls),
        "skipped": [_to_skipped_dto(row) for row in skipped],
        "deadLinks": dead_links,
        "hash": snapshot_hash,
        "published": False,
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

    # 7) Chained runs: read the published snapshot, and write the new one only when
    #    its hash differs. One SET, so a reader never sees half of a rebuild. No TTL,
    #    so Key Value never evicts it.
    current = await ctx.run(kv_get, {"key": SITE_KEY})
    if _snapshot_hash(current.get("value")) != snapshot_hash:
        await ctx.run(kv_set, {"key": SITE_KEY, "value": snapshot})
        result["published"] = True

    # 8) Chained run, then a batched fan-out: compare each page against what the
    #    branch already holds, so a quiet day produces no commit and no deploy.
    #    listTree comes first because getFileContents throws a 404 on a path that
    #    doesn't exist yet, and a new profile's page never does.
    repo = f"{cfg.repo_owner}/{cfg.repo_name}"
    tree = await ctx.run(list_tree, {"repo": repo, "ref": cfg.branch})
    on_branch = set(tree["paths"])
    existing = [file for file in files if file["path"] in on_branch]
    currents = await map_in_batches(
        existing,
        lambda file, _i: ctx.run(
            get_file_contents, {"repo": repo, "path": file["path"], "ref": cfg.branch}
        ),
    )
    current_by_path = {
        file["path"]: current["content"] for file, current in zip(existing, currents, strict=True)
    }

    changed = [file for file in files if current_by_path.get(file["path"]) != file["content"]]
    result["changedPaths"] = [file["path"] for file in changed]

    if not changed:
        # A quiet day is not worth a Slack message. A broken link is.
        if dead_links:
            await _notify(
                ctx, "grouplink is unchanged, but some links are unreachable.", dead_links
            )
        return result

    # 9) Chained run: every changed page in one commit, so one deploy covers them all.
    commit = await ctx.run(
        commit_files,
        {
            "owner": cfg.repo_owner,
            "repo": cfg.repo_name,
            "branch": cfg.branch,
            "message": f"chore(site): rebuild {len(changed)} page(s) ({len(card_urls)} links)",
            "files": changed,
        },
    )
    result["committed"] = True
    result["commitSha"] = commit["commitSha"]

    # 10) Chained runs: deploy the static site and wait for it to go live.
    deploy = await ctx.run(
        trigger_deploy, {"serviceId": cfg.static_site_id, "commitId": commit["commitSha"]}
    )
    result["deployId"] = deploy["deployId"]
    await ctx.run(await_deploy, {"serviceId": cfg.static_site_id, "deployId": deploy["deployId"]})

    # 11) Chained run: post the outcome.
    await _notify(
        ctx,
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
    skipped = skipped_rows(link_pages, profiles) + reserved_profiles(profiles)
    for row in skipped:
        log.info('skipped "%s": %s', row.title, row.reason)
    for name in unknown_icons(link_pages):
        log.info('unknown Icon "%s", using %s', name, DEFAULT_ICON)
    return skipped


async def _report_failure(ctx: TaskContext, error: BaseException) -> None:
    """Never masks the error it is reporting: a failed Slack post is logged and dropped."""
    try:
        await _notify(ctx, f"grouplink.rebuild failed: {error}", [])
    except Exception:
        log.exception("could not post the failure to Slack")


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


async def _notify(ctx: TaskContext, text: str, dead_links: list[str]) -> None:
    body = f"{text}\nUnreachable: {', '.join(dead_links)}" if dead_links else text
    await ctx.run(post_message, {"text": body})
