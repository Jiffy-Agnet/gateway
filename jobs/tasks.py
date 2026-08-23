"""Celery tasks for job execution."""

import logging
import time
from typing import Any

from celery import shared_task
from django.conf import settings
from django.db import connections

from apps.ingestion.callback import send_fallback_callback
from jobs.callback_specs import build_sandbox_callback_config, get_callback_spec
from jobs.execution.agent import (
    AgentResult,
    build_agent_instructions,
    read_agent_result,
)
from jobs.execution.container import (
    clone_repo_in_container,
    ensure_sandbox_image,
    run_agent_in_container,
    start_generic_sandbox_container,
)
from jobs.execution.exceptions import ExecutionError
from jobs.models import Task
from jobs.utils.redis import load_payload_from_redis

logger = logging.getLogger(__name__)

# How hard the worker tries to read a Task row that the ingestion service has
# already committed.  See ``_fetch_task``.
TASK_LOOKUP_ATTEMPTS = 5
TASK_LOOKUP_DELAY_SECONDS = 0.5
# Delay before Celery re-delivers the job when the row is still missing after
# the in-process attempts above.
TASK_LOOKUP_RETRY_COUNTDOWN = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _database_path() -> str:
    """Path of the database this process is actually talking to."""
    return str(settings.DATABASES["default"]["NAME"])


def _task_row_count() -> str:
    """Total Task rows, for diagnostics — never raises."""
    try:
        return str(Task.objects.count())
    except Exception as exc:  # pragma: no cover - diagnostics only
        return f"unavailable ({exc})"


def _fetch_task(task_id: int) -> Task | None:
    """Load the Task row, tolerating a commit that is not visible yet.

    Ingestion enqueues the job from ``transaction.on_commit``, so the row is
    committed before the Celery message is published.  A worker in another
    process can still miss it on the first read — a connection inherited across
    the prefork fork, or a bind-mounted SQLite file that has not been re-read
    yet — so retry a few times on a *fresh* connection before giving up.
    """
    for attempt in range(1, TASK_LOOKUP_ATTEMPTS + 1):
        try:
            return Task.objects.get(id=task_id)
        except Task.DoesNotExist:
            if attempt == TASK_LOOKUP_ATTEMPTS:
                return None
            logger.warning(
                "[task_id=%d] Task row not visible yet (attempt %d/%d) — "
                "reopening the DB connection and retrying",
                task_id,
                attempt,
                TASK_LOOKUP_ATTEMPTS,
            )
            # Drop the connection so the next read cannot be served from a
            # stale/forked one.
            connections["default"].close()
            time.sleep(TASK_LOOKUP_DELAY_SECONDS)
    return None


def _report_missing_task(task_id: int) -> None:
    """Best-effort failure callback for a task whose DB row cannot be found.

    Without the row there is no provider or callback URL in the DB, but the
    ingestion payload in Redis carries both — so the issue thread gets a reply
    instead of silence.  Every failure here is swallowed: this is already the
    last-resort path.
    """
    try:
        payload = load_payload_from_redis(task_id)
        provider = payload.get("provider") or ""
        callback = payload.get("callback") or {}
        if not provider or not callback.get("url"):
            logger.error(
                "[task_id=%d] Cannot notify the issue thread: payload has no "
                "provider/callback URL",
                task_id,
            )
            return

        # Unsaved instance — used only to build the callback request.
        send_fallback_callback(
            Task(
                id=task_id,
                provider=provider,
                callback_url=callback["url"],
                callback_secret=callback.get("secret", ""),
                status="failed",
            ),
            status="failed",
            error_message=(
                "Internal Gateway error: the task record was not found by the "
                "worker. Please retry the request."
            ),
        )
    except Exception as exc:
        logger.error(
            "[task_id=%d] Could not send the missing-task callback: %s",
            task_id,
            exc,
        )


def _task_log(
    task_id: int,
    level: int,
    msg: str,
    *args: Any,
    provider: str = "",
    **kwargs: Any,
) -> None:
    """Log a message prefixed with ``[task_id]`` (and optionally provider) for easy grep."""
    prefix = f"[{task_id}]"
    if provider:
        prefix = f"[{task_id}|{provider}]"
    logger.log(level, f"{prefix} {msg}", *args, **kwargs)


def _update_status(task: Task, status: str) -> None:
    """Update the task status in its own short write transaction."""
    task.status = status
    task.save(update_fields=["status", "updated_at"])


def _agent_callback_succeeded(result: AgentResult | None) -> bool:
    """Return True if the agent successfully delivered its own callback."""
    if result is None:
        return False
    cb = result.callback
    if not isinstance(cb, dict):
        return False
    return bool(cb.get("attempted")) and bool(cb.get("succeeded"))


