"""The ASGI wrapper. It owns POST /webhooks/notion and delegates everything else to
the render-lab-triggers DispatchServer.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from grouplink.notion_webhook import NotionWebhook
from grouplink.webhook import Receiver, build_receiver


class Dispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Any]]] = []

    async def start(self, task: str, args: list[Any]) -> dict[str, str]:
        self.calls.append((task, args))
        return {"runId": "run-1"}


async def call(app: Any, method: str, path: str, body: str = "") -> dict[str, Any]:
    sent: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }
    messages = iter([{"type": "http.request", "body": body.encode(), "more_body": False}])

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return {
        "status": sent[0]["status"],
        "body": sent[1]["body"].decode(),
    }


@pytest.fixture
def receiver() -> Receiver:
    dispatcher = Dispatcher()
    return Receiver(
        workflow_slug="grouplink",
        webhook=NotionWebhook(dispatch=dispatcher.start, task="grouplink.rebuild"),
    )


async def test_answers_the_notion_route_itself(receiver: Receiver) -> None:
    body = json.dumps({"verification_token": "from-notion"})
    response = await call(receiver, "POST", "/webhooks/notion", body)
    assert response["status"] == 200
    assert json.loads(response["body"]) == {"ok": True}


async def test_sends_no_body_with_a_204(receiver: Receiver) -> None:
    body = json.dumps({"type": "comment.created"})
    response = await call(receiver, "POST", "/webhooks/notion", body)
    assert response["status"] == 204
    assert response["body"] == ""


async def test_delegates_the_health_check_to_the_dispatch_server(receiver: Receiver) -> None:
    response = await call(receiver, "GET", "/healthz")
    assert (response["status"], response["body"]) == (200, "ok")


async def test_delegates_an_unauthenticated_task_dispatch(receiver: Receiver) -> None:
    response = await call(receiver, "POST", "/tasks/grouplink.rebuild", "[]")
    assert response["status"] == 401


async def test_delegates_a_get_on_the_notion_path(receiver: Receiver) -> None:
    response = await call(receiver, "GET", "/webhooks/notion")
    assert response["status"] == 404


def test_build_receiver_names_the_variable_it_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_SLUG", raising=False)
    with pytest.raises(ValueError, match="WORKFLOW_SLUG"):
        build_receiver()
