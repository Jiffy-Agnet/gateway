# Jiffy Generic Sandbox Image

One generic image for all tasks. Bundles Python/Node.js/Go runtime managers
(nvm, uv, gvm), git provider CLIs (`gh`, `glab`, `tea`), `git`, `curl`,
`build-essential`, `iptables`, and the OpenCode coding agent CLI. The agent
installs any specific language/runtime versions or extra dependencies itself at
runtime.

```bash
# Build
./build.sh                            # tags jiffy-sandbox:1.3.0
# Or with a registry target:
./build.sh ghcr.io/org/jiffy-sandbox:1.3.0
```

## Network Egress Restriction

Sandbox containers are **restricted by default**: outbound network access is
limited to a default allow-list of package registries and git provider domains.
Enforcement uses plain `iptables` inside the container's own network namespace
(the container is started with the `NET_ADMIN` capability) — no extra
infrastructure or external dependencies.

> **Failure is loud, not silent.** If restriction is active and the iptables
> rules cannot be applied, the container is torn down and the task fails with a
> clear error. The sandbox never silently runs unrestricted.

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `JIFFY_SANDBOX_NETWORK_RESTRICTED` | `true` | `false` fully disables the restriction (open network) for debugging. |
| `SANDBOX_NETWORK_ALLOWLIST` | built-in defaults (below) | Comma-separated hostnames. If set, **replaces** the defaults. |
| `SANDBOX_NETWORK_ALLOWLIST_EXTRA` | *(unset)* | Comma-separated hostnames **appended** to the allow-list. Use for self-hosted git server instances and the LLM provider endpoint OpenCode is configured to use — these vary per install and cannot be hardcoded. |

Default allow-list:

```
pypi.org, files.pythonhosted.org, registry.npmjs.org, crates.io,
static.crates.io, proxy.golang.org, sum.golang.org, github.com,
api.github.com, objects.githubusercontent.com, codeload.github.com,
gitlab.com, gitea.com
```

### Examples

Restricted to the defaults only (this is the default behaviour — nothing to set):

```bash
JIFFY_SANDBOX_NETWORK_RESTRICTED=true
```

Restricted, plus a self-hosted Gitea instance and a custom LLM endpoint:

```bash
JIFFY_SANDBOX_NETWORK_RESTRICTED=true
SANDBOX_NETWORK_ALLOWLIST_EXTRA=git.example.com,llm.example.com
```

Fully replace the allow-list (e.g. only your own registries):

```bash
SANDBOX_NETWORK_ALLOWLIST=git.example.com,pypi.example.com
```

Debugging: open network, no restriction:

```bash
JIFFY_SANDBOX_NETWORK_RESTRICTED=false
```

### Logging

Every sandbox run logs whether restriction was active and, if active, the
effective allow-list used. This appears both in the Gateway worker log
(`jobs.execution.container`) and in the container's own startup report
(`startup-report.sh`), which reads `JIFFY_SANDBOX_NETWORK_RESTRICTED` and
`JIFFY_SANDBOX_NETWORK_ALLOWLIST` from the container environment.

### How it works / limitations

- The restriction is applied right after the container starts, before the repo
  is cloned or the agent runs.
- Allow-listed hostnames are resolved at container start; connections to the
  resolved IPs are allowed, everything else is dropped (default-deny OUTPUT
  policy). DNS queries to Docker's embedded resolver are always allowed, so the
  agent can resolve allow-listed hosts — this means DNS resolution *leaks* for
  other hosts, but actual connections to them are still blocked.
- Enforcement is IPv4 (`iptables`). IPv6 is typically disabled on the default
  bridge network; if you enable IPv6 networking you may also need
  `ip6tables` rules.
- Hosts are matched by resolved IP, so a host must resolve at container start.
  If a host fails to resolve, it is simply not reachable (fail-closed).

## Smoke test

```bash
docker build -t jiffy-sandbox:1.3.0 .
docker run --rm jiffy-sandbox:1.3.0 bash /smoke-test.sh
```
