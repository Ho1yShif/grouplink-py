# Moving grouplink to render-lab

This runbook moves `Ho1yShif/grouplink-py` to `render-lab/grouplink-py` and
replaces the personal access token (PAT) with a GitHub App owned by render-lab.
`grouplink-ts` has the same runbook in its `docs/github.md`. The manager steps
cover both repos, so the manager does them once.

- **Manager**: a render-lab org owner. Creates and installs the app.
- **IC**: `Ho1yShif`, a render-lab member. Does everything else.

## End state

- The repo is `render-lab/grouplink-py`.
- The Workflow commits pages as the render-lab GitHub App, so its commits show
  `<app-slug>[bot]` as the author.
- No Render service has a personal token set, and the old PAT is revoked.

## Order

Do the steps in this order:

1. IC: [I1. Add GitHub App auth to `render-lab-tasks-github`](#i1-add-github-app-auth-to-render-lab-tasks-github)
2. Manager: [M1. Create the GitHub App](#m1-create-the-github-app)
3. IC: [I2. Pause the pipeline](#i2-pause-the-pipeline) and [I3. Transfer the repo](#i3-transfer-the-repo)
4. Manager: [M2. Install the apps](#m2-install-the-apps) and [M3. Let the app push to `main`](#m3-let-the-app-push-to-main)
5. IC: [I4](#i4-update-render) through [I7](#i7-revoke-the-pat)

I1 and M1 can run at the same time. The rest must follow the order, for these
reasons:

- The Workflow can't use a GitHub App until I1 ships. `render-lab-tasks-github`
  0.1.1 sends `GITHUB_TOKEN` as a bearer token on every request. An
  installation token expires after one hour, so a pasted token stops working
  between Notion edits.
- A fine-grained PAT scoped to `Ho1yShif` loses access to the repo after the
  transfer, so commits fail until I4 is done. I2 pauses commits for that gap.
- An app installed with **Only select repositories** can only select repos that
  render-lab already owns, so M2 must come after I3.

## Manager steps

Run these with `gh` and `jq`, signed in as a render-lab owner. Listing the org's
app installations needs the `admin:org` scope:

```bash
gh auth refresh -h github.com -s admin:org
```

### M1. Create the GitHub App

GitHub has no API that creates an app. The manifest flow is the closest
equivalent: a local form posts the app settings, you click one button, and one
API call returns the credentials.

The app gets only these repository permissions. It has no organization
permissions and no webhook.

- `Contents`: Read and write. The run reads the current pages and commits new
  ones.
- `Metadata`: Read. GitHub requires this permission.

```bash
cat > /tmp/grouplink-app.html <<'EOF'
<form id="f" method="post" action="https://github.com/organizations/render-lab/settings/apps/new">
<input type="hidden" name="manifest" value='{
  "name": "render-lab-grouplink",
  "url": "https://grouplink-site.onrender.com",
  "redirect_url": "https://grouplink-site.onrender.com",
  "public": false,
  "hook_attributes": {"url": "https://grouplink-site.onrender.com", "active": false},
  "default_permissions": {"contents": "write", "metadata": "read"},
  "default_events": []
}'>
</form>
<script>document.getElementById("f").submit()</script>
EOF
open /tmp/grouplink-app.html
```

1. In the browser, click **Create GitHub App**. If GitHub says the name is
   taken, change `name` in the file and run `open` again. The name becomes the
   commit author.
2. GitHub redirects to the grouplink page. Copy the `code` value from the
   address bar. It expires in one hour.
3. Exchange the code for the app's credentials:

   ```bash
   CODE=<code>
   gh api -X POST "app-manifests/$CODE/conversions" > /tmp/grouplink-app.json
   jq '{id, slug}' /tmp/grouplink-app.json
   jq -r .pem /tmp/grouplink-app.json > /tmp/grouplink-app.pem
   ```

4. Save the app ID, the slug, and the contents of `grouplink-app.pem` in the
   team password manager, and share the entry with the IC.
5. Delete the local copies. The JSON file also holds a client secret that the
   app doesn't use.

   ```bash
   rm /tmp/grouplink-app.*
   ```

Don't install the app yet.

### M2. Install the apps

Wait until the IC has transferred both repos (I3).

Install the grouplink app on both repos. In the browser, pick render-lab, then
**Only select repositories**, then `grouplink-py` and `grouplink-ts`:

```bash
SLUG=render-lab-grouplink   # the slug from M1
open "https://github.com/apps/$SLUG/installations/new"
```

Get the installation ID and add it to the password manager entry:

```bash
gh api orgs/render-lab/installations \
  --jq ".installations[] | select(.app_slug==\"$SLUG\") | .id"
```

Check that Render can see both repos:

```bash
gh api orgs/render-lab/installations \
  --jq '.installations[] | select(.app_slug=="render") | {id, repository_selection}'
```

- `"repository_selection": "all"`: nothing to do.
- `"repository_selection": "selected"`: add both repos to the installation. The
  REST endpoint for this accepts only a classic PAT, so use the browser:
  `open https://github.com/organizations/render-lab/settings/installations/<id>`.
- No output: Render isn't installed on render-lab. Run
  `open https://github.com/apps/render/installations/new`, pick render-lab, and
  select both repos.

Give the IC admin on both repos:

```bash
for r in grouplink-py grouplink-ts; do
  gh api -X PUT "repos/render-lab/$r/collaborators/Ho1yShif" -f permission=admin
done
```

### M3. Let the app push to `main`

The Workflow commits straight to `main`. A ruleset that requires a pull request
or a status check blocks it. List the org rulesets:

```bash
gh api orgs/render-lab/rulesets --jq '.[] | {id, name, enforcement}'
```

If no active ruleset covers `grouplink-*`, you're done. Otherwise add the app to
that ruleset's bypass list:

```bash
RULESET_ID=<id>
APP_ID=<app id from M1>
gh api "orgs/render-lab/rulesets/$RULESET_ID" \
  | jq --argjson app "$APP_ID" \
      '{bypass_actors: ((.bypass_actors // []) + [{actor_id: $app, actor_type: "Integration", bypass_mode: "always"}])}' \
  | gh api -X PUT "orgs/render-lab/rulesets/$RULESET_ID" --input -
```

Tell the IC that M2 and M3 are done.

## IC steps

The IC needs write access to `render-lab/render-tasks-python` for I1 and a
Render role that can edit service settings and environment variables.

### I1. Add GitHub App auth to `render-lab-tasks-github`

Change `GitHubClient` in `render-lab/render-tasks-python`. It authenticates as
an app installation when these variables are set, and falls back to
`GITHUB_TOKEN` when they are not:

| Var                          | Value                        |
| ---------------------------- | ---------------------------- |
| `GITHUB_APP_ID`              | The app ID from M1.          |
| `GITHUB_APP_PRIVATE_KEY`     | The PEM private key from M1. |
| `GITHUB_APP_INSTALLATION_ID` | The installation ID from M2. |

The client already uses `httpx`:

1. Sign a JSON Web Token with the app ID and private key, using
   [PyJWT](https://pyjwt.readthedocs.io/) with the `crypto` extra and RS256.
2. Exchange it at `POST /app/installations/{installation_id}/access_tokens` for
   an installation token.
3. Cache the token, and get a new one a few minutes before its `expires_at`.

The `HttpClient` in `render-lab-tasks-core` already awaits an async auth
callback, so the change stays inside `render-lab-tasks-github`. The full plan
is in `render-tasks-python/github.md`. See GitHub's guide,
[Authenticating as a GitHub App installation](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation).

Publish the release, then bump the package here:

```bash
uv lock --upgrade-package render-lab-tasks-github
uv export --frozen --no-dev --no-emit-project -o requirements.txt
```

Document the three variables in `.env.example` and in the README section "The
GitHub token". The PAT still works through the fallback, so this change can
merge before the transfer.

### I2. Pause the pipeline

On the Workflow service, set `DRY_RUN=true`. A Notion edit during the move then
reads and health-checks but doesn't commit or deploy.

### I3. Transfer the repo

render-lab lets members create repos, so the IC can transfer into it:

```bash
gh api -X POST repos/Ho1yShif/grouplink-py/transfer -f new_owner=render-lab
git remote set-url origin git@github.com:render-lab/grouplink-py.git
```

GitHub redirects the old URL for web and git traffic. Transfer `grouplink-ts`
too, then ask the manager to run M2 and M3.

### I4. Update Render

On each service built from this repo, open **Settings > Build & Deploy >
Repository**. If it shows `Ho1yShif/grouplink-py`, change it to
`render-lab/grouplink-py`. In the deployment from `manual.md`, those services
are `grouplink-site`, the `grouplink-py` Workflow, and `grouplink-webhook-py`.
`grouplink-site` is shared with `grouplink-ts`, so point it at whichever repo is
live.

On the Workflow service, set these environment variables from the password
manager entry:

- `GITHUB_REPO_OWNER=render-lab`
- `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, and `GITHUB_APP_INSTALLATION_ID`.
  Paste the PEM as it is. Render accepts multi-line values.
- Delete `GITHUB_TOKEN`. If it stays, the fallback can hide a broken app setup.

### I5. Update the repo

Change every `Ho1yShif/grouplink-py` reference to `render-lab/grouplink-py`:

- The Deploy to Render button in the README.
- The repository names in `manual.md`. Change its `Ho1yShif/grouplink-ts`
  references too.

Commit and push to `main`.

### I6. Verify

1. Start a dry run from the Workflow's Tasks page with `[{"dryRun":true}]` and
   confirm it finishes.
2. Remove `DRY_RUN` from the Workflow.
3. Edit a row in Notion. Confirm that a commit by `<app-slug>[bot]` appears on
   `main`, that `grouplink-site` deploys, and that the page shows the edit.
4. Push a change under `grouplink/` and confirm that `grouplink-webhook-py`
   deploys from the new repo.

### I7. Revoke the PAT

In GitHub, go to **Settings > Developer settings > Personal access tokens** and
delete the token the Workflow used. Delete any copy in a local `.env` or the
password manager.
