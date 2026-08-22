# Jiffy Gateway — AGENTS.md

Central server for **Jiffy**: receives pre-filtered task requests (full Issue/thread history) from edge components (GitHub Actions / GitLab CI / Gitea Actions), provisions an isolated sandbox container, clones the repo, hands everything else — analysis, installs, implementation, verification, commit/push, PR, code-review mention — to a coding agent running **inside** the container, then reports the result via `callback.url`.

The Gateway is deliberately thin. It never parses requirements, detects languages/versions, names branches, opens PRs, or performs code review. The **agent** decides all of that from the verbatim issue text.

## Layout

- `apps/ingestion/` — DRF ingestion views (`github/ingestion`, `gitlab/ingestion`, `gitea/ingestion`), token auth, payload serializers, callback dispatch.
- `jobs/` — the core app. `models.py` (`Task`), `tasks.py` (`execute_task`), `execution/` (`container.py` = Docker lifecycle + clone + network restriction, `agent.py` = instructions + result parsing), `callback_specs.py` (per-provider callback wire format), `utils/redis.py`.
- `config/` — Django project. `settings/{base,test}.py`, `celery.py`, `urls.py`.
- `docker/sandbox/` — the single generic sandbox image (Dockerfile, build/smoke scripts, README).
- `tests/` — Django tests plus `tests/edge/jiffy_workflow.test.cjs` (Node test for the GitHub edge workflow).
- `.github/workflows/` — `ci.yml`, `release.yml`, `docker-publish.yml`, and `jiffy.yml` (the GitHub *edge* dispatch workflow — out of scope for this repo, but regression-tested).

There is **no `apps/execution`** app and no Task `admin.py`; don't go looking for either.

## Commands

Managed with `uv`. No lint or typecheck tooling is configured.

```bash
uv sync --frozen --all-extras          # install
uv run python manage.py test           # CI runs this with DJANGO_SETTINGS_MODULE=config.settings.test
uv run pytest                          # same suite; pytest reports 122, manage.py test 111, in-memory SQLite
node tests/edge/jiffy_workflow.test.cjs # GitHub edge workflow regression test (manual)
uv run python manage.py migrate
uv run python manage.py runserver      # dev server
uv run celery -A config worker -Q execute -l info -n execute@%h   # concurrency from CELERY_WORKER_CONCURRENCY (default 1)
docker compose up                      # web + celery + docker-socket-proxy + redis (+ optional sandbox dev container)
```

Test settings (`config.settings.test`) use in-memory SQLite so no DB file is touched. Note the local `.venv` may be root-owned/broken; a fresh `uv sync` into another env works.

## End-to-end flow

