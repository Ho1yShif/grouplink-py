"""Fan-out width for the per-URL stages, and the helper that enforces it.

Shared so the workflow and the preview script hit linked sites at the same rate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

#: Without a cap, a 100-row database opens 100 concurrent runs per stage and hits
#: every linked site at once.
BATCH_SIZE = 10


async def map_in_batches[T, R](items: list[T], fn: Callable[[T, int], Awaitable[R]]) -> list[R]:
    """asyncio.gather in fixed-size batches, in input order."""
    results: list[R] = []
    for start in range(0, len(items), BATCH_SIZE):
        batch = items[start : start + BATCH_SIZE]
        results.extend(await asyncio.gather(*(fn(item, start + i) for i, item in enumerate(batch))))
    return results
