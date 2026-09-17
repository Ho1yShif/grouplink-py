"""Composition test. Drives the real grouplink.rebuild task, routing every chained
ctx.run to the owning pack's *_impl with a fake injected at the vendor port. No
network and no secrets, but the pack code, the DTO mapping, the relation shim, and
the composition in grouplink/rebuild.py are all real.

The vendor port for six of the seven packs is an httpx.AsyncClient, so the fake goes
in as an httpx.MockTransport and the pack's own request building, pagination, and DTO
mapping all run. Key Value talks to redis instead, so that one gets a port fake.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from render_lab_tasks_github.client import GitHubClient, GitHubDeps
from render_lab_tasks_github.commit_files import commit_files_impl
from render_lab_tasks_github.get_file_contents import get_file_contents_impl
from render_lab_tasks_github.list_tree import list_tree_impl
from render_lab_tasks_http.client import Client as HttpPackClient
from render_lab_tasks_http.client import Deps as HttpDeps
from render_lab_tasks_http.request import request_impl
from render_lab_tasks_notion.client import Client as NotionPackClient
from render_lab_tasks_notion.client import Deps as NotionDeps
from render_lab_tasks_notion.query_database import query_database_impl
from render_lab_tasks_render.await_deploy import await_deploy_impl
from render_lab_tasks_render.client import Client as RenderPackClient
from render_lab_tasks_render.client import Deps as RenderDeps
from render_lab_tasks_render.trigger_deploy import trigger_deploy_impl
from render_lab_tasks_render_kv.client import Deps as KvDeps
from render_lab_tasks_render_kv.get import get_impl as kv_get_impl
from render_lab_tasks_render_kv.set import set_impl as kv_set_impl
from render_lab_tasks_scrape.client import Client as ScrapePackClient
from render_lab_tasks_scrape.client import Deps as ScrapeDeps
from render_lab_tasks_scrape.extract_metadata import extract_metadata_impl
from render_lab_tasks_slack.client import SlackDeps, SlackWebhook
from render_lab_tasks_slack.post_message import post_message_impl

from grouplink.rebuild import rebuild

ENV = {
    "NOTION_LINKS_DATABASE_ID": "db_links",
    "NOTION_PEOPLE_DATABASE_ID": "db_people",
    "SITE_DEFAULT_SLUG": "shifra",
    "GITHUB_REPO_OWNER": "acme",
    "GITHUB_REPO_NAME": "grouplink",
    "RENDER_STATIC_SITE_ID": "srv-1",
    "SITE_URL": "https://grouplink.onrender.com",
    "NOTION_TOKEN": "notion-token",
    "GITHUB_TOKEN": "github-token",
    "RENDER_API_KEY": "render-key",
    "SLACK_WEBHOOK_URL": "https://hooks.slack.test/services/T/B/x",
    "DRY_RUN": "false",
}

SHIFRA = "person-shifra"
ALEX = "person-alex"


def title_prop(text: str) -> dict[str, Any]:
    return {"type": "title", "title": [{"plain_text": text}]}


def raw_page(
    title: str,
    url: str,
    n: int,
    visible: bool = True,
    people: list[str] | None = None,
    everyone: bool = False,
) -> dict[str, Any]:
    """`n` only makes the Notion page id unique."""
    return {
        "id": f"p-{n}",
        "url": f"https://www.notion.so/p-{n}",
        "created_time": "2026-01-01T00:00:00.000Z",
        "last_edited_time": "2026-01-01T00:00:00.000Z",
        "properties": {
            "Title": title_prop(title),
            "URL": {"type": "url", "url": url},
            "Visible": {"type": "checkbox", "checkbox": visible},
            "Everyone": {"type": "checkbox", "checkbox": everyone},
            "People": {
                "type": "relation",
                "relation": [{"id": id} for id in ([SHIFRA] if people is None else people)],
            },
        },
    }


def raw_person(id: str, name: str, slug: str, tagline: str) -> dict[str, Any]:
    return {
        "id": id,
        "url": f"https://www.notion.so/{id}",
        "created_time": "2026-01-01T00:00:00.000Z",
        "last_edited_time": "2026-01-01T00:00:00.000Z",
        "properties": {
            "Name": title_prop(name),
            "Slug": {"type": "rich_text", "rich_text": [{"plain_text": slug}]},
            "Tagline": {"type": "rich_text", "rich_text": [{"plain_text": tagline}]},
        },
    }


LINK_PAGES = [
    # Shared by both people, so it must be scraped and checked exactly once.
    raw_page("Discord", "https://discord.com/invite/x", 1, people=[SHIFRA, ALEX]),
    raw_page("Startups", "https://render.com/startups", 2),
    raw_page("Hidden", "https://render.com/secret", 3, visible=False),
    raw_page("Alex only", "https://example.com/alex", 4, people=[ALEX]),
]

PEOPLE_PAGES = [
    raw_person(SHIFRA, "Shifra Williams", "shifra", "Developer relations at Render."),
    raw_person(ALEX, "Alex Rivera", "alex", "Engineer at Render."),
]


class FakeKv:
    """The Key Value port. Its vendor dependency is redis, not httpx."""

    def __init__(self, cache: dict[str, str]) -> None:
        self._cache = cache
        self.sets: list[dict[str, Any]] = []

    async def get(self, input: dict[str, Any]) -> dict[str, Any]:
        return {"value": self._cache.get(input["key"])}

    async def set(self, input: dict[str, Any]) -> dict[str, Any]:
        self.sets.append(dict(input))
        self._cache[input["key"]] = input["value"]
        return {"ok": True}

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"kv.{name} is not stubbed")


@dataclass
class Fakes:
    #: URL -> cached JSON, for kv.get. Keys are card URLs, not cache keys.
    cache: dict[str, str] = field(default_factory=dict)
    #: URL -> status, for http.request. Defaults to 200.
    statuses: dict[str, int] = field(default_factory=dict)
    #: Path -> contents already on the branch. Its keys are the default tree.
    current: dict[str, str] = field(default_factory=dict)
    #: Paths github.listTree reports, when they differ from `current`'s keys.
    on_branch: list[str] | None = None
    #: Raw link rows, when a case needs more or fewer than LINK_PAGES.
    links: list[dict[str, Any]] | None = None


class Harness:
    def __init__(self, fakes: Fakes) -> None:
        self.fakes = fakes
        self.scraped: list[str] = []
        self.checked: list[str] = []
        self.blobs: list[str] = []
        self.trees: list[list[dict[str, Any]]] = []
        self.commits: list[dict[str, Any]] = []
        self.deploys: list[dict[str, Any]] = []
        self.slack_posts: list[dict[str, Any]] = []
        self.kv = FakeKv(
            {f"gl:meta:v1:{url}": value for url, value in fakes.cache.items()}
        )
        self._in_flight = 0
        self.peak_in_flight = 0
        self._http = httpx.AsyncClient(transport=httpx.MockTransport(self._route))

    # --- the transport every httpx-backed pack is wired to ---

    def _route(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        handler = (
            self._notion
            if url.host == "api.notion.com"
            else self._github
            if url.host == "api.github.com"
            else self._render_api
            if url.host == "api.render.com"
            else self._slack
            if url.host == "hooks.slack.test"
            else self._link_site
        )
        return handler(request)

    def _notion(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v1/databases/"):
            database_id = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"data_sources": [{"id": f"ds-{database_id}"}]})
        if path.startswith("/v1/data_sources/") and path.endswith("/query"):
            source = path.split("/")[3]
            rows = (
                PEOPLE_PAGES
                if source == "ds-db_people"
                else (self.fakes.links if self.fakes.links is not None else LINK_PAGES)
            )
            return httpx.Response(
                200, json={"results": rows, "has_more": False, "next_cursor": None}
            )
        raise AssertionError(f"no fake for Notion {request.method} {path}")

    def _github(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/repos/acme/grouplink")
        body = json.loads(request.content) if request.content else {}

        if path.startswith("/git/trees/") and request.method == "GET":
            paths = (
                self.fakes.on_branch
                if self.fakes.on_branch is not None
                else list(self.fakes.current)
            )
            return httpx.Response(
                200,
                json={
                    "tree": [{"path": p, "type": "blob"} for p in paths],
                    "truncated": False,
                },
            )
        if path.startswith("/contents/"):
            file_path = path.removeprefix("/contents/")
            content = self.fakes.current.get(file_path, "<html>stale</html>")
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "path": file_path,
                    "content": base64.b64encode(content.encode()).decode(),
                    "encoding": "base64",
                    "sha": "sha1",
                },
            )
        if path == "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "head1"}})
        if path == "/git/commits/head1":
            return httpx.Response(200, json={"tree": {"sha": "tree0"}})
        if path == "/git/blobs" and request.method == "POST":
            self.blobs.append(body["content"])
            return httpx.Response(201, json={"sha": f"blob{len(self.blobs)}"})
        if path == "/git/trees" and request.method == "POST":
            self.trees.append(body["tree"])
            return httpx.Response(201, json={"sha": "tree1"})
        if path == "/git/commits" and request.method == "POST":
            self.commits.append(body)
            return httpx.Response(201, json={"sha": "commit1"})
        if path == "/git/refs/heads/main" and request.method == "PATCH":
            return httpx.Response(200, json={"object": {"sha": body["sha"]}})
        raise AssertionError(f"no fake for GitHub {request.method} {path}")

    def _render_api(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/services/srv-1/deploys" and request.method == "POST":
            self.deploys.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "dep-1", "status": "created"})
        if path == "/v1/services/srv-1/deploys/dep-1":
            return httpx.Response(200, json={"id": "dep-1", "status": "live"})
        raise AssertionError(f"no fake for the Render API {request.method} {path}")

    def _slack(self, request: httpx.Request) -> httpx.Response:
        self.slack_posts.append(json.loads(request.content))
        return httpx.Response(200, text="ok")

    def _link_site(self, request: httpx.Request) -> httpx.Response:
        """Both the scrape and the health check land here."""
        url = str(request.url)
        status = self.fakes.statuses.get(url, 200)
        return httpx.Response(
            status,
            headers={"content-type": "text/html"},
            text=(
                "<html><head><title>T</title>"
                f'<meta name="description" content="desc for {url}">'
                "</head></html>"
            ),
        )

    # --- the context ---

    async def _scrape(self, input: dict[str, Any]) -> Any:
        self.scraped.append(input["url"])
        self._in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
        try:
            await asyncio.sleep(0)
            return await extract_metadata_impl(
                self.ctx, input, deps=ScrapeDeps(scrape=ScrapePackClient(self._http, env=ENV))
            )
        finally:
            self._in_flight -= 1

    async def _check(self, input: dict[str, Any]) -> Any:
        self.checked.append(input["url"])
        return await request_impl(
            self.ctx, input, deps=HttpDeps(http=HttpPackClient(self._http, env=ENV))
        )

    @property
    def ctx(self) -> Any:
        return self

    async def run(self, task: Any, *args: Any) -> Any:
        routes: dict[str, Callable[[dict[str, Any]], Any]] = {
            "notion.queryDatabase": lambda a: query_database_impl(
                self.ctx, a, deps=NotionDeps(notion=NotionPackClient(self._http, env=ENV))
            ),
            "scrape.extractMetadata": self._scrape,
            "kv.get": lambda a: kv_get_impl(self.ctx, a, deps=KvDeps(render_kv=self.kv)),
            "kv.set": lambda a: kv_set_impl(self.ctx, a, deps=KvDeps(render_kv=self.kv)),
            "http.request": self._check,
            "github.listTree": lambda a: list_tree_impl(
                self.ctx, a, deps=GitHubDeps(github=GitHubClient(self._http, env=ENV))
            ),
            "github.getFileContents": lambda a: get_file_contents_impl(
                self.ctx, a, deps=GitHubDeps(github=GitHubClient(self._http, env=ENV))
            ),
            "github.commitFiles": lambda a: commit_files_impl(
                self.ctx, a, deps=GitHubDeps(github=GitHubClient(self._http, env=ENV))
            ),
            "render.triggerDeploy": lambda a: trigger_deploy_impl(
                self.ctx, a, deps=RenderDeps(render=RenderPackClient(self._http, env=ENV))
            ),
            "render.awaitDeploy": lambda a: await_deploy_impl(
                self.ctx, a, deps=RenderDeps(render=RenderPackClient(self._http, env=ENV))
            ),
            "slack.postMessage": lambda a: post_message_impl(
                self.ctx, a, deps=SlackDeps(slack=SlackWebhook(self._http, env=ENV))
            ),
        }
        route = routes.get(task.name)
        if route is None:
            raise AssertionError(f'no fake for ctx.run("{task.name}")')
        return await route(args[0] if args else {})

    # --- what the run wrote ---

    def committed(self) -> dict[str, str]:
        """Path -> HTML handed to github.commitFiles on the last run."""
        if not self.trees:
            return {}
        return {
            entry["path"]: self.blobs[i] for i, entry in enumerate(self.trees[-1])
        }

    def committed_html(self) -> str:
        """The root page's HTML."""
        return self.committed().get("site/index.html", "")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    def apply(**extra: str) -> None:
        for key, value in {**ENV, **extra}.items():
            monkeypatch.setenv(key, value)

    apply()
    yield apply