def _handle_callback(
    task: Task,
    result: AgentResult | None,
    status: str,
    summary: str | None = None,
    technical_report: str | None = None,
    branch_name: str | None = None,
    pr_url: str | None = None,
    error_message: str | None = None,
    question: str | None = None,
) -> None:
    """Handle callback delivery: agent-first, Gateway fallback.

    If the agent already delivered the callback successfully, skip Gateway
    callback.  Otherwise fall back to the Gateway sending via the spec.
    """
    if _agent_callback_succeeded(result):
        _task_log(
            task.id,
            logging.INFO,
            "Agent already delivered callback successfully — skipping Gateway callback",
            provider=task.provider,
        )
        return

    _task_log(
        task.id,
        logging.WARNING,
        "Agent callback not delivered (attempted=%s, succeeded=%s) — Gateway falling back",
        result.callback.get("attempted", False) if result else "N/A",
        result.callback.get("succeeded", False) if result else "N/A",
        provider=task.provider,
    )

    send_fallback_callback(
        task,
        status=status,
        summary=summary,
        technical_report=technical_report,
        branch_name=branch_name,
        pr_url=pr_url,
        error_message=error_message,
        question=question,
    )


def _ask_task(task: Task, result: AgentResult) -> None:
    """Park a task on the agent's question and relay it to the issue thread.

    Not a failure: the agent stopped rather than guess. The question is posted
    as a reply on the originating issue, and the answer comes back as a new
    Jiffy task with the full thread.
    """
    task.status = "needs_input"
    task.question = result.question
    task.branch_name = result.branch_name
    task.programming_language = result.programming_language
    task.pr_url = result.pr_url
    task.save(
        update_fields=[
            "status",
            "question",
            "branch_name",
            "programming_language",
            "pr_url",
            "updated_at",
        ]
    )
    _task_log(
        task.id,
        logging.INFO,
        "Agent asked a question — status → needs_input: %s",
        result.question,
        provider=task.provider,
    )
    _handle_callback(
        task,
        result,
        status="question",
        summary=result.summary,
        branch_name=result.branch_name,
        question=result.question,
    )


def _fail_task(task: Task, error_message: str, result: AgentResult | None = None) -> None:
    """Mark a task as failed and handle callback (agent-first, Gateway fallback)."""
    task.status = "failed"
    task.error_message = error_message
    task.save(update_fields=["status", "error_message", "updated_at"])
    _task_log(task.id, logging.ERROR, "Task failed: %s", error_message, provider=task.provider)
    _handle_callback(task, result, status="failed", error_message=error_message)


def _redact_payload_for_log(payload: dict) -> dict:
    """Return a shallow copy of the payload with secrets masked for logging."""
    redacted = dict(payload)
    repo = dict(redacted.get("repo", {}))
    repo["token"] = "***"
    redacted["repo"] = repo
    callback = dict(redacted.get("callback", {}))
    callback["secret"] = "***"
    redacted["callback"] = callback
    return redacted


