"""Callback dispatch — sends reports to callback_url.

Uses a declarative callback spec per provider so the agent (first attempt)
and the Gateway (fallback) build requests identically.
"""

import logging
import time
from typing import TYPE_CHECKING, Any

import requests

from jobs.callback_retry import (
    BASE_DELAY_SECONDS,
    MAX_ATTEMPTS,
    TIMEOUT_SECONDS,
    delay_before_attempt,
    is_success,
    is_transient_status,
)
from jobs.callback_specs import (
    build_callback_body,
    build_callback_headers,
    get_callback_spec,
)

if TYPE_CHECKING:
    from jobs.models import Task

logger = logging.getLogger(__name__)

# Kept as module-level names for backwards compatibility; the values now come
# from the shared policy so the Gateway and the sandbox wrapper retry alike.
MAX_RETRIES = MAX_ATTEMPTS
RETRY_DELAY_SECONDS = BASE_DELAY_SECONDS


# Marks a comment as a question rather than a result. Kept on the first line and
# stable across providers so a reader — a human scanning the thread, the edge, or
# the next run reading its own earlier turn — can tell the two apart without
# parsing the prose.
QUESTION_TAG = "[JIFFY:QUESTION]"

QUESTION_REPLY_HINT = (
    "Reply on this issue to answer — your reply starts a new Jiffy task with "
    "the full thread."
)


def format_callback_body(
    task_id: int,
    status: str,
    summary: str | None = None,
    technical_report: str | None = None,
    branch_name: str | None = None,
    pr_url: str | None = None,
    error_message: str | None = None,
    question: str | None = None,
) -> str:
    """Format the callback payload as human-readable text.

    Args:
        task_id: The task ID.
        status: The final status ("done", "failed", or "question").
        summary: Optional summary of the result.
        technical_report: Optional detailed technical report in markdown.
        branch_name: Optional branch name.
        pr_url: Optional PR/MR URL if one was opened.
        error_message: Optional error message if the task failed.
        question: The agent's question, when *status* is "question".

    Returns:
        Human-readable text suitable for posting as an issue/PR comment.
    """
    if status == "question":
        # The agent stopped to ask rather than guess. The question is posted
        # back as a comment on the originating issue; the answer arrives as a
        # reply, which the edge picks up as a brand-new task.
        lines = [
            f"{QUESTION_TAG} Task #{task_id}: ❓ Jiffy has a question before continuing.",
            "",
            f"**Question:** {question or 'No question text was provided.'}",
        ]
        if branch_name:
            lines.append("")
            lines.append(f"**Branch:** {branch_name}")
        if summary:
            lines.append("")
            lines.append(f"**Progress so far:** {summary}")
        lines.extend(["", "---", "", QUESTION_REPLY_HINT])
        return "\n".join(lines)

    if status == "failed":
        lines = [
            f"Task #{task_id}: ❌ Jiffy could not complete this task.",
            "",
        ]
        if error_message:
            lines.append(f"**Reason:** {error_message}")
        return "\n".join(lines)

    lines = [
        f"Task #{task_id}: ✅ Jiffy completed this task.",
    ]
    if summary:
        lines.append("")
        lines.append(f"**Summary:** {summary}")
    if branch_name:
        lines.append(f"**Branch:** {branch_name}")
    if pr_url:
        lines.append(f"**Pull Request:** {pr_url}")
    if technical_report:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("### Technical Report")
        lines.append(technical_report)
    return "\n".join(lines)


def send_callback(
        task: "Task",
        status: str,
        summary: str | None = None,
        technical_report: str | None = None,
        pr_url: str | None = None,
        error_message: str | None = None,
        model: str | None = None,
) -> None:
    """Send a callback to task.callback_url using the provider's callback spec.

    Retries up to MAX_RETRIES times on failure. Logs failures without
    raising exceptions.

    Args:
        task: The Task instance.
        status: The final status ("done" or "failed").
        summary: Optional summary of the result.
        technical_report: Optional detailed technical report in markdown.
        pr_url: Optional PR/MR URL if one was opened.
        error_message: Optional error message if the task failed.
        model: Optional LLM model used for the task.
    """
    try:
        spec = get_callback_spec(task.provider)
    except KeyError:
        logger.error(
            "Unknown provider %s for task %d — cannot build callback",
            task.provider,
            task.id,
        )
        return

    _send_callback_via_spec(
        spec=spec,
        task_id=task.id,
        callback_url=task.callback_url,
        callback_secret=task.callback_secret,
        status=status,
        summary=summary,
        technical_report=technical_report,
        branch_name=task.branch_name,
        pr_url=pr_url,
        error_message=error_message or task.error_message,
    )


