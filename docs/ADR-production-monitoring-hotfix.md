# ADR: Production Monitoring-Driven Hotfix Automation

## Status

Proposed

## Context

Jiffy currently starts work only when a human mentions `@jiffy` in an Issue comment on a supported git provider (GitHub/GitLab/Gitea). This preserves two core principles: the Issue pipeline is the only path into Jiffy, and task execution never starts without an explicit human mention.

We want to close the loop on production incidents: when a crash or error is detected by a production monitoring/logging tool (PM2, Sentry, Loki, OpenTelemetry), Jiffy should automatically open an Issue describing the bug, mention itself to start a hotfix, produce a patch, and open a PR - with a human still gating the actual merge to `main`/`develop`.

This requires a deliberate, scoped exception to the "no autonomous task selection" principle, plus new ingestion, security, and noise-control mechanisms that do not exist in Jiffy today.

## Decision

### 1. Scope of the autonomous-trigger exception

Only Issues created by this monitoring pipeline are auto-mentioned. The general rule - a human must mention `@jiffy` to start any other task - is unchanged. This exception exists solely to shorten detection-to-hotfix time for production incidents, and must be documented as an explicit, narrow exception rather than a change to the general rule.

### 2. MVP scope

All four tools ship together in the first phase: PM2, Sentry, Loki, OpenTelemetry.

### 3. Identity & permissions

A dedicated bot identity/token (`jiffy-hotfix-bot`) is created and added to the whitelist, instead of reusing a personal token. This keeps whitelist-based authorization meaningful (a real, auditable identity) and limits blast radius if an ingestion secret is ever compromised - a leaked secret cannot be used together with personal git credentials.

### 4. Ingestion strategy - per tool, not uniform

Because the tools differ in what they natively support, each uses the ingestion path that fits it best rather than forcing one architecture on all four:

- **Sentry** - via Sentry's own Alert Rules -> Webhook action, signed with HMAC. Sentry already does high-quality error grouping/fingerprinting; reimplementing that would be wasted effort.
- **PM2** - direct, via a custom PM2 module listening on PM2's internal bus (`process:exception` and related events) and forwarding straight to Jiffy's edge endpoint. Open-source PM2 has no external server of its own, so there is no webhook to hook into.
- **Loki** - direct, via a Promtail/Alloy pipeline stage that matches error-level log lines and forwards them, rather than standing up Alertmanager purely for this purpose (extra infrastructure that conflicts with the "lightweight, low-cost to run" principle).
- **OpenTelemetry** - direct, via an OTel Collector processor/exporter that filters error-level spans/logs and forwards them - a single integration point per project with no application code changes required.

Sending data directly to Jiffy where possible (rather than round-tripping through a vendor's own server) also keeps with Jiffy's "full infrastructure sovereignty" differentiator and reduces detection latency.

### 5. Authentication of ingestion

Each ingestion path authenticates independently before anything is created:

- Sentry: HMAC signature verification against Sentry's webhook secret.
- PM2 / Loki / OTel extensions: a per-extension shared secret in a custom header.
- Any payload that fails authentication is rejected and logged with a reason. No Issue is created, no noise reaches Jiffy.

### 6. Noise reduction - two layers

- **Extension side (production):** short-window, in-memory rate-limiting/dedup to suppress bursts of the same error before they ever reach Jiffy. This is volume control, not correctness.
- **Jiffy edge side:** a lightweight fingerprint-state check (does an open/in-progress Issue already exist for this fingerprint?) before creating a new Issue. This stays consistent with "Gateway stays thin - state tracking only", since it is a state lookup, not business logic, and it catches duplicates across multiple instances of the same service that the extension-side window alone cannot see.

### 7. Credential distribution

The `jiffy-hotfix-bot` token lives only on Jiffy's side. Extensions never hold a git-provider token; they send a normalized event (fingerprint, repo/provider mapping, their own extension secret) to Jiffy's edge endpoint, and Jiffy is the only place that creates the Issue.

### 8. Repo mapping

Configured inside each extension itself (service -> provider + repo), set up once during installation - consistent with "ease of installation" over a centrally maintained mapping file.

### 9. Branch & PR strategy

The hotfix branch is cut from `main` (since production runs on `main`), and Jiffy opens a single PR against `main` only. The PR description explicitly instructs the human reviewer to also apply the change to `develop`. Jiffy does not open a second PR automatically - this keeps the flow simple and puts the sync step where a human is already in the loop.

### 10. Merge

Manual, by a human, following the existing PR review process. No auto-merge.

## Consequences

- Higher upfront engineering cost: four separate extensions plus a new Jiffy-side ingestion/edge component, versus a single uniform ingestion path.
- Lower load on Jiffy's server, since expensive/frequent-event filtering happens at the edge (production side).
- Improved security posture: dedicated bot identity, per-source authentication, and no git-provider credentials distributed to production environments.
- Detection-to-hotfix time is minimized for tools sent directly (PM2, Loki, OTel), and Sentry benefits from mature built-in grouping instead of a reimplementation.
- A human still gates every merge to `main`/`develop`; only Issue creation and mention are automated.

## Open Questions

- Exact schema of the normalized event Jiffy's edge endpoint accepts (fields, versioning) - left to implementation.
- Fingerprint algorithm specifics per tool (what goes into the hash) - left to implementation per extension.
- Where extension secrets and the `jiffy-hotfix-bot` token are provisioned/rotated during `install.sh` - needs a follow-up decision, likely tied to the existing `.env.example` work.
