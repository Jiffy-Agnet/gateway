"""Declarative callback specs per provider.

Each spec describes how to call that provider's callback endpoint:
HTTP method, auth header name/value prefix, content type, any extra static
headers, how the report text is carried in the body, and which fields go in
the body, query string, or headers.

Adding a new provider means adding a new entry here — no pipeline code changes.
"""

import json
from typing import Any

PROVIDER_NAMES = ("github", "gitlab", "gitea")

# GitHub requires an explicit API version header; pin it here so callbacks
# don't silently follow whatever the API's default version becomes.
GITHUB_API_VERSION = "2026-03-10"

CALLBACK_SPECS: dict[str, dict[str, Any]] = {
    # callback.url is the Issue Comments API:
    # POST https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments
    # It expects the comment markdown as JSON under "body" — not text/plain.
    "github": {
        "method": "POST",
        "auth_header": "Authorization",
        "auth_value_prefix": "Bearer ",
        "content_type": "application/json",
        "extra_headers": {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
        "body_format": "json",
        "body_text_field": "body",
        "body_fields": [
            "task_id",
            "status",
            "summary",
            "technical_report",
            "branch_name",
            "pr_url",
            "error_message",
        ],
        "query_fields": [],
        "header_fields": [],
    },
    # NOTE: GitLab's Issue Notes API and Gitea's Issue Comments API also expect
    # JSON under "body". They are left on the pre-existing plain-text behavior
    # here on purpose — flipping them is a separate, separately-testable change.
    "gitlab": {
        "method": "POST",
        "auth_header": "Authorization",
        "auth_value_prefix": "Bearer ",
        "content_type": "text/plain; charset=utf-8",
        "body_fields": [
            "task_id",
            "status",
            "summary",
            "technical_report",
            "branch_name",
            "pr_url",
            "error_message",
        ],
        "query_fields": [],
        "header_fields": [],
    },
    "gitea": {
        "method": "POST",
        "auth_header": "Authorization",
        "auth_value_prefix": "Bearer ",
        "content_type": "text/plain; charset=utf-8",
        "body_fields": [
            "task_id",
            "status",
            "summary",
            "technical_report",
            "branch_name",
            "pr_url",
            "error_message",
        ],
        "query_fields": [],
        "header_fields": [],
    },
}


def get_callback_spec(provider: str) -> dict[str, Any]:
    """Return the callback spec for *provider*, raising KeyError if unknown."""
    if provider not in CALLBACK_SPECS:
        raise KeyError(
            f"Unknown provider {provider!r}. Known providers: {', '.join(CALLBACK_SPECS)}"
        )
    return CALLBACK_SPECS[provider]


def build_callback_headers(spec: dict[str, Any], callback_secret: str) -> dict[str, str]:
    """Build the request headers defined by *spec*.

    The secret is forwarded byte-for-byte behind the spec's auth prefix — the
    Gateway never signs, hashes, or otherwise transforms it.
    """
    headers: dict[str, str] = {
        "Content-Type": spec.get("content_type", "text/plain; charset=utf-8"),
        spec["auth_header"]: spec["auth_value_prefix"] + callback_secret,
    }
    headers.update(spec.get("extra_headers", {}))
    return headers


def build_callback_body(spec: dict[str, Any], body_text: str) -> bytes:
    """Encode the human-readable report *body_text* as the spec's wire body.

    ``body_format: "json"`` wraps the text in a JSON object under the spec's
    ``body_text_field`` — what a comment API such as GitHub's Issue Comments
    endpoint expects. Anything else sends the text as raw UTF-8 bytes.
    """
    if spec.get("body_format") == "json":
        field = spec.get("body_text_field", "body")
        return json.dumps(
            {field: body_text}, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    return body_text.encode("utf-8")


def build_sandbox_callback_config(
    spec: dict[str, Any],
    callback_url: str,
    callback_secret: str,
) -> dict[str, Any]:
    """Config for the sandbox's callback wrapper: where to post, and how to retry.

    Everything the wrapper needs is resolved here, from the same spec the
    Gateway's own sender uses, so the two cannot describe the endpoint
    differently. The retry half comes from ``jobs.callback_retry``.
    """
    from jobs.callback_retry import policy_for_sandbox

    config: dict[str, Any] = {
        "method": spec["method"],
        "url": callback_url,
        "headers": build_callback_headers(spec, callback_secret),
        "body_format": spec.get("body_format", "text"),
        "body_text_field": spec.get("body_text_field", "body"),
    }
    config.update(policy_for_sandbox())
    return config


def build_callback_payload(
    spec: dict[str, Any],
    task_id: int,
    status: str,
    summary: str | None = None,
    technical_report: str | None = None,
    branch_name: str | None = None,
    pr_url: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """Build the body payload as defined by *spec*.

    Only includes fields listed in the spec's ``body_fields`` that are
    non-``None``, plus ``task_id`` and ``status`` (both always included).
    """
    payload: dict[str, Any] = {"task_id": task_id, "status": status}
    for field in spec.get("body_fields", []):
        if field in ("task_id", "status"):
            continue
        val = {
            "summary": summary,
            "technical_report": technical_report,
            "branch_name": branch_name,
            "pr_url": pr_url,
            "error_message": error_message,
        }.get(field)
        if val is not None:
            payload[field] = val
    return payload


def build_callback_request(
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
) -> tuple[str, str, dict[str, str], bytes]:
    """Build the (method, url, headers, body) for a callback request.

    Uses the declarative *spec* so the caller doesn't have to know provider-
    specific details.
    """
    method = spec["method"]
    body = build_callback_payload(
        spec,
        task_id=task_id,
        status=status,
        summary=summary,
        technical_report=technical_report,
        branch_name=branch_name,
        pr_url=pr_url,
        error_message=error_message,
    )

    body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")

    headers = build_callback_headers(spec, callback_secret)

    for field_name in spec.get("header_fields", []):
        val = {
            "task_id": str(task_id),
            "status": status,
            "summary": summary,
            "branch_name": branch_name,
            "pr_url": pr_url,
            "error_message": error_message,
        }.get(field_name)
        if val is not None:
            headers[field_name] = str(val)

    return method, callback_url, headers, body_bytes
