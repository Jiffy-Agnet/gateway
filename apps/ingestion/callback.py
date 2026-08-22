"""Callback dispatch — sends reports to callback_url.

Uses a declarative callback spec per provider so the agent (first attempt)
and the Gateway (fallback) build requests identically.
"""

import logging
import time
from typing import TYPE_CHECKING, Any

import requests

from jobs.callback_specs import (
    build_callback_body,
    build_callback_headers,
    get_callback_spec,
)

if TYPE_CHECKING:
    from jobs.models import Task

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2


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
            f"Task #{task_id}: ❓ Jiffy has a question before continuing.",
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

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.request(
                method, url, data=body, headers=headers, timeout=30
            )
            if response.status_code < 300:
                logger.info(
                    "Callback delivered for task %d (attempt %d)",
                    task_id,
                    attempt,
                )
                return
            logger.warning(
                "Callback for task %d returned %d (attempt %d/%d)",
                task_id,
                response.status_code,
                attempt,
                MAX_RETRIES,
            )
        except requests.RequestException as exc:
            logger.warning(
                "Callback for task %d failed with %s (attempt %d/%d)",
                task_id,
                exc,
                attempt,
                MAX_RETRIES,
            )

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)

    logger.error(
        "All %d callback attempts failed for task %d. "
        "callback_url=%s, status=%s",
        MAX_RETRIES,
        task_id,
        callback_url,
        status,
    )