1. Ingestion view authenticates via `X-JIFFY-TOKEN` header (`hmac.compare_digest` against the provider's env secret), validates the payload, creates `Task(status="queued")`, writes the payload to Redis, acquires a dedup lock, then enqueues `execute_task.apply_async(args=[task.id], task_id=celery_uuid())` via `transaction.on_commit` and returns `202`. Duplicate delivery → `202 {"status": "already_queued"}`, no new task.
2. `execute_task` (`jobs/tasks.py`): ensure sandbox image → `provisioning` → start container → `cloning` → `git clone` → `running` → write instructions → `opencode run --auto` → read `/workspace/.jiffy_result.json` → cleanup container → `done`/`failed` + callback.
3. Status transitions actually set: `queued → provisioning → cloning → running → done|failed|needs_input`. `needs_input` means the agent stopped to ask a question (see "Agent questions"). The `reporting` status exists in the model but is **never set** by current code.

## Data model & storage rules

- `Task`: `provider`, `repo_url`, `issue_external_id`, `callback_url`, `callback_secret`, `status`, plus audit-only fields (`programming_language`, `branch_name`, `pr_url`, `error_message`, `question`) populated **from the agent's final result after the fact** — never pre-computed. DB is SQLite at `data/db.sqlite3`, overridable with `DATABASE_PATH` (`config/settings/base.py`), opened in WAL mode with a 30s busy timeout so the web service and the worker can read/write it concurrently. **The web container and the Celery worker must resolve that path to the same file** — both compose files mount `./data:/app/data` into both services for exactly that reason. If they diverge, the worker migrates its own empty DB and every job dies with `Task not found in DB`; the worker logs its DB path and row count at startup so this is visible in one line. The root `db.sqlite3`, `jiffy-db/`, and `jiffy_db/` are stale leftovers — don't use them.
- Full payload + repo token live **only** in Redis under `jiffy:task:{task_id}:payload` with a 4-hour TTL, written with `DjangoJSONEncoder`. Never persist them to the DB. Locks use `jiffy:lock:issue:{provider}:{external_issue_id}` (NX, 300s TTL).
- **Naming quirk**: the payload/API field is `issue.external_issue_id`; the `Task` DB column is `issue_external_id`. Both appear; don't "fix" one to match the other blindly.
- **Hard rules**: Django ORM only — no raw SQL, no SQLite-specific syntax, portable field types (`JSONField`, `CharField`), Django migrations for every schema change, and keep write transactions short (never hold a transaction across a Docker/network wait). Don't wrap the pipeline in `transaction.atomic()`.

## Ingestion payload

```json
{
  "repo": { "url": "", "token": "", "username": "" },
  "issue": { "turns": [{"role": "user|agent", "author": "", "body": "", "created_at": ""}], "external_issue_id": "" },
  "callback": { "url": "", "secret": "" }
}
```

- `issue` must have **either** `turns` (preferred, array of `{role, author, body, created_at}`) **or** legacy `text`. `repo.username` is required for GitLab/Gitea (token URL format), optional for GitHub. Missing/blank fields → `400`, never a `KeyError`/`500`.
- **No size cap on the issue text.** A thread is the issue body plus every comment, so it can run to megabytes. `DATA_UPLOAD_MAX_MEMORY_SIZE` is `None` by default (Django's 2.5 MiB default would 400 large threads at `request.body`); set `MAX_INGEST_BODY_BYTES` to a positive byte count to re-impose one. The `issue.text`/`turns[].body` fields carry no `max_length` and no whitespace trimming — the text reaches the agent verbatim. Nothing about the thread is persisted to the DB, so no column limit applies to it either.
- Per-provider secrets from env: `GITHUB_INGEST_TOKEN`, `GITLAB_INGEST_TOKEN`, `GITEA_INGEST_TOKEN` — never shared. Rotation is manual (single-secret; a brief mismatch window is accepted).
- OpenAPI/Swagger at `/api/schema/`, `/api/docs/`, `/api/redoc/`.

## Callback / reporting — agent-first, Gateway fallback

This changed recently; don't assume the Gateway owns the callback.

1. `build_agent_instructions` (in `jobs/execution/agent.py`) instructs the agent to make **exactly one** HTTP call to `callback.url` after finishing, with `Authorization: Bearer <secret>` (secret forwarded byte-for-byte, never signed/hashed) and a **human-readable markdown comment body** (`Task #N: ✅ Jiffy completed this task.` with `**Summary:**`, `**Branch:**`, `**Pull Request:**`, `### Technical Report`; failure format uses `❌` + `**Reason:**`). Omit empty lines/sections.
2. Wire format is provider-specific via `jobs/callback_specs.py`: **GitHub** = JSON `{"body": "<markdown>"}` with `Accept: application/vnd.github+json` and `X-GitHub-Api-Version: 2026-03-10` (the callback URL is the Issue Comments API); **GitLab/Gitea** = plain `text/plain; charset=utf-8`. Add a new provider by adding a spec entry, not pipeline code.
3. `execute_task` checks the agent result's `callback.attempted/succeeded`; if the agent did not deliver, the Gateway falls back via `send_fallback_callback` (same spec, retries 3×, 2s apart, logs failures — task stays `done`/`failed` even if all attempts fail). `send_callback` is the older path, used only in tests.
4. `technical_report` is a structured markdown report (`## What was done`, `## Technology / approach chosen`, `## Reasoning`, `## Setup / installation instructions`, `## Known limitations / follow-ups`).

## Agent questions

Nothing about a run is interactive, so a question the agent cannot post is a question nobody sees. The instructions tell the agent that any blocking question — ambiguity, a contradiction with the codebase, a decision it cannot make — must be reported rather than guessed at:

- The agent writes `status: "question"` plus a `question` field in `.jiffy_result.json`, and delivers it through the same one-shot callback with a `Task #N: ❓ Jiffy has a question before continuing.` body (`**Question:**`, optional `**Branch:**` / `**Progress so far:**`, and a closing "reply on this issue" note).
- `read_agent_result()` downgrades a `"question"` with no question text to `failed` — an unreadable question is worse than an honest failure.
- `execute_task` routes it to `_ask_task()`: status `needs_input`, `question` stored on the row, callback via the same agent-first/Gateway-fallback path. It is **not** a failure and is never retried.
- The answer comes back as a reply on the issue, which the edge picks up as a **brand-new** task with the full thread — same as the code-review loop. There is no synchronous wait.

## Agent instruction contract

- `build_agent_instructions(payload)` builds one agent-agnostic prompt: verbatim issue text (from `turns` if present, else `text`), workspace at `/workspace`, and the full self-provisioning workflow (analyze → install missing tools → implement → verify → commit/push on a new branch → open PR **only if asked** → mention the review bot **only if asked** → attempt callback).
- Branch fallback convention is `Jiffy/<short-description-of-change>` when the issue text doesn't specify one. The Gateway never computes branch names.
- **Required final output**: a JSON file at `/workspace/.jiffy_result.json` with `status` (`"done"`|`"failed"`|`"question"`), `branch_name`, `branch_base`, `pr_url`, `programming_language`, `summary`, `technical_report`, `error_message`, `question`, `model`, and `callback {attempted, succeeded, error}`. `read_agent_result()` parses this; a missing/malformed file is treated as a failure (not an exception). Any agent plugged into this system must honor this contract.

## Sandbox / container gotchas

- One generic image (default tag `jiffy-sandbox:1.2.0`, from `SANDBOX_IMAGE`), built from `docker/sandbox/Dockerfile` on demand by `ensure_sandbox_image()` if missing. Bundles nvm/uv/gvm, node, python, go, gh/glab, git, curl, build-essential, iptables, pnpm, and the `opencode` CLI. Everything else is installed by the agent at runtime — there is no per-language image selection and no pre-container language check.
- **Resource limits are env-driven** (`config/settings/base.py` → `jobs/execution/container.py`): `SANDBOX_MEMORY_LIMIT` (default `2g`), `SANDBOX_MEMORY_SWAP_LIMIT` (default `4g`, the *combined* mem+swap ceiling — must be ≥ the memory limit or the Gateway warns and uses 2× it; `-1` = unlimited), `SANDBOX_CPU_LIMIT` (default `1.5`, applied as `nano_cpus`, **not** `cpuset_cpus` — that used to pin a core index and couldn't express fractions). `SANDBOX_MEM_LIMIT` is kept as a backwards-compatible alias for `SANDBOX_MEMORY_LIMIT`.
- Package installs are the usual OOM (exit 137) trigger. `build_package_manager_env()` puts `NODE_OPTIONS=--max-old-space-size=<SANDBOX_NODE_MAX_OLD_SPACE_MB or half the mem limit, floor 512>`, `npm_config_maxsockets/jobs`, `CARGO_BUILD_JOBS`, `MAKEFLAGS`, `GOMAXPROCS` (all from `SANDBOX_PACKAGE_CONCURRENCY`, default 2) into the container env at create time; `_apply_pnpm_limits()` rewrites `~/.config/pnpm/config.yaml` after start because pnpm v11 ignores `npm_config_*`. That one is best-effort and never fails the task.
- The worker manages containers via the Docker SDK. **`DOCKER_HOST` must be set when the worker runs inside a container** (e.g. `tcp://docker-socket-proxy:2375`); `get_docker_client()` raises a clear error otherwise.
- **Network egress is restricted by default** (`JIFFY_SANDBOX_NETWORK_RESTRICTED=true`): the container starts with `NET_ADMIN` and `iptables` default-deny OUTPUT, allowing only the allow-list hosts (resolved at start). **Fail-closed**: if the rules can't be applied the container is torn down and the task fails.
- DNS is only allowed to Docker's embedded resolver `127.0.0.11`, which exists **only on user-defined networks** — so restricted containers are placed on `jiffy-sandbox-net` (created on demand). Don't move them to the default bridge while restriction is active.
- Allow-list = `SANDBOX_NETWORK_ALLOWLIST` (defaults: PyPI, npm, crates.io, Go proxy, GitHub/GitLab/Gitea hosts) merged with `SANDBOX_NETWORK_ALLOWLIST_EXTRA` (self-hosted git + the LLM provider endpoint — both vary per install). `JIFFY_SANDBOX_NETWORK_RESTRICTED=false` = open network, for debugging only.
- Containers are run `tty=True`, `remove=False` at create, then explicitly stopped+removed by the `start_generic_sandbox_container` context manager (`JIFFY_SANDBOX_CLEANUP=false` leaves them alive for debugging). Default user is non-root `jiffy`; the restriction script runs as root.
- `OPEN_API_BASE_URL` / `OPEN_API_KEY` are **optional**: read from the worker's own env and forwarded into the container by `build_agent_provider_env()` (names come from `SANDBOX_AGENT_ENV_PASSTHROUGH`). Unset or blank ones are skipped, so the pipeline runs unchanged without them and the agent falls back to whatever `opencode.json`/the image provides. When they are set and network restriction is on, the endpoint host must also be in `SANDBOX_NETWORK_ALLOWLIST_EXTRA`.
- There is **no env var for the OpenCode config path** — `_inject_opencode_config` reads the project-root `opencode.json` and writes it to `/home/jiffy/.config/opencode/opencode.json` inside the container. (The old `SANDBOX_OPENCODE_CONFIG_PATH` setting was dead and has been removed.)
- The agent runs as `opencode run --auto <prompt>` inside `bash -l -c` (login shell so `/etc/profile.d` version managers load), workdir `/workspace`. The instructions are staged to `/tmp/jiffy_instructions.txt` by `write_instructions_file()` as a **tar upload** (`put_archive`) — never echoed through a shell command, which would itself be an argv entry and blow the kernel's 128 KiB `MAX_ARG_STRLEN` on a large issue thread. `build_agent_command()` then substitutes the file inline (`"$(cat …)"`) while it is under `MAX_INLINE_INSTRUCTIONS_BYTES` (96 KiB), and above that hands the agent the *path* with a short bootstrap prompt telling it to read the file first — `opencode run` takes its prompt from argv only, so this is what keeps arbitrarily large threads runnable. Agent stdout is redirected to the container's main stdout for live `docker logs`.
- That exec is **detached** (`exec_create` + `exec_start(detach=True)`), then polled via `exec_inspect` every 5s until it finishes. Don't "simplify" it back to a blocking `exec_run`: that holds one Docker API connection open for the whole run, and `docker-socket-proxy` (HAProxy, `timeout client/server 10m`) cuts it at exactly 600s — docker-py then sees a clean EOF and `exec_inspect` still reports `Running`, which surfaced as the bogus `Agent exited with code None`. The run window is `SANDBOX_AGENT_TIMEOUT` (default 3600s), enforced by the Gateway itself; on timeout the task fails and the container teardown kills the run.
- Clone injects the token into the URL: GitHub `token@host`, GitLab/Gitea `username:token@host`. The token is passed as container env `REPO_TOKEN` — never in the DB or baked into an image.

## Celery & worker

- Celery app lives in `config/celery.py` and autodiscovers `jobs` + `apps.ingestion`. **The app module is `config`, not `jiffy`.** Single queue `execute`; concurrency comes from `CELERY_WORKER_CONCURRENCY` (settings → `worker_concurrency`, default 1) — the compose commands deliberately pass **no** `--concurrency` flag so the env var is what applies. Each slot can hold a full sandbox container, so size it as `concurrency × SANDBOX_MEMORY_LIMIT`.
- `execute_task`: `bind=True, max_retries=3, default_retry_delay=60, acks_late=True`, routed to `execute`. Retries happen only on unexpected internal exceptions (transient errors); `ExecutionError` and agent/logical failures go straight to `failed` + callback with no retry.
- A missing `Task` row is never dropped silently: `_fetch_task` re-reads it `TASK_LOOKUP_ATTEMPTS` times on a fresh connection, then Celery re-delivers the job (`TASK_LOOKUP_RETRY_COUNTDOWN`); once the retries are exhausted the worker reports the failure to `callback.url` using the provider/callback data kept in the Redis payload (ingestion stores `provider` there for this).
- `worker_process_init` closes DB connections inherited from the parent process — a SQLite connection must not be reused across the prefork `fork()`.
- On `worker_ready`, orphaned tasks still in `queued` status are re-dispatched (crash recovery). The startup `ensure_sandbox_image` call there is currently commented out; the image is still ensured per job.

## Code review loop

The Gateway never reviews and never decides whether a review was requested. The agent reads the issue text: if a review is asked, it mentions the code-review bot in the PR description (if it opened one) or in its final summary (which flows into the callback). The exact bot handle is **not finalized** — keep it a placeholder string inside the instructions. Any review feedback that leads to changes comes back as a **new** Jiffy mention → a brand-new task; there is no synchronous review step.

## Git / CI / release workflow

- `develop` is the active development branch; `master` is the release branch (currently ~13 commits behind). Feature branches use `Jiffy/<slug>`. CI runs on PRs/pushes to `master` and `staging`: `uv sync --frozen --all-extras` → migrations → `manage.py test`.
- Releases are generated by `python-semantic-release` on push to `master` (release.yml → docker-publish.yml to GHCR on `v*` tags). Commit conventions: `feat` = minor, `fix`/`refactor`/`perf` = patch; `chore`/`ci` excluded from the changelog. Version lives in `pyproject.toml` (mirrored to `config/settings/base.py:__version__`); CHANGELOG.md is auto-generated — don't hand-edit it or hand-bump versions.
- `.github/workflows/jiffy.yml` (the edge dispatch workflow) is out of scope but is covered by `node tests/edge/jiffy_workflow.test.cjs`.

## Out of scope (don't build these here)

Edge components (mention detection, thread collection), Telegram/Slack integrations, multi-language/multi-image orchestration, any UI beyond Django Admin, and any chat/comment-posting credential handling — the Gateway holds only the short-lived per-task repo token.
