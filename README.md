# grouplink

Render's links page. A Render Workflow reads the link list from Notion, enriches
it, and writes the result to Key Value. A Render web service reads it from Key
Value and serves one plain HTML page per profile.

It replaces a Linktree page that couldn't be styled to brand and was two-thirds
Linktree's own affiliate marketplace.

Links are rendered exactly as the Notion row gives them. Put any tracking
parameters in the Notion URL itself.

## Pipeline

`grouplink/rebuild.py` — one task, `grouplink.rebuild`:

```
grouplink.rebuild
├── notion.queryDatabase   ×2   read the link rows and the profiles
├── kv.get              ×N      look for cached metadata
├── scrape.extractMetadata ×N   scrape the misses
├── kv.set              ×N      cache them for 7 days
├── http.request        ×N      health-check every link
├── kv.get                      read the published snapshot
├── kv.set                      write the new snapshot, if its hash changed
└── slack.postMessage           the live URL, or the dead links
```

Every `ctx.run` is a separate durable run with the owning package's retry policy.
The `×N` steps fan out into independent chained runs, ten at a time, so a large
database does not open one run per link or hit every site at once. The URLs are
deduplicated first, so a link on three pages is scraped and health-checked once.

The health check counts a link as dead when it answers 404, 5xx, or nothing at all.
A 401, 403, 405, 429, or 999 means the site is up and refusing a request with no
browser fingerprint, which is what X and LinkedIn do.

The snapshot is one JSON document under the `grouplink:site` key. It holds every
profile's page model and the default slug, but no HTML. The run writes it with one
`SET` and no TTL, and skips the write when the hash matches the published one. The
page is live as soon as the `SET` returns.

`grouplink-webhook` reads the key on each page request and renders it with
`grouplink/page.py`. A change to `page.py` goes live with the web service deploy
and needs no rebuild. If Key Value is down or the value won't parse, the service
serves its last good copy from memory, and it answers 503 if it has none. If the
key is missing, it starts a `grouplink.rebuild` and answers 503 until the run
writes the key. It starts another run every 10 minutes while the key stays
missing.

A run starts when someone edits Notion. `grouplink-webhook` verifies Notion's signature, drops the event types that can't
change a page, and waits 60 seconds of quiet before dispatching
`grouplink.rebuild`. Editing eight rows in one sitting gives you one run.

There is no schedule. The run is also what health-checks every link, so a link
that rots is only reported the next time someone edits Notion.

If the run itself fails, it posts the error to Slack and rethrows. The receiver
has already answered Notion by then, so its response says nothing about how the
run went.

A run publishes unless you set `DRY_RUN=true`. A dry run reads, scrapes, caches,
and health-checks, then returns the result without writing the snapshot or
posting to Slack.

## Run it locally

```bash
uv sync
cp .env.example .env      # fill in NOTION_TOKEN, NOTION_LINKS_DATABASE_ID, REDIS_URL
render workflows dev -- uv run render-workflows grouplink.main:app
```

In another terminal:

```bash
render workflows tasks list --local
render workflows start grouplink.rebuild --local --input='[{"dryRun":true}]'
```

`uv sync` installs from `uv.lock`. `requirements.txt` is the export Render builds
from, so regenerate it whenever a dependency changes:

```bash
uv export --frozen --no-dev --no-emit-project -o requirements.txt
```

### Previewing the page

The preview needs a local Redis. Run `docker run -p 6379:6379 redis`, or
`redis-server` if you have it installed.

`scripts/preview.py` reads both Notion databases, scrapes each card's
description, and writes the snapshot to the local Redis. It needs
`NOTION_TOKEN`, both database IDs, and `SITE_DEFAULT_SLUG` in `.env`. It writes
to `REDIS_URL`, or to `redis://localhost:6379` when that is unset, and refuses a
Redis that is not on this machine, so it can't overwrite the live site.

```bash
uv run python -m scripts.preview
REDIS_URL=redis://localhost:6379 WORKFLOW_SLUG=local uv run python -m grouplink.webhook
```

Open `http://localhost:3000`. There is no reload on save. Re-run the script, or
restart the server after a change to `page.py`.

`scripts/placeholder.py` writes a snapshot of the seed links instead, without
touching Notion, for looking at the design before the databases exist. Run it
with `uv run python -m scripts.placeholder`. It writes `/shifra` and `/graham`,
and `/` shows `shifra`.

## The Notion databases

There are two. Links:

