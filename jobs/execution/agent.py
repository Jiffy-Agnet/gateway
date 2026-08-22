"""Handles agent instructions and result parsing."""
import json
import logging
import re
from typing import Any, Dict, NamedTuple

from docker.models.containers import Container

from apps.ingestion.callback import QUESTION_TAG
from jobs.callback_specs import get_callback_spec
from jobs.execution.container import INSTRUCTIONS_PATH

logger = logging.getLogger(__name__)

AGENT_RESULT_PATH = "/workspace/.jiffy_result.json"

# Statuses the agent may report in its result file. "question" means the run
# ended because the agent needs an answer from the requester before it can
# continue — it is neither a success nor a failure.
VALID_AGENT_STATUSES = ("done", "failed", "question")

# The issue text is fenced by these markers so the agent can tell a short
# request apart from a truncated one. Seeing the end marker is proof it got
# the whole thread — which is what stops it asking the requester to "re-send
# the task", the one question that is never worth a round trip.
ISSUE_BEGIN_MARKER = "<<<JIFFY-ISSUE-BEGIN>>>"
ISSUE_END_MARKER = "<<<JIFFY-ISSUE-END>>>"


class AgentResult(NamedTuple):
    status: str
    branch_name: str | None
    pr_url: str | None
    programming_language: str | None
    summary: str | None
    technical_report: str | None
    error_message: str | None
    model: str | None
    callback: dict | None
    # Trails the required fields so it can default: only a "question" status
    # carries one.
    question: str | None = None


def _format_turns(turns: list[dict]) -> str:
    """Format a turns array into a human-readable thread with role annotations."""
    blocks = []
    for t in turns:
        role_label = "User" if t.get("role") == "user" else "Agent (Jiffy)"
        author = t.get("author", "unknown")
        body = t.get("body", "")
        blocks.append(f"--- Turn: {role_label} ({author}) ---\n{body}")
    return "\n\n".join(blocks)


def _extract_issue_text(payload: Dict[str, Any]) -> str:
    """Extract the issue thread text from payload, preferring structured turns over legacy text."""
    issue = payload.get("issue", {})
    turns = issue.get("turns")
    if turns and isinstance(turns, list) and len(turns) > 0:
        text = _format_turns(turns)
    else:
        text = issue.get("text", "")
    if not text.strip():
        # The agent is about to be handed an empty request, which is the one
        # thing guaranteed to make it ask what the task is. Say so here, where
        # it is a Gateway/edge bug and not the requester's fault.
        logger.warning(
            "Issue %s produced no text — the agent will receive an empty request",
            issue.get("external_issue_id", "?"),
        )
    return text


# Matches a "Suggested codebase areas" section header (with or without a
# markdown heading marker or trailing colon) and captures everything up to
# the next section-like heading or the end of the text.
_SUGGESTED_AREAS_RE = re.compile(
    r"(?im)^#{0,6}\s*suggested codebase areas\s*:?\s*\n"
    r"(.*?)"
    r"(?=\n#{1,6}\s+\S|\n[A-Z][A-Za-z0-9 /_-]*:\s*\n|\Z)",
    re.DOTALL,
)


def _extract_suggested_areas(issue_text: str) -> str | None:
    """Extract a "Suggested codebase areas" section from the issue text, if present."""
    match = _SUGGESTED_AREAS_RE.search(issue_text)
    if not match:
        return None
    content = match.group(1).strip()
    return content or None


