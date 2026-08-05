# Jiffy
### Autonomous Software Engineering Platform

**Self-hosted AI workers that turn development tasks into reviewed code changes — from GitHub, GitLab, Gitea, Telegram, APIs, and beyond.**

Jiffy is an open-source, autonomous software engineering platform: it lets any team delegate development work to an AI agent from wherever that work already gets requested — a mentioned Issue, a chat message, or a direct API call — and get the result back as a reviewed Pull Request, without any manual commit/push/PR work.

Unlike platform-bundled coding agents, Jiffy is designed to be **self-hosted on your own infrastructure**, **channel-agnostic** (any place a task can be described can become a Jiffy entry point), and lets you **choose your own LLM/agent backend** — useful for teams on self-managed GitLab/Gitea, teams with data-residency or compliance constraints, or teams that already standardize on a specific model provider.

> **Current phase**: GitHub, GitLab, and Gitea Issues are fully supported today. Telegram, a public API, and other entry points are on the [roadmap](#roadmap) — the architecture is already channel-agnostic by design, so adding them doesn't require changing the platform's core.

---

## How It Works

1. **Mention the bot** on an Issue (or an Issue comment) in your repository, describing the task you want done.
2. **A lightweight edge check** (a GitHub Action / GitLab CI job / Gitea Action, running in your own repo) verifies the bot was actually mentioned. If not, nothing happens — no noise ever reaches the gateway.
3. **On a valid mention**, the edge component collects the full Issue thread (all comments) and sends it to your self-hosted Jiffy server.
4. **The gateway queues the task** and, when a worker is free, runs a coding agent against an isolated copy of your repository to make the requested changes.
5. **Once finished**, Jiffy commits the changes, pushes a new branch, and opens a Pull Request. If you didn't specify a branch name, one is generated from the task title (e.g. `Jiffy/add-rate-limiting`).
6. **A summary report and the PR link** are posted back as a comment on the same Issue.

```
Issue mention ──▶ Edge check (Action) ──▶ gateway ──▶ Queue ──▶ Isolated container
                                                                            │
                        Issue comment (report + PR link) ◀── PR opened ◀───┘
```

## Features

- 🌐 **Channel-agnostic by design**: GitHub, GitLab, and Gitea Issues today; Telegram, a public API, and other entry points planned — new channels plug into the same core pipeline.
- 🧠 **Model-agnostic execution**: bring your own coding agent/LLM backend instead of being locked to one vendor.
- 🏠 **Fully self-hosted**: runs on your own infrastructure — no code ever needs to leave your network unless you configure it to.
- 🔒 **Isolated execution**: every task runs in an ephemeral, resource-limited container, so nothing outside its sandbox can be affected.
- 🌐 **Restricted by default**: sandbox network egress is limited to an allow-list of package registries and git providers by default, with env-var overrides for self-hosted git servers and LLM endpoints (see [`docker/sandbox/README.md`](docker/sandbox/README.md)).
- 🚦 **No noise, low overhead**: mention-detection happens at the edge (in your repo's own CI, or the relevant channel's own filter), so only validated requests ever reach the gateway — designed for low request volumes (hundreds/day), not massive scale.
- 🌿 **Automatic branch naming**: generates a sensible branch name from the task when you don't provide one.
- 📊 **Traceable task status**: every task's state (queued, running, done, failed) is tracked and inspectable.
- 🧩 **Extensible core**: the platform's execution pipeline (queueing, isolated execution, commit/push/PR, reporting) is fully decoupled from how a task enters the system — adding a channel means writing an adapter, not touching the core.

## Architecture

- **Platform core**: Django (Django ORM, SQLite by default — designed to move to PostgreSQL with a config-only change as the project grows)
- **Task queue**: Celery, backed by Redis
- **Execution**: Docker, one ephemeral container per task
- **Channel adapters**: per-source integrations (GitHub Action / GitLab CI / Gitea Action today; Telegram bot and a public API planned) responsible only for detecting a valid task request and forwarding the full task context — the platform core has no channel-specific logic

## Installation & Setup

> ⚠️ Jiffy is under active development. Setup steps will stabilize as the project matures — check the [Releases](../../releases) page for the latest stable version.

### Prerequisites

- A Linux host (Docker and Docker Compose will be installed for you if missing)
- A git provider account/token with permission to push branches and open PRs on the target repo(s)
- Access to your chosen coding agent/LLM backend

### Quick Start

The fastest way to get Jiffy Gateway running is the installer script. It installs every prerequisite it can (including Docker itself), generates `.env`, starts all services, and runs migrations — with no manual steps beyond running it:

```bash
curl -fsSL https://raw.githubusercontent.com/Jiffy-Agnet/gateway/develop/install.sh | bash
```

It's safe to re-run. If some value it can't generate for you (a real external credential) still needs filling in, it will tell you exactly which `.env` key(s) to edit and ask you to re-run the same command — it never prompts interactively mid-run.

<details>
<summary>Prefer to set it up by hand?</summary>

```bash
git clone https://github.com/Jiffy-Agnet/gateway.git jiffy-gateway
cd jiffy-gateway
cp .env.example .env       # fill in git provider tokens, Redis URL, agent/LLM credentials
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml exec web python manage.py migrate
```

</details>

### Connect a Repository

1. Add the Jiffy webhook edge component to your repository:
   - **GitHub**: add the provided GitHub Action workflow file to `.github/workflows/`.
   - **GitLab**: add the provided CI job to `.gitlab-ci.yml`.
   - **Gitea**: add the provided Gitea Action workflow.
2. Set the shared secret (used to authenticate requests to your gateway) as a repository secret.
3. Point the edge component's webhook URL at your Jiffy server.
4. Mention the bot on an Issue to trigger your first task.

Detailed configuration options (mention tag, timeouts, resource limits, allowed repos) are documented in [`docs/configuration.md`](docs/configuration.md).

## Roadmap

- [x] GitHub Issue support
- [x] GitLab Issue support
- [x] Gitea Issue support
- [ ] Public API channel (submit tasks directly, no git-issue-tracker required)
- [ ] Configurable task triage (skip tasks that are too ambiguous/high-risk for automation)
- [ ] Pluggable coding-agent backends (Claude Code, others)
- [ ] PostgreSQL migration guide for larger deployments
- [ ] Telegram channel support
- [ ] Slack channel support
- [ ] Web dashboard for task history and status (beyond Django Admin)

Have an idea? Open an Issue with the `enhancement` label — or better yet, mention the bot and let Jiffy build it.

## Contributing

Contributions are welcome! Please:

1. Open an Issue describing the bug/feature before submitting large changes, so we can discuss approach first.
2. Fork the repo and create a feature branch (`git checkout -b feature/your-feature`).
3. Make sure existing tests pass and add tests for new behavior.
4. Open a Pull Request with a clear description of what changed and why.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for coding conventions and development setup details.

## Security

If you discover a security vulnerability, please do **not** open a public Issue. Instead, report it privately as described in [`SECURITY.md`](SECURITY.md).

## License

This project is licensed under the [MIT License](LICENSE) — free to use, self-host, and modify.

## Acknowledgments

Jiffy is built for teams who want the convenience of an autonomous software engineering platform — turning development requests into reviewed code changes — without being locked into a single git platform, channel, or LLM vendor.