| Property   | Type     | Purpose                                                                                   |
| ---------- | -------- | ----------------------------------------------------------------------------------------- |
| `Title`    | title    | Card text. Not scraped — this is the copy you control.                                    |
| `URL`      | url      | Where the card points. See below.                                                         |
| `Visible`  | checkbox | Unchecked rows are dropped.                                                               |
| `Everyone` | checkbox | Checked puts the link on every profile's page.                                            |
| `Profiles` | relation | Which pages the link appears on. Relate it to two rows and it appears on both.            |
| `Icon`     | select   | Which icon the card draws. One option per file under `grouplink/assets/link-icons/`.      |
| `Order`    | number   | Card position, lowest first. Ties go oldest first. Rows with no number go last. Required. |

A `URL` cell is read as `https://`. A cell with no scheme, such as `render.com`,
gets `https://`, and an `http://` cell is upgraded. A `mailto:` cell keeps its
scheme, and the card renders without a scrape or a health check. Any other
scheme, such as `ftp://`, skips the row. The rebuild lists every skipped row and
the check it failed.

Profiles:

| Property  | Type  | Purpose                                     |
| --------- | ----- | ------------------------------------------- |
| `Name`    | title | The heading on that profile's page.         |
| `Slug`    | text  | The URL path. `shifra` serves at `/shifra`. |
| `Tagline` | text  | The page description in the metadata. Not shown on the page. |

A link's audience is `Everyone` plus whatever `Profiles` names. A row with both
set is redundant, not contradictory, and a row with neither renders nowhere.

A profile's page is served at `/<slug>`. The profile named by `SITE_DEFAULT_SLUG`
is served at `/` as well.

The run skips a profile and lists it with the skipped rows when its slug:

- is `assets`, `healthz`, `tasks`, or `webhooks`, which the web service uses for its own routes.
- contains `/`.
- is already used by another profile. The first profile Notion returns keeps the slug.

`Icon` is matched case-insensitively against the nine names in
`grouplink/icons.py`: `arrow`, `credits`, `download`, `email`, `form`, `info`,
`render`, `upload`, and `workflows`. An empty cell or an option no file matches
renders `arrow`, and the run logs every unmatched option. The property name
itself is case-sensitive, so it has to be spelled exactly `Icon`.

Share both databases with the Notion integration that owns `NOTION_TOKEN`.

`render-lab-tasks-notion` 0.1.0 drops relation properties, because its
`simplify_property` returns `None` for any type it doesn't recognize. A link
row's `Profiles` relation therefore arrives empty, and every row without
`Everyone` checked renders nowhere. `grouplink/notion_relation.py` replaces that function with one
that reads relations, and `grouplink/app.py` imports it before any pack code runs.
Delete both once the fix ships in a release.

### Card order

Cards on every page are in ascending `Order`. A profile's page shows its own rows
and the `Everyone` rows, and a personal row takes its place among the shared rows
by its number.

- Number the rows in steps of 10. To move a card, give it a number between two
  others, such as 15.
- The numbers are shared across profiles. Two personal rows on different pages
  never appear together, so their numbers can be the same.
- Make one Notion view per profile. Filter it to `Profiles` contains the profile
  or `Everyone` is checked, and sort it by `Order`. The view then shows the order
  of that profile's page.
- A change to `Order` is a property edit, so it starts a rebuild. Dragging rows in
  a Notion view does not change the site.

If the `Order` column is missing or renamed, Notion rejects the query and the run
fails. The site keeps serving the last snapshot.

## Configuration

| Var                                      | Default  | Purpose                                         |
| ---------------------------------------- | -------- | ----------------------------------------------- |
| `NOTION_TOKEN`                           | —        | Notion integration token.                       |
| `NOTION_LINKS_DATABASE_ID`               | —        | The links database.                             |
| `NOTION_PROFILES_DATABASE_ID`            | —        | The profiles database.                          |
| `REDIS_URL`                   | —        | Key Value instance holding the site and the metadata cache. |
| `SITE_URL`                    | —        | Public URL, quoted in the Slack message.        |
| `SLACK_WEBHOOK_URL`           | —        | Optional. Unset logs the digest to the console. |
| `DRY_RUN`                     | `false`  | Set `true` to skip the snapshot write and the Slack post. |
| `SITE_DEFAULT_SLUG`           | —        | Slug of the profile the root page shows.        |
| `METADATA_TTL_SECONDS`        | `604800` | How long a scraped description is cached.       |
| `LINKS_LIMIT`                 | `100`    | Notion rows to read per run.                    |

The web service reads its own set:

