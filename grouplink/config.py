"""Config resolution: per-run input overrides, then env, then defaults.

Same shape as the render-tasks examples — the workflow task reads nothing from
os.environ directly.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NotRequired, TypedDict


class RebuildInput(TypedDict):
    databaseId: NotRequired[str]
    peopleDatabaseId: NotRequired[str]
    dryRun: NotRequired[bool]
    limit: NotRequired[int]


@dataclass(frozen=True)
class RebuildConfig:
    #: Notion database holding the link rows.
    database_id: str
    #: Notion database holding one row per person: Name, Slug, Tagline.
    people_database_id: str
    limit: int
    #: Skips the commit, the deploy, and the Slack post.
    dry_run: bool

    #: Slug of the person the root page renders. Their page is written twice.
    default_slug: str

    #: Seconds a scraped metadata record stays in Key Value.
    cache_ttl_seconds: int

    repo_owner: str
    repo_name: str
    branch: str
    #: Directory the pages are committed under, without a trailing slash.
    site_dir: str

    static_site_id: str
    site_url: str


FALSY = {"false", "0", "no", "off"}


def env_flag(value: str | None, fallback: bool) -> bool:
    """Case-insensitive, because DRY_RUN guards the commit, the deploy, and the Slack
    post — reading `False` as true would publish a run the operator meant to hold.
    """
    normalized = (value or "").strip().lower()
    if normalized == "":
        return fallback
    return normalized not in FALSY


def env_int(value: str | None, fallback: int) -> int:
    """Shared with the webhook receiver, which parses its own DEBOUNCE_MS and PORT."""
    try:
        return int((value or "").strip())
    except ValueError:
        return fallback


def load_config(
    input: RebuildInput | None = None, env: Mapping[str, str] | None = None
) -> RebuildConfig:
    run_input: RebuildInput = input or {}
    environ = os.environ if env is None else env

    database_id = run_input.get("databaseId") or environ.get("NOTION_LINKS_DATABASE_ID", "")
    if not database_id:
        raise ValueError("set NOTION_LINKS_DATABASE_ID, or pass databaseId in the run input")

    people_database_id = run_input.get("peopleDatabaseId") or environ.get(
        "NOTION_PEOPLE_DATABASE_ID", ""
    )
    if not people_database_id:
        raise ValueError(
            "set NOTION_PEOPLE_DATABASE_ID, or pass peopleDatabaseId in the run input"
        )

    # Required, because an unset value would silently publish a site with no root page.
    default_slug = environ.get("SITE_DEFAULT_SLUG", "").strip().lower()
    if not default_slug:
        raise ValueError("set SITE_DEFAULT_SLUG to the slug of the person the root page shows")

    dry_run = run_input.get("dryRun")

    return RebuildConfig(
        database_id=database_id,
        people_database_id=people_database_id,
        limit=run_input.get("limit") or env_int(environ.get("LINKS_LIMIT"), 100),
        dry_run=env_flag(environ.get("DRY_RUN"), False) if dry_run is None else dry_run,
        default_slug=default_slug,
        cache_ttl_seconds=env_int(environ.get("METADATA_TTL_SECONDS"), 86_400),
        repo_owner=environ.get("GITHUB_REPO_OWNER", ""),
        repo_name=environ.get("GITHUB_REPO_NAME", ""),
        branch=environ.get("GITHUB_BRANCH", "main"),
        site_dir=environ.get("SITE_DIR", "site").rstrip("/"),
        static_site_id=environ.get("RENDER_STATIC_SITE_ID", ""),
        site_url=environ.get("SITE_URL", ""),
    )


def assert_writable(cfg: RebuildConfig) -> None:
    """The write path needs more than the read path does.

    Called only when the run is about to commit, so a dry run works with just a Notion
    token and a Key Value URL.
    """
    missing = [
        name
        for name, value in (
            ("GITHUB_REPO_OWNER", cfg.repo_owner),
            ("GITHUB_REPO_NAME", cfg.repo_name),
            ("RENDER_STATIC_SITE_ID", cfg.static_site_id),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"set {', '.join(missing)}, or run with dryRun: true")