def build_agent_instructions(payload: Dict[str, Any]) -> str:
    """Build the instructions text handed to the coding agent.

    The instructions are agent-agnostic — they describe the contract without
    assuming any particular CLI conventions.
    """
    issue_text = _extract_issue_text(payload)
    suggested_areas = _extract_suggested_areas(issue_text)
    provider = payload.get("repo", {}).get("provider_hint", "github")
    callback_url = payload.get("callback", {}).get("url", "")
    callback_secret = payload.get("callback", {}).get("secret", "")

    # Resolve provider from repo URL if not explicitly set
    if provider == "github" and "gitlab" in payload.get("repo", {}).get("url", ""):
        provider = "gitlab"
    elif provider == "github" and "gitea" in payload.get("repo", {}).get("url", ""):
        provider = "gitea"

    spec = get_callback_spec(provider)

    extra_header_lines = "\n".join(
        f"  - `{name}: {value}`" for name, value in spec.get("extra_headers", {}).items()
    ) or "  - (none beyond the auth header and Content-Type)"

    if spec.get("body_format") == "json":
        body_field = spec.get("body_text_field", "body")
        body_description = (
            f"a JSON object with the formatted text above as a single string "
            f'under the `{body_field}` key, e.g. '
            f'`{{"{body_field}": "Task #1: ✅ Jiffy completed this task.\\n..."}}`. '
            "Escape newlines properly — it must be valid JSON."
        )
    else:
        body_description = "the formatted text described above (UTF-8 encoded bytes)."

    where_to_start_block = ""
    if suggested_areas:
        where_to_start_block = (
            "\n## Where to Start\n\n"
            'The issue text includes a "Suggested codebase areas" section '
            "identifying the paths most likely relevant to this task:\n\n"
            f"{suggested_areas}\n\n"
            "Begin your exploration there instead of scanning the entire "
            "repository from scratch. Treat it as a starting point, not an "
            "exhaustive boundary — follow the investigation beyond these "
            "paths if it leads you elsewhere.\n"
        )

    return f"""\
You are a coding agent working in an isolated sandbox environment.

## Issue / Request

Between the two markers below is the full, verbatim text of the task you must
complete. Do not summarize or pre-parse it — implement exactly what it asks for.

The markers are an integrity check: if you can see `{ISSUE_END_MARKER}`, you have
the whole request, however short it looks, and there is nothing missing to ask
about. If the end marker is absent, your copy was truncated in transit — read
the complete text from `{INSTRUCTIONS_PATH}` and work from that instead. Either
way, never ask the requester to re-send or complete the task text.

{ISSUE_BEGIN_MARKER}
{issue_text}
{ISSUE_END_MARKER}

## Working Directory

The target repository has already been cloned into:

    /workspace

and is already checked out on the `develop` branch. This is done for you —
under no circumstances should you run `git clone` again; the clone already
exists and re-cloning wastes time and can conflict with the existing working
tree. If you need to switch to a different branch or refresh the current one,
use `git checkout <branch>` or `git pull` only.

All work — reading files, making changes, running tests — happens inside that
directory unless you have a reason to go elsewhere.

## Resource Limits

This sandbox runs with a hard memory and CPU cap. Package installs and builds
are the usual way to hit it: a process killed with exit code 137 means the
container ran out of memory, not that your change was wrong.

`NODE_OPTIONS`, `npm_config_*`, `CARGO_BUILD_JOBS`, `MAKEFLAGS` and
`GOMAXPROCS` are already set in your environment to keep installs within the
cap — do not unset or raise them. If an install or build is still OOM-killed,
retry it with lower parallelism (e.g. `npm install --maxsockets=1`,
`pnpm install --network-concurrency=1`) rather than raising the limits.
{where_to_start_block}
## What You Must Do

You own the entire rest of the workflow. Specifically:

1. **Analyze** the repository and the issue text to understand what is being
   requested and what languages, tools, and runtime versions are required.
2. **Install** anything the generic sandbox image does not already provide.
   You have network access to PyPI, npm, Go module proxies, and common apt
   mirrors — use them freely.
3. **Implement** the requested change.
4. **Verify** your work (run the project's existing tests, or write new ones if
   the project has none, or at minimum confirm the code runs without errors).
5. **Commit and push** your changes to a new branch. Use the git credentials
   available in your environment. If the issue text does not specify a branch
   name, create one with the pattern `Jiffy/<short-description-of-change>`.
6. **Open a Pull Request** via the provider's tooling *only if the issue text
   explicitly asks you to open one*.
7. **Code review mention** — *only if the issue text explicitly asks for a code
   review* — include a mention of the configured code-review bot handle in the
   PR description (if you opened a PR) or in your final result summary (if you
   did not).
8. **Callback attempt** — *after* completing the above (whether you succeeded
   or partially succeeded), you MUST attempt to call the callback endpoint to
   report your result. See "Callback Delivery" below for details.

## Asking a Question — Last Resort Only

Asking costs the requester a round trip and stalls the task until a human comes
back, so a question is a failure to be avoided, not a safety net. **Do the work.**
Almost everything that feels like a question is answerable from the repository
in front of you: existing patterns, tests, config, README, git history, and the
conventions the codebase already follows are the answer to "which way should I
do this?". Read before you ask.

Before you even consider asking, you must have done all of these:

1. **Searched the repository** for the answer — an existing implementation of
   something similar is a decision already made for you.
2. **Done every part of the task that does not depend on the answer.** A
   question about one detail is not a reason to stop on everything else.
3. **Looked for a reasonable default.** If one interpretation is clearly the
   most sensible, take it, implement it, and write down the assumption in your
   `summary` and `technical_report`. A delivered change under a stated
   assumption is far more useful than a question. The requester can correct a
   stated assumption in one reply; they cannot use an empty branch.
4. **Checked that the answer would actually change what you build.** If both
   readings lead to the same code, there is nothing to ask.

Only ask when, after all of that, proceeding would be actively harmful or
useless: it would destroy data or break production, it needs a credential or an
external decision you cannot obtain, or the request has two plausible readings
that lead to genuinely different work and nothing in the repo favours either.

Never ask about the task text itself. In particular, never ask the requester to
re-send, repeat, clarify or "complete" the issue text on the grounds that what
you received looks short, cut off, or incomplete. The complete, verbatim text
is always on disk at `{INSTRUCTIONS_PATH}`, and the issue section above is
delimited by explicit `{ISSUE_BEGIN_MARKER}` / `{ISSUE_END_MARKER}` markers. If you
do not see the end marker, your copy of the prompt was truncated in transit —
read `{INSTRUCTIONS_PATH}` in full and work from that. If the text genuinely is
short, that is simply how the requester wrote it: treat it as the whole request
and implement it.

If you do have to ask, ask well: one message covering everything you need, each
question stated with what you already tried and understood, and concrete options
("A or B?") rather than an open-ended prompt. You get exactly one reply, and it
arrives as a brand-new task rather than a continuation of this run, so the
question must stand on its own.

To ask:

- Set `status` to `"question"` in your result file and put the question text in
  the `question` field.
- Deliver it through the callback using the question format below, so it is
  posted as a reply on the issue thread the request came from. It must carry
  the `{QUESTION_TAG}` tag as documented there — that tag is how the thread and
  the next run recognise the comment as a question rather than a result.
- Commit and push whatever partial, coherent work you already have (if any) so
  it is not lost, and report the branch. Do not open a PR for incomplete work
  unless the issue text asked for one.

Anything you ask that is not delivered through the callback is lost — nobody is
watching your terminal and there is no interactive prompt. So a question left in
your logs, or in your summary, is a question nobody will ever answer.

## Callback Delivery

You MUST attempt exactly ONE call to the callback endpoint after finishing
your work. Make exactly ONE attempt — do not retry. If the call fails, report
that in your result's `callback` object.

The report you send must be **human-readable markdown** suitable for posting
as an issue/PR comment. Compose it in the format below, then send it exactly
as described under "Callback endpoint details" — that section says whether it
goes on the wire as raw text or wrapped in a JSON field.

For a successful task:

```
Task #<task_id>: ✅ Jiffy completed this task.

**Summary:** <summary>
**Branch:** <branch_name>
**Pull Request:** <pr_url>

---

### Technical Report
<technical_report content>
```

For a failed task:

```
Task #<task_id>: ❌ Jiffy could not complete this task.

**Reason:** <error_message>
```

For a task that is blocked on a question (see "Asking a Question" above):

```
{QUESTION_TAG} Task #<task_id>: ❓ Jiffy has a question before continuing.

**Question:** <question>

**Branch:** <branch_name>

---

Reply on this issue to answer — your reply starts a new Jiffy task with the
full thread.
```

Rules:
- If `pr_url` is null/empty, omit the **Pull Request:** line entirely.
- If `branch_name` is null/empty, omit the **Branch:** line entirely.
- If `technical_report` is missing or empty, omit the entire `Technical Report` section (including the `---` separator and heading) entirely.
- In the question format, omit the **Branch:** line if you have no branch to report; keep the closing "Reply on this issue" note always.
- The question format's leading `{QUESTION_TAG}` tag is mandatory and must be the first thing on the first line, exactly as written. It is what marks the comment as a question rather than a result; the success and failure formats must never carry it.
- The `technical_report` field, when present, must use the following structure:

  ## What was done
  Detailed account of the actual changes made (files touched, logic changed) — more detail than the short `summary`.

  ## Technology / approach chosen
  Any libraries, patterns, or design decisions selected, and why (e.g. why a particular library over an alternative).

  ## Reasoning
  Rationale behind key implementation choices — tradeoffs considered, edge cases handled.

  ## Setup / installation instructions
  If the change introduces or modifies a module/service that needs setup (new dependency, env var, migration, config file), step-by-step instructions to install/configure/run it. Omit this section entirely if not applicable.

  ## Known limitations / follow-ups
  Anything left incomplete, deferred, or worth reviewing further.

  Omit sections that don't apply rather than filling them with placeholder text.

- Use the task ID from your environment if available, or 0 as a fallback.

Callback endpoint details:
- **URL**: {callback_url}
- **Method**: {spec['method']}
- **Auth header**: `{spec['auth_header']}: {spec['auth_value_prefix']}<callback_secret>`
- **Callback secret**: {callback_secret}
- **Content-Type**: {spec['content_type']}
- **Body**: {body_description}
- **Query**: none.
- **Additional required headers**:
{extra_header_lines}

## Required Final Output

When you are finished — whether you succeeded, failed, or stopped to ask a
question — you MUST produce a JSON file at the following path:

    {AGENT_RESULT_PATH}

The file must contain exactly one JSON object with these fields:

| Field                  | Type     | Required | Description |
|------------------------|----------|----------|-------------|
| `status`               | string   | yes      | `"done"` if the task was completed, `"failed"` if it was not, `"question"` if you stopped to ask the requester something. |
| `branch_name`          | string   | yes      | The branch you created or worked on. |
| `branch_base`          | string   | yes      | The branch you created new branch from. |
| `pr_url`               | string   | no       | URL of the PR/MR you opened, if any. |
| `programming_language` | string   | no       | Best-effort detection of the primary language used (e.g. `"python"`, `"typescript"`). |
| `summary`              | string   | yes      | A brief summary of what you did. |
| `technical_report`     | string   | no       | Detailed technical report in markdown format for developer/technical reviewer. Must use the structure described in the Callback Delivery section above. |
| `error_message`        | string   | no       | Details of what went wrong, if `status` is `"failed"`. |
| `question`             | string   | yes if `status` is `"question"` | The question you need answered before you can continue. Required — a `"question"` status without it is treated as a failure. |
| `callback`             | object   | yes      | Outcome of your callback attempt. See below. |

### The `callback` object

| Field       | Type    | Required | Description |
|-------------|---------|----------|-------------|
| `attempted` | boolean | yes      | Whether you attempted the callback call. |
| `succeeded` | boolean | yes      | Whether the callback was delivered successfully (HTTP < 300). |
| `error`     | string  | no       | Error message if the callback attempt failed. |

Example:
```json
{{"attempted": true, "succeeded": true, "error": null}}
```

If you could not attempt the callback at all (e.g. network unavailable), set
`attempted` to false and explain why in `error`.

Do not skip this step. If you do not produce this file the system will treat
your run as a failure.
"""


