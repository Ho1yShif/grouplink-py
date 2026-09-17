"""Entry point for grouplink-webhook, the web service Notion posts to.

It is the only thing that starts a rebuild now that the cron job is gone.

render-lab-triggers can mount webhook adapters, but an adapter's `map()` result is
dispatched immediately, so the debounce cannot live inside one. This uses the package
for the dispatcher and the rest of the app, and handles one route itself.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from render_lab_triggers import create_dispatch_server, render_dispatcher

from grouplink.config import env_int
from grouplink.notion_webhook import (
    DEFAULT_DEBOUNCE_MS,
    NotionWebhook,
    WebhookRequest,
)

log = logging.getLogger(__name__)

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

NOTION_PATH = "/webhooks/notion"


class Receiver:
    """An ASGI callable that answers POST /webhooks/notion and delegates the rest.

    GET /healthz and POST /tasks/:task come from render-lab-triggers. The task route
    is how you force a rebuild or run a dry run with custom input from the command
    line. Passing the dispatcher in means both routes start runs through the same
    client.
    """

    def __init__(self, *, workflow_slug: str, webhook: NotionWebhook) -> None:
        self._webhook = webhook
        self._server = create_dispatch_server(
            workflow_slug=workflow_slug, dispatcher=render_dispatcher(slug=workflow_slug)
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["path"] != NOTION_PATH
            or scope["method"] != "POST"
        ):
            await self._server(scope, receive, send)
            return

        headers = {
            key.decode("latin1").lower(): value.decode("latin1")
            for key, value in scope.get("headers", [])
        }
        result = self._webhook.handle(
            WebhookRequest(headers=headers, raw_body=await _read_body(receive))
        )
        await _respond(send, result.status, result.body)


async def _read_body(receive: Receive) -> str:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise ConnectionError("Client disconnected")
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks).decode("utf-8", errors="replace")


async def _respond(send: Send, status: int, body: Any) -> None:
    data = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = [(b"content-length", str(len(data)).encode())]
    if data:
        headers.append((b"content-type", b"application/json"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": data})


def build_receiver() -> Receiver:
    workflow_slug = os.environ.get("WORKFLOW_SLUG")
    if not workflow_slug:
        raise ValueError("set WORKFLOW_SLUG to the Workflow service's slug")

    dispatcher = render_dispatcher(slug=workflow_slug)
    return Receiver(
        workflow_slug=workflow_slug,
        webhook=NotionWebhook(
            dispatch=dispatcher.start,
            task=os.environ.get("REBUILD_TASK", "grouplink.rebuild"),
            secret=os.environ.get("NOTION_WEBHOOK_SECRET"),
            debounce_ms=env_int(os.environ.get("DEBOUNCE_MS"), DEFAULT_DEBOUNCE_MS),
        ),
    )


def main() -> None:
    import uvicorn

    port = env_int(os.environ.get("PORT"), 3000)
    log.info("webhook receiver listening on %s", port)
    uvicorn.run(build_receiver(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
