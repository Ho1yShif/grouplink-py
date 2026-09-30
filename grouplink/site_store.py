"""The web service's copy of the published site.

Reads the snapshot from Key Value on each request and keeps the last good copy in
memory, so a Key Value outage or a snapshot this build cannot read does not take
the page down. Renders each page once per snapshot.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date

from grouplink.page import render_page
from grouplink.snapshot import Snapshot, parse_snapshot

log = logging.getLogger(__name__)


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
    ) -> None:
        """`read` returns the raw snapshot, or None when the key is missing.
        `rebuild` starts one grouplink.rebuild run.
        """
        self._read = read
        self._rebuild = rebuild
        self._last_good: Snapshot | None = None
        self._rebuild_started = False
        #: (snapshot hash, slug, footer year) -> page. Holds one snapshot's pages.
        self._rendered: dict[tuple[str, str, int], RenderedPage] = {}

    async def snapshot(self) -> Snapshot | None:
        """The current snapshot, or the last good one. None when there is neither."""
        try:
            raw = await self._read()
        except Exception as error:
            log.error("could not read the site from Key Value: %s", error)
            return self._last_good

        if raw is None:
            await self._start_rebuild()
            return self._last_good

        try:
            snapshot = parse_snapshot(raw)
        except ValueError as error:
            log.error("could not read the site snapshot: %s", error)
            return self._last_good

        if self._last_good is None or self._last_good.hash != snapshot.hash:
            self._rendered.clear()
        self._last_good = snapshot
        self._rebuild_started = False
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

    async def _start_rebuild(self) -> None:
        """One run per missing key. A new Key Value instance, or a lost key, starts
        the rebuild that writes it.
        """
        if self._rebuild_started:
            return
        self._rebuild_started = True
        try:
            await self._rebuild()
            log.info("the site key is missing, so a rebuild started")
        except Exception as error:
            # Let the next request try again.
            self._rebuild_started = False
            log.error("could not start a rebuild for the missing site key: %s", error)
