"""Entry point for the grouplink Workflow service.

Start it with `render-workflows grouplink.main:app`, or with
`python -m grouplink.main`, which does the same thing.
"""

from __future__ import annotations

from render import TaskContext

from grouplink.app import app
from grouplink.rebuild import rebuild  # noqa: F401  (imported to register the task)


@app.task(name="ping")
def ping(ctx: TaskContext) -> str:
    """Zero-dep smoke task, handy for verifying the service is live."""
    return "pong"


if __name__ == "__main__":
    app.start()
