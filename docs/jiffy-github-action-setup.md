# Setting Up the Jiffy GitHub Action Workflow

This guide explains how to configure the `jiffy.yml` GitHub Action workflow
in your repository so that mentioning `@jiffy` on an Issue triggers a task
on your Jiffy Gateway.

---

## 1. Where to place the file

Create the file at:

```
.github/workflows/jiffy.yml
```

GitHub reads all `.yml` files under `.github/workflows/` automatically.

---

## 2. Trigger events and the `@jiffy` mention check

The workflow must trigger on two events:

- `issues: opened` — when a new issue is created
- `issue_comment: created` — when a comment is added to an issue

Every trigger fires on **every** issue and comment, so you must guard the
job with an `if:` condition that checks for the `@jiffy` mention:

```yaml
if: |
  (github.event_name == 'issues' && contains(github.event.issue.body, '@jiffy')) ||
  (github.event_name == 'issue_comment' && contains(github.event.comment.body, '@jiffy'))
```

Only job runs where the issue body or the comment body contains `@jiffy`
will proceed past this gate.

---

## 3. Collecting the full issue thread

The workflow uses `actions/github-script@v9` to:

1. Fetch the issue body via `github.rest.issues.get`.
2. Fetch all comments via `github.paginate(github.rest.issues.listComments, ...)`.
3. Build an ordered array of **turns**: the first turn is the issue body
   (role: `"user"`, author: the issue creator), followed by each comment
   in chronological order (role determined by comparing `comment.user.login`
   against the `JIFFY_BOT_LOGIN` variable — `"agent"` if it matches,
   `"user"` otherwise).

Each turn object includes `role`, `author`, `body`, and `created_at`.
The turns array is sent as `issue.turns` in the payload to the Gateway.

---

## 4. Payload shape expected by the Gateway

The Gateway's ingestion endpoint (`POST /api/github/ingestion`) expects a
JSON body with the following nested structure:

```json
{
  "repo": {
    "url": "https://github.com/owner/repo.git",
    "token": "github_pat_...",
    "username": "octocat"
  },
  "issue": {
    "turns": [
      {
        "role": "user",
        "author": "octocat",
        "body": "Issue body text here...",
        "created_at": "2025-01-01T00:00:00Z"
      },
      {
        "role": "user",
        "author": "other-dev",
        "body": "A comment from another developer",
        "created_at": "2025-01-01T01:00:00Z"
      },
      {
        "role": "agent",
        "author": "jiffy-bot",
        "body": "I'll take care of this!",
        "created_at": "2025-01-01T02:00:00Z"
      }
    ],
    "external_issue_id": "42"
  },
  "callback": {
    "url": "https://api.github.com/repos/owner/repo/issues/42/comments",
    "secret": "opaque-secret-value"
  }
}
```

### Field explanations

| Field | Description |
|-------|-------------|
| `repo.url` | Full clone URL of the repository. |
| `repo.token` | A GitHub PAT (or other short-lived token) with scope to clone, push, and open PRs. |
| `repo.username` | The GitHub actor who triggered the workflow (used for attribution / audit). |
| `issue.turns` | Ordered array of conversation turns. The first turn is always the issue body (role `"user"`). Subsequent turns are comments in chronological order. Role is determined by comparing the comment author's login against `JIFFY_BOT_LOGIN`. Each turn contains `role`, `author`, `body`, and `created_at`. |
| `issue.external_issue_id` | The issue number as a string. Used for deduplication and callback correlation. |
| `callback.url` | URL where the Gateway will POST the result. For GitHub this is the Issues Comments API endpoint for the originating issue. |
| `callback.secret` | Opaque secret that the Gateway forwards unchanged in the callback request. The receiving endpoint uses it to verify the callback is authentic. |

---

## 5. Required repository secrets

Create the following secrets and variables in your repository's **Settings > Secrets and
variables > Actions** page.

