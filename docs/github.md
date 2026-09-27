# Moving grouplink to render-lab

This runbook moves `Ho1yShif/grouplink-py` to `render-lab/grouplink-py` and
replaces the personal access token (PAT) with a GitHub App owned by render-lab.
`grouplink-ts` has the same runbook in its `docs/github.md`. The manager steps
cover both repos, so the manager does them once.

- **Manager**: a render-lab org owner. Approves the PRs and creates and
  installs the app.
- **IC**: `Ho1yShif`, a render-lab member. Does everything else.

## End state

- The repo is `render-lab/grouplink-py`.
- The Workflow commits pages as the render-lab GitHub App, so its commits show
  `<app-slug>[bot]` as the author.
- No Render service has a personal token set, and the old PAT is revoked.

## Order

Do the steps in this order:

1. Manager: [M1. Approve the PRs](#m1-approve-the-prs) and [M2. Create the GitHub App](#m2-create-the-github-app)
2. IC: [I1. Merge the PRs and start the releases](#i1-merge-the-prs-and-start-the-releases)
3. Manager: [M3. Approve the releases](#m3-approve-the-releases)
4. IC: [I2](#i2-bump-render-lab-tasks-github) through [I4. Transfer the repo](#i4-transfer-the-repo)
5. Manager: [M4. Install the apps](#m4-install-the-apps) and [M5. Let the app push to `main`](#m5-let-the-app-push-to-main)
6. IC: [I5](#i5-update-render) through [I8](#i8-revoke-the-pat)

The steps must follow this order:

- The Workflow can't use a GitHub App until the new package ships.
  `render-lab-tasks-github` 0.1.1 sends `GITHUB_TOKEN` as a bearer token on
  every request. An installation token expires after one hour, so a pasted
  token stops working between Notion edits.
- A fine-grained PAT scoped to `Ho1yShif` loses access to the repo after the
  transfer, so commits fail until I5 is done. I3 pauses commits for that gap.
- An app installed with **Only select repositories** can only select repos that
  render-lab already owns, so M4 must come after I4.

## Manager steps

Run these with `gh` and `jq`, signed in as a render-lab owner. Listing the org's
app installations needs the `admin:org` scope:

```bash
gh auth refresh -h github.com -s admin:org
```

### M1. Approve the PRs

Two PRs by the IC add GitHub App auth to the `tasks-github` packs. When
`GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, and `GITHUB_APP_INSTALLATION_ID` are
set, the client mints installation tokens and replaces them before they expire.
App auth takes precedence over `GITHUB_TOKEN`. Both PRs pass CI and passed a
live test against a test app on 2026-09-26.

| PR                                                                                           | Package                          |
| -------------------------------------------------------------------------------------------- | -------------------------------- |
| [render-lab/render-tasks-python#6](https://github.com/render-lab/render-tasks-python/pull/6) | `render-lab-tasks-github` 0.2.0  |
| [render-lab/render-tasks#37](https://github.com/render-lab/render-tasks/pull/37)             | `@render-lab/tasks-github` 0.9.0 |

The IC asked for a careful review of the TypeScript in #37.

```bash
gh pr diff 6 -R render-lab/render-tasks-python
gh pr review 6 -R render-lab/render-tasks-python --approve

gh pr diff 37 -R render-lab/render-tasks
gh pr review 37 -R render-lab/render-tasks --approve
```

### M2. Create the GitHub App

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

### M3. Approve the releases

Each publish run waits for a reviewer on a GitHub environment: `pypi` in
render-tasks-python and `npm-publish` in render-tasks. `R4ph-t` is the required
reviewer on both. If that isn't you, forward this step.

Run this after the IC starts the releases (I1), once
`gh run list -R render-lab/<repo> --workflow publish.yml -L 1` shows `waiting`:

```bash
approve() {
  run=$(gh run list -R "render-lab/$1" --workflow publish.yml -L 1 --json databaseId --jq '.[0].databaseId')
  env=$(gh api "repos/render-lab/$1/actions/runs/$run/pending_deployments" --jq '.[0].environment.id')
  gh api -X POST "repos/render-lab/$1/actions/runs/$run/pending_deployments" \
    -F "environment_ids[]=$env" -f state=approved -f comment="tasks-github GitHub App auth"
}
approve render-tasks-python
approve render-tasks
```

### M4. Install the apps

Wait until the IC has transferred both repos (I4).

Install the grouplink app on both repos. In the browser, pick render-lab, then
**Only select repositories**, then `grouplink-py` and `grouplink-ts`:

```bash
SLUG=render-lab-grouplink   # the slug from M2
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

### M5. Let the app push to `main`

The Workflow commits straight to `main`. A ruleset that requires a pull request
or a status check blocks it. List the org rulesets:

```bash
gh api orgs/render-lab/rulesets --jq '.[] | {id, name, enforcement}'
```

If no active ruleset covers `grouplink-*`, you're done. Otherwise add the app to
that ruleset's bypass list:

```bash
RULESET_ID=<id>
APP_ID=<app id from M2>
gh api "orgs/render-lab/rulesets/$RULESET_ID" \
  | jq --argjson app "$APP_ID" \
      '{bypass_actors: ((.bypass_actors // []) + [{actor_id: $app, actor_type: "Integration", bypass_mode: "always"}])}' \
  | gh api -X PUT "orgs/render-lab/rulesets/$RULESET_ID" --input -
```

Tell the IC that M4 and M5 are done.

## IC steps

The IC needs write access to `render-lab/render-tasks-python` and
`render-lab/render-tasks`, and a Render role that can edit service settings and
environment variables.

### I1. Merge the PRs and start the releases

Merge #37 first. #6 records the TypeScript commit in `parity.json`, so update
that entry on #6 to the merge commit of #37 before you merge #6.

```bash
gh pr merge 37 -R render-lab/render-tasks --squash --delete-branch
gh pr view 37 -R render-lab/render-tasks --json mergeCommit --jq .mergeCommit.oid
# Update parity.json on the github-app-auth branch, push, and wait for CI.
gh pr merge 6 -R render-lab/render-tasks-python --squash --delete-branch
```

Start both releases, then ask the manager to run M3:

```bash
gh workflow run publish.yml -R render-lab/render-tasks-python \
  -f package=render-lab-tasks-github -f version=0.2.0 -f publish=true
gh workflow run publish.yml -R render-lab/render-tasks \
  -f package=@render-lab/tasks-github
```

After the npm release, bump `tasks-github@0.8.0` to `0.9.0` in the render-tasks
quickstart, as #37 notes.

### I2. Bump `render-lab-tasks-github`

`pyproject.toml` pins the exact version, so change the pin, then regenerate
`requirements.txt`:

```bash
uv add render-lab-tasks-github==0.2.0
uv export --frozen --no-dev --no-emit-project -o requirements.txt
```

Document the three variables in `.env.example` and in the README section "The
GitHub token". The PAT still works when the app variables are unset, so this
change can ship before the transfer.

### I3. Pause the pipeline

On the Workflow service, set `DRY_RUN=true`. A Notion edit during the move then
reads and health-checks but doesn't commit or deploy.

### I4. Transfer the repo

render-lab lets members create repos, so the IC can transfer into it:

```bash
gh api -X POST repos/Ho1yShif/grouplink-py/transfer -f new_owner=render-lab
git remote set-url origin git@github.com:render-lab/grouplink-py.git
```

GitHub redirects the old URL for web and git traffic. Transfer `grouplink-ts`
too, then ask the manager to run M4 and M5.

### I5. Update Render

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
  Paste the PEM as it is. The packages accept real newlines or `\n` escapes.
- Delete `GITHUB_TOKEN`. App auth takes precedence, so the Workflow no longer
  uses it.

### I6. Update the repo

Change every `Ho1yShif/grouplink-py` reference to `render-lab/grouplink-py`:

- The Deploy to Render button in the README.
- The repository names in `manual.md`. Change its `Ho1yShif/grouplink-ts`
  references too.

Commit and push to `main`.

### I7. Verify

1. Start a dry run from the Workflow's Tasks page with `[{"dryRun":true}]` and
   confirm it finishes.
2. Remove `DRY_RUN` from the Workflow.
3. Edit a row in Notion. Confirm that a commit by `<app-slug>[bot]` appears on
   `main`, that `grouplink-site` deploys, and that the page shows the edit.
4. Push a change under `grouplink/` and confirm that `grouplink-webhook-py`
   deploys from the new repo.

This run also covers the hosted Render check that both PRs list as not done.

### I8. Revoke the PAT

In GitHub, go to **Settings > Developer settings > Personal access tokens** and
delete the token the Workflow used. Delete any copy in a local `.env` or the
password manager.
