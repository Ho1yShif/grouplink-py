"""Regenerate the pages under site/ from seed links, without scraped descriptions.

The workflow overwrites these files on its first real run. They exist so the static
site has something to serve before then, and so the local preview has the same shape
as production: one page per person, plus a copy of the default person's page at the
root. Run with `uv run python scripts/placeholder.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from grouplink.links import favicon_url, page_path
from grouplink.page import LinkCard, PageModel, render_page


@dataclass(frozen=True)
class SeedPerson:
    name: str
    slug: str
    links: list[tuple[str, str]]


SHARED = [
    ("Funded founder? Apply to the Render startup program", "https://render.com/startups"),
    ("Website", "https://render.com/"),
]

PEOPLE = [
    SeedPerson(
        name="Shifra",
        slug="shifra",
        links=[
            *SHARED,
            (
                "Tutorial | Get started with Render Workflows",
                "https://render.com/tutorials/render-workflows",
            ),
        ],
    ),
    SeedPerson(
        name="Graham",
        slug="graham",
        links=[*SHARED, ("Docs", "https://render.com/docs")],
    ),
]

DEFAULT_SLUG = "shifra"
TAGLINE = "The fastest path to production for full-stack applications and agents"

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    for person in PEOPLE:
        html = render_page(
            PageModel(
                name=person.name,
                tagline=TAGLINE,
                cards=[
                    LinkCard(title=title, url=url, description="", icon_url=favicon_url(url))
                    for title, url in person.links
                ],
            )
        )

        paths = [page_path("site", person.slug)]
        if person.slug == DEFAULT_SLUG:
            paths.append(page_path("site", ""))
        for path in paths:
            out = REPO_ROOT / path
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(html)
            print(f"wrote {path} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
