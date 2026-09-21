"""The card icon library. A leaf module: both the Notion reader and the page
renderer name icons, and neither should have to import the other.

An icon name is the filename without .png under site/assets/link-icons. Adding an
icon takes a file, a name here, and an option in the Notion dropdown; no other code
names an icon.
"""

from __future__ import annotations

from typing import Literal, TypeGuard, get_args

IconName = Literal[
    "arrow",
    "credits",
    "download",
    "form",
    "info",
    "render",
    "upload",
    "workflows",
]

#: The same names at runtime, in declaration order. That order decides the order of
#: the mask rules page.py generates, which changes the CSP style hash.
ICON_NAMES: tuple[IconName, ...] = get_args(IconName)

#: What a row with an empty or unrecognized Icon cell renders.
DEFAULT_ICON: IconName = "arrow"


def is_icon_name(value: str) -> TypeGuard[IconName]:
    """True when the value names one of the icon files."""
    return value in ICON_NAMES
