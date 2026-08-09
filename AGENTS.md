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
3. Status transitions actually set: `queued → provisioning → cloning → running → done|failed`. The `reporting` status exists in the model but is **never set** by current code.

## Data model & storage rules

- `Task`: `provider`, `repo_url`, `issue_external_id`, `callback_url`, `callback_secret`, `status`, plus audit-only fields (`programming_language`, `branch_name`, `pr_url`, `error_message`) populated **from the agent's final result after the fact** — never pre-computed. DB is SQLite at `data/db.sqlite3` (`config/settings/base.py`). The root `db.sqlite3`, `jiffy-db/`, and `jiffy_db/` are stale leftovers — don't use them.
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
- Per-provider secrets from env: `GITHUB_INGEST_TOKEN`, `GITLAB_INGEST_TOKEN`, `GITEA_INGEST_TOKEN` — never shared. Rotation is manual (single-secret; a brief mismatch window is accepted).
- OpenAPI/Swagger at `/api/schema/`, `/api/docs/`, `/api/redoc/`.

## Callback / reporting — agent-first, Gateway fallback

This changed recently; don't assume the Gateway owns the callback.

1. `build_agent_instructions` (in `jobs/execution/agent.py`) instructs the agent to make **exactly one** HTTP call to `callback.url` after finishing, with `Authorization: Bearer <secret>` (secret forwarded byte-for-byte, never signed/hashed) and a **human-readable markdown comment body** (`Task #N: ✅ Jiffy completed this task.` with `**Summary:**`, `**Branch:**`, `**Pull Request:**`, `### Technical Report`; failure format uses `❌` + `**Reason:**`). Omit empty lines/sections.
2. Wire format is provider-specific via `jobs/callback_specs.py`: **GitHub** = JSON `{"body": "<markdown>"}` with `Accept: application/vnd.github+json` and `X-GitHub-Api-Version: 2026-03-10` (the callback URL is the Issue Comments API); **GitLab/Gitea** = plain `text/plain; charset=utf-8`. Add a new provider by adding a spec entry, not pipeline code.
3. `execute_task` checks the agent result's `callback.attempted/succeeded`; if the agent did not deliver, the Gateway falls back via `send_fallback_callback` (same spec, retries 3×, 2s apart, logs failures — task stays `done`/`failed` even if all attempts fail). `send_callback` is the older path, used only in tests.
4. `technical_report` is a structured markdown report (`## What was done`, `## Technology / approach chosen`, `## Reasoning`, `## Setup / installation instructions`, `## Known limitations / follow-ups`).

## Agent instruction contract

- `build_agent_instructions(payload)` builds one agent-agnostic prompt: verbatim issue text (from `turns` if present, else `text`), workspace at `/workspace`, and the full self-provisioning workflow (analyze → install missing tools → implement → verify → commit/push on a new branch → open PR **only if asked** → mention the review bot **only if asked** → attempt callback).
- Branch fallback convention is `Jiffy/<short-description-of-change>` when the issue text doesn't specify one. The Gateway never computes branch names.
- **Required final output**: a JSON file at `/workspace/.jiffy_result.json` with `status` (`"done"`|`"failed"`), `branch_name`, `branch_base`, `pr_url`, `programming_language`, `summary`, `technical_report`, `error_message`, `model`, and `callback {attempted, succeeded, error}`. `read_agent_result()` parses this; a missing/malformed file is treated as a failure (not an exception). Any agent plugged into this system must honor this contract.

## Sandbox / container gotchas

