"""Regenerate the pages under site/ from seed links, with the descriptions the scrape
finds in production written out by hand.

The workflow overwrites these files on its first real run. They exist so the static
site has something to serve before then, and so the local preview has the same shape
as production: one page per person, plus a copy of the default person's page at the
root. Run with `uv run python -m scripts.placeholder`.
"""

from __future__ import annotations

from dataclasses import dataclass

from grouplink.icons import IconName
from grouplink.links import to_card
from grouplink.page import PageModel
from scripts.write_pages import write_pages


@dataclass(frozen=True)
class SeedLink:
    title: str
    url: str
    description: str
    icon: IconName


@dataclass(frozen=True)
class SeedPerson:
    name: str
    slug: str
    links: list[SeedLink]


SHARED = [
    SeedLink(
        title="Funded founder? Apply to the Render startup program",
        url="https://render.com/startups",
        description=(
            "Build and scale your startup's apps and agents on infrastructure developers "
            "love, and get up to $100,000 in credits through Render for Startups."
        ),
        icon="form",
    ),
    SeedLink(
        title="Render website",
        url="https://render.com/",
        description=(
            "Deploy and scale any app or agent from your first user to your billionth. "
            "Build faster on intuitive cloud infrastructure for the modern web."
        ),
        icon="render",
    ),
]

PEOPLE = [
    SeedPerson(
        name="Shifra",
        slug="shifra",
        links=[
            *SHARED,
            SeedLink(
                title="Get started with Render Workflows",
                url="https://render.com/tutorials/render-workflows",
                description=(
                    "Scaffold a Render Workflow, write your first task, run it locally, "
                    "and deploy it — in Python or TypeScript."
                ),
                icon="workflows",
            ),
        ],
    ),
    SeedPerson(
        name="Graham",
        slug="graham",
        links=[
            *SHARED,
            SeedLink(
                title="Render docs",
                url="https://render.com/docs",
                description=(
                    "Guides and reference for deploying web services, static sites, "
                    "workers, cron jobs, Postgres, and Key Value on Render."
                ),
                icon="info",
            ),
        ],
    ),
]

DEFAULT_SLUG = "shifra"
TAGLINE = "The fastest path to production for full-stack applications and agents"


def main() -> None:
    for person in PEOPLE:
        write_pages(
            PageModel(
                name=person.name,
                tagline=TAGLINE,
                cards=[to_card(link, link.description) for link in person.links],
            ),
            site_dir="site",
            slug=person.slug,
            default_slug=DEFAULT_SLUG,
        )


if __name__ == "__main__":
    main()
