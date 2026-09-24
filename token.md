# Fixing the 403 on commit

Run `trn-08l4gdaq6tb8jo6nc73db7gh0` failed all four attempts at
`github.commitFiles`. `GITHUB_TOKEN` can read `ho1yshif/grouplink-py` and cannot
write to it.

## What happened

At 2026-09-24 00:30 UTC the commit subtask made three GitHub calls:

| Call | Result |
| --- | --- |
| `GET /repos/ho1yshif/grouplink-py/git/ref/heads/main` | 200 |
| `GET /repos/ho1yshif/grouplink-py/git/commits/795e174` | 200 |
| `POST /repos/ho1yshif/grouplink-py/git/blobs` | 403 `Resource not accessible by personal access token` |

Every attempt made the same three calls and stopped at the same one. The rebuild
got to step 8 of 10, so nothing was committed and `grouplink-site` was not
deployed. Slack got the error.

The schemeless-URL fix works. The run at 00:15 failed with `Request URL is
missing an 'http://' or 'https://' protocol`, and the runs after commit `795e174`
deployed got through the scrape and the health check to the commit.

## Why the reads pass and the write fails

`ho1yshif/grouplink-py` is public, so anyone can read it and the two 200s say
nothing about the token. GitHub returns 403 with `Resource not accessible by
personal access token` when a token authenticates but lacks the permission for
the call, and 401 when it is expired or revoked. The token is live and has no
write access to the repo contents.

Three causes are ruled out:

- Branch protection. `main` has no protection rule and the repo has no rulesets.
- Expiry. That returns 401.
- Org approval of a fine-grained token. The repo is on a personal account.

That leaves the token's own grant.

| Token type | What is likely wrong |
| --- | --- |
| Fine-grained (`github_pat_…`) | `Contents` is `Read` instead of `Read and write`, or repository access is `Public repositories` or a list that omits grouplink-py. |
| Classic (`ghp_…`) | Neither `repo` nor `public_repo` is checked. |

## Fix it

1. Find which token is set. Render dashboard → the Workflow
   `wfl-daons7qd0e5s73fn2llg` → Environment → `GITHUB_TOKEN`. The value is
   hidden, so match it to a token at GitHub → Settings → Developer settings by
   name and last-used date.

2. Reproduce the failure before changing anything. Export the token locally and
   post a blob:

   ```
   curl -s -o /dev/null -w '%{http_code}\n' \
     -X POST https://api.github.com/repos/ho1yshif/grouplink-py/git/blobs \
     -H "Authorization: Bearer $GITHUB_TOKEN" \
     -d '{"content":"probe","encoding":"utf-8"}'
   ```

   403 confirms the diagnosis. 201 means the token is fine and the Workflow holds
   a different value. A blob no tree points at is unreachable and gets collected,
   so the probe leaves nothing in the repo.

   For a classic token, `curl -sI -H "Authorization: Bearer $GITHUB_TOKEN"
   https://api.github.com/user | grep -i x-oauth-scopes` prints its scopes. A
   fine-grained token returns that header empty.

3. Grant write access.

   - Fine-grained: open the token, set Repository access to **Only select
     repositories** with `grouplink-py` in the list, set `Contents` to **Read and
     write**, and save. The edit applies to the existing value, so there is
     nothing to re-paste.
   - Classic: a scope change needs a new token. Create one with `public_repo`.

4. Run the step 2 probe again. It should return 201.

5. If the value changed, update `GITHUB_TOKEN` on the Workflow and save. Render
   restarts the service. Revoke the old token.

6. Trigger a run and watch it:

   ```
   render workflows start grouplink-py/grouplink.rebuild --input='[{}]' --confirm
   render logs --resources wfl-daons7qd0e5s73fn2llg --tail
   ```

7. Confirm the blob POST returns 201, the run reaches the deploy step, and
   `grouplink-site` deploys.

## Rate-limit warnings

Seven lines in the run read `Request to Render failed (1/25), retry in 0.2s: get
task result failed with status 429`. Each one succeeded on the first retry. The
Workflows client polls for subtask results and hits the Render API limit while it
waits. Nothing in the rebuild failed. Leave it.

## After the fix

`github.md` replaces the personal token with a render-lab GitHub App. That is a
separate piece of work. Whichever token is in place today needs write access
first, or every run keeps failing at the same call.
