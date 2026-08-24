"""Handles agent instructions and result parsing."""
import hashlib
import json
import logging
import re
from typing import Any, Dict, NamedTuple

from docker.models.containers import Container

from jobs.callback_specs import get_callback_spec
from jobs.execution.container import CALLBACK_SCRIPT_PATH, TASK_JSON_PATH, WORKSPACE
from jobs.execution.exceptions import AgentError

logger = logging.getLogger(__name__)

AGENT_RESULT_PATH = "/workspace/.jiffy_result.json"

# Statuses the agent may report in its result file. The prompt only ever asks
# for "done" or "failed" — asking a question is forbidden there, because no
# human is watching a run. "question" is still accepted so that a result which
# arrives that way is relayed to the issue rather than dropped.
VALID_AGENT_STATUSES = ("done", "failed", "question")

# The request is fenced by these markers so the agent can tell a short request
# apart from a truncated one. Seeing the end marker is proof it has the whole
# thing.
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


# How much issue text is reproduced inside the prompt itself. ``opencode run``
# takes its prompt from argv only and the kernel caps a single argv entry at
# 128 KiB (MAX_ARG_STRLEN); the surrounding contract is ~15 KiB, so this leaves
# a wide margin. The request is ALWAYS inlined up to this size — a model handed
# a pointer to a file instead of a task tends to answer "what would you like me
# to work on?" rather than go and read it. Above the budget the middle is
# elided and the head and tail are kept, so the original request and the latest
# comments are both in front of the agent, with the complete copy in the task
# file.
MAX_INLINE_ISSUE_TEXT_BYTES = 80 * 1024

# When eliding, how much of the budget goes to the start of the thread. The
# opening comment is the request itself; the tail is the most recent
# instructions. Both matter more than the middle of a long discussion.
ELISION_HEAD_SHARE = 0.6


def build_task_document(payload: Dict[str, Any], task_id: int = 0) -> Dict[str, Any]:
    """The complete task, as the JSON document staged inside the container.

    This is the single source of truth for what was asked. It carries the whole
    thread however large it is, so nothing about the request depends on what
    fits in the agent's argv. It deliberately carries no credentials: the repo
    token is container env, and the callback secret lives in the callback
    wrapper's own config.
    """
    issue = payload.get("issue", {})
    issue_text = _extract_issue_text(payload)
    encoded = issue_text.encode("utf-8")
    return {
        "task_id": task_id,
        "issue_text": issue_text,
        "issue_text_bytes": len(encoded),
        "issue_text_sha256": hashlib.sha256(encoded).hexdigest(),
        "issue_text_elided_in_prompt": len(encoded) > MAX_INLINE_ISSUE_TEXT_BYTES,
        "external_issue_id": issue.get("external_issue_id", ""),
        "turns": issue.get("turns") or [],
        "repo_url": payload.get("repo", {}).get("url", ""),
        "workspace": WORKSPACE,
        "result_path": AGENT_RESULT_PATH,
    }


def _elide_middle(issue_text: str, max_bytes: int) -> str:
    """Keep the head and tail of *issue_text*, marking what was left out."""
    encoded = issue_text.encode("utf-8")
    marker_template = (
        "\n\n[... {omitted} bytes of the middle of this thread are omitted here "
        f"to fit. The complete text is in `{TASK_JSON_PATH}` under `issue_text` "
        "if you need the part that is missing. Everything above and below this "
        "line is verbatim. ...]\n\n"
    )
    budget = max_bytes - len(marker_template.format(omitted=len(encoded)).encode("utf-8"))
    head_bytes = int(budget * ELISION_HEAD_SHARE)
    tail_bytes = budget - head_bytes
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore")
    omitted = len(encoded) - head_bytes - tail_bytes
    return head + marker_template.format(omitted=omitted) + tail


