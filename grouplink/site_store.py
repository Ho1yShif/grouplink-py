"""The web service's copy of the published site.

Reads the snapshot from Key Value on each request and keeps the last good copy in
memory, so a Key Value outage or a snapshot this build cannot read does not take
the page down. Renders each page once per snapshot.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date

from grouplink.page import render_page
from grouplink.snapshot import Snapshot, parse_snapshot

log = logging.getLogger(__name__)

#: Seconds before a key that is still missing starts another rebuild. The first run
#: can fail or be a dry run, and nothing else would start one.
REBUILD_RETRY_SECONDS = 600


@dataclass(frozen=True)
class RenderedPage:
    html: str
    #: Quoted, ready for the ETag header. Hash of the HTML, so a page.py change or
    #: a new footer year changes it even when the snapshot does not.
    etag: str


class SiteStore:
    def __init__(
        self,
        *,
        read: Callable[[], Awaitable[str | None]],
        rebuild: Callable[[], Awaitable[object]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`read` returns the raw snapshot, or None when the key is missing.
        `rebuild` starts one grouplink.rebuild run.
        """
        self._read = read
        self._rebuild = rebuild
        self._last_good: Snapshot | None = None
        self._clock = clock
        #: Clock time of the last rebuild this store started. None when there is none.
        self._rebuild_started_at: float | None = None
        #: Kept so the task is not garbage collected before it finishes.
        self._rebuild_task: asyncio.Task[None] | None = None
        #: Number of reads started, and the number of the read that set _last_good.
        #: A slow read that finishes after a newer one must not replace its result.
        self._reads = 0
        self._last_good_read = 0
        #: (snapshot hash, slug, footer year) -> page. Holds one snapshot's pages.
        self._rendered: dict[tuple[str, str, int], RenderedPage] = {}

    async def snapshot(self) -> Snapshot | None:
        """The current snapshot, or the last good one. None when there is neither."""
        self._reads += 1
        read_number = self._reads
        try:
            raw = await self._read()
        except Exception as error:
            log.error("could not read the site from Key Value: %s", error)
            return self._last_good

        if raw is None:
            self._start_rebuild()
            return self._last_good

        try:
            snapshot = parse_snapshot(raw)
        except ValueError as error:
            log.error("could not read the site snapshot: %s", error)
            return self._last_good

        if read_number > self._last_good_read:
            if self._last_good is None or self._last_good.hash != snapshot.hash:
                self._rendered.clear()
            self._last_good = snapshot
            self._last_good_read = read_number
        self._rebuild_started_at = None
        return snapshot

    def page(self, snapshot: Snapshot, slug: str) -> RenderedPage | None:
        """The rendered page for `slug`, or None when the snapshot has no such page."""
        model = snapshot.pages.get(slug)
        if model is None:
            return None

        year = date.today().year
        key = (snapshot.hash, slug, year)
        page = self._rendered.get(key)
        if page is None:
            html = render_page(model, year)
            page = RenderedPage(html=html, etag=f'"{hashlib.sha256(html.encode()).hexdigest()}"')
            self._rendered[key] = page
        return page

    def _start_rebuild(self) -> None:
        """One run per missing key every REBUILD_RETRY_SECONDS. A new Key Value
        instance, or a lost key, starts the rebuild that writes it. The request does
        not wait for the Render API call.
        """
        now = self._clock()
        started_at = self._rebuild_started_at
        if started_at is not None and now - started_at < REBUILD_RETRY_SECONDS:
            return
        self._rebuild_started_at = now
        self._rebuild_task = asyncio.create_task(self._run_rebuild())

    async def _run_rebuild(self) -> None:
        try:
            await self._rebuild()
            log.info("the site key is missing, so a rebuild started")
        except Exception as error:
            # Let the next request try again.
            self._rebuild_started_at = None
            log.error("could not start a rebuild for the missing site key: %s", error)