def harness(**kwargs: Any) -> Harness:
    return Harness(Fakes(**kwargs))


class TestRebuild:
    async def test_renders_every_visible_link_in_notion_order(self, env: Any) -> None:
        h = harness()
        result = await rebuild.func(h.ctx, {})

        assert result["pageCount"] == 2
        assert result["linkCount"] == 3

        html = h.committed_html()
        assert html.index("Discord") < html.index("Startups")
        assert "Hidden" not in html
        assert "render.com/secret" not in html

    async def test_puts_a_persons_links_in_their_own_file_and_nobody_elses(
        self, env: Any
    ) -> None:
        h = harness()
        await rebuild.func(h.ctx, {})
        files = h.committed()

        assert "Startups" in files["site/shifra/index.html"]
        assert "Alex only" not in files["site/shifra/index.html"]
        assert "Alex only" in files["site/alex/index.html"]
        assert "Startups" not in files["site/alex/index.html"]
        # The shared link is on both.
        assert "Discord" in files["site/shifra/index.html"]
        assert "Discord" in files["site/alex/index.html"]

    async def test_puts_an_everyone_link_on_every_page(self, env: Any) -> None:
        h = harness(
            links=[
                *LINK_PAGES,
                raw_page("Careers", "https://render.com/careers", 5, people=[], everyone=True),
            ]
        )
        await rebuild.func(h.ctx, {})
        files = h.committed()

        assert "Careers" in files["site/shifra/index.html"]
        assert "Careers" in files["site/alex/index.html"]

    async def test_scrapes_an_everyone_link_once_not_once_per_page(self, env: Any) -> None:
        h = harness(
            links=[
                *LINK_PAGES,
                raw_page("Careers", "https://render.com/careers", 5, people=[], everyone=True),
            ]
        )
        await rebuild.func(h.ctx, {})

        assert h.scraped.count("https://render.com/careers") == 1

    async def test_gives_each_page_its_own_name_and_tagline(self, env: Any) -> None:
        h = harness()
        await rebuild.func(h.ctx, {})
        files = h.committed()

        assert "Developer relations at Render." in files["site/shifra/index.html"]
        assert "Alex Rivera" in files["site/alex/index.html"]
        assert "Shifra Williams" not in files["site/alex/index.html"]

    async def test_reports_why_a_row_renders_on_no_page(self, env: Any) -> None:
        h = harness(
            links=[
                *LINK_PAGES,
                raw_page("Orphan", "https://example.com/orphan", 20, people=[]),
                raw_page("No URL", "", 21),
            ]
        )
        result = await rebuild.func(h.ctx, {})

        reasons = {row["title"]: row["reason"] for row in result["skipped"]}
        assert reasons["Hidden"] == "Visible is unchecked"
        assert reasons["Orphan"] == "no People relation and Everyone is unchecked"
        assert reasons["No URL"] == "no URL"

    async def test_serves_the_default_person_at_the_root_byte_for_byte(self, env: Any) -> None:
        h = harness()
        await rebuild.func(h.ctx, {})
        files = h.committed()

        assert files["site/index.html"] == files["site/shifra/index.html"]

    async def test_refuses_to_run_when_the_default_slug_matches_nobody(self, env: Any) -> None:
        env(SITE_DEFAULT_SLUG="nobody")
        h = harness()
        with pytest.raises(ValueError, match="matches no Slug"):
            await rebuild.func(h.ctx, {})

    async def test_checks_and_scrapes_a_shared_link_once(self, env: Any) -> None:
        h = harness()
        await rebuild.func(h.ctx, {})

        assert h.scraped.count("https://discord.com/invite/x") == 1
        assert len(h.scraped) == 3
        assert len(h.checked) == 3

    async def test_references_assets_from_the_site_root(self, env: Any) -> None:
        h = harness()
        await rebuild.func(h.ctx, {})

        html = h.committed().get("site/alex/index.html", "")
        assert 'href="/assets/render-logomark-black.svg"' in html
        assert "url('/assets/render-logo-white.png')" in html
        assert "url('/assets/icons/github.svg')" in html
        assert "url('/assets/fonts/RoobertVF.woff2')" in html
        assert not [m for m in ("\"assets/", "'assets/", "(assets/") if m in html]

    async def test_carries_the_scraped_description_onto_the_card(self, env: Any) -> None:
        h = harness()
        await rebuild.func(h.ctx, {})
        assert "desc for https://discord.com/invite/x" in h.committed_html()

    async def test_links_to_the_notion_url_untouched(self, env: Any) -> None:
        h = harness()
        await rebuild.func(h.ctx, {})
        html = h.committed_html()
        assert 'href="https://render.com/startups"' in html
        assert 'href="https://discord.com/invite/x"' in html
        assert "utm_" not in html

    async def test_skips_the_scrape_for_a_url_already_in_key_value(self, env: Any) -> None:
        h = harness(
            cache={"https://discord.com/invite/x": json.dumps({"description": "cached blurb"})}
        )
        result = await rebuild.func(h.ctx, {})

        assert result["cacheHits"] == 1
        assert len(h.scraped) == 2
        assert "cached blurb" in h.committed_html()

    async def test_caches_every_fresh_scrape_with_a_ttl(self, env: Any) -> None:
        h = harness()
        await rebuild.func(h.ctx, {})
        assert len(h.kv.sets) == 3
        assert h.kv.sets[0]["ttlSeconds"] == 86_400

    async def test_writes_the_cache_in_the_same_json_shape_as_typescript(self, env: Any) -> None:
        h = harness()
        await rebuild.func(h.ctx, {})
        assert h.kv.sets[0]["value"].startswith('{"description":"desc for ')
        assert ", " not in h.kv.sets[0]["value"]

    async def test_reports_unreachable_links(self, env: Any) -> None:
        h = harness(statuses={"https://render.com/startups": 404})
        result = await rebuild.func(h.ctx, {})
        assert result["deadLinks"] == ["https://render.com/startups (404)"]

    async def test_does_not_call_a_link_dead_when_the_site_refuses_a_bot_get(
        self, env: Any
    ) -> None:
        h = harness(statuses={"https://example.com/alex": 403})
        result = await rebuild.func(h.ctx, {})
        assert result["deadLinks"] == []

    async def test_commits_every_page_once_deploys_and_posts(self, env: Any) -> None:
        h = harness()
        result = await rebuild.func(h.ctx, {})

        assert result["committed"] is True
        assert result["changedPaths"] == [
            "site/shifra/index.html",
            "site/index.html",
            "site/alex/index.html",
        ]
        assert result["commitSha"] == "commit1"
        assert result["deployId"] == "dep-1"
        assert len(h.commits) == 1
        assert len(h.deploys) == 1
        assert len(h.slack_posts) == 1

    async def test_leaves_an_unchanged_page_out_of_the_commit(self, env: Any) -> None:
        first = harness()
        await rebuild.func(first.ctx, {})
        rendered = first.committed()

        # Alex's page is already on the branch and unchanged; Shifra's is stale.
        second = harness(
            current={
                "site/alex/index.html": rendered["site/alex/index.html"],
                "site/shifra/index.html": "<html>old</html>",
                "site/index.html": "<html>old</html>",
            }
        )
        result = await rebuild.func(second.ctx, {})

        assert result["changedPaths"] == ["site/shifra/index.html", "site/index.html"]
        assert "site/alex/index.html" not in second.committed()

    async def test_skips_the_commit_when_nothing_changed(self, env: Any) -> None:
        first = harness()
        await rebuild.func(first.ctx, {})

        second = harness(current=first.committed())
        result = await rebuild.func(second.ctx, {})

        assert result["committed"] is False
        assert result["changedPaths"] == []
        assert second.commits == []
        assert second.deploys == []

    async def test_commits_a_brand_new_persons_page(self, env: Any) -> None:
        first = harness()
        await rebuild.func(first.ctx, {})
        rendered = first.committed()

        # Everything is current except alex, whose file has never been committed.
        second = harness(
            current={
                "site/shifra/index.html": rendered["site/shifra/index.html"],
                "site/index.html": rendered["site/index.html"],
            }
        )
        result = await rebuild.func(second.ctx, {})

        assert result["changedPaths"] == ["site/alex/index.html"]

    async def test_scrapes_in_batches_instead_of_one_run_per_link(self, env: Any) -> None:
        links = [raw_page(f"Link {i}", f"https://example.com/{i}", i) for i in range(25)]
        h = harness(links=links)
        result = await rebuild.func(h.ctx, {})

        assert result["linkCount"] == 25
        assert len(h.scraped) == 25
        assert h.peak_in_flight <= 10

    async def test_posts_the_failure_to_slack_before_it_rethrows(self, env: Any) -> None:
        env(SITE_DEFAULT_SLUG="nobody")
        h = harness()
        with pytest.raises(ValueError, match="matches no Slug"):
            await rebuild.func(h.ctx, {})

        assert len(h.slack_posts) == 1
        assert "grouplink.rebuild failed" in h.slack_posts[0]["text"]

    async def test_writes_nothing_on_a_dry_run(self, env: Any) -> None:
        env(DRY_RUN="true")
        h = harness()
        result = await rebuild.func(h.ctx, {})

        assert result["dryRun"] is True
        assert result["committed"] is False
        assert result["linkCount"] == 3
        assert h.commits == []
        assert h.slack_posts == []
