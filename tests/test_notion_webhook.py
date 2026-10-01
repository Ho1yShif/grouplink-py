"""The handler takes a request object and a dispatch function, so these run with no
server, no Render API key, and no real debounce window.

DEBOUNCE_MS is 40 here rather than the production 60_000, so the tests wait
milliseconds instead of minutes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

import pytest

from grouplink.notion_webhook import NotionWebhook, WebhookRequest

SECRET = "verification-token"
TASK = "grouplink.rebuild"
DEBOUNCE_MS = 40


def sign(raw_body: str, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), raw_body.encode(), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def event(type: str) -> dict[str, Any]:
    return {"id": "evt-1", "type": type, "data": {"parent": {"id": "ds-1"}}}


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Any]]] = []

    async def __call__(self, task: str, args: list[Any]) -> dict[str, str]:
        self.calls.append((task, args))
        return {"runId": "run-1"}


class Setup:
    """Signed and secret by default; pass `secret=None` for the handshake case."""

    def __init__(self, **overrides: Any) -> None:
        self.dispatch = Recorder()
        options: dict[str, Any] = {
            "dispatch": self.dispatch,
            "task": TASK,
            "debounce_ms": DEBOUNCE_MS,
            "secret": SECRET,
        }
        options.update(overrides)
        self.webhook = NotionWebhook(**options)

    def raw_post(self, raw_body: str, headers: dict[str, str] | None = None) -> Any:
        return self.webhook.handle(WebhookRequest(headers=headers or {}, raw_body=raw_body))

    def post(self, body: Any, headers: dict[str, str] | None = None) -> Any:
        return self.raw_post(json.dumps(body), headers)

    def signed_post(self, body: Any) -> Any:
        raw_body = json.dumps(body)
        return self.raw_post(raw_body, {"x-notion-signature": sign(raw_body)})

    async def settle(self) -> None:
        """Let the debounce window pass, plus enough slack for the dispatch."""
        await asyncio.sleep(DEBOUNCE_MS / 1000 * 3)
        await self.webhook.drain()


class TestNotionWebhook:
    async def test_schedules_one_dispatch_for_a_signed_event(self) -> None:
        s = Setup()

        response = s.signed_post(event("page.properties_updated"))
        assert (response.status, response.body) == (202, {"scheduled": True})
        assert s.dispatch.calls == []

        await s.settle()
        assert s.dispatch.calls == [(TASK, [{}])]

    async def test_rejects_a_wrong_signature_without_scheduling_anything(self) -> None:
        s = Setup()
        body = event("page.created")

        wrong = {"x-notion-signature": sign(json.dumps(body), "wrong")}
        assert s.post(body, wrong).status == 401
        assert s.post(body).status == 401

        await s.settle()
        assert s.dispatch.calls == []

    async def test_accepts_the_unsigned_handshake_only_while_the_secret_is_unset(self) -> None:
        handshake = {"verification_token": "secret-from-notion"}

        assert Setup(secret=None).post(handshake).status == 200
        assert Setup().post(handshake).status == 401

    async def test_rejects_a_body_that_is_not_json(self) -> None:
        assert Setup(secret=None).raw_post("{").status == 400

    async def test_ignores_an_event_type_outside_the_rebuild_list(self) -> None:
        s = Setup()

        response = s.signed_post(event("comment.created"))
        assert (response.status, response.body) == (204, None)

        await s.settle()
        assert s.dispatch.calls == []

    async def test_collapses_a_burst_of_edits_into_one_run(self) -> None:
        s = Setup()

        for _ in range(3):
            s.signed_post(event("page.content_updated"))
            await asyncio.sleep(DEBOUNCE_MS / 1000 / 2)
        assert s.dispatch.calls == []

        await s.settle()
        assert len(s.dispatch.calls) == 1

    async def test_a_failing_dispatch_does_not_take_the_receiver_down(self) -> None:
        async def boom(task: str, args: list[Any]) -> dict[str, str]:
            raise RuntimeError("the Workflow service is down")

        s = Setup(dispatch=boom)
        assert s.signed_post(event("page.created")).status == 202
        await s.settle()

    async def test_flush_starts_the_waiting_run_without_the_quiet_period(self) -> None:
        s = Setup()
        s.signed_post(event("page.created"))

        await s.webhook.flush()
        assert s.dispatch.calls == [(TASK, [{}])]

        # The cancelled timer must not start a second run.
        await s.settle()
        assert s.dispatch.calls == [(TASK, [{}])]

    async def test_flush_does_nothing_when_no_run_is_waiting(self) -> None:
        s = Setup()
        await s.webhook.flush()
        assert s.dispatch.calls == []

        s.signed_post(event("page.created"))
        await s.settle()
        await s.webhook.flush()
        assert len(s.dispatch.calls) == 1

    async def test_flush_waits_for_a_dispatch_already_in_flight(self) -> None:
        release = asyncio.Event()
        calls: list[str] = []

        async def slow(task: str, args: list[Any]) -> dict[str, str]:
            calls.append(task)
            await release.wait()
            return {"runId": "run-1"}

        s = Setup(dispatch=slow)
        s.signed_post(event("page.created"))
        # Past the quiet period, so the dispatch has started and is blocked.
        await asyncio.sleep(DEBOUNCE_MS / 1000 * 2)
        assert calls == [TASK]

        flush = asyncio.create_task(s.webhook.flush())
        await asyncio.sleep(0)
        assert not flush.done()

        release.set()
        await flush
        assert calls == [TASK]


@pytest.mark.parametrize(
    "type",
    [
        "page.created",
        "page.deleted",
        "page.undeleted",
        "page.properties_updated",
        "page.content_updated",
        "data_source.content_updated",
        "data_source.schema_updated",
    ],
)
async def test_every_rebuild_event_schedules_a_run(type: str) -> None:
    s = Setup(secret=None)
    assert s.post(event(type)).status == 202
    await s.settle()
    assert len(s.dispatch.calls) == 1