| Secret / Variable name | Description |
|------------------------|-------------|
| `JIFFY_GATEWAY_URL` (variable) | The base URL of your Jiffy Gateway instance (e.g. `https://jiffy.example.com`). It's not a credential — just the address the workflow posts to — so set it as a repository **variable** in **Settings > Secrets and variables > Actions > Variables**, not a secret. |
| `JIFFY_GITHUB_INGEST_TOKEN` (secret) | The shared ingestion secret for this provider / deployment. Must match the `GITHUB_INGEST_TOKEN` environment variable configured on the Gateway. Generate it with a cryptographically secure tool (see [SECRETS.md](../SECRETS.md)). |
| `JIFFY_BOT_LOGIN` (variable) | The GitHub login of the Jiffy bot account used to post replies on issues. This is used to distinguish agent replies from human comments when building the `turns` array. Set it as a repository **variable** (not a secret) in **Settings > Secrets and variables > Actions > Variables**. |
| `JIFFY_REPO_PAT` (secret) | A fine-grained GitHub Personal Access Token with **Contents** (read/write) and **Pull requests** (read/write) permissions, scoped to the repository. |
| `JIFFY_USER_WHITELIST` (variable, optional) | The `user_whitelist` config option for the Jiffy workflow (`jiffy.ci`). A comma-separated list of GitHub usernames allowed to trigger Jiffy. If undefined or empty, Jiffy is disabled in the repository (see [section 6](#6-optional-restrict-who-can-trigger-jiffy-user_whitelist)). Set it as a repository **variable** (not a secret). |

### Why not the default `GITHUB_TOKEN`?

The built-in `GITHUB_TOKEN` expires when the workflow run finishes. Since
Jiffy processes tasks asynchronously — the Gateway worker may not push
changes or open a PR until minutes or hours later — you need a PAT with a
longer lifetime. Set the expiry to a duration that matches your team's
operational cadence (e.g. 30, 60, or 90 days) and rotate it before it
expires. A shorter expiry is safer; set a calendar reminder to rotate.

---

## 6. Optional: restrict who can trigger Jiffy (`user_whitelist`)

By default, any GitHub user can trigger a Jiffy task by mentioning the bot.
If you want to restrict triggering to a specific set of users, configure the
`user_whitelist` option in the Jiffy workflow config (`jiffy.ci`). The list
is read from a repository **variable** named `JIFFY_USER_WHITELIST`.

### Config example

1. Open **Settings → Secrets and variables → Actions → Variables** in your repository.
2. Add a variable named `JIFFY_USER_WHITELIST`.
3. Set its value to a comma-separated list of GitHub usernames:

```
JIFFY_USER_WHITELIST: octocat, jane-doe
```

Usernames are compared case-insensitively, and both comma- and
whitespace-separated values are accepted.

### How the workflow behaves

| `user_whitelist` value | Behavior |
|------------------------|----------|
| Undefined or empty | The request is **not** forwarded to the Gateway. The workflow posts a single comment on the Issue explaining that no whitelist is configured and how to set `JIFFY_USER_WHITELIST`. The comment is posted at most once per Issue. |
| Defined and the mentioning user is in the list | Existing behavior — the full Issue thread is forwarded to the Gateway, no change. |
| Defined and the mentioning user is **not** in the list | Nothing happens — no comment, no forwarding. Fully silent. |

The whitelist check happens entirely in the edge component (the GitHub Action);
the Gateway is unaware of this concept.

---

## 7. Required permissions block

The workflow must declare the following top-level `permissions` so the
`actions/github-script` step can read issues, list comments, and (via the
PAT) push branches and open PRs:

```yaml
permissions:
  contents: write
  pull-requests: write
  issues: write
```

---

## 8. Complete working example

Below is the current version of `jiffy.yml` that you can copy directly:

```yaml
name: Jiffy Dispatch

permissions:
  contents: write
  pull-requests: write
  issues: write

on:
  issues:
    types: [opened]
  issue_comment:
    types: [created]

jobs:
  dispatch:
    runs-on: ubuntu-latest
    if: |
      (github.event_name == 'issues' && contains(github.event.issue.body, '@jiffy')) ||
      (github.event_name == 'issue_comment' && contains(github.event.comment.body, '@jiffy'))
    steps:
      - name: Collect thread and dispatch to Jiffy Gateway
        uses: actions/github-script@v9
        env:
          JIFFY_URL: ${{ vars.JIFFY_GATEWAY_URL }}
          JIFFY_INGEST_TOKEN: ${{ secrets.JIFFY_GITHUB_INGEST_TOKEN }}
          JIFFY_BOT_LOGIN: ${{ vars.JIFFY_BOT_LOGIN }}
          JIFFY_USER_WHITELIST: ${{ vars.JIFFY_USER_WHITELIST }}
          REPO_TOKEN: ${{ secrets.JIFFY_REPO_PAT }}
          ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION: true
        with:
          script: |
            const crypto = require('crypto');
            const issueNumber = context.payload.issue.number;

            const whitelist = (process.env.JIFFY_USER_WHITELIST || '')
              .split(/[\s,]+/)
              .map((login) => login.trim().toLowerCase())
              .filter(Boolean);
            const mentionerLogin = String(context.actor || '').toLowerCase();
            const whitelistNoticeMarker = 'no `user_whitelist` is configured';

            if (whitelist.length === 0) {
              const existingComments = await github.paginate(github.rest.issues.listComments, {
                owner: context.repo.owner,
                repo: context.repo.repo,
                issue_number: issueNumber
              });
              const alreadyNotified = existingComments.some((comment) =>
                (comment.body || '').includes(whitelistNoticeMarker)
              );
              if (!alreadyNotified) {
                await github.rest.issues.createComment({
                  owner: context.repo.owner,
                  repo: context.repo.repo,
                  issue_number: issueNumber,
                  body: [
                    'Jiffy can\'t run in this repository yet — no `user_whitelist` is configured.',
                    '',
                    'To enable Jiffy, set the `user_whitelist` option in the Jiffy workflow config (the `jiffy.ci` GitHub Action):',
                    '',
                    '1. Open **Settings → Secrets and variables → Actions → Variables** in this repository.',
                    '2. Add a repository variable named `JIFFY_USER_WHITELIST`.',
                    '3. Set it to a comma-separated list of GitHub usernames allowed to trigger Jiffy, e.g. `octocat, jane-doe`.',
                    '',
                    'Only listed usernames can trigger tasks. Once configured, mention the bot again on this issue to try.'
                  ].join('\n')
                });
              }
              console.log('user_whitelist is empty or unset; skipping dispatch to the Jiffy Gateway.');
              return;
            }

            if (!whitelist.includes(mentionerLogin)) {
              console.log(`@${context.actor} is not in user_whitelist; skipping dispatch to the Jiffy Gateway.`);
              return;
            }

            const issue = await github.rest.issues.get({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: issueNumber
            });

            const comments = await github.paginate(github.rest.issues.listComments, {
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: issueNumber
            });

            const botLogin = process.env.JIFFY_BOT_LOGIN || '';

            const turns = [
              {
                role: 'user',
                author: issue.data.user.login,
                body: issue.data.body || '',
                created_at: issue.data.created_at,
              },
              ...comments.map(c => ({
                role: c.user.login === botLogin ? 'agent' : 'user',
                author: c.user.login,
                body: c.body || '',
                created_at: c.created_at,
              })),
            ];

            const repoToken = process.env.REPO_TOKEN;
            const repoUrl = `https://github.com/${context.repo.owner}/${context.repo.repo}.git`;
            const callbackUrl = `https://api.github.com/repos/${context.repo.owner}/${context.repo.repo}/issues/${issueNumber}/comments`;
            const callbackSecret = repoToken;

            const payload = {
              repo: { url: repoUrl, token: repoToken, username: context.actor },
              issue: { turns: turns, external_issue_id: String(issueNumber) },
              callback: { url: callbackUrl, secret: callbackSecret }
            };

            const response = await fetch(`${process.env.JIFFY_URL}/api/github/ingestion`, {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
                'X-JIFFY-TOKEN': process.env.JIFFY_INGEST_TOKEN
              },
              body: JSON.stringify(payload)
            });

            const responseText = await response.text();

            if (response.status >= 400) {
              core.setFailed(`Jiffy ingestion failed with status ${response.status}: ${responseText}`);
            }
```

---

## 9. Common pitfalls

### Token expiry causing push / callback failures

`JIFFY_REPO_PAT` must be a PAT with a long-enough expiry, not the short-lived
`GITHUB_TOKEN`. If the PAT expires mid-task, the Gateway worker will fail
when it tries to push the new branch or open a PR. Set a sensible expiry
(e.g. 90 days) and rotate before it expires.

### Raw `${{ }}` interpolation into shell commands

Avoid passing `${{ secrets.SECRET_NAME }}` directly into a `run:` shell
script. GitHub Actions interpolates these as environment variables, but
shell expansions can leak values in error output or process listings.
Use `env:` to inject secrets and reference them via `process.env` (when
using `actions/github-script`) or `$SECRET_NAME` (when using `env:` with
a `run:` step).

### Why `actions/github-script` instead of plain bash/curl

The `actions/github-script` action provides:

- An authenticated `github` client (via `github.rest.*`) without
  manually managing the API token.
- A `context` object with structured event metadata (`context.repo`,
  `context.payload`, `context.actor`, etc.).
- Built-in helpers like `github.paginate()` to traverse all pages of
  the comments API with a single call.
- Automatic error handling and logging via `core.setFailed()`.

Using plain `bash` + `curl` for this workflow would require manually
constructing API calls, handling pagination, managing the API token, and
parsing JSON responses — all of which `actions/github-script` handles
natively. Prefer it for any workflow that interacts with the GitHub API.