- One generic image (default tag `jiffy-sandbox:1.2.0`, from `SANDBOX_IMAGE`), built from `docker/sandbox/Dockerfile` on demand by `ensure_sandbox_image()` if missing. Bundles nvm/uv/gvm, node, python, go, gh/glab, git, curl, build-essential, iptables, pnpm, and the `opencode` CLI. Everything else is installed by the agent at runtime — there is no per-language image selection and no pre-container language check.
- **Resource limits are env-driven** (`config/settings/base.py` → `jobs/execution/container.py`): `SANDBOX_MEMORY_LIMIT` (default `2g`), `SANDBOX_MEMORY_SWAP_LIMIT` (default `4g`, the *combined* mem+swap ceiling — must be ≥ the memory limit or the Gateway warns and uses 2× it; `-1` = unlimited), `SANDBOX_CPU_LIMIT` (default `1.5`, applied as `nano_cpus`, **not** `cpuset_cpus` — that used to pin a core index and couldn't express fractions). `SANDBOX_MEM_LIMIT` is kept as a backwards-compatible alias for `SANDBOX_MEMORY_LIMIT`.
- Package installs are the usual OOM (exit 137) trigger. `build_package_manager_env()` puts `NODE_OPTIONS=--max-old-space-size=<SANDBOX_NODE_MAX_OLD_SPACE_MB or half the mem limit, floor 512>`, `npm_config_maxsockets/jobs`, `CARGO_BUILD_JOBS`, `MAKEFLAGS`, `GOMAXPROCS` (all from `SANDBOX_PACKAGE_CONCURRENCY`, default 2) into the container env at create time; `_apply_pnpm_limits()` rewrites `~/.config/pnpm/config.yaml` after start because pnpm v11 ignores `npm_config_*`. That one is best-effort and never fails the task.
- The worker manages containers via the Docker SDK. **`DOCKER_HOST` must be set when the worker runs inside a container** (e.g. `tcp://docker-socket-proxy:2375`); `get_docker_client()` raises a clear error otherwise.
- **Network egress is restricted by default** (`JIFFY_SANDBOX_NETWORK_RESTRICTED=true`): the container starts with `NET_ADMIN` and `iptables` default-deny OUTPUT, allowing only the allow-list hosts (resolved at start). **Fail-closed**: if the rules can't be applied the container is torn down and the task fails.
- DNS is only allowed to Docker's embedded resolver `127.0.0.11`, which exists **only on user-defined networks** — so restricted containers are placed on `jiffy-sandbox-net` (created on demand). Don't move them to the default bridge while restriction is active.
- Allow-list = `SANDBOX_NETWORK_ALLOWLIST` (defaults: PyPI, npm, crates.io, Go proxy, GitHub/GitLab/Gitea hosts) merged with `SANDBOX_NETWORK_ALLOWLIST_EXTRA` (self-hosted git + the LLM provider endpoint — both vary per install). `JIFFY_SANDBOX_NETWORK_RESTRICTED=false` = open network, for debugging only.
- Containers are run `tty=True`, `remove=False` at create, then explicitly stopped+removed by the `start_generic_sandbox_container` context manager (`JIFFY_SANDBOX_CLEANUP=false` leaves them alive for debugging). Default user is non-root `jiffy`; the restriction script runs as root.
- There is **no env var for the OpenCode config path** — `_inject_opencode_config` reads the project-root `opencode.json` and writes it to `/home/jiffy/.config/opencode/opencode.json` inside the container. (The old `SANDBOX_OPENCODE_CONFIG_PATH` setting was dead and has been removed.)
- The agent runs as `opencode run --auto "$INSTRUCTIONS"` inside `bash -l -c` (login shell so `/etc/profile.d` version managers load), workdir `/workspace`, with the instructions staged to `/tmp/jiffy_instructions.txt` first. Agent stdout is redirected to the container's main stdout for live `docker logs`.
- Clone injects the token into the URL: GitHub `token@host`, GitLab/Gitea `username:token@host`. The token is passed as container env `REPO_TOKEN` — never in the DB or baked into an image.

## Celery & worker

- Celery app lives in `config/celery.py` and autodiscovers `jobs` + `apps.ingestion`. **The app module is `config`, not `jiffy`.** Single queue `execute`; concurrency comes from `CELERY_WORKER_CONCURRENCY` (settings → `worker_concurrency`, default 1) — the compose commands deliberately pass **no** `--concurrency` flag so the env var is what applies. Each slot can hold a full sandbox container, so size it as `concurrency × SANDBOX_MEMORY_LIMIT`.
- `execute_task`: `bind=True, max_retries=3, default_retry_delay=60, acks_late=True`, routed to `execute`. Retries happen only on unexpected internal exceptions (transient errors); `ExecutionError` and agent/logical failures go straight to `failed` + callback with no retry.
- On `worker_ready`, orphaned tasks still in `queued` status are re-dispatched (crash recovery). The startup `ensure_sandbox_image` call there is currently commented out; the image is still ensured per job.

## Code review loop

The Gateway never reviews and never decides whether a review was requested. The agent reads the issue text: if a review is asked, it mentions the code-review bot in the PR description (if it opened one) or in its final summary (which flows into the callback). The exact bot handle is **not finalized** — keep it a placeholder string inside the instructions. Any review feedback that leads to changes comes back as a **new** Jiffy mention → a brand-new task; there is no synchronous review step.

## Git / CI / release workflow

- `develop` is the active development branch; `master` is the release branch (currently ~13 commits behind). Feature branches use `Jiffy/<slug>`. CI runs on PRs/pushes to `master` and `staging`: `uv sync --frozen --all-extras` → migrations → `manage.py test`.
- Releases are generated by `python-semantic-release` on push to `master` (release.yml → docker-publish.yml to GHCR on `v*` tags). Commit conventions: `feat` = minor, `fix`/`refactor`/`perf` = patch; `chore`/`ci` excluded from the changelog. Version lives in `pyproject.toml` (mirrored to `config/settings/base.py:__version__`); CHANGELOG.md is auto-generated — don't hand-edit it or hand-bump versions.
- `.github/workflows/jiffy.yml` (the edge dispatch workflow) is out of scope but is covered by `node tests/edge/jiffy_workflow.test.cjs`.

## Out of scope (don't build these here)

Edge components (mention detection, thread collection), Telegram/Slack integrations, multi-language/multi-image orchestration, any UI beyond Django Admin, and any chat/comment-posting credential handling — the Gateway holds only the short-lived per-task repo token.
