"""Shared by scripts/placeholder.py and scripts/preview.py: write a snapshot to a
local Redis, where `python -m grouplink.webhook` serves it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import redis

from grouplink.page import PageModel
from grouplink.snapshot import SITE_KEY, build_snapshot

REPO_ROOT = Path(__file__).resolve().parent.parent

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def write_snapshot(pages: dict[str, PageModel], default_slug: str) -> None:
    """Writes to REDIS_URL, or to redis://localhost:6379 when it is unset.

    Refuses a Redis that is not on this machine, because the key is the live site
    on a Render Key Value instance.
    """
    url = os.environ.get("REDIS_URL") or "redis://localhost:6379"
    if urlparse(url).hostname not in LOCAL_HOSTS:
        raise ValueError(f"REDIS_URL must point at a local Redis for a preview, got {url}")

    redis.Redis.from_url(url).set(SITE_KEY, build_snapshot(pages, default_slug, datetime.now(UTC)))
    print(f"wrote {SITE_KEY} with {len(pages)} page(s) to {url}")
    print(f"serve it with: REDIS_URL={url} WORKFLOW_SLUG=local uv run python -m grouplink.webhook")
