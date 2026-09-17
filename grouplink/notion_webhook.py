"""The Notion webhook receiver, minus the HTTP.

Handling a request is verify, filter, debounce, dispatch, and none of those steps
needs a server or the Render SDK, so the tests need neither.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: Starts a workflow run. `RenderDispatcher.start` satisfies this.
Dispatch = Callable[[str, list[Any]], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class WebhookRequest:
    """A request as the handler sees it: lowercased headers and the unparsed body."""

    headers: Mapping[str, str]
    raw_body: str


@dataclass(frozen=True)
class WebhookResponse:
    """What the caller should answer. `body` is None for a 204."""

    status: int
    body: Any = None


# Event types that mean a page the site renders may have changed. Notion sends many
# more, including comment and workspace events.
REBUILD_EVENTS = frozenset(
    {
        "page.created",
        "page.deleted",
        "page.undeleted",
        "page.properties_updated",
        "page.content_updated",
        "data_source.content_updated",
        "data_source.schema_updated",
    }
)

DEFAULT_DEBOUNCE_MS = 60_000


def verify_signature(raw_body: str, header: str | None, secret: str) -> bool:
    """Notion signs the raw body with HMAC-SHA256 keyed by the subscription's
    verification token.
    """
    if not header:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body.encode("utf-8"), hashlib.sha256)
    return hmac.compare_digest(f"sha256={digest.hexdigest()}", header)


def _string_field(body: Any, key: str) -> str:
    """A top-level string field of the parsed body, or "" if it is absent."""
    if not isinstance(body, dict):
        return ""
    value = body.get(key)
    return value if isinstance(value, str) else ""


class NotionWebhook:
    """One pending dispatch lives on the instance, so eight edits in one sitting
    collapse into one run and one health-check pass over every link.

    There is no filter on database ID. Under Notion API version 2025-09-03 an event's
    `data.parent.id` is a data source ID rather than the database ID in
    NOTION_LINKS_DATABASE_ID, so an ID filter would drop every event. The integration
    is shared with only the two databases, and the debounce absorbs whatever else
    arrives.
    """

    def __init__(
        self,
        *,
        dispatch: Dispatch,
        task: str,
        secret: str | None = None,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
    ) -> None:
        self._dispatch = dispatch
        self._task = task
        self._secret = secret
        self._debounce_ms = debounce_ms
        self._pending: asyncio.Task[None] | None = None

    def _schedule(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
        self._pending = asyncio.create_task(self._dispatch_after_quiet())

    async def _dispatch_after_quiet(self) -> None:
        try:
            await asyncio.sleep(self._debounce_ms / 1000)
        except asyncio.CancelledError:
            return
        self._pending = None
        try:
            started = await self._dispatch(self._task, [{}])
            log.info("dispatched %s (%s)", self._task, started.get("runId"))
        except Exception as error:
            log.error("dispatch failed: %s", error)

    async def drain(self) -> None:
        """Await the pending dispatch, if there is one. For tests and shutdown."""
        pending = self._pending
        if pending is not None:
            try:
                await pending
            except asyncio.CancelledError:
                pass

    def handle(self, request: WebhookRequest) -> WebhookResponse:
        # Creating a subscription makes Notion POST the verification token once, with
        # no signature and before there is a secret to check it against. So an
        # unsigned body is accepted while the secret is unset, and never after.
        if self._secret and not verify_signature(
            request.raw_body, request.headers.get("x-notion-signature"), self._secret
        ):
            return WebhookResponse(401, {"error": "bad signature"})

        try:
            body = json.loads(request.raw_body)
        except ValueError:
            return WebhookResponse(400, {"error": "bad JSON"})

        token = _string_field(body, "verification_token")
        if token:
            log.info("notion verification_token: %s", token)
            return WebhookResponse(200, {"ok": True})

        if _string_field(body, "type") not in REBUILD_EVENTS:
            return WebhookResponse(204)

        self._schedule()
        # Not a run ID: the run does not exist yet, and will not for debounce_ms. A
        # restart inside that window drops the pending dispatch, and Notion's retries
        # do not cover it, because this answer was already a success.
        return WebhookResponse(202, {"scheduled": True})
