"""Entry point for grouplink-webhook-py, the web service that serves the page and
receives Notion's webhook.

It serves each profile's page from the snapshot grouplink.rebuild writes to Key
Value, and it is the only thing that starts a rebuild.

render-lab-triggers can mount webhook adapters, but an adapter's `map()` result is
dispatched immediately, so the debounce cannot live inside one. This uses the package
for the dispatcher and the task route, and handles the rest itself.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from redis.asyncio import Redis
from render_lab_triggers import create_dispatch_server, render_dispatcher
from render_lab_triggers.types import WorkflowDispatcher
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from grouplink.config import env_int
from grouplink.logs import configure as configure_logging
from grouplink.notion_webhook import (
    DEFAULT_DEBOUNCE_MS,
    NotionWebhook,
    WebhookRequest,
)
from grouplink.site_store import SiteStore
from grouplink.snapshot import SITE_KEY

log = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent / "assets"

NOT_FOUND_HTML = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    "<title>Not found</title></head>"
    '<body><p>There is no page here. <a href="/">Go to the links page</a>.</p></body></html>'
)

#: Largest Notion webhook body the service reads. The same limit as the
#: render-lab-triggers task route.
MAX_BODY_BYTES = 1_048_576

#: Seconds a client waits before it asks again while there is no site to serve.
RETRY_AFTER_SECONDS = 60


class ResponseHeaders:
    """Security headers on every response, plus a one-year cache on the fonts."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                if message["status"] == 200 and scope["path"].startswith("/assets/fonts/"):
                    headers["Cache-Control"] = "public, max-age=31536000, immutable"
            await send(message)

        await self._app(scope, receive, send_with_headers)


def create_app(
    *,
    workflow_slug: str,
    dispatcher: WorkflowDispatcher,
    webhook: NotionWebhook,
    store: SiteStore,
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
) -> Starlette:
    """POST /tasks/:task comes from render-lab-triggers. It is how you force a
    rebuild or run a dry run with custom input from the command line. Passing the
    dispatcher in means every route starts runs through the same client.
    """
    dispatch_server = create_dispatch_server(workflow_slug=workflow_slug, dispatcher=dispatcher)

    async def page(request: Request) -> Response:
        snapshot = await store.snapshot()
        if snapshot is None:
            return PlainTextResponse(
                "The page is being built. Try again in a minute.",
                status_code=503,
                headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            )

        slug = request.path_params.get("slug", snapshot.default_slug)
        rendered = store.page(snapshot, slug)
        if rendered is None:
            return HTMLResponse(NOT_FOUND_HTML, status_code=404)

        # no-cache makes the browser revalidate, which the ETag makes cheap.
        headers = {"ETag": rendered.etag, "Cache-Control": "no-cache"}
        if _etag_matches(request.headers.get("if-none-match", ""), rendered.etag):
            return Response(status_code=304, headers=headers)
        return HTMLResponse(rendered.html, headers=headers)

    async def healthz(_request: Request) -> Response:
        return PlainTextResponse("ok")

    async def notion(request: Request) -> Response:
        body = await _read_body(request, MAX_BODY_BYTES)
        if body is None:
            return JSONResponse({"error": "payload too large"}, status_code=413)
        result = webhook.handle(
            WebhookRequest(
                headers={key.lower(): value for key, value in request.headers.items()},
                raw_body=body.decode("utf-8", errors="replace"),
            )
        )
        if result.body is None:
            return Response(status_code=result.status)
        return JSONResponse(result.body, status_code=result.status)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        yield
        # Render stops the old instance on every deploy. Start the waiting run now,
        # or the edit that scheduled it is lost.
        await webhook.flush()
        if on_shutdown is not None:
            await on_shutdown()

    return Starlette(
        routes=[
            Route("/", page, methods=["GET"]),
            Route("/healthz", healthz, methods=["GET"]),
            Route("/tasks/{task}", dispatch_server, methods=["POST"]),
            Route("/webhooks/notion", notion, methods=["POST"]),
            Mount("/assets", StaticFiles(directory=ASSETS_DIR)),
            Route("/{slug}", page, methods=["GET"]),
            Route("/{slug}/", page, methods=["GET"]),
        ],
        middleware=[Middleware(ResponseHeaders)],
        lifespan=lifespan,
    )


async def _read_body(request: Request, limit: int) -> bytes | None:
    """The request body, or None when it is longer than `limit` bytes."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _etag_matches(if_none_match: str, etag: str) -> bool:
    candidates = {tag.strip().removeprefix("W/") for tag in if_none_match.split(",")}
    return etag in candidates or "*" in candidates


def build_app() -> Starlette:
    workflow_slug = os.environ.get("WORKFLOW_SLUG")
    if not workflow_slug:
        raise ValueError("set WORKFLOW_SLUG to the Workflow service's slug")
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        raise ValueError("set REDIS_URL to the grouplink-cache Key Value connection string")

    # Short timeouts, because a request waits on this read. The store serves its
    # last good copy when the read fails.
    redis = Redis.from_url(
        redis_url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2
    )
    task = os.environ.get("REBUILD_TASK", "grouplink.rebuild")
    dispatcher = render_dispatcher(slug=workflow_slug)

    async def read_site() -> str | None:
        value: str | None = await redis.get(SITE_KEY)
        return value

    return create_app(
        workflow_slug=workflow_slug,
        dispatcher=dispatcher,
        webhook=NotionWebhook(
            dispatch=dispatcher.start,
            task=task,
            secret=os.environ.get("NOTION_WEBHOOK_SECRET"),
            debounce_ms=env_int("DEBOUNCE_MS", os.environ.get("DEBOUNCE_MS"), DEFAULT_DEBOUNCE_MS),
        ),
        store=SiteStore(read=read_site, rebuild=lambda: dispatcher.start(task, [{}])),
        on_shutdown=redis.aclose,
    )


def main() -> None:
    import uvicorn

    configure_logging()
    port = env_int("PORT", os.environ.get("PORT"), 3000)
    log.info("grouplink-webhook-py listening on %s", port)
    uvicorn.run(build_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
