"""The non-page routes of the web service: POST /webhooks/notion, and the routes it
delegates to the render-lab-triggers DispatchServer.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.testclient import TestClient

from grouplink.notion_webhook import NotionWebhook
from grouplink.site_store import SiteStore
from grouplink.webhook import MAX_BODY_BYTES, build_app, create_app


class Dispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Any]]] = []

    async def start(self, task: str, args: list[Any]) -> dict[str, str]:
        self.calls.append((task, args))
        return {"runId": "run-1"}


async def no_site() -> str | None:
    return None


@pytest.fixture
def client() -> TestClient:
    dispatcher = Dispatcher()
    return TestClient(
        create_app(
            workflow_slug="grouplink",
            dispatcher=dispatcher,
            webhook=NotionWebhook(dispatch=dispatcher.start, task="grouplink.rebuild"),
            store=SiteStore(read=no_site, rebuild=lambda: dispatcher.start("x", [])),
        )
    )


def test_answers_the_notion_route_itself(client: TestClient) -> None:
    body = json.dumps({"verification_token": "from-notion"})
    response = client.post("/webhooks/notion", content=body)
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_sends_no_body_with_a_204(client: TestClient) -> None:
    body = json.dumps({"type": "comment.created"})
    response = client.post("/webhooks/notion", content=body)
    assert response.status_code == 204
    assert response.content == b""


def test_answers_the_health_check(client: TestClient) -> None:
    response = client.get("/healthz")
    assert (response.status_code, response.text) == (200, "ok")
    assert client.head("/healthz").status_code == 200


def test_refuses_a_notion_body_over_the_limit(client: TestClient) -> None:
    response = client.post("/webhooks/notion", content=b"x" * (MAX_BODY_BYTES + 1))
    assert response.status_code == 413


def test_delegates_an_unauthenticated_task_dispatch(client: TestClient) -> None:
    response = client.post("/tasks/grouplink.rebuild", content="[]")
    assert response.status_code == 401


def test_refuses_a_get_on_the_notion_path(client: TestClient) -> None:
    response = client.get("/webhooks/notion")
    assert response.status_code == 405


def test_build_app_names_the_variables_it_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_SLUG", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(ValueError, match="WORKFLOW_SLUG"):
        build_app()

    monkeypatch.setenv("WORKFLOW_SLUG", "grouplink")
    with pytest.raises(ValueError, match="REDIS_URL"):
        build_app()