| Var                     | Default             | Purpose                                        |
| ----------------------- | ------------------- | ---------------------------------------------- |
| `REDIS_URL`             | —                   | Key Value instance holding the site. The Blueprint sets it. |
| `RENDER_API_KEY`        | —                   | Used to start Workflow runs.                   |
| `WORKFLOW_SLUG`         | —                   | Slug of the Workflow service to dispatch to.   |
| `NOTION_WEBHOOK_SECRET` | —                   | Verification token of the Notion subscription. |
| `DISPATCH_TOKEN`        | —                   | Bearer token required on `POST /tasks/:task`.  |
| `REBUILD_TASK`          | `grouplink.rebuild` | Task the webhook dispatches.                   |
| `DEBOUNCE_MS`           | `60000`             | Quiet period before an edit starts a run.      |

`LINKS_LIMIT`, `METADATA_TTL_SECONDS`, and `DEBOUNCE_MS` must each be a whole
number of 1 or more. Anything else fails at startup and names the variable,
because `DEBOUNCE_MS=10s` used to parse as 10 milliseconds and `LINKS_LIMIT=-5`
used to ask Notion for a negative page size. Unset or empty still falls back.

Each page's name comes from its Profiles row, not from configuration.

Per-run overrides go in the input: `--input='[{"dryRun":false}]'`.

## The page

`grouplink/page.py` is one function returning the whole document — no framework, no
build step, inline CSS. It follows Render's brand foundations: semantic color
tokens with a dark override, PP Neue Montreal for prose, square corners, 1px
hairlines, and purple reserved for links and focus rings.

Each card is a two-column grid: an icon on the left, then the title, the scraped
description, and the mono target line. The icon comes from the row's `Icon`
column. The nine files under `grouplink/assets/link-icons/` are dark artwork on
transparency, drawn as CSS masks in `var(--text-faint)` so they read on both
backgrounds. Adding a tenth takes a file, a name in `grouplink/icons.py`, and an
option in the Notion dropdown. The order the names are declared in decides the
order of the generated mask rules, which changes the CSP style hash.

The footer carries a copyright year. `render_page` takes it as an argument rather
than reading the clock, so the golden fixtures stay stable. The web service passes
the current year on each request.

The masthead is centered, with the Render wordmark above a row of social icons.
It is the same on every page. The icons are YouTube, LinkedIn, X, GitHub, and
Discord, and they come from `SOCIALS` in `grouplink/page.py` rather than from Notion,
so editing that list is the only way to change the row. Each label needs a
matching file in `grouplink/assets/icons/`. The wordmark and the icons are white files
drawn as CSS masks and painted with the text color, so they read on both the
light and dark background.

The brand woff2 files under `grouplink/assets/fonts/` are commercial faces. If this
repo needs to stop redistributing them, delete the three `@font-face` blocks and
load Manrope and Roboto Mono instead — the fallback chain already names them.

Assets are referenced from the site root (`/assets/…`) so they resolve the same
from `/` and from `/<slug>/`. The web service serves them from `grouplink/assets/`.

Every response carries `X-Content-Type-Options: nosniff` and
`Referrer-Policy: strict-origin-when-cross-origin`. The fonts are cached for a
year. A page response has `Cache-Control: no-cache` and an `ETag`, so a browser
revalidates on each visit and gets a 304 when nothing changed.

`grouplink/jsurl.py` is a subset of the WHATWG URL parser. `safe_url` and
`favicon_url` ran through the JS `URL` constructor in the TypeScript build, which
normalizes a href: a bare origin gains a trailing slash, the host lowercases,
and a default port drops. `urllib.parse` does none of that, so the Python pages
would differ from the TypeScript ones.

## Deploy

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Ho1yShif/grouplink-py)

Blueprints don't support Workflows yet, so the Workflow service is created in the
Dashboard and everything else comes from [`render.yaml`](render.yaml).

The Blueprint comes first even though the web service needs the Workflow's slug,
because the Workflow needs `REDIS_URL` from the Key Value instance, and the
Blueprint creates it.
`grouplink-webhook` fails its first deploy as a result: it exits at startup
while `WORKFLOW_SLUG` is empty, and step 5 is what fixes it.

