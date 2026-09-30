"""The page routes of the web service: the snapshot in Key Value, rendered per request."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from starlette.testclient import TestClient

from grouplink.notion_webhook import NotionWebhook
from grouplink.page import LinkCard, PageModel, render_page
from grouplink.site_store import REBUILD_RETRY_SECONDS, SiteStore
from grouplink.snapshot import build_snapshot
from grouplink.webhook import create_app

SHIFRA = PageModel(
    name="Shifra Williams",
    tagline="Developer relations at Render.",
    cards=[
        LinkCard(
            title="Docs",
            url="https://render.com/docs",
            description="Guides",
            icon_url="https://render.com/favicon.ico",
            icon="info",
        )
    ],
)
ALEX = PageModel(name="Alex Rivera", tagline="Engineer at Render.", cards=[])


def snapshot(**pages: PageModel) -> str:
    return build_snapshot(
        pages or {"shifra": SHIFRA, "alex": ALEX}, "shifra", datetime(2026, 9, 30, tzinfo=UTC)
    )


class FakeKv:
    """The one Key Value read the store makes. `value` is what GET returns."""

    def __init__(self, value: str | None) -> None:
        self.value = value
        self.error: Exception | None = None
        self.reads = 0

    async def read(self) -> str | None:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.value


class Dispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Any]]] = []

    async def start(self, task: str, args: list[Any]) -> dict[str, str]:
        self.calls.append((task, args))
        return {"runId": "run-1"}


class App:
    def __init__(self, value: str | None) -> None:
        self.kv = FakeKv(value)
        self.dispatcher = Dispatcher()
        store = SiteStore(
            read=self.kv.read,
            rebuild=lambda: self.dispatcher.start("grouplink.rebuild", [{}]),
        )
        self.client = TestClient(
            create_app(
                workflow_slug="grouplink",
                dispatcher=self.dispatcher,
                webhook=NotionWebhook(dispatch=self.dispatcher.start, task="grouplink.rebuild"),
                store=store,
            )
        )


@pytest.fixture
def app() -> App:
    return App(snapshot())


def test_serves_the_default_profile_at_the_root(app: App) -> None:
    response = app.client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.text == render_page(SHIFRA)


def test_serves_a_profile_at_its_slug_with_or_without_a_trailing_slash(app: App) -> None:
    assert app.client.get("/alex").text == render_page(ALEX)
    assert app.client.get("/alex/").text == render_page(ALEX)
    assert app.client.get("/shifra").text == app.client.get("/").text


def test_answers_an_unknown_slug_with_a_404_page(app: App) -> None:
    response = app.client.get("/nobody")
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]


def test_answers_a_reserved_slug_with_a_404_page(app: App) -> None:
    for path in ("/tasks", "/webhooks", "/assets"):
        assert app.client.get(path).status_code == 404, path


def test_reads_key_value_on_each_request(app: App) -> None:
    app.client.get("/")
    app.kv.value = snapshot(shifra=ALEX)
    assert app.client.get("/").text == render_page(ALEX)
    assert app.kv.reads == 2


def test_serves_the_last_good_copy_when_key_value_fails(app: App) -> None:
    app.client.get("/")
    app.kv.error = ConnectionError("Key Value is down")
    response = app.client.get("/")
    assert response.status_code == 200
    assert response.text == render_page(SHIFRA)


def test_serves_the_last_good_copy_when_the_value_will_not_parse(app: App) -> None:
    app.client.get("/")
    doc = json.loads(snapshot())
    doc["schema"] = 2
    app.kv.value = json.dumps(doc)
    assert app.client.get("/").text == render_page(SHIFRA)


def test_answers_503_when_key_value_fails_before_any_good_copy() -> None:
    app = App(snapshot())
    app.kv.error = ConnectionError("Key Value is down")
    response = app.client.get("/")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert app.dispatcher.calls == []


def test_starts_one_rebuild_and_answers_503_while_the_key_is_missing() -> None:
    app = App(None)
    assert app.client.get("/").status_code == 503
    assert app.client.get("/alex").status_code == 503
    assert app.dispatcher.calls == [("grouplink.rebuild", [{}])]


def test_serves_the_page_once_the_rebuild_writes_the_key() -> None:
    app = App(None)
    app.client.get("/")
    app.kv.value = snapshot()
    assert app.client.get("/").status_code == 200


def test_sends_an_etag_and_answers_304_when_it_matches(app: App) -> None:
    first = app.client.get("/")
    assert first.headers["cache-control"] == "no-cache"
    etag = first.headers["etag"]

    second = app.client.get("/", headers={"if-none-match": etag})
    assert second.status_code == 304
    assert second.content == b""


def test_changes_the_etag_when_the_page_changes(app: App) -> None:
    etag = app.client.get("/").headers["etag"]
    app.kv.value = snapshot(shifra=ALEX)
    assert app.client.get("/").headers["etag"] != etag


def test_sets_the_security_headers_on_every_response(app: App) -> None:
    for path in ("/", "/nobody", "/healthz", "/assets/render-logomark-black.svg"):
        response = app.client.get(path)
        assert response.headers["x-content-type-options"] == "nosniff", path
        assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin", path


def test_serves_the_assets_with_their_content_types(app: App) -> None:
    svg = app.client.get("/assets/render-logomark-black.svg")
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")

    font = app.client.get("/assets/fonts/RoobertVF.woff2")
    assert font.status_code == 200
    assert font.headers["cache-control"] == "public, max-age=31536000, immutable"

    assert app.client.get("/assets/nothing.png").status_code == 404


def test_answers_a_head_request(app: App) -> None:
    response = app.client.head("/alex")
    assert response.status_code == 200
    assert response.content == b""


async def test_starts_another_rebuild_when_the_key_stays_missing() -> None:
    """The first run can fail or be a dry run, so the page must not stay at 503."""
    now = [0.0]
    kv = FakeKv(None)
    dispatcher = Dispatcher()
    store = SiteStore(
        read=kv.read,
        rebuild=lambda: dispatcher.start("grouplink.rebuild", [{}]),
        clock=lambda: now[0],
    )

    await store.snapshot()
    now[0] = REBUILD_RETRY_SECONDS - 1
    await store.snapshot()
    assert len(dispatcher.calls) == 1

    now[0] = REBUILD_RETRY_SECONDS + 1
    await store.snapshot()
    assert len(dispatcher.calls) == 2
