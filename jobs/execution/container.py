"""Manages the lifecycle of a Docker container for a task."""
import io
import json
import logging
import os
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator
from urllib.parse import urlparse

import docker
from docker.errors import ImageNotFound, NotFound
from docker.models.containers import Container
from django.conf import settings

from .exceptions import ContainerError

logger = logging.getLogger(__name__)

WORKSPACE = "/workspace"
AGENT_RESULT_PATH = "/workspace/.jiffy_result.json"

# Path to the sandbox Dockerfile, relative to the project root.
SANDBOX_DOCKERFILE_DIR = Path(__file__).resolve().parent.parent.parent / "docker" / "sandbox"

# Docker's embedded DNS resolver, reachable from every container on the
# default bridge network.  DNS queries are allowed so the agent can resolve
# allowlisted hosts; actual connections to non-allowlisted destinations are
# still dropped by the OUTPUT rules.
DOCKER_EMBEDDED_DNS = "127.0.0.11"


# ---------------------------------------------------------------------------
# Docker client construction
# ---------------------------------------------------------------------------


# Minimum request/socket timeout (seconds) for every Docker client the
# Gateway creates. docker-py's own default (60s) is far shorter than the
# slowest single call the Gateway makes (a sandbox image build), so it is
# raised to 15 minutes. The agent run itself no longer depends on this: it is
# executed detached and polled, so it is never bounded by a single request.
MIN_DOCKER_CLIENT_TIMEOUT_SECONDS = 900

# Agent run window used when neither the caller nor settings specify one.
DEFAULT_AGENT_TIMEOUT_SECONDS = 3600
# How often the detached agent exec is polled for completion, and how often a
# still-running agent is reported in the log.
AGENT_POLL_INTERVAL_SECONDS = 5
AGENT_PROGRESS_LOG_INTERVAL_SECONDS = 300


