"""Composition test. Drives the real grouplink.rebuild task, routing every chained
ctx.run to the owning pack's *_impl with a fake injected at the vendor port. No
network and no secrets, but the pack code, the DTO mapping, the relation shim, and
the composition in grouplink/rebuild.py are all real.

The vendor port for four of the five packs is an httpx.AsyncClient, so the fake goes
in as an httpx.MockTransport and the pack's own request building, pagination, and DTO
mapping all run. Key Value talks to redis instead, so that one gets a port fake.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from render_lab_tasks_http.client import Client as HttpPackClient
from render_lab_tasks_http.client import Deps as HttpDeps
from render_lab_tasks_http.request import request_impl
from render_lab_tasks_notion.client import Client as NotionPackClient
from render_lab_tasks_notion.client import Deps as NotionDeps
from render_lab_tasks_notion.query_database import query_database_impl
from render_lab_tasks_render_kv.client import Deps as KvDeps
from render_lab_tasks_render_kv.get import get_impl as kv_get_impl
from render_lab_tasks_render_kv.set import set_impl as kv_set_impl
from render_lab_tasks_scrape.client import Client as ScrapePackClient
from render_lab_tasks_scrape.client import Deps as ScrapeDeps
from render_lab_tasks_scrape.extract_metadata import extract_metadata_impl
from render_lab_tasks_slack.client import SlackDeps, SlackWebhook
from render_lab_tasks_slack.post_message import post_message_impl

from grouplink.links import LINK_SORTS
from grouplink.page import render_page
from grouplink.rebuild import rebuild
from grouplink.snapshot import SITE_KEY, Snapshot, parse_snapshot

ENV = {
    "NOTION_LINKS_DATABASE_ID": "db_links",
    "NOTION_PROFILES_DATABASE_ID": "db_profiles",
    "SITE_DEFAULT_SLUG": "shifra",
    "SITE_URL": "https://grouplink.onrender.com",
    "NOTION_TOKEN": "notion-token",
    "SLACK_WEBHOOK_URL": "https://hooks.slack.test/services/T/B/x",
    "DRY_RUN": "false",
}

SHIFRA = "profile-shifra"
ALEX = "profile-alex"


def title_prop(text: str) -> dict[str, Any]:
    return {"type": "title", "title": [{"plain_text": text}]}


def raw_page(
    title: str,
    url: str,
    n: int,
    visible: bool = True,
    profiles: list[str] | None = None,
    everyone: bool = False,
    order: float | None = None,
) -> dict[str, Any]:
    """`n` only makes the Notion page id unique. An `order` of None is an empty cell."""
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
            "Profiles": {
                "type": "relation",
                "relation": [{"id": id} for id in ([SHIFRA] if profiles is None else profiles)],
            },
            "Order": {"type": "number", "number": order},
        },
    }


def raw_profile(id: str, name: str, slug: str, tagline: str) -> dict[str, Any]:
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
    # Shared by both profiles, so it must be scraped and checked exactly once.
    raw_page("Discord", "https://discord.com/invite/x", 1, profiles=[SHIFRA, ALEX]),
    raw_page("Startups", "https://render.com/startups", 2),
    raw_page("Hidden", "https://render.com/secret", 3, visible=False),
    raw_page("Alex only", "https://example.com/alex", 4, profiles=[ALEX]),
]

PROFILE_PAGES = [
    raw_profile(SHIFRA, "Shifra Williams", "shifra", "Developer relations at Render."),
    raw_profile(ALEX, "Alex Rivera", "alex", "Engineer at Render."),
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
    #: Raw link rows, when a case needs more or fewer than LINK_PAGES.
    links: list[dict[str, Any]] | None = None
    #: Raw profile rows, when a case needs more or fewer than PROFILE_PAGES.
    profiles: list[dict[str, Any]] | None = None
    #: The snapshot Key Value already holds.
    site: str | None = None


class Harness:
    def __init__(self, fakes: Fakes) -> None:
        self.fakes = fakes
        self.scraped: list[str] = []
        self.checked: list[str] = []
        #: Data source id -> the body of its Notion query.
        self.notion_queries: dict[str, dict[str, Any]] = {}
        #: Name of every task the run dispatched, in order.
        self.tasks: list[str] = []
        self.slack_posts: list[dict[str, Any]] = []
        self.kv = FakeKv({f"gl:meta:v1:{url}": value for url, value in fakes.cache.items()})
        if fakes.site is not None:
            self.kv._cache[SITE_KEY] = fakes.site
        self._in_flight = 0
        self.peak_in_flight = 0
        self._http = httpx.AsyncClient(transport=httpx.MockTransport(self._route))

    # --- the transport every httpx-backed pack is wired to ---

    def _route(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        handler = (
            self._notion
            if url.host == "api.notion.com"
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
            self.notion_queries[source] = json.loads(request.content) if request.content else {}
            if source == "ds-db_profiles":
                rows = self.fakes.profiles if self.fakes.profiles is not None else PROFILE_PAGES
            else:
                rows = self.fakes.links if self.fakes.links is not None else LINK_PAGES
            return httpx.Response(
                200, json={"results": rows, "has_more": False, "next_cursor": None}
            )
        raise AssertionError(f"no fake for Notion {request.method} {path}")

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
            "slack.postMessage": lambda a: post_message_impl(
                self.ctx, a, deps=SlackDeps(slack=SlackWebhook(self._http, env=ENV))
            ),
        }
        self.tasks.append(task.name)
        route = routes.get(task.name)
        if route is None:
            raise AssertionError(f'no fake for ctx.run("{task.name}")')
        return await route(args[0] if args else {})

    # --- what the run wrote ---

    def site_sets(self) -> list[dict[str, Any]]:
        return [entry for entry in self.kv.sets if entry["key"] == SITE_KEY]

    def meta_sets(self) -> list[dict[str, Any]]:
        return [entry for entry in self.kv.sets if entry["key"] != SITE_KEY]

    def snapshot(self) -> Snapshot:
        """The snapshot the last run wrote to Key Value."""
        return parse_snapshot(self.site_sets()[-1]["value"])

    def html(self, slug: str = "shifra") -> str:
        """One profile's page, rendered from the snapshot the last run wrote."""
        return render_page(self.snapshot().pages[slug], 2026)


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

        html = h.html()
        assert html.index("Discord") < html.index("Startups")
        assert "Hidden" not in html
        assert "render.com/secret" not in html

    async def test_sorts_the_links_query_and_leaves_the_profiles_query_unsorted(
        self, env: Any
    ) -> None:
        env(DRY_RUN="true")
        h = harness()
        await rebuild.func(h.ctx, {})

        assert h.notion_queries["ds-db_links"]["sorts"] == LINK_SORTS
        assert "sorts" not in h.notion_queries["ds-db_profiles"]

    async def test_keeps_a_personal_row_in_its_place_among_the_everyone_rows(
        self, env: Any
    ) -> None:
        h = harness(
            links=[
                raw_page(
                    "Docs", "https://render.com/docs", 1, profiles=[], everyone=True, order=10
                ),
                raw_page("Mine", "https://example.com/mine", 2, order=15),
                raw_page(
                    "Blog", "https://render.com/blog", 3, profiles=[], everyone=True, order=20
                ),
            ]
        )
        await rebuild.func(h.ctx, {})

        shifra = h.html("shifra")
        assert shifra.index("Docs") < shifra.index("Mine") < shifra.index("Blog")

        alex = h.html("alex")
        assert "Mine" not in alex
        assert alex.index("Docs") < alex.index("Blog")

    async def test_puts_a_profiles_links_in_its_own_file_and_nobody_elses(self, env: Any) -> None:
        h = harness()
        await rebuild.func(h.ctx, {})

        assert "Startups" in h.html("shifra")
        assert "Alex only" not in h.html("shifra")
        assert "Alex only" in h.html("alex")
        assert "Startups" not in h.html("alex")
        # The shared link is on both.
        assert "Discord" in h.html("shifra")
        assert "Discord" in h.html("alex")

    async def test_puts_an_everyone_link_on_every_page(self, env: Any) -> None:
        h = harness(
            links=[
                *LINK_PAGES,
                raw_page("Careers", "https://render.com/careers", 5, profiles=[], everyone=True),
            ]
        )
        await rebuild.func(h.ctx, {})

        assert "Careers" in h.html("shifra")
        assert "Careers" in h.html("alex")

    async def test_scrapes_an_everyone_link_once_not_once_per_page(self, env: Any) -> None:
        h = harness(
            links=[
                *LINK_PAGES,
                raw_page("Careers", "https://render.com/careers", 5, profiles=[], everyone=True),
            ]
        )
        await rebuild.func(h.ctx, {})

        assert h.scraped.count("https://render.com/careers") == 1

    async def test_gives_each_page_its_own_name_and_tagline(self, env: Any) -> None:
        h = harness()
        await rebuild.func(h.ctx, {})

        assert "Developer relations at Render." in h.html("shifra")
        assert "Alex Rivera" in h.html("alex")
        assert "Shifra Williams" not in h.html("alex")

    async def test_reports_why_a_row_renders_on_no_page(self, env: Any) -> None:
        h = harness(
            links=[
                *LINK_PAGES,
                raw_page("Orphan", "https://example.com/orphan", 20, profiles=[]),
                raw_page("No URL", "", 21),
                raw_page("FTP", "ftp://example.com", 23),
                raw_page("Script", "javascript:alert(1)", 22),
            ]
        )
        result = await rebuild.func(h.ctx, {})

        reasons = {row["title"]: row["reason"] for row in result["skipped"]}
        assert reasons["Hidden"] == "Visible is unchecked"
        assert reasons["Orphan"] == "no Profiles relation and Everyone is unchecked"
        assert reasons["No URL"] == "no URL"
        assert reasons["FTP"] == "URL is not an http, https, or mailto: link"
        assert reasons["Script"] == "URL is not an http, https, or mailto: link"

    async def test_reads_a_url_with_no_scheme_as_https(self, env: Any) -> None:
        """A Notion cell that holds a bare host renders. The run scrapes and checks the
        https URL, because httpx refuses the cell as typed.
        """
        h = harness(links=[*LINK_PAGES, raw_page("Bare host", "render.com/careers", 22)])
        await rebuild.func(h.ctx, {})

        assert "render.com/careers" not in h.scraped
        assert "render.com/careers" not in h.checked
        assert "https://render.com/careers" in h.scraped
        assert "https://render.com/careers" in h.checked
        assert "Bare host" in h.html()

    async def test_never_fetches_a_url_httpx_refuses(self, env: Any) -> None:
        """httpx refuses a URL with no usable scheme, so before this check one bad Notion
        row failed the whole run and the site kept serving the previous commit.
        """
        h = harness(links=[*LINK_PAGES, raw_page("Script", "javascript:alert(1)", 22)])
        await rebuild.func(h.ctx, {})

        assert "javascript:alert(1)" not in h.scraped
        assert "javascript:alert(1)" not in h.checked
        assert "Script" not in h.html()

    async def test_renders_a_mailto_row_without_scraping_or_checking_it(self, env: Any) -> None:
        address = "mailto:shifra@render.com"
        h = harness(links=[*LINK_PAGES, raw_page("Email me", address, 24)])
        result = await rebuild.func(h.ctx, {})

        assert address not in h.scraped
        assert address not in h.checked
        assert [row["title"] for row in result["skipped"]] == ["Hidden"]

        html = h.html("shifra")
        assert f'href="{address}"' in html
        assert f'<span class="card__target">{address}</span>' in html

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

        html = h.html("alex")
        assert 'href="/assets/render-logomark-black.svg"' in html
        assert "url('/assets/render-logo-white.png')" in html
        assert "url('/assets/icons/github.svg')" in html
        assert "url('/assets/fonts/RoobertVF.woff2')" in html
        assert not [m for m in ('"assets/', "'assets/", "(assets/") if m in html]

    async def test_carries_the_scraped_description_onto_the_card(self, env: Any) -> None:
        h = harness()
        await rebuild.func(h.ctx, {})
        assert "desc for https://discord.com/invite/x" in h.html()

    async def test_links_to_the_notion_url_untouched(self, env: Any) -> None:
        h = harness()
        await rebuild.func(h.ctx, {})
        html = h.html()
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
        assert "cached blurb" in h.html()

    async def test_caches_every_fresh_scrape_with_a_ttl(self, env: Any) -> None:
        h = harness()
        await rebuild.func(h.ctx, {})
        assert len(h.meta_sets()) == 3
        assert h.meta_sets()[0]["ttlSeconds"] == 604_800

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

    async def test_publishes_and_posts_that_the_site_is_live(self, env: Any) -> None:
        h = harness()
        result = await rebuild.func(h.ctx, {})

        assert result["published"] is True
        assert len(h.slack_posts) == 1
        assert "grouplink is live with 3 links across 2 pages." in h.slack_posts[0]["text"]

    async def test_never_calls_github_or_the_render_api(self, env: Any) -> None:
        h = harness()
        result = await rebuild.func(h.ctx, {})

        assert not [task for task in h.tasks if task.startswith(("github.", "render."))]
        assert not {"committed", "commitSha", "deployId", "changedPaths"} & result.keys()

    async def test_posts_nothing_when_the_site_is_unchanged(self, env: Any) -> None:
        first = harness()
        await rebuild.func(first.ctx, {})

        second = harness(site=first.site_sets()[0]["value"])
        result = await rebuild.func(second.ctx, {})

        assert result["published"] is False
        assert second.slack_posts == []

    async def test_posts_dead_links_even_when_the_site_is_unchanged(self, env: Any) -> None:
        first = harness(statuses={"https://render.com/startups": 404})
        await rebuild.func(first.ctx, {})

        second = harness(
            site=first.site_sets()[0]["value"], statuses={"https://render.com/startups": 404}
        )
        await rebuild.func(second.ctx, {})

        assert len(second.slack_posts) == 1
        assert "unchanged" in second.slack_posts[0]["text"]
        assert "https://render.com/startups (404)" in second.slack_posts[0]["text"]

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

    async def test_writes_the_site_to_key_value_once_with_no_ttl(self, env: Any) -> None:
        h = harness()
        result = await rebuild.func(h.ctx, {})

        assert len(h.site_sets()) == 1
        assert h.site_sets()[0].get("ttlSeconds") is None
        snapshot = h.snapshot()
        assert snapshot.default_slug == "shifra"
        assert list(snapshot.pages) == ["shifra", "alex"]
        assert snapshot.pages["alex"].name == "Alex Rivera"
        assert [card.title for card in snapshot.pages["shifra"].cards] == ["Discord", "Startups"]
        assert (
            snapshot.pages["shifra"].cards[0].description == "desc for https://discord.com/invite/x"
        )
        assert result["published"] is True
        assert result["hash"] == snapshot.hash

    async def test_skips_the_site_write_when_the_hash_matches(self, env: Any) -> None:
        first = harness()
        await rebuild.func(first.ctx, {})

        second = harness(site=first.site_sets()[0]["value"])
        result = await rebuild.func(second.ctx, {})

        assert second.site_sets() == []
        assert result["published"] is False
        assert result["hash"] == first.snapshot().hash

    async def test_overwrites_a_site_value_it_cannot_read(self, env: Any) -> None:
        h = harness(site="not json")
        result = await rebuild.func(h.ctx, {})

        assert len(h.site_sets()) == 1
        assert result["published"] is True

    async def test_skips_and_reports_a_profile_with_a_reserved_slug(self, env: Any) -> None:
        h = harness(
            profiles=[
                *PROFILE_PAGES,
                raw_profile("profile-assets", "Assets", "assets", "Not a person."),
            ]
        )
        result = await rebuild.func(h.ctx, {})

        assert "assets" not in h.snapshot().pages
        assert {
            "title": "Assets",
            "url": "",
            "reason": 'Slug "assets" is reserved for the web service',
        } in result["skipped"]

    async def test_writes_nothing_on_a_dry_run(self, env: Any) -> None:
        env(DRY_RUN="true")
        h = harness()
        result = await rebuild.func(h.ctx, {})

        assert result["dryRun"] is True
        assert result["linkCount"] == 3
        assert h.slack_posts == []
        assert h.site_sets() == []
        assert result["published"] is False