def send_fallback_callback(
    task: "Task",
    status: str,
    summary: str | None = None,
    technical_report: str | None = None,
    branch_name: str | None = None,
    pr_url: str | None = None,
    error_message: str | None = None,
    question: str | None = None,
) -> None:
    """Gateway fallback callback using the provider's spec.

    Called when the agent did not attempt or failed its own callback attempt.
    Uses the same declarative spec the agent was given.
    """
    try:
        spec = get_callback_spec(task.provider)
    except KeyError:
        logger.error(
            "Unknown provider %s for task %d — cannot send fallback callback",
            task.provider,
            task.id,
        )
        return

    _send_callback_via_spec(
        spec=spec,
        task_id=task.id,
        callback_url=task.callback_url,
        callback_secret=task.callback_secret,
        status=status,
        summary=summary,
        technical_report=technical_report,
        branch_name=branch_name,
        pr_url=pr_url,
        error_message=error_message,
        question=question,
    )


def _send_callback_via_spec(
    spec: dict[str, Any],
    task_id: int,
    callback_url: str,
    callback_secret: str,
    status: str,
    summary: str | None = None,
    technical_report: str | None = None,
    branch_name: str | None = None,
    pr_url: str | None = None,
    error_message: str | None = None,
    question: str | None = None,
) -> None:
    """Low-level callback delivery using a declarative spec.

    Shared by ``send_callback`` (Gateway-owned fallback) and the agent's own
    first-attempt logic (documented in the agent instructions).
    """
    method = spec["method"]
    url = callback_url

    body_text = format_callback_body(
        task_id=task_id,
        status=status,
        summary=summary,
        technical_report=technical_report,
        branch_name=branch_name,
        pr_url=pr_url,
        error_message=error_message,
        question=question,
    )

    headers = build_callback_headers(spec, callback_secret)
    body = build_callback_body(spec, body_text)

    last_error = "no attempt was made"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        delay = delay_before_attempt(attempt)
        if delay:
            logger.info(
                "Callback for task %d: waiting %.1fs before attempt %d/%d",
                task_id,
                delay,
                attempt,
                MAX_ATTEMPTS,
            )
            time.sleep(delay)

        try:
            response = requests.request(
                method, url, data=body, headers=headers, timeout=TIMEOUT_SECONDS
            )
            if is_success(response.status_code):
                logger.info(
                    "Callback delivered for task %d on attempt %d/%d (HTTP %d)",
                    task_id,
                    attempt,
                    MAX_ATTEMPTS,
                    response.status_code,
                )
                return
            last_error = f"HTTP {response.status_code}"
            if not is_transient_status(response.status_code):
                # A rejected secret or a missing issue answers the same way
                # however many times it is asked. Fail now rather than spend
                # the retry budget confirming it.
                logger.error(
                    "Callback for task %d returned %d on attempt %d/%d — client "
                    "error, not retrying. callback_url=%s, status=%s",
                    task_id,
                    response.status_code,
                    attempt,
                    MAX_ATTEMPTS,
                    callback_url,
                    status,
                )
                return
            logger.warning(
                "Callback for task %d returned %d (attempt %d/%d) — retrying",
                task_id,
                response.status_code,
                attempt,
                MAX_ATTEMPTS,
            )
        except requests.RequestException as exc:
            last_error = str(exc)
            logger.warning(
                "Callback for task %d failed with %s (attempt %d/%d) — retrying",
                task_id,
                exc,
                attempt,
                MAX_ATTEMPTS,
            )

    logger.error(
        "All %d callback attempts failed for task %d (last error: %s). "
        "callback_url=%s, status=%s",
        MAX_ATTEMPTS,
        task_id,
        last_error,
        callback_url,
        status,
    )
