"""The one Workflows app every task registers on.

TypeScript registers every task into one shared registry as a side effect of
importing the pack, so `src/main.ts` importing `./rebuild.js` is the whole job.
Python has no shared registry. Each pack exports its own `app`, and this module
combines them. Miss a pack here and `ctx.run` on one of its tasks fails to resolve
at runtime rather than at import.

The Notion relation shim is installed here, before any pack code reads a property,
because `grouplink.rebuild` cannot join links to profiles without it.
"""

from __future__ import annotations

from render import Workflows
from render_lab_tasks_github.tasks import app as github_app
from render_lab_tasks_http.tasks import app as http_app
from render_lab_tasks_notion.tasks import app as notion_app
from render_lab_tasks_render.tasks import app as render_app
from render_lab_tasks_render_kv.tasks import app as kv_app
from render_lab_tasks_scrape.tasks import app as scrape_app
from render_lab_tasks_slack.tasks import app as slack_app

import grouplink.notion_relation  # noqa: F401  (imported for its side effect)

app = Workflows.from_workflows(
    notion_app, scrape_app, kv_app, http_app, github_app, render_app, slack_app
)
