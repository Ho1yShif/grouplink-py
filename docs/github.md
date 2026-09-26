# Moving grouplink to render-lab

This is the plan to move the repo from `Ho1yShif/grouplink-py` to
`render-lab/grouplink-py` and to replace the personal access token (PAT) with a
GitHub App owned by render-lab. Do the steps in order. Each step keeps the page
working or pauses it on purpose.

## End state

- The repo is `render-lab/grouplink-py`.
- The Workflow commits pages as a render-lab GitHub App, so its commits show
  `<app-name>[bot]` as the author.
- No personal token is set on any Render service, and the old PAT is revoked.

## Why the order matters

- After the transfer, a fine-grained PAT that you scoped to your own account
  loses access to the repo, so commits fail until the app is in place. A classic
  PAT keeps working but still acts as you.
- Render reaches the repo through the Render GitHub App installation on the
  account that owns it. After the transfer, the installation on your account
  can't see the repo, so Render can't build or deploy it until the installation
  on render-lab includes it.
- The Workflow can't use a GitHub App yet. `render-lab-tasks-github` 0.1.1 is
  the latest release, and its client sends `GITHUB_TOKEN` from the environment
  as the bearer token on every request. Every `ctx.run` runs on its own instance
  and reads `GITHUB_TOKEN` from the service environment. An installation token
  expires after one hour, so a pasted token stops working between Notion edits.
  The package needs app support first (step 1).

## Permissions you need

| Where                                  | Permission                                                                                     | Needed for     |
| -------------------------------------- | ---------------------------------------------------------------------------------------------- | -------------- |
| `Ho1yShif/grouplink-py`                | Admin (you own it)                                                                             | Step 5         |
| render-lab org                         | Permission to create repos. You are a member and members can create repos, so you have it.     | Step 5         |
| render-lab org                         | Owner, or the GitHub App manager role, to create the app and install it                        | Steps 2 and 6  |
| render-lab org                         | Owner, to install the Render GitHub App or add a repo to its installation                      | Step 3         |
| render-lab org                         | Admin on the new repo, or owner, to edit rulesets and branch protection                        | Step 8         |
| `render-lab/render-tasks-python`       | Write, to change `render-lab-tasks-github` and publish a release                               | Step 1         |
| Render workspace                       | A role that can edit service settings and environment variables                                | Steps 4 and 7  |

You are a member of render-lab, not an owner. Ask an org owner for the GitHub App
manager role, or ask them to do steps 2, 3, and 6 with you.

## The GitHub App's permissions

Give the app only these repository permissions:

- `Contents`: Read and write. The run reads the branch tree and the current
  pages, then commits the changed pages.
- `Metadata`: Read. GitHub makes this permission mandatory.

Leave every other permission at no access. The app needs no organization or
account permissions and no webhook events.

## Steps

### 1. Add GitHub App auth to `render-lab-tasks-github`

Change `GitHubClient` in `render-lab/render-tasks-python` so that it
authenticates as an app installation when these variables are set, and falls
back to `GITHUB_TOKEN` when they are not:

| Var                          | Value                                             |
| ---------------------------- | ------------------------------------------------- |
| `GITHUB_APP_ID`              | The app's ID, from its settings page.             |
| `GITHUB_APP_PRIVATE_KEY`     | The PEM private key that you generate in step 2.  |
| `GITHUB_APP_INSTALLATION_ID` | The ID of the app's installation on render-lab.   |

The client already uses `httpx`, so the smallest change is:

1. Sign a JSON Web Token with the app ID and private key, using
   [PyJWT](https://pyjwt.readthedocs.io/) with the `crypto` extra and the RS256
   algorithm.
2. Exchange it at `POST /app/installations/{installation_id}/access_tokens` for
   an installation token.
3. Cache the token and get a new one a few minutes before its `expires_at`.

The `HttpClient` in `render-lab-tasks-core` already awaits an async auth
callback, so the change stays inside `render-lab-tasks-github`. The full plan is
in `render-tasks-python/github.md`. See also GitHub's guide,
[Authenticating as a GitHub App installation](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation).

Publish the release, then bump `render-lab-tasks-github` in `pyproject.toml`
and regenerate `requirements.txt`:

```bash
uv lock --upgrade-package render-lab-tasks-github
uv export --frozen --no-dev --no-emit-project -o requirements.txt
```

Update `.env.example` and the "The GitHub token" section of the README to
document the three new variables. The PAT keeps working through the fallback,
so you can merge this change while the repo is still on `Ho1yShif`.

### 2. Create the GitHub App in render-lab

In render-lab's settings, go to **Developer settings > GitHub Apps > New
GitHub App**.

- Name it, for example `render-lab-grouplink`. The name is the commit author.
- Set any homepage URL, such as the repo URL.
- Under **Webhook**, clear **Active**.
- Set the permissions from [The GitHub App's permissions](#the-github-apps-permissions).
- Under **Where can this GitHub App be installed?**, pick **Only on this
  account**.

After you create the app, note the App ID and click **Generate a private key**.
Store the downloaded `.pem` file in the team password manager. Don't install the
app yet, because the repo isn't in render-lab. If you also move `grouplink-ts`,
create one app and install it on both repos.

### 3. Give Render access to render-lab

In render-lab's settings, go to **GitHub Apps** and find the Render app. If it is
installed with **Only select repositories**, you add the repo in step 6. If it
isn't installed, connect the render-lab org from the Render Dashboard. Render
starts the GitHub install flow the first time you pick a repo from a new
account.

### 4. Pause the pipeline

On the Workflow service, set `DRY_RUN=true`. A Notion edit during the move then
reads and health-checks but doesn't try to commit or deploy.

### 5. Transfer the repo

On `Ho1yShif/grouplink-py`, go to **Settings > General > Danger Zone > Transfer**
and enter `render-lab`. GitHub redirects the old URL to the new one for web and
git traffic.

Point your local clone at the new URL:

```bash
git remote set-url origin git@github.com:render-lab/grouplink-py.git
```

### 6. Install both apps on the repo

- Install the grouplink GitHub App on render-lab with **Only select
  repositories** and pick `grouplink-py`. The installation ID is the number at
  the end of the installation's settings URL:
  `https://github.com/organizations/render-lab/settings/installations/<id>`.
- Add `grouplink-py` to the Render GitHub App's installation on render-lab.

### 7. Update Render

Open **Settings > Build & Deploy > Repository** on each service built from this
repo. In the deployment from `manual.md`, those services are `grouplink-site`,
the `grouplink-py` Workflow, and `grouplink-webhook-py`. If a service still
shows `Ho1yShif/grouplink-py`, change it to `render-lab/grouplink-py`. The
static site is shared with `grouplink-ts`, so point it at whichever repo is live.

On the Workflow service, set these environment variables:

- `GITHUB_REPO_OWNER=render-lab`
- `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, and `GITHUB_APP_INSTALLATION_ID`.
  Render accepts a multi-line value, so paste the PEM file as it is.
- Delete `GITHUB_TOKEN`. If you keep it, the fallback can hide a broken app
  setup.

### 8. Check branch protection

The run commits straight to `main` with a fast-forward ref update. If render-lab
has an org ruleset, or you add a rule on `main` that requires a pull request or a
passing status check, the commit fails. Add the grouplink app to the rule's
bypass list, or leave `main` without those rules.

### 9. Update the repo

Change every `Ho1yShif/grouplink-py` reference to `render-lab/grouplink-py`:

- The Deploy to Render button in the README.
- The repository names in `manual.md`. Its references to
  `Ho1yShif/grouplink-ts` change to `render-lab/grouplink-ts` only if that repo
  moves too.

Commit and push to `main`.

### 10. Verify

1. Start a dry run from the Workflow's Tasks page with
   `[{"dryRun":true}]` and confirm it finishes.
2. Remove `DRY_RUN` from the Workflow, or set it to `false`.
3. Edit a row in Notion. Confirm that a commit by `<app-name>[bot]` appears on
   `main`, that `grouplink-site` deploys, and that the page shows the edit.
4. Push a change under `grouplink/` and confirm `grouplink-webhook-py` deploys
   from the new repo.

### 11. Revoke the PAT

In your GitHub account, go to **Settings > Developer settings > Personal access
tokens** and delete the token that the Workflow used. Also delete any copy of it
in a local `.env` or a password manager.