def get_docker_client(timeout_seconds: int | None = None) -> docker.DockerClient:
    """Return a Docker client configured from the DOCKER_HOST environment variable.

    In containerised deployments the Celery worker connects to the Docker
    engine through a socket proxy (``docker-socket-proxy``), so ``DOCKER_HOST``
    **must** be set (e.g. ``tcp://docker-socket-proxy:2375``).  If it is
    missing the worker would silently fall back to a host socket path that
    does not exist inside its own container, producing confusing connection
    errors later.  This function checks up front and raises a clear error.

    Outside containers (local development) the environment variable is
    optional — the Docker SDK falls back to the default socket automatically.

    The client's request timeout is always at least
    ``MIN_DOCKER_CLIENT_TIMEOUT_SECONDS``; pass ``timeout_seconds`` to request
    a longer one (e.g. to match a specific job's execution window).
    """
    effective_timeout = max(timeout_seconds or 0, MIN_DOCKER_CLIENT_TIMEOUT_SECONDS)

    docker_host = os.environ.get("DOCKER_HOST")
    if docker_host:
        logger.debug("Using Docker host from DOCKER_HOST: %s", docker_host)
        return docker.from_env(timeout=effective_timeout)

    # No DOCKER_HOST set — check whether the default socket is reachable.
    # This path is fine for local development but will fail in a container
    # that does not mount the host socket.
    try:
        client = docker.from_env(timeout=effective_timeout)
        # Lightweight connectivity check
        client.ping()
        return client
    except Exception as exc:
        raise ContainerError(
            "DOCKER_HOST environment variable is not set and the default "
            "Docker socket is not reachable.  In containerised deployments, "
            "set DOCKER_HOST to the socket proxy address "
            "(e.g. tcp://docker-socket-proxy:2375).  Original error: " + str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# Sandbox image management
# ---------------------------------------------------------------------------


def ensure_sandbox_image() -> None:
    """Ensure the configured SANDBOX_IMAGE exists locally.

    If the image is already present this is a cheap no-op.  If it is missing,
    builds it from ``docker/sandbox/Dockerfile`` and tags it.  Logs which path
    was taken and how long it took so operators can see it in the console.

    This function is called both at worker startup (via the ``worker_ready``
    signal) and before each job's container is provisioned, as a safety net
    in case the image was removed between startup and job execution.

    Raises ``ContainerError`` if the build fails.
    """
    image_ref = settings.SANDBOX_IMAGE
    client = get_docker_client()

    t0 = time.monotonic()
    try:
        client.images.get(image_ref)
        elapsed = time.monotonic() - t0
        logger.info(
            "Sandbox image %s found locally — no build needed (%.1fs)",
            image_ref,
            elapsed,
        )
        return
    except ImageNotFound:
        pass

    logger.info(
        "Sandbox image %s not found locally, building from %s",
        image_ref,
        SANDBOX_DOCKERFILE_DIR,
    )
    try:
        client.images.build(
            path=str(SANDBOX_DOCKERFILE_DIR),
            tag=image_ref,
            rm=True,
        )
        elapsed = time.monotonic() - t0
        logger.info(
            "Sandbox image %s built successfully in %.1fs",
            image_ref,
            elapsed,
        )
    except Exception as exc:
        elapsed = time.monotonic() - t0
        msg = f"Failed to build sandbox image {image_ref} after {elapsed:.1f}s: {exc}"
        logger.error(msg)
        raise ContainerError(msg) from exc


# ---------------------------------------------------------------------------
# Network restriction helpers
# ---------------------------------------------------------------------------


def _extract_git_host(repo_url: str) -> str | None:
    """Extract the hostname from a git remote URL."""
    try:
        parsed = urlparse(repo_url)
        return parsed.hostname
    except Exception:
        return None


def _effective_network_allowlist() -> list[str]:
    """Return the effective, de-duplicated allow-list for sandbox egress.

    Merges ``SANDBOX_NETWORK_ALLOWLIST`` (the defaults, or a full override)
    with ``SANDBOX_NETWORK_ALLOWLIST_EXTRA`` (appended additions for
    self-hosted git servers and per-install LLM provider endpoints).
    """
    base = list(getattr(settings, "SANDBOX_NETWORK_ALLOWLIST", []))
    extra = list(getattr(settings, "SANDBOX_NETWORK_ALLOWLIST_EXTRA", []))
    seen: set[str] = set()
    result: list[str] = []
    for raw in base + extra:
        host = raw.strip().lower()
        if host and host not in seen:
            seen.add(host)
            result.append(host)
    return result


def _build_network_restriction_script(allowlist: list[str]) -> str:
    """Build a bash script that restricts container egress via iptables.

    The script runs as root inside the container (which must be started with
    the ``NET_ADMIN`` capability).  It allows loopback traffic, replies to
    established connections, DNS queries to Docker's embedded resolver, and
    connections to the IPs of each allowlisted host.  Everything else is
    dropped via a default-deny OUTPUT policy.

    Hosts are resolved at container start so IP changes (e.g. CDN backends)
    are picked up per run.  This is intentionally lightweight — plain
    ``iptables`` available in the Debian bookworm base image, no extra infra.
    """
    lines = [
        "#!/bin/bash",
        "set -e",
        "",
        "# Flush any existing OUTPUT rules (fresh container).",
        "iptables -F OUTPUT",
        "",
        "# Allow loopback traffic.",
        "iptables -A OUTPUT -o lo -j ACCEPT",
        "",
        "# Allow replies to established connections.",
        "iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        "",
        "# Allow DNS queries to Docker's embedded resolver.",
        f"iptables -A OUTPUT -p udp --dport 53 -d {DOCKER_EMBEDDED_DNS} -j ACCEPT",
        f"iptables -A OUTPUT -p tcp --dport 53 -d {DOCKER_EMBEDDED_DNS} -j ACCEPT",
        "",
        "# Allow connections to allowlisted hosts.",
    ]
    for host in allowlist:
        safe_host = host.replace("'", "'\\''")
        lines.append(
            "for ip in $(getent ahostsv4 '{host}' 2>/dev/null | awk '{{print $1}}' | sort -u); do "
            "iptables -A OUTPUT -d \"$ip\" -j ACCEPT; done".format(host=safe_host)
        )
    lines += [
        "",
        "# Default-deny all other egress.",
        "iptables -P OUTPUT DROP",
        "iptables -A OUTPUT -j DROP",
    ]
    return "\n".join(lines) + "\n"


def _apply_network_restriction(container: Container, allowlist: list[str], task_id: int = 0) -> None:
    """Apply iptables egress restriction inside the container.

    Runs as root (the sandbox image's default user is non-root) — the
    container must be started with the ``NET_ADMIN`` capability.  Raises
    ``ContainerError`` if the rules cannot be applied so the caller fails
    closed rather than silently running an unrestricted sandbox.
    """
    script = _build_network_restriction_script(allowlist)
    exit_code, (output, err) = container.exec_run(
        cmd=["bash", "-c", script],
        user="root",
        demux=True,
    )
    if exit_code != 0:
        detail = (err or output or b"").decode(errors="replace").strip()
        raise ContainerError(
            f"Failed to apply sandbox network restriction (exit {exit_code}): {detail}"
        )
    logger.info(
        "[%d] Network restriction applied — egress limited to %d allowlisted host(s)",
        task_id,
        len(allowlist),
    )


def _ensure_network(client: docker.DockerClient) -> str:
    """Ensure the jiffy-sandbox bridge network exists; return its name."""
    network_name = "jiffy-sandbox-net"
    try:
        client.networks.get(network_name)
    except NotFound:
        client.networks.create(
            network_name,
            driver="bridge",
            internal=False,
        )
        logger.info("Created Docker network %s", network_name)
    return network_name


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------

# Fallbacks used when a configured value is missing or unparseable.  They match
# the settings defaults so a bad env var degrades to the documented behaviour
# instead of to "no limit at all".
DEFAULT_MEMORY_LIMIT = "2g"
DEFAULT_CPU_LIMIT = 1.5

_MEMORY_SUFFIX_MULTIPLIERS = {
    "b": 1,
    "k": 1024,
    "m": 1024 ** 2,
    "g": 1024 ** 3,
}


def parse_memory_bytes(value: Any) -> int | None:
    """Parse a Docker memory size (``"512m"``, ``"2g"``, ``1073741824``) to bytes.

    Returns ``-1`` for the "unlimited" sentinel and ``None`` if the value
    cannot be parsed, so callers can fall back with a warning rather than
    passing garbage to the Docker API.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)

    raw = str(value).strip().lower()
    if not raw:
        return None
    if raw == "-1":
        return -1

    suffix = raw[-1]
    if suffix in _MEMORY_SUFFIX_MULTIPLIERS:
        number, multiplier = raw[:-1], _MEMORY_SUFFIX_MULTIPLIERS[suffix]
    else:
        number, multiplier = raw, 1
    try:
        return int(float(number) * multiplier)
    except ValueError:
        return None


def resolve_memory_limits() -> tuple[str, int]:
    """Return the ``(mem_limit, memswap_limit)`` pair for a sandbox container.

    ``memswap_limit`` is the *combined* memory + swap ceiling, so Docker
    requires it to be at least the memory limit.  When
    ``SANDBOX_MEMORY_SWAP_LIMIT`` is unparseable or smaller than
    ``SANDBOX_MEMORY_LIMIT`` we warn and fall back to twice the memory limit —
    keeping some swap headroom rather than silently disabling it, since the
    whole point of the setting is to stop installs being OOM-killed.
    """
    configured_mem = str(getattr(settings, "SANDBOX_MEMORY_LIMIT", DEFAULT_MEMORY_LIMIT)).strip()
    mem_bytes = parse_memory_bytes(configured_mem)
    if mem_bytes is None or mem_bytes <= 0:
        logger.warning(
            "SANDBOX_MEMORY_LIMIT=%r is not a valid Docker memory size — falling back to %s",
            configured_mem,
            DEFAULT_MEMORY_LIMIT,
        )
        configured_mem = DEFAULT_MEMORY_LIMIT
        mem_bytes = parse_memory_bytes(DEFAULT_MEMORY_LIMIT)

    configured_swap = str(getattr(settings, "SANDBOX_MEMORY_SWAP_LIMIT", "")).strip()
    swap_bytes = parse_memory_bytes(configured_swap)
    if swap_bytes == -1:
        # Explicit "unlimited swap" — a valid Docker value, pass it through.
        return configured_mem, -1
    if swap_bytes is None or swap_bytes < mem_bytes:
        fallback = mem_bytes * 2
        logger.warning(
            "SANDBOX_MEMORY_SWAP_LIMIT=%r must be a size >= SANDBOX_MEMORY_LIMIT (%s) — "
            "falling back to 2x the memory limit (%d bytes)",
            configured_swap,
            configured_mem,
            fallback,
        )
        swap_bytes = fallback
    return configured_mem, swap_bytes


def resolve_cpu_nano_cpus() -> int:
    """Return the sandbox CPU quota as ``nano_cpus`` (cores x 1e9).

    ``nano_cpus`` is the API equivalent of ``docker run --cpus``, so fractional
    values like ``1.5`` work.  (The previous ``cpuset_cpus`` pinned the
    container to a specific core index instead of capping its share, and could
    not express a fraction at all.)
    """
    configured = str(getattr(settings, "SANDBOX_CPU_LIMIT", DEFAULT_CPU_LIMIT)).strip()
    try:
        cores = float(configured)
        if cores <= 0:
            raise ValueError(configured)
    except ValueError:
        logger.warning(
            "SANDBOX_CPU_LIMIT=%r is not a positive number of cores — falling back to %s",
            configured,
            DEFAULT_CPU_LIMIT,
        )
        cores = DEFAULT_CPU_LIMIT
    return int(cores * 1_000_000_000)


def resolve_node_heap_mb(mem_limit: str) -> int:
    """Return the Node.js old-space (heap) cap in MiB for the sandbox.

    Uses ``SANDBOX_NODE_MAX_OLD_SPACE_MB`` when set; otherwise derives half of
    the container memory limit (floor 512 MiB) so the heap cap tracks whatever
    memory the container was given.  A Node heap that is allowed to grow past
    the cgroup limit is the usual reason an install dies with exit 137.
    """
    configured = str(getattr(settings, "SANDBOX_NODE_MAX_OLD_SPACE_MB", "")).strip()
    if configured:
        try:
            explicit = int(float(configured))
            if explicit > 0:
                return explicit
        except ValueError:
            pass
        logger.warning(
            "SANDBOX_NODE_MAX_OLD_SPACE_MB=%r is not a positive number of MiB — deriving from "
            "the memory limit instead",
            configured,
        )

    mem_bytes = parse_memory_bytes(mem_limit) or 0
    if mem_bytes <= 0:
        return 512
    return max(512, int(mem_bytes / (1024 ** 2) / 2))


def build_agent_provider_env() -> Dict[str, str]:
    """Env vars the agent's LLM provider config (``opencode.json``) reads.

    Taken from the Gateway process environment and passed straight through to
    the container.  Blank/unset names are skipped so the sandbox never gets an
    empty API key that looks configured.
    """
    provider_env: Dict[str, str] = {}
    for name in getattr(settings, "SANDBOX_AGENT_ENV_PASSTHROUGH", ()):
        value = os.environ.get(name, "").strip()
        if value:
            provider_env[name] = value
    return provider_env


def build_package_manager_env(mem_limit: str) -> Dict[str, str]:
    """Environment variables that cap package-manager memory/parallelism.

    Applied to the container at creation time so they are in effect for every
    command the agent runs, including the install steps that trigger OOM kills
    on small hosts.  npm reads ``npm_config_*``; ``NODE_OPTIONS`` caps the V8
    heap for every Node process (npm/pnpm themselves included).
    """
    concurrency = int(getattr(settings, "SANDBOX_PACKAGE_CONCURRENCY", 2) or 2)
    heap_mb = resolve_node_heap_mb(mem_limit)
    return {
        "NODE_OPTIONS": f"--max-old-space-size={heap_mb}",
        "npm_config_maxsockets": str(concurrency),
        "npm_config_jobs": str(concurrency),
        "npm_config_fund": "false",
        "npm_config_audit": "false",
        "npm_config_progress": "false",
        # Rust/Go build parallelism, for the same reason.
        "CARGO_BUILD_JOBS": str(concurrency),
        "MAKEFLAGS": f"-j{concurrency}",
        "GOMAXPROCS": str(concurrency),
    }


# pnpm v11 reads network/concurrency settings only from its own global config
# file, not from npm_config_* env vars — see docker/sandbox/Dockerfile.
PNPM_CONFIG_PATH_IN_CONTAINER = "/home/jiffy/.config/pnpm/config.yaml"


def _apply_pnpm_limits(container: Container, task_id: int = 0) -> None:
    """Lower pnpm's install concurrency inside the container.

    Best-effort: a failure here only means installs run at the image's default
    parallelism, which is not worth failing the whole task over.
    """
    concurrency = int(getattr(settings, "SANDBOX_PACKAGE_CONCURRENCY", 2) or 2)
    # Drop any existing networkConcurrency/childConcurrency lines from the
    # image's baked-in config, then append the runtime values.
    script = (
        f"mkdir -p $(dirname {PNPM_CONFIG_PATH_IN_CONTAINER}) && "
        f"touch {PNPM_CONFIG_PATH_IN_CONTAINER} && "
        f"sed -i '/^\\(networkConcurrency\\|childConcurrency\\):/d' {PNPM_CONFIG_PATH_IN_CONTAINER} && "
        f"printf 'networkConcurrency: %s\\nchildConcurrency: %s\\n' "
        f"'{concurrency}' '{concurrency}' >> {PNPM_CONFIG_PATH_IN_CONTAINER}"
    )
    try:
        exit_code, (_, err) = container.exec_run(cmd=["bash", "-c", script], demux=True)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[%d] Failed to apply pnpm concurrency limits: %s", task_id, exc)
        return
    if exit_code != 0:
        logger.warning(
            "[%d] Failed to apply pnpm concurrency limits: %s",
            task_id,
            (err or b"").decode(errors="replace").strip(),
        )
    else:
        logger.info("[%d] pnpm install concurrency limited to %d", task_id, concurrency)


# ---------------------------------------------------------------------------
# Config injection
# ---------------------------------------------------------------------------

SANDBOX_OPENCODE_CONFIG_PATH_IN_CONTAINER = "/home/jiffy/.config/opencode/opencode.json"


def _inject_opencode_config(container: Container, task_id: int) -> None:
    """Read the OpenCode config file and write it into the sandbox container.

    This avoids volume-mount path issues when the Celery worker runs inside
    Docker (the Docker daemon needs host-accessible paths, but the config
    file is only accessible inside the Celery container).
    """
    # Read opencode.json from the project root
    config_path = Path(__file__).resolve().parent.parent.parent / "opencode.json"
    if not config_path.is_file():
        logger.warning(
            "[%d] opencode.json not found in project root: %s",
            task_id,
            config_path,
        )
        return

    try:
        config_content = config_path.read_text(encoding="utf-8")
        # Write config into the container using printf + heredoc to avoid escaping issues
        escaped = config_content.replace("\\", "\\\\").replace("'", "'\\''")
        write_cmd = f"printf '%s' '{escaped}' > {SANDBOX_OPENCODE_CONFIG_PATH_IN_CONTAINER}"
        exit_code, (_, err) = container.exec_run(
            cmd=["bash", "-c", write_cmd],
            demux=True,
        )
        if exit_code != 0:
            logger.warning(
                "[%d] Failed to inject OpenCode config: %s",
                task_id,
                (err or b"").decode(errors="replace"),
            )
        else:
            logger.info("[%d] Injected OpenCode config into sandbox container", task_id)
    except Exception as e:
        logger.warning("[%d] Failed to inject OpenCode config: %s", task_id, e)


# ---------------------------------------------------------------------------
# Container lifecycle
# ---------------------------------------------------------------------------


@contextmanager
def start_generic_sandbox_container(
        task_id: int,
        env_vars: Dict[str, Any],
        repo_url: str = "",
) -> Generator[Container, None, None]:
    """Start a generic sandbox container for the given task.

    Manages the full lifecycle: start, yield for use, stop, and remove.
    On any error the container is always cleaned up.

    When network restriction is active (the default) the container is started
    with the ``NET_ADMIN`` capability and iptables egress rules are applied
    before the job proceeds.  If the restriction cannot be applied the
    container is torn down and a ``ContainerError`` is raised — the sandbox
    never silently runs unrestricted.
    """
    client = get_docker_client()
    container = None
    try:
        effective_allowlist = _effective_network_allowlist()
        restricted = settings.SANDBOX_NETWORK_RESTRICTED

        if restricted:
            logger.info(
                "[%d] Network restriction ACTIVE — egress limited to %d allowlisted host(s): %s",
                task_id,
                len(effective_allowlist),
                ", ".join(effective_allowlist) or "(none)",
            )
        else:
            logger.info(
                "[%d] Network restriction DISABLED (JIFFY_SANDBOX_NETWORK_RESTRICTED=false) — "
                "sandbox has open network egress",
                task_id,
            )

        mem_limit, memswap_limit = resolve_memory_limits()
        nano_cpus = resolve_cpu_nano_cpus()

        logger.info(
            "[%d] Starting sandbox container (image=%s, mem=%s, mem+swap=%s, cpus=%.2f)",
            task_id,
            settings.SANDBOX_IMAGE,
            mem_limit,
            "unlimited" if memswap_limit == -1 else memswap_limit,
            nano_cpus / 1_000_000_000,
        )

        # Surface the restriction config to the container so the startup
        # report (and anything inside) can log it.  No secrets here.
        container_env = dict(env_vars)
        container_env["JIFFY_SANDBOX_NETWORK_RESTRICTED"] = "true" if restricted else "false"
        container_env["JIFFY_SANDBOX_NETWORK_ALLOWLIST"] = ",".join(effective_allowlist)
        # Cap package-manager memory/parallelism before anything runs inside.
        container_env.update(build_package_manager_env(mem_limit))
        # Forward the LLM provider credentials the injected opencode.json needs.
        container_env.update(build_agent_provider_env())

        run_kwargs: Dict[str, Any] = {
            "detach": True,
            "remove": False,
            "tty": True,
            "mem_limit": mem_limit,
            # Passed together with mem_limit: this is the combined memory+swap
            # ceiling, so the container can spill into swap under pressure
            # instead of being OOM-killed outright.
            "memswap_limit": memswap_limit,
            "nano_cpus": nano_cpus,
            "environment": container_env,
        }
        if restricted:
            run_kwargs["cap_add"] = ["NET_ADMIN"]
            # Docker only provides the 127.0.0.11 embedded DNS resolver (which
            # the restriction script allow-lists) to containers on a
            # user-defined network. On the default "bridge" network, Docker
            # instead copies the host's own resolv.conf nameservers into the
            # container — those aren't allow-listed, so once the DROP policy
            # is in place ALL DNS resolution breaks, including for
            # allow-listed hosts.
            run_kwargs["network"] = _ensure_network(client)

        container = client.containers.run(settings.SANDBOX_IMAGE, **run_kwargs)
        logger.info("[%d] Container %s started (id=%s)", task_id, container.short_id, container.id[:12])

        # Inject OpenCode config into the sandbox container
        _inject_opencode_config(container, task_id)

        # pnpm ignores npm_config_* env vars, so lower its concurrency in its
        # own config file before the agent can run any install command.
        _apply_pnpm_limits(container, task_id)

        # Apply egress restriction before handing the container to the job.
        # Fail closed: if the rules cannot be applied, raise so the job never
        # runs against an (intended to be) restricted sandbox.
        if restricted:
            _apply_network_restriction(container, effective_allowlist, task_id=task_id)

        yield container
        logger.info("[%d] Container %s finished (id=%s)", task_id, container.short_id, container.id[:12])
    except ContainerError:
        raise
    except Exception as e:
        logger.exception("[%d] Failed to start sandbox container", task_id)
        raise ContainerError(f"Failed to start container: {e}")
    finally:
        if container:
            if settings.SANDBOX_CLEANUP:
                try:
                    container.stop(timeout=5)
                except (NotFound, Exception):
                    pass
                try:
                    container.remove(force=True)
                    logger.info("[%d] Container %s removed (id=%s)", task_id, container.short_id, container.id[:12])
                except (NotFound, Exception):
                    pass
            else:
                logger.info(
                    "[%d] Container %s cleanup skipped (JIFFY_SANDBOX_CLEANUP=false)",
                    task_id,
                    container.short_id,
                )


# ---------------------------------------------------------------------------
# Git clone
# ---------------------------------------------------------------------------


def _inject_token_into_url(url: str, token: str, provider: str = "github", username: str = "") -> str:
    """Inject a token into a git URL for authentication.

    Provider-specific formats:
    - GitHub:  https://TOKEN@github.com/user/repo.git
    - GitLab:  https://USERNAME:TOKEN@gitlab.example.com/user/repo.git
    - Gitea:   https://USERNAME:TOKEN@gitea.example.com/user/repo.git
    """
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https") and parsed.hostname:
        # GitLab and Gitea require username:token format
        if provider in ("gitlab", "gitea") and username:
            userinfo = f"{username}:{token}"
        else:
            userinfo = token
        authenticated = f"{parsed.scheme}://{userinfo}@{parsed.hostname}"
        if parsed.port:
            authenticated += f":{parsed.port}"
        authenticated += parsed.path
        if parsed.query:
            authenticated += f"?{parsed.query}"
        return authenticated
    return url


def _redact_url(url: str) -> str:
    """Redact any token/userinfo from a URL for safe logging."""
    parsed = urlparse(url)
    if parsed.username:
        redacted_netloc = parsed.netloc.replace(parsed.username, "***")
        return url.replace(parsed.netloc, redacted_netloc)
    return url


def clone_repo_in_container(
        container: Container,
        repo_url: str,
        token: str,
        task_id: int = 0,
        provider: str = "github",
        username: str = "",
) -> None:
    """Clone the repository into the container's workspace directory."""
    logger.info("[%d] Cloning %s into container %s", task_id, _redact_url(repo_url), container.short_id)

    authenticated_url = _inject_token_into_url(repo_url, token, provider=provider, username=username)

    exit_code, (output, err) = container.exec_run(
        cmd=["git", "clone", authenticated_url, WORKSPACE],
        demux=True,
    )
    if exit_code != 0:
        raise ContainerError(
            f"git clone failed (exit {exit_code}): {(err or b'').decode(errors='replace')}"
        )
    logger.info("[%d] Repository cloned into %s", task_id, WORKSPACE)

    # Explicitly land on `develop` rather than trusting the remote's default
    # branch (HEAD) — the agent must always start from a known branch, and
    # this also lets the agent skip re-cloning to switch branches itself.
    exit_code, (output, err) = container.exec_run(
        cmd=["git", "fetch", "origin", "develop"],
        workdir=WORKSPACE,
        demux=True,
    )
    if exit_code != 0:
        raise ContainerError(
            f"git fetch origin develop failed (exit {exit_code}): {(err or b'').decode(errors='replace')}"
        )

    exit_code, (output, err) = container.exec_run(
        cmd=["git", "checkout", "develop"],
        workdir=WORKSPACE,
        demux=True,
    )
    if exit_code != 0:
        raise ContainerError(
            f"git checkout develop failed (exit {exit_code}): {(err or b'').decode(errors='replace')}"
        )
    logger.info("[%d] Checked out develop branch in %s", task_id, WORKSPACE)


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------


def _get_opencode_model(container: Container) -> str:
    """Read the configured LLM model from opencode's config inside the container.

    Returns the model name or "unknown" if not found.
    """
    try:
        exit_code, (output, _) = container.exec_run(
            cmd=["cat", SANDBOX_OPENCODE_CONFIG_PATH_IN_CONTAINER], demux=True
        )
        if exit_code == 0 and output:
            config = json.loads(output)
            # opencode config has provider.model format like "anthropic/claude-sonnet-4-20250514"
            model = config.get("model")
            if model:
                return model
            # Check nested provider config
            provider = config.get("provider", {})
            if isinstance(provider, dict):
                model = provider.get("model")
                if model:
                    return model
    except (json.JSONDecodeError, Exception):
        pass
    return "unknown"


def _wait_for_exec(
        api_client: Any,
        exec_id: str,
        timeout_seconds: int,
        task_id: int = 0,
) -> int:
    """Block until a detached exec finishes and return its exit code.

    Polls ``exec_inspect`` instead of holding the exec's output stream open,
    so no single request has to survive for the whole run (see
    ``run_agent_in_container`` for why that matters).

    Raises ``ContainerError`` if the exec is still running when
    ``timeout_seconds`` elapses, or if Docker reports it as finished without
    an exit code.
    """
    started = time.monotonic()
    deadline = started + timeout_seconds
    last_progress_log = started

    while True:
        info = api_client.exec_inspect(exec_id)
        if not info.get("Running"):
            exit_code = info.get("ExitCode")
            if exit_code is None:
                raise ContainerError(
                    "Agent exec finished but Docker reported no exit code "
                    f"(state: {info.get('Status') or 'unknown'})"
                )
            return int(exit_code)

        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            raise ContainerError(
                f"Agent did not finish within {timeout_seconds}s — giving up "
                "(the container is torn down, which kills the run)"
            )

        if now - last_progress_log >= AGENT_PROGRESS_LOG_INTERVAL_SECONDS:
            logger.info(
                "[%d] Agent still running after %.0fs (%.0fs left of budget)",
                task_id,
                now - started,
                remaining,
            )
            last_progress_log = now

        time.sleep(min(AGENT_POLL_INTERVAL_SECONDS, remaining))


# ---------------------------------------------------------------------------
# Agent instructions staging
# ---------------------------------------------------------------------------

# Where the full instructions text is staged inside the container.
INSTRUCTIONS_PATH = "/tmp/jiffy_instructions.txt"

# The Linux kernel caps a *single* argv entry at MAX_ARG_STRLEN (32 pages =
# 128 KiB); anything longer makes execve fail with E2BIG. An issue thread is
# the whole conversation — issue body plus every comment — so a busy thread
# can blow past that on its own. Instructions up to this size are passed to
# the agent inline as before; beyond it they are handed over by path (see
# ``build_agent_command``) so no thread is ever too large to run.
MAX_INLINE_INSTRUCTIONS_BYTES = 96 * 1024


def write_instructions_file(container: Container, instructions: str) -> None:
    """Stage the instructions text at ``INSTRUCTIONS_PATH`` inside *container*.

    Written as a tar stream through the Docker API rather than echoed through
    a shell command: a shell command carrying the text inline is itself an
    argv entry, so it would fail with "Argument list too long" on exactly the
    large issue threads this needs to support. A tar upload has no such limit.
    """
    payload = instructions.encode("utf-8")
    directory, _, filename = INSTRUCTIONS_PATH.rpartition("/")

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo(name=filename)
        info.size = len(payload)
        info.mode = 0o644
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(payload))
    archive.seek(0)

    try:
        if not container.put_archive(directory or "/", archive.getvalue()):
            raise ContainerError(
                f"Docker rejected the instructions upload to {INSTRUCTIONS_PATH}"
            )
    except ContainerError:
        raise
    except Exception as exc:
        raise ContainerError(f"Failed to write instructions file: {exc}") from exc


