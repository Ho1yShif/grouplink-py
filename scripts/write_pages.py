"""Shared by scripts/placeholder.py and scripts/preview.py: render one profile's
page and write it everywhere it belongs under site/.
"""

from __future__ import annotations

from pathlib import Path

from grouplink.links import page_paths_for
from grouplink.page import PageModel, render_page

REPO_ROOT = Path(__file__).resolve().parent.parent


def write_pages(model: PageModel, *, site_dir: str, slug: str, default_slug: str) -> None:
    """Writes the page to the profile's own path, plus the site root when it is the
    default profile. Paths are relative to the repo root, as the workflow commits them.
    """
    html = render_page(model)
    for path in page_paths_for(site_dir, slug, default_slug):
        out = REPO_ROOT / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html)
        print(f"wrote {path} ({len(html)} bytes)")