# ---------------------------------------------------------------------------
# Main task
# ---------------------------------------------------------------------------


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    queue="execute",
)
def execute_task(self, task_id: int) -> None:
    """Execute a coding task in an isolated sandbox container.

    Sequence:
      0. Load task + payload, log start
      1. Ensure sandbox image exists (build if missing)
      2. Provision the generic sandbox container
      3. Clone the repo into it
      4. Hand off to the agent with instructions
      5. Read the agent's structured result
      6. Report via callback
    """
    start_time = time.monotonic()

    # --- Load task and payload ------------------------------------------------
    task = _fetch_task(task_id)
    if task is None:
        logger.error(
            "[task_id=%d] Task not found in DB after %d attempts "
            "(database=%s, task rows=%s). The worker and the ingestion service "
            "must use the same database file — check that both containers mount "
            "the same data volume (see DATABASE_PATH).",
            task_id,
            TASK_LOOKUP_ATTEMPTS,
            _database_path(),
            _task_row_count(),
        )
        # Give a slow/lagging commit one more chance before writing the task off.
        if not self.request.called_directly and self.request.retries < self.max_retries:
            raise self.retry(countdown=TASK_LOOKUP_RETRY_COUNTDOWN)
        _report_missing_task(task_id)
        return

    _task_log(task_id, logging.INFO, "Task started", provider=task.provider)

    try:
        payload = load_payload_from_redis(task_id)
    except ValueError as e:
        _task_log(task_id, logging.ERROR, "Failed to load payload: %s", e, provider=task.provider)
        _fail_task(task, str(e))
        return

    repo_url = payload["repo"]["url"]
    repo_token = payload["repo"]["token"]
    repo_username = payload["repo"].get("username", "")
    callback = payload["callback"]
    issue_id = payload.get("issue", {}).get("external_issue_id", "?")
    _task_log(
        task_id,
        logging.INFO,
        "Payload loaded — repo=%s issue=%s",
        repo_url,
        issue_id,
        provider=task.provider,
    )

    # --- Ensure sandbox image exists -----------------------------------------
    try:
        _task_log(task_id, logging.INFO, "Checking sandbox image...", provider=task.provider)
        ensure_sandbox_image()
    except ExecutionError as e:
        _task_log(task_id, logging.ERROR, "Sandbox image check/build failed: %s", e, provider=task.provider)
        _fail_task(task, str(e))
        return

    # --- Provision → Clone → Run → Report ------------------------------------
    result: AgentResult | None = None
    try:
        _update_status(task, "provisioning")
        _task_log(task_id, logging.INFO, "Status → provisioning", provider=task.provider)

        env_vars = {"REPO_TOKEN": repo_token}

        with start_generic_sandbox_container(task.id, env_vars, repo_url=repo_url) as container:
            # Cloning
            _update_status(task, "cloning")
            _task_log(task_id, logging.INFO, "Status → cloning", provider=task.provider)
            clone_repo_in_container(
                container, repo_url, repo_token, task_id=task_id,
                provider=task.provider, username=repo_username,
            )

            # Running — agent does everything from here
            _update_status(task, "running")
            instructions = build_agent_instructions(payload)
            try:
                callback_config = build_sandbox_callback_config(
                    get_callback_spec(task.provider),
                    callback_url=callback["url"],
                    callback_secret=callback.get("secret", ""),
                )
            except KeyError:
                # Unknown provider: the run still goes ahead, and the Gateway
                # fallback remains the safety net for reporting.
                callback_config = None
                _task_log(
                    task_id,
                    logging.WARNING,
                    "No callback spec for this provider — the sandbox will run "
                    "without the callback wrapper",
                    provider=task.provider,
                )
            # Size is logged so a prompt that arrives at the agent truncated is
            # visible in one line rather than inferred from the agent asking
            # what the task was.
            _task_log(
                task_id,
                logging.INFO,
                "Status → running — handing off to agent (%d bytes of instructions)",
                len(instructions.encode("utf-8")),
                provider=task.provider,
            )
            run_agent_in_container(
                container,
                instructions,
                task_id=task_id,
                callback_config=callback_config,
            )

            # Read result
            result = read_agent_result(container)

        if result.status == "done":
            _task_log(
                task_id,
                logging.INFO,
                "Agent result: done — model=%s branch=%s pr=%s lang=%s callback=%s",
                result.model or "(unknown)",
                result.branch_name or "(none)",
                result.pr_url or "(none)",
                result.programming_language or "(none)",
                result.callback or "(none)",
                provider=task.provider,
            )
        elif result.status == "question":
            _task_log(
                task_id,
                logging.INFO,
                "Agent result: question — model=%s branch=%s callback=%s",
                result.model or "(unknown)",
                result.branch_name or "(none)",
                result.callback or "(none)",
                provider=task.provider,
            )
        else:
            _task_log(
                task_id,
                logging.WARNING,
                "Agent result: failed — model=%s error=%s callback=%s",
                result.model or "(unknown)",
                result.error_message or "(no details)",
                result.callback or "(none)",
                provider=task.provider,
            )

        if result.status == "question":
            _ask_task(task, result)
            return

        if result.status != "done":
            _fail_task(task, error_message=result.error_message or "Agent reported failure without details.", result=result)
            return

        task.branch_name = result.branch_name
        task.programming_language = result.programming_language
        task.pr_url = result.pr_url
        task.save(update_fields=["branch_name", "programming_language", "pr_url"])

        # Callback: agent attempted first, Gateway falls back if needed
        _handle_callback(
            task,
            result=result,
            status="done",
            summary=result.summary,
            technical_report=result.technical_report,
            branch_name=result.branch_name,
            pr_url=result.pr_url,
        )
        _update_status(task, "done")

        elapsed = time.monotonic() - start_time
        _task_log(
            task_id,
            logging.INFO,
            "Task completed successfully in %.1fs",
            elapsed,
            provider=task.provider,
        )

    except ExecutionError as e:
        elapsed = time.monotonic() - start_time
        _task_log(
            task_id,
            logging.ERROR,
            "Execution failed after %.1fs: %s",
            elapsed,
            e,
            provider=task.provider,
        )
        _fail_task(task, str(e), result=result)
    except Exception as e:
        elapsed = time.monotonic() - start_time
        _task_log(
            task_id,
            logging.ERROR,
            "Unexpected error after %.1fs: %s",
            elapsed,
            e,
            provider=task.provider,
        )
        _fail_task(task, "An unexpected internal error occurred.", result=result)
        raise self.retry(exc=e)
