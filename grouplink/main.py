"""Entry point for the grouplink Workflow service.

Start it with `render-workflows grouplink.main:app`, or with
`python -m grouplink.main`, which does the same thing.

Logging is configured on import rather than under `__main__`, because
`render-workflows grouplink.main:app` imports this module instead of running it,
and the rebuild reports its skipped rows and unrecognized icons at INFO.
"""

from __future__ import annotations

from render import TaskContext

from grouplink.app import app
from grouplink.logs import configure as configure_logging
from grouplink.rebuild import rebuild  # noqa: F401  (imported to register the task)

configure_logging()


@app.task(name="ping")
def ping(ctx: TaskContext) -> str:
    """Zero-dep smoke task, handy for verifying the service is live."""
    return "pong"


if __name__ == "__main__":
    app.start()