1. Create the Notion connection at
   [notion.so/profile/integrations](https://www.notion.so/profile/integrations).
   Click **+ New connection** and name it `grouplink`. Set the capabilities,
   which are the same for both connection types:
   - Under **Capabilities** in the **Content capabilities** section, keep **Read
     content** and uncheck insert and update content. The run only calls
     `queryDatabase`.
   - Under **User information**, pick **No user information**.

   Then pick a type. **OAuth** works for any Notion account:
   - There is no installation access token and no **⋯ > Connections** step. The
     token comes from a code exchange, and you choose the databases during the
     authorization flow.
   - Follow [Using a public connection](#using-a-public-connection) below, then
     come back here for step 2.

   **Internal** is shorter but needs workspace owner rights:
   - Open the **Configuration** tab and copy the **Installation access token**.
     It starts with `ntn_` and is `NOTION_TOKEN`. Older docs call it the
     Internal Integration Secret.
   - Open the links database in Notion, click **⋯** in the top right, then
     **Connections > Connect to**, and pick `grouplink`. Repeat on the profiles
     database. The connection reads nothing you haven't connected it to.

   `NOTION_WEBHOOK_SECRET` isn't part of either path. Notion generates it when
   you save the subscription in step 6, which needs the receiver's URL.

2. Click the button, or Dashboard → **New > Blueprint** and link this repo. It
   creates the Key Value instance (`grouplink-cache`) and the web service
   (`grouplink-webhook`). Leave `WORKFLOW_SLUG` blank when it prompts. Note the
   web service's URL. The button reads `render.yaml` from `main`, so push first.
3. Dashboard → **New > Workflow** on the same repo.
   Build: `pip install -r requirements.txt`. Start: `python -m grouplink.main`.
   Render's Python image does not ship `uv`, so the build installs from the
   exported requirements file.
4. Set the [Configuration](#configuration) vars on the Workflow. The webhook
   receiver's table doesn't apply here. Required:
   - `NOTION_TOKEN`, `NOTION_LINKS_DATABASE_ID`, `NOTION_PROFILES_DATABASE_ID`.
   - `REDIS_URL`, the internal connection string of `grouplink-cache`.
   - `SITE_URL`, the URL you noted at step 2, and `SITE_DEFAULT_SLUG`.
   - `DRY_RUN=true` for the first deploy, so a misconfigured run can't publish.
     The default is `false`.

   Optional: `SLACK_WEBHOOK_URL`, plus `METADATA_TTL_SECONDS` and `LINKS_LIMIT`
   if the defaults don't suit.

   Confirm the tasks appear on the service's Tasks page and note the slug.

5. Set `WORKFLOW_SLUG` and `RENDER_API_KEY` on `grouplink-webhook` and redeploy
   it. Leave `NOTION_WEBHOOK_SECRET` unset for now.
6. Create the Notion subscription and finish the handshake. See
   [The Notion subscription](#the-notion-subscription).
7. Remove `DRY_RUN` or set it to `false`, then edit a row in Notion. Until the
   first run writes the snapshot, the page answers 503.

### The Notion subscription

`NOTION_WEBHOOK_SECRET` doesn't exist anywhere until you create the
subscription. Notion mints it and posts it to the receiver, so the Configuration
tab won't show it and there is nothing to look up in advance.

1. Open the connection's **Webhooks** tab — a separate tab from
   **Configuration** — and click **+ Create a subscription**.
2. Set the webhook URL to
   `https://grouplink-webhook.onrender.com/webhooks/notion`. You may need to append your slug to this URL
3. Click the minus sign to unsubscribe from all events so you can select only the ones you need.
   Subscribe to `page.created`, `page.deleted`, `page.undeleted`,
   `page.properties_updated`, `page.content_updated`,
   `data_source.content_updated`, and `data_source.schema_updated`.
4. Click **Create subscription**. Notion immediately posts a one-time
   `verification_token` to the receiver. That request carries no signature, and
   it arrives before there is a secret to check it against, so the receiver
   accepts unsigned bodies while `NOTION_WEBHOOK_SECRET` is unset and logs the
   token.
5. Find the line `notion verification_token: ntn_...` in the receiver's logs on
   Render and copy the value.
6. Back on the Webhooks tab, click the **Verify** button next to the
   subscription, paste the token, and confirm.
7. Set the same value as `NOTION_WEBHOOK_SECRET` on `grouplink-webhook` and redeploy.
   From then on every request needs a valid `X-Notion-Signature`.

The receiver filters on event type alone. Under Notion API version 2025-09-03 an
event's `data.parent.id` is a data source ID rather than the database ID in
`NOTION_LINKS_DATABASE_ID`, so filtering on the ID would drop every event. Share
the integration with the two databases and nothing else.

### Using a public connection

Creating an internal connection requires workspace owner rights. A public
connection doesn't, so that is the way in if you're a member rather than an
owner. It runs the same code — `render-lab-tasks-notion` sends whatever is in
`NOTION_TOKEN` as a bearer token, and doesn't care where it came from.

Do the first part with step 1 and the rest after step 2, once the receiver
exists and you know its hostname.

1. Create the connection as above, but set the type to **Public**. Notion asks
   for a company name, a homepage URL, a privacy policy URL, and a terms URL,
   and any reachable page satisfies all four.
2. Set the redirect URI to `https://grouplink-webhook.onrender.com/oauth`,
   substituting the receiver's real hostname if Render had to suffix the name.
   Nothing serves that path, so the redirect 404s and the code stays in the
   address bar. The form prepends `https://` to whatever you type, so a
   `http://localhost` URI can't be entered. Register one redirect URI and no
   more; a second one changes whether `redirect_uri` is required later.
3. Copy the **OAuth client ID** and **OAuth client secret**.
4. Check **Installation scope**. The workspace holding the two databases has to
   be on the list of workspaces allowed to install the connection, and a new
   connection starts out limited to your development workspace.
5. Open the **Authorization URL** from the Configuration tab in a browser. It is
   already built for you, client ID and redirect URI included, and looks like
   this:

   ```
   https://api.notion.com/v1/oauth/authorize?client_id=<CLIENT_ID>&response_type=code&owner=user&redirect_uri=https%3A%2F%2Fgrouplink-webhook.onrender.com%2Foauth
   ```

   Pick the links and profiles databases, and approve. The browser lands on the
   receiver's 404 page. Copy the `?code=` parameter out of the address bar. It
   is a UUID, and it is good for one attempt within ten minutes.

6. Exchange the code. Set all three variables first, and keep the JSON in double
   quotes so the shell expands `$CODE` — single quotes send the literal text and
   Notion answers `Auth code must be a valid UUID`.

   ```bash
   CLIENT_ID=<client id from step 3>
   CLIENT_SECRET=<client secret from step 3>
   CODE=<code from step 5>

   curl -X POST https://api.notion.com/v1/oauth/token \
     -u "$CLIENT_ID:$CLIENT_SECRET" \
     -H "Content-Type: application/json" \
     -d "{
       \"grant_type\": \"authorization_code\",
       \"code\": \"$CODE\",
       \"redirect_uri\": \"https://grouplink-webhook.onrender.com/oauth\"
     }"
   ```

   Re-run the authorization URL for a fresh code if the exchange fails for any
   reason.

   The `access_token` in the response is `NOTION_TOKEN`. Set it on the Workflow
   at step 4. A public connection has no installation access token, and the
   OAuth client secret is not a substitute — it only authenticates this
   exchange.

Two things differ from the internal path. You choose which pages the connection
can read during the authorization flow rather than through **⋯ > Connections**,
so re-run the flow to add a database later. And the response also carries a
`refresh_token`, because Notion can rotate these tokens. Keep the client ID,
client secret, and refresh token somewhere you can find them, so a run that
starts failing with a 401 at `notion.queryDatabase` is a token exchange away
from working again.

The Webhooks tab works the same either way.

You are done here. Go back to [Deploy](#deploy) and pick up at step 3, the
Workflow service. The `access_token` goes in as `NOTION_TOKEN` at step 4.

### Forcing a run

`POST /tasks/grouplink.rebuild` on the receiver starts a run without waiting for
a Notion edit, and takes the same run input the CLI does:

```bash
curl -X POST https://grouplink-webhook.onrender.com/tasks/grouplink.rebuild \
  -H "Authorization: Bearer $DISPATCH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '[{"dryRun":true}]'
```

## Checks

- `uv run pytest` — Tier 1. Hermetic. The composition test drives the real
  `grouplink.rebuild`, routing every chained run to the owning package's
  `*_impl` with a fake at the vendor port. `pytest-socket` blocks the network,
  so a test that reaches for it fails loudly.
- `RUN_LIVE=1 uv run pytest tests/test_rebuild_live.py --enable-socket` — Tier 2.
  Hits real Notion, real sites, and a real Key Value instance in dry-run.

`tests/golden/` holds the three pages the TypeScript build rendered from the seed
links in `scripts/placeholder.py`. `tests/test_page.py` renders them again from
that same seed data and asserts byte equality, which is what keeps the HTML escaping, the URL normalization, the CSP
hashes, and the whitespace from drifting.

`uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy` cover
lint, formatting, and types.

## Where the tasks come from

Every step is a published task from
[render-tasks-python](https://github.com/render-lab/render-tasks-python),
installed from PyPI: `render-lab-tasks-{notion,scrape,render-kv,http,slack}`
and `render-lab-triggers`. All of them pin `render==1.0.1` exactly, so don't
upgrade the SDK on its own.

Each pack exports its own `Workflows` app, and `grouplink/app.py` combines them
with `Workflows.from_workflows`. Leave a pack out of that call and `ctx.run` on
one of its tasks fails at runtime. Nothing goes wrong at import.

## License

MIT. See [LICENSE](LICENSE).