def build_agent_command(instructions_bytes: int, task_id: int = 0) -> str:
    """Build the shell command that runs the agent over the staged instructions.

    Small instructions are substituted into the agent's argv, which is what
    ``opencode run`` expects. Oversized ones would hit the kernel's
    per-argument limit, so the agent is instead pointed at the staged file and
    told to read it first — the full text still reaches the agent, just
    through the filesystem instead of argv.
    """
    if instructions_bytes <= MAX_INLINE_INSTRUCTIONS_BYTES:
        prompt = f'"$(cat {INSTRUCTIONS_PATH})"'
    else:
        logger.info(
            "[%d] Instructions are %d bytes — handing them to the agent by "
            "path instead of inline to stay under the argv limit",
            task_id,
            instructions_bytes,
        )
        prompt = (
            f'"Your full task instructions are too large to pass inline and '
            f'have been written to {INSTRUCTIONS_PATH}. Read that entire file '
            f'first — it is the complete, verbatim task, including the required '
            f'output contract — then carry it out exactly as written."'
        )

    return (
        f"cd {WORKSPACE} && "
        f"opencode run --auto {prompt} > /proc/1/fd/1 2>&1"
    )


def run_agent_in_container(
        container: Container,
        instructions: str,
        task_id: int = 0,
        timeout_seconds: int | None = None,
) -> None:
    """Run the coding agent inside the container with the given instructions."""
    effective_timeout = timeout_seconds or getattr(
        settings, "SANDBOX_AGENT_TIMEOUT_SECONDS", DEFAULT_AGENT_TIMEOUT_SECONDS
    )
    model = _get_opencode_model(container)
    logger.info("[%d] Running agent in container %s (timeout=%ds, model=%s)", task_id, container.short_id,
                effective_timeout, model)

    write_instructions_file(container, instructions)

    # Use login shell (-l) so .profile is sourced and all tools (nvm, uv, etc.) are available
    # Redirect agent stdout/stderr to the container's main stdout so docker logs
    # shows real-time output; exit code remains captured via exec_inspect.
    run_cmd = build_agent_command(len(instructions.encode("utf-8")), task_id=task_id)

    # Start the agent *detached* and poll for completion instead of blocking on
    # the exec's output stream.
    #
    # A non-detached exec holds one HTTP connection open for the command's
    # entire duration, and that connection usually crosses a socket proxy:
    # the compose stack's docker-socket-proxy (HAProxy) closes any connection
    # after 10 minutes ("timeout client/server 10m"). When that happened,
    # docker-py saw a clean EOF, the following exec_inspect still reported the
    # exec as running, and the caller got the useless "Agent exited with code
    # None" — while the agent itself was very much alive inside the container.
    #
    # Polling keeps every request short, so no proxy or client timeout can
    # truncate a long run, and the execution window becomes a budget the
    # Gateway enforces itself. Nothing is lost by detaching: the command
    # already redirects all of its output to the container's main stdout for
    # `docker logs`, and the real result is read from .jiffy_result.json.
    api_client = container.client.api
    exec_id = api_client.exec_create(
        container.id,
        cmd=["bash", "-l", "-c", run_cmd],
        workdir=WORKSPACE,
    )["Id"]
    api_client.exec_start(exec_id, detach=True)

    exit_code = _wait_for_exec(api_client, exec_id, effective_timeout, task_id=task_id)

    if exit_code != 0:
        raise ContainerError(
            f"Agent exited with code {exit_code}"
        )
    logger.info("[%d] Agent finished in container %s (exit 0)", task_id, container.short_id)