def read_agent_result(container: Container) -> AgentResult:
    """Read and parse the structured result the agent was required to produce.

    If the result file is missing or malformed, returns a failed AgentResult
    rather than raising — the caller treats any non-"done" status the same way.
    """
    def _parse_callback(data: dict) -> dict:
        """Extract and validate the callback sub-object from agent result data."""
        raw = data.get("callback")
        if not isinstance(raw, dict):
            return {"attempted": False, "succeeded": False, "error": "Missing or invalid 'callback' object in agent result"}
        return {
            "attempted": bool(raw.get("attempted", False)),
            "succeeded": bool(raw.get("succeeded", False)),
            "error": raw.get("error") if isinstance(raw.get("error"), str) else None,
        }

    def _make_result(
        status: str = "failed",
        branch_name: str | None = None,
        pr_url: str | None = None,
        programming_language: str | None = None,
        summary: str | None = None,
        technical_report: str | None = None,
        error_message: str | None = None,
        question: str | None = None,
        model: str | None = None,
        callback: dict | None = None,
    ) -> AgentResult:
        return AgentResult(
            status=status,
            branch_name=branch_name,
            pr_url=pr_url,
            programming_language=programming_language,
            summary=summary,
            technical_report=technical_report,
            error_message=error_message,
            question=question,
            model=model,
            callback=callback or {"attempted": False, "succeeded": False, "error": "No callback information available"},
        )

    try:
        exit_code, (output, _) = container.exec_run(
            cmd=["cat", AGENT_RESULT_PATH], demux=True
        )
        if exit_code != 0:
            return _make_result(
                status="failed",
                error_message=(
                    f"Agent result file not found at {AGENT_RESULT_PATH} "
                    f"(exit code {exit_code}). The agent did not produce the "
                    "required output contract."
                ),
            )

        result_data = json.loads(output)
        callback = _parse_callback(result_data)
        status = result_data.get("status")
        question = result_data.get("question")
        if not isinstance(question, str) or not question.strip():
            question = None
        # A "question" status means the agent stopped to ask for clarification
        # rather than guessing; without a question to relay it is just a
        # failure, so it is downgraded to one below.
        if status == "question" and question is None:
            status = "failed"
            result_data.setdefault(
                "error_message",
                "Agent reported status 'question' without a 'question' field.",
            )
        if status not in VALID_AGENT_STATUSES:
            return _make_result(
                status="failed",
                branch_name=result_data.get("branch_name"),
                pr_url=result_data.get("pr_url"),
                programming_language=result_data.get("programming_language"),
                summary=result_data.get("summary"),
                technical_report=result_data.get("technical_report"),
                error_message=(
                    result_data.get("error_message")
                    or "Agent result JSON is missing a valid 'status' field "
                    f"(must be one of: {', '.join(sorted(VALID_AGENT_STATUSES))})."
                ),
                question=question,
                model=result_data.get("model"),
                callback=callback,
            )
        return _make_result(
            status=status,
            branch_name=result_data.get("branch_name"),
            pr_url=result_data.get("pr_url"),
            programming_language=result_data.get("programming_language"),
            summary=result_data.get("summary"),
            technical_report=result_data.get("technical_report"),
            error_message=result_data.get("error_message"),
            question=question,
            model=result_data.get("model"),
            callback=callback,
        )
    except json.JSONDecodeError as e:
        return _make_result(
            status="failed",
            error_message=f"Agent result file is not valid JSON: {e}",
        )
    except Exception as e:
        logger.exception(
            "Unexpected error reading agent result from container %s",
            container.short_id,
        )
        return _make_result(
            status="failed",
            error_message=f"Failed to read agent result: {e}",
        )