def _issue_section(issue_text: str, suggested_areas: str | None) -> str:
    """The part of the prompt that carries the request.

    The request is always here, between the markers — never replaced by a
    pointer to a file. Handing a model a path instead of a task is what
    produced "What would you like me to work on?" and "the message appears to
    be cut off"; a model cannot skip reading what is already in front of it.
    """
    encoded = issue_text.encode("utf-8")
    if len(encoded) <= MAX_INLINE_ISSUE_TEXT_BYTES:
        completeness = (
            f"This is the entire request. If you can see {ISSUE_END_MARKER}, "
            "nothing is missing — however short it looks."
        )
        body = issue_text
    else:
        completeness = (
            f"This thread is {len(encoded)} bytes, so the middle of it is "
            "omitted below and the start and end are shown in full. The "
            f"complete text is in `{TASK_JSON_PATH}` under `issue_text`; read "
            "it only if you need the omitted middle. What is shown here is "
            "already enough to begin."
        )
        body = _elide_middle(issue_text, MAX_INLINE_ISSUE_TEXT_BYTES)

    return f"""\
{completeness} Nothing here needs to be re-sent and there is no one to ask:
work from what is below.

{ISSUE_BEGIN_MARKER}
{body}
{ISSUE_END_MARKER}
"""


def build_agent_instructions(payload: Dict[str, Any]) -> str:
    """Build the instructions text handed to the coding agent.

    The instructions are agent-agnostic — they describe the contract without
    assuming any particular CLI conventions.

    The contract is always complete. The request text is reproduced inline when
    it fits in the agent's argv, and otherwise pointed at by path — the full
    text always reaches the container as ``build_task_document``.
    """
    issue_text = _extract_issue_text(payload)
    if not issue_text.strip():
        # An empty request is a Gateway or edge bug, and handing it to the
        # agent only produces "what would you like me to do?" hours later.
        # Fail here, where the reason can still be reported accurately.
        raise AgentError(
            "The issue text arrived empty — there is nothing for the agent to "
            "work on. This is a Gateway/edge problem, not a problem with the "
            "request: check that the ingestion payload carried a non-empty "
            "'issue.turns[].body' or 'issue.text'."
        )
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

    issue_section = _issue_section(issue_text, suggested_areas)

    return f"""\
# YOUR TASK — START WORKING ON THIS NOW

You are a coding agent. The repository is already cloned at `{WORKSPACE}` and
this is the request you must implement. Do not reply asking what to work on:
the request is right here, and there is no one to answer you. Read it, then
start.

{issue_section}
# HOW TO CARRY IT OUT

Everything below is the standing procedure for the request above. Implement
exactly what the request asks for — do not summarize or pre-parse it, and do
not stop to check anything with anyone.

Nobody is reading your chat output. Nothing you say in a reply reaches the
person who asked: the only two things that leave this container are the file
you write at `{AGENT_RESULT_PATH}` and the comment the callback wrapper posts.
Answering in chat instead of doing those is the same as doing nothing.

## Working Directory

The repository is cloned at `/workspace` and is already checked out on the
`develop` branch. This is done for you —
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
8. **Report back** — *after* completing the above (whether you succeeded or
   partially succeeded), you MUST run the staged callback wrapper to post your
   result to the issue thread. See "Callback Delivery" below for details.
9. **Write the result file** at `{AGENT_RESULT_PATH}`. This is not optional and
   it is not the same thing as answering in chat: a run that ends without this
   file on disk is recorded as a failure no matter how much work you did. Write
   it last, and write it even if something went wrong — see "Required Final
   Output" below for the exact fields.

## When Something Is Unclear

**Never ask a question. There is nobody to answer it.** This run is not a
conversation: no human is reading your output, and a reply that asks for
clarification is simply a run that did nothing. Whatever you understood of the
request, implement it — completely.

When something is ambiguous, resolve it yourself:

- **Look in the repository.** Existing patterns, tests, config, the README and
  git history are the answer to almost every "which way should I do this?".
  The codebase has already made most of these decisions.
- **Take the most reasonable reading** and build it. If two readings are
  plausible, pick the one the codebase supports, or the smaller and more
  easily reversed one.
- **Write the assumption down** in your `summary` and in the `Reasoning`
  section of your `technical_report`. A delivered change with a stated
  assumption is useful and can be corrected in one reply; a question delivers
  nothing.
- **Do every part you do understand.** One unclear detail is never a reason to
  stop on the rest. Implement everything else in full, and note the part you
  had to interpret.

Never ask for the request to be re-sent, repeated, or completed, and never
answer with "what would you like me to work on?" — the request is in this
message, between the markers above. If you believe something is missing from
it, you are wrong: proceed with what is there.

The only acceptable outcome that is not finished work is an honest `"failed"`
result explaining what blocked you — and that is for things you genuinely
cannot do (a credential you do not have, an operation that would destroy
data), never for something you merely find unclear.

## Callback Delivery

After finishing your work you MUST report it back to the issue thread. You do
**not** write the HTTP call yourself: a wrapper is already staged in this
container that owns delivery, including retries. Your job is to compose the
comment body and run the wrapper once.

    1. Write your comment body (the markdown described below) to a file, e.g.
       /tmp/jiffy_callback_body.md
    2. Run:  python3 {CALLBACK_SCRIPT_PATH} --body-file /tmp/jiffy_callback_body.md
    3. Copy the single JSON object it prints on stdout verbatim into the
       `callback` field of your result file.

Rules for this step, all of them absolute:

- Run the wrapper **exactly once**. It already retries transient failures on
  its own schedule and gives up on permanent ones; running it again would post
  a duplicate comment.
- Do **not** write your own HTTP request, curl command, or retry loop for the
  callback, and do not change the endpoint, headers or secret. Delivery policy
  is not yours to decide — the wrapper is the only correct way to report.
- Do **not** invent the `callback` object. Use exactly what the wrapper
  printed. If the wrapper could not run at all (exit code 2), report
  `{{"attempted": false, "succeeded": false, "error": "<what went wrong>"}}`.
- A failed callback does **not** change your `status`. If you completed the
  work, `status` stays `"done"` even when the comment could not be posted; the
  Gateway notices the undelivered callback and reports it for you.
- Still write your result file either way. It is read from the container's
  filesystem, not over the network, so it survives any delivery failure.

The wrapper prints each attempt to stderr, which is already redirected to the
container's log, so you do not need to add logging of your own.

The report you compose must be **human-readable markdown** suitable for posting
as an issue/PR comment. Use the format below. The endpoint details at the end
of this section are documentation of what the wrapper does — you do not need to
apply them yourself.

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

Rules:
- If `pr_url` is null/empty, omit the **Pull Request:** line entirely.
- If `branch_name` is null/empty, omit the **Branch:** line entirely.
- If `technical_report` is missing or empty, omit the entire `Technical Report` section (including the `---` separator and heading) entirely.
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

Callback endpoint details (what the wrapper sends — for reference only):
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

When you are finished — whether you succeeded or failed — you MUST produce a
JSON file at the following path:

    {AGENT_RESULT_PATH}

The file must contain exactly one JSON object with these fields:

| Field                  | Type     | Required | Description |
|------------------------|----------|----------|-------------|
| `status`               | string   | yes      | `"done"` if the task was completed, `"failed"` if it was not. |
| `branch_name`          | string   | yes      | The branch you created or worked on. |
| `branch_base`          | string   | yes      | The branch you created new branch from. |
| `pr_url`               | string   | no       | URL of the PR/MR you opened, if any. |
| `programming_language` | string   | no       | Best-effort detection of the primary language used (e.g. `"python"`, `"typescript"`). |
| `summary`              | string   | yes      | A brief summary of what you did. |
| `technical_report`     | string   | no       | Detailed technical report in markdown format for developer/technical reviewer. Must use the structure described in the Callback Delivery section above. |
| `error_message`        | string   | no       | Details of what went wrong, if `status` is `"failed"`. |
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


# How much of the agent's own output to keep when it ends without a result
# file. Enough to see what it was doing, bounded so a chatty run cannot flood
# the worker log.
AGENT_OUTPUT_TAIL_BYTES = 4000


def _log_agent_output_tail(container: Container) -> None:
    """Log the tail of the agent's output. Never raises — this is diagnostics.

    Deliberately worker-log only: the transcript is unfiltered agent output and
    has no business being posted to a public issue thread.
    """
    try:
        raw = container.logs(tail=60)
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    except Exception as exc:  # pragma: no cover - diagnostics only
        logger.warning("Could not read agent output from container: %s", exc)
        return

    tail = text[-AGENT_OUTPUT_TAIL_BYTES:].strip()
    if not tail:
        logger.warning(
            "Agent produced no result file and no output at all — it may have "
            "exited before starting"
        )
        return
    logger.warning(
        "Agent ended without a result file. Last %d chars of its output:\n%s",
        len(tail),
        tail,
    )


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
            # The agent ended without writing its contract. Whatever it did say
            # went to the container log and is about to be thrown away with the
            # container, so keep a bounded tail of it in the worker log — that
            # is the only record of why the run went nowhere.
            _log_agent_output_tail(container)
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
