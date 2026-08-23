"""Tests for the execution pipeline: agent instructions, result parsing, task orchestration,
sandbox image management, and logging."""

import json
import logging
import os
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.ingestion.callback import QUESTION_TAG, format_callback_body
from jobs.execution.agent import (
    ISSUE_BEGIN_MARKER,
    ISSUE_END_MARKER,
    AgentResult,
    build_agent_instructions,
    read_agent_result,
    _extract_issue_text,
    _format_turns,
)
from jobs.execution.container import (
    INSTRUCTIONS_PATH,
    _apply_network_restriction,
    _apply_pnpm_limits,
    _build_network_restriction_script,
    _effective_network_allowlist,
    _extract_git_host,
    _inject_token_into_url,
    _redact_url,
    build_agent_provider_env,
    build_package_manager_env,
    ensure_sandbox_image,
    get_docker_client,
    parse_memory_bytes,
    resolve_cpu_nano_cpus,
    resolve_memory_limits,
    resolve_node_heap_mb,
    start_generic_sandbox_container,
)
from jobs.execution.exceptions import ContainerError
from jobs.models import Task


# ---------------------------------------------------------------------------
# build_agent_instructions
# ---------------------------------------------------------------------------


class FormatTurnsTest(TestCase):
    """Unit tests for _format_turns and _extract_issue_text."""

    def test_format_turns_basic(self):
        turns = [
            {"role": "user", "author": "alice", "body": "Hello", "created_at": "2025-01-01T00:00:00Z"},
            {"role": "agent", "author": "jiffy-bot", "body": "Hi there!", "created_at": "2025-01-01T01:00:00Z"},
        ]
        result = _format_turns(turns)
        self.assertIn("--- Turn: User (alice) ---", result)
        self.assertIn("Hello", result)
        self.assertIn("--- Turn: Agent (Jiffy) (jiffy-bot) ---", result)
        self.assertIn("Hi there!", result)

    def test_format_turns_empty_body(self):
        turns = [
            {"role": "user", "author": "bob", "body": "", "created_at": "2025-01-01T00:00:00Z"},
        ]
        result = _format_turns(turns)
        self.assertIn("bob", result)

    def test_extract_issue_text_prefers_turns(self):
        payload = {
            "issue": {
                "turns": [
                    {"role": "user", "author": "alice", "body": "From turns", "created_at": "2025-01-01T00:00:00Z"},
                ],
                "text": "From legacy text",
            }
        }
        result = _extract_issue_text(payload)
        self.assertIn("From turns", result)
        self.assertNotIn("From legacy text", result)

    def test_extract_issue_text_falls_back_to_text(self):
        payload = {
            "issue": {
                "text": "Legacy text fallback",
            }
        }
        result = _extract_issue_text(payload)
        self.assertEqual(result, "Legacy text fallback")

    def test_extract_issue_text_empty_turns_list(self):
        payload = {
            "issue": {
                "turns": [],
                "text": "Fallback when turns empty",
            }
        }
        result = _extract_issue_text(payload)
        self.assertEqual(result, "Fallback when turns empty")

    def test_extract_issue_text_no_issue_at_all(self):
        result = _extract_issue_text({})
        self.assertEqual(result, "")

    def test_extract_issue_text_turns_not_a_list(self):
        payload = {"issue": {"turns": "not_a_list"}}
        result = _extract_issue_text(payload)
        self.assertEqual(result, "")


class BuildAgentInstructionsTest(TestCase):
    """Unit tests for build_agent_instructions."""

    def _make_payload(self, issue_text="Fix the bug", extra=None):
        payload = {
            "issue": {"text": issue_text, "external_issue_id": "123"},
            "repo": {"url": "https://github.com/user/repo", "token": "tok"},
            "callback": {"url": "https://example.com/cb", "secret": "s"},
        }
        if extra:
            payload.update(extra)
        return payload

    def test_includes_raw_issue_text(self):
        text = "This is the exact issue text — no summarization."
        instructions = build_agent_instructions(self._make_payload(issue_text=text))
        self.assertIn(text, instructions)

    def test_issue_text_not_modified(self):
        """Whitespace, special characters, and casing must pass through unchanged."""
        text = "  Line one\nLine TWO\n\ttabbed  "
        instructions = build_agent_instructions(self._make_payload(issue_text=text))
        self.assertIn(text, instructions)

    def test_includes_source_path(self):
        instructions = build_agent_instructions(self._make_payload())
        self.assertIn("/workspace", instructions)

    def test_includes_output_contract(self):
        instructions = build_agent_instructions(self._make_payload())
        self.assertIn(".jiffy_result.json", instructions)
        self.assertIn("status", instructions)
        self.assertIn("branch_name", instructions)
        self.assertIn("summary", instructions)
        self.assertIn("technical_report", instructions)

    def test_mentions_branch_fallback(self):
        instructions = build_agent_instructions(self._make_payload())
        self.assertIn("Jiffy/", instructions)

    def test_mentions_pr_only_if_asked(self):
        instructions = build_agent_instructions(self._make_payload())
        self.assertIn("only if the issue text", instructions.lower())

    def test_mentions_code_review_only_if_asked(self):
        instructions = build_agent_instructions(self._make_payload())
        self.assertIn("code review", instructions.lower())

    def test_empty_issue_text(self):
        instructions = build_agent_instructions(self._make_payload(issue_text=""))
        self.assertIn("/workspace", instructions)
        self.assertIn(".jiffy_result.json", instructions)

    def test_includes_callback_spec(self):
        instructions = build_agent_instructions(self._make_payload())
        self.assertIn("Callback Delivery", instructions)
        self.assertIn("- **URL**:", instructions)
        self.assertIn("attempted", instructions)
        self.assertIn("succeeded", instructions)

    def test_includes_callback_url_and_secret(self):
        instructions = build_agent_instructions(self._make_payload())
        self.assertIn("https://example.com/cb", instructions)

    def test_callback_field_in_output_contract(self):
        instructions = build_agent_instructions(self._make_payload())
        self.assertIn("callback", instructions)
        self.assertIn("attempted", instructions)
        self.assertIn("succeeded", instructions)

    def test_includes_technical_report_in_output_contract(self):
        instructions = build_agent_instructions(self._make_payload())
        self.assertIn("technical_report", instructions)

    def test_mentions_technical_report_structure(self):
        instructions = build_agent_instructions(self._make_payload())
        self.assertIn("Technical Report", instructions)
        self.assertIn("What was done", instructions)
        self.assertIn("Technology / approach chosen", instructions)
        self.assertIn("Reasoning", instructions)
        self.assertIn("Known limitations / follow-ups", instructions)

    def test_turns_preferred_over_text(self):
        payload = {
            "issue": {
                "turns": [
                    {"role": "user", "author": "alice", "body": "Fix the bug", "created_at": "2025-01-01T00:00:00Z"},
                    {"role": "agent", "author": "jiffy-bot", "body": "On it!", "created_at": "2025-01-01T01:00:00Z"},
                ],
            },
            "repo": {"url": "https://github.com/user/repo", "token": "tok"},
            "callback": {"url": "https://example.com/cb", "secret": "s"},
        }
        instructions = build_agent_instructions(payload)
        self.assertIn("Fix the bug", instructions)
        self.assertIn("On it!", instructions)
        self.assertIn("User (alice)", instructions)
        self.assertIn("Agent (Jiffy) (jiffy-bot)", instructions)

    def test_turns_missing_falls_back_to_text(self):
        payload = {
            "issue": {"text": "Legacy fallback text"},
            "repo": {"url": "https://github.com/user/repo", "token": "tok"},
            "callback": {"url": "https://example.com/cb", "secret": "s"},
        }
        instructions = build_agent_instructions(payload)
        self.assertIn("Legacy fallback text", instructions)


# ---------------------------------------------------------------------------
# read_agent_result
# ---------------------------------------------------------------------------


class ReadAgentResultTest(TestCase):
    """Unit tests for read_agent_result."""

    def _make_container(self, exit_code=0, output=None):
        container = MagicMock()
        container.short_id = "abc123"
        container.exec_run.return_value = (exit_code, (output, b""))
        return container

    def test_valid_done_result(self):
        result_data = {
            "status": "done",
            "branch_name": "Jiffy/fix-bug",
            "pr_url": "https://github.com/user/repo/pull/42",
            "programming_language": "python",
            "summary": "Fixed the bug.",
            "technical_report": "## What was done\nFixed the bug.",
            "error_message": None,
            "callback": {"attempted": True, "succeeded": True, "error": None},
        }
        container = self._make_container(output=json.dumps(result_data).encode())
        result = read_agent_result(container)

        self.assertEqual(result.status, "done")
        self.assertEqual(result.branch_name, "Jiffy/fix-bug")
        self.assertEqual(result.pr_url, "https://github.com/user/repo/pull/42")
        self.assertEqual(result.programming_language, "python")
        self.assertEqual(result.summary, "Fixed the bug.")
        self.assertEqual(result.technical_report, "## What was done\nFixed the bug.")
        self.assertIsNone(result.error_message)
        self.assertEqual(result.callback, {"attempted": True, "succeeded": True, "error": None})

    def test_valid_failed_result(self):
        result_data = {
            "status": "failed",
            "branch_name": "Jiffy/attempt",
            "pr_url": None,
            "programming_language": None,
            "summary": None,
            "technical_report": None,
            "error_message": "Could not install dependency X.",
            "callback": {"attempted": True, "succeeded": False, "error": "HTTP 500"},
        }
        container = self._make_container(output=json.dumps(result_data).encode())
        result = read_agent_result(container)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_message, "Could not install dependency X.")
        self.assertEqual(result.callback, {"attempted": True, "succeeded": False, "error": "HTTP 500"})
        self.assertIsNone(result.technical_report)

    def test_missing_result_file(self):
        container = self._make_container(exit_code=1, output=None)
        result = read_agent_result(container)

        self.assertEqual(result.status, "failed")
        self.assertIn("not found", result.error_message.lower())

    def test_malformed_json(self):
        container = self._make_container(output=b"not json at all")
        result = read_agent_result(container)

        self.assertEqual(result.status, "failed")
        self.assertIn("not valid JSON", result.error_message)

    def test_missing_status_field(self):
        container = self._make_container(output=b'{"branch_name": "x"}')
        result = read_agent_result(container)

        self.assertEqual(result.status, "failed")
        self.assertIn("missing a valid 'status' field", result.error_message)

    def test_empty_json_object(self):
        container = self._make_container(output=b"{}")
        result = read_agent_result(container)

        self.assertEqual(result.status, "failed")
        self.assertIn("missing a valid 'status' field", result.error_message)

    def test_container_exec_run_exception(self):
        container = MagicMock()
        container.short_id = "err1"
        container.exec_run.side_effect = RuntimeError("connection lost")
        result = read_agent_result(container)

        self.assertEqual(result.status, "failed")
        self.assertIn("connection lost", result.error_message)

    def test_default_callback_when_missing(self):
        """When the agent result has no callback field, the default is not-attempted."""
        result_data = {
            "status": "done",
            "branch_name": "Jiffy/fix",
            "pr_url": None,
            "programming_language": None,
            "summary": "Worked.",
            "error_message": None,
        }
        container = self._make_container(output=json.dumps(result_data).encode())
        result = read_agent_result(container)

        self.assertEqual(result.status, "done")
        self.assertIsInstance(result.callback, dict)
        self.assertFalse(result.callback["attempted"])
        self.assertFalse(result.callback["succeeded"])
        self.assertIn("Missing or invalid", result.callback["error"])

    def test_callback_invalid_type(self):
        """A non-dict callback field should produce a default not-attempted."""
        result_data = {
            "status": "done",
            "branch_name": "Jiffy/fix",
            "pr_url": None,
            "programming_language": None,
            "summary": "Worked.",
            "error_message": None,
            "callback": "not_a_dict",
        }
        container = self._make_container(output=json.dumps(result_data).encode())
        result = read_agent_result(container)

        self.assertEqual(result.status, "done")
        self.assertFalse(result.callback["attempted"])
        self.assertFalse(result.callback["succeeded"])

    def test_callback_attempted_but_failed(self):
        result_data = {
            "status": "done",
            "branch_name": "Jiffy/fix",
            "pr_url": None,
            "programming_language": None,
            "summary": "Worked.",
            "error_message": None,
            "callback": {"attempted": True, "succeeded": False, "error": "Connection refused"},
        }
        container = self._make_container(output=json.dumps(result_data).encode())
        result = read_agent_result(container)

        self.assertEqual(result.status, "done")
        self.assertTrue(result.callback["attempted"])
        self.assertFalse(result.callback["succeeded"])
        self.assertEqual(result.callback["error"], "Connection refused")

    def test_technical_report_parsed_when_present(self):
        """technical_report is correctly parsed from agent JSON."""
        result_data = {
            "status": "done",
            "branch_name": "Jiffy/fix",
            "pr_url": None,
            "programming_language": "python",
            "summary": "Fixed the bug.",
            "technical_report": "## What was done\nChanged X.\n\n## Reasoning\nBecause Y.",
            "error_message": None,
        }
        container = self._make_container(output=json.dumps(result_data).encode())
        result = read_agent_result(container)

        self.assertEqual(result.status, "done")
        self.assertEqual(result.technical_report, "## What was done\nChanged X.\n\n## Reasoning\nBecause Y.")

    def test_technical_report_missing_returns_none(self):
        """If technical_report is missing from JSON, it should be None."""
        result_data = {
            "status": "done",
            "branch_name": "Jiffy/fix",
            "pr_url": None,
            "programming_language": "python",
            "summary": "Fixed the bug.",
        }
        container = self._make_container(output=json.dumps(result_data).encode())
        result = read_agent_result(container)

        self.assertEqual(result.status, "done")
        self.assertIsNone(result.technical_report)


# ---------------------------------------------------------------------------
# execute_task orchestration (all Docker/agent calls mocked)
# ---------------------------------------------------------------------------


class ExecuteTaskTest(TestCase):
    """Integration-style tests for execute_task with mocked Docker calls."""

    def _create_task(self, **kwargs):
        defaults = {
            "provider": "github",
            "repo_url": "https://github.com/user/repo",
            "issue_external_id": "100",
            "callback_url": "https://example.com/cb",
            "callback_secret": "sec",
            "status": "queued",
        }
        defaults.update(kwargs)
        return Task.objects.create(**defaults)

    def _make_payload(self, **overrides):
        payload = {
            "repo": {"url": "https://github.com/user/repo", "token": "ghp_test"},
            "issue": {"text": "Fix the thing", "external_issue_id": "100"},
            "callback": {"url": "https://example.com/cb", "secret": "sec"},
        }
        payload.update(overrides)
        return payload

    def _make_agent_result(self, **overrides):
        result = {
            "status": "done",
            "branch_name": "Jiffy/fix-thing",
            "pr_url": "https://github.com/user/repo/pull/1",
            "programming_language": "python",
            "summary": "Fixed the thing.",
            "technical_report": "## What was done\nFixed the thing.",
            "error_message": None,
            "model": None,
            "callback": {"attempted": True, "succeeded": True, "error": None},
        }
        result.update(overrides)
        return AgentResult(**{k: v for k, v in result.items() if k in AgentResult._fields})

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.ensure_sandbox_image")
    @patch("jobs.tasks.read_agent_result")
    @patch("jobs.tasks.run_agent_in_container")
    @patch("jobs.tasks.clone_repo_in_container")
    @patch("jobs.tasks.start_generic_sandbox_container")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_happy_path_agent_callback_success(
        self, mock_load, mock_container, mock_clone, mock_run, mock_result, mock_ensure, mock_cb
    ):
        """Agent succeeds and delivers callback — Gateway skips own callback."""
        task = self._create_task()
        mock_load.return_value = self._make_payload()
        mock_container.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_container.return_value.__exit__ = MagicMock(return_value=False)
        mock_result.return_value = self._make_agent_result()

        from jobs.tasks import execute_task

        execute_task(task.id)

        task.refresh_from_db()
        self.assertEqual(task.status, "done")
        self.assertEqual(task.branch_name, "Jiffy/fix-thing")
        self.assertEqual(task.pr_url, "https://github.com/user/repo/pull/1")
        self.assertEqual(task.programming_language, "python")
        mock_cb.assert_not_called()

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.ensure_sandbox_image")
    @patch("jobs.tasks.read_agent_result")
    @patch("jobs.tasks.run_agent_in_container")
    @patch("jobs.tasks.clone_repo_in_container")
    @patch("jobs.tasks.start_generic_sandbox_container")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_agent_callback_failed_gateway_falls_back(
        self, mock_load, mock_container, mock_clone, mock_run, mock_result, mock_ensure, mock_cb
    ):
        """Agent attempts callback but fails — Gateway falls back via spec."""
        task = self._create_task()
        mock_load.return_value = self._make_payload()
        mock_container.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_container.return_value.__exit__ = MagicMock(return_value=False)
        mock_result.return_value = self._make_agent_result(
            callback={"attempted": True, "succeeded": False, "error": "HTTP 500"}
        )

        from jobs.tasks import execute_task

        execute_task(task.id)

        task.refresh_from_db()
        self.assertEqual(task.status, "done")
        mock_cb.assert_called_once()

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.ensure_sandbox_image")
    @patch("jobs.tasks.read_agent_result")
    @patch("jobs.tasks.run_agent_in_container")
    @patch("jobs.tasks.clone_repo_in_container")
    @patch("jobs.tasks.start_generic_sandbox_container")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_agent_no_callback_attempt_gateway_falls_back(
        self, mock_load, mock_container, mock_clone, mock_run, mock_result, mock_ensure, mock_cb
    ):
        """Agent never attempts callback — Gateway falls back via spec."""
        task = self._create_task()
        mock_load.return_value = self._make_payload()
        mock_container.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_container.return_value.__exit__ = MagicMock(return_value=False)
        mock_result.return_value = self._make_agent_result(
            callback={"attempted": False, "succeeded": False, "error": "Network unavailable"}
        )

        from jobs.tasks import execute_task

        execute_task(task.id)

        task.refresh_from_db()
        self.assertEqual(task.status, "done")
        mock_cb.assert_called_once()

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.ensure_sandbox_image")
    @patch("jobs.tasks.read_agent_result")
    @patch("jobs.tasks.run_agent_in_container")
    @patch("jobs.tasks.clone_repo_in_container")
    @patch("jobs.tasks.start_generic_sandbox_container")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_agent_crashed_no_result_gateway_falls_back(
        self, mock_load, mock_container, mock_clone, mock_run, mock_result, mock_ensure, mock_cb
    ):
        """Agent crashes with no result — Gateway sends fallback with generic failure."""
        task = self._create_task()
        mock_load.return_value = self._make_payload()
        mock_container.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_container.return_value.__exit__ = MagicMock(return_value=False)
        mock_result.return_value = AgentResult(
            status="failed",
            branch_name=None,
            pr_url=None,
            programming_language=None,
            summary=None,
            technical_report=None,
            error_message="Agent result file not found. The agent did not produce the required output contract.",
            model=None,
            callback={"attempted": False, "succeeded": False, "error": "No callback information available"},
        )

        from jobs.tasks import execute_task

        execute_task(task.id)

        task.refresh_from_db()
        self.assertEqual(task.status, "failed")
        mock_cb.assert_called_once()

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.ensure_sandbox_image")
    @patch("jobs.tasks.read_agent_result")
    @patch("jobs.tasks.run_agent_in_container")
    @patch("jobs.tasks.clone_repo_in_container")
    @patch("jobs.tasks.start_generic_sandbox_container")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_failed_agent_result_short_circuits(
        self, mock_load, mock_container, mock_clone, mock_run, mock_result, mock_ensure, mock_cb
    ):
        """A failed agent result must NOT update branch_name, pr_url, or programming_language."""
        task = self._create_task()
        mock_load.return_value = self._make_payload()
        mock_container.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_container.return_value.__exit__ = MagicMock(return_value=False)
        mock_result.return_value = AgentResult(
            status="failed",
            branch_name="Jiffy/attempt",
            pr_url="https://example.com/pr",
            programming_language="python",
            summary=None,
            technical_report=None,
            error_message="Agent failed.",
            model=None,
            callback={"attempted": True, "succeeded": False, "error": "HTTP 500"},
        )

        from jobs.tasks import execute_task

        execute_task(task.id)

        task.refresh_from_db()
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.error_message, "Agent failed.")
        self.assertIsNone(task.branch_name)
        self.assertIsNone(task.pr_url)
        self.assertIsNone(task.programming_language)
        mock_cb.assert_called_once()

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_missing_payload_fails_task(self, mock_load, mock_cb):
        task = self._create_task()
        mock_load.side_effect = ValueError("Payload expired")

        from jobs.tasks import execute_task

        execute_task(task.id)

        task.refresh_from_db()
        self.assertEqual(task.status, "failed")
        self.assertIn("Payload expired", task.error_message)

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.ensure_sandbox_image")
    @patch("jobs.tasks.clone_repo_in_container")
    @patch("jobs.tasks.start_generic_sandbox_container")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_clone_failure_fails_task(
        self, mock_load, mock_container, mock_clone, mock_ensure, mock_cb
    ):
        task = self._create_task()
        mock_load.return_value = self._make_payload()
        mock_container.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_container.return_value.__exit__ = MagicMock(return_value=False)
        mock_clone.side_effect = ContainerError("git clone failed")

        from jobs.tasks import execute_task

        execute_task(task.id)

        task.refresh_from_db()
        self.assertEqual(task.status, "failed")
        self.assertIn("git clone failed", task.error_message)

    @patch("jobs.tasks.TASK_LOOKUP_DELAY_SECONDS", 0)
    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_nonexistent_task_does_not_crash(self, mock_load, mock_cb):
        mock_load.return_value = self._make_payload()

        from jobs.tasks import execute_task

        execute_task(99999)

    @patch("jobs.tasks.TASK_LOOKUP_DELAY_SECONDS", 0)
    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_missing_task_row_reports_via_redis_payload(self, mock_load, mock_cb):
        """A task whose row cannot be read still reports back to the issue."""
        mock_load.return_value = self._make_payload(provider="github")

        from jobs.tasks import execute_task

        execute_task(99999)

        mock_cb.assert_called_once()
        reported_task = mock_cb.call_args.args[0]
        self.assertEqual(reported_task.id, 99999)
        self.assertEqual(reported_task.provider, "github")
        self.assertEqual(reported_task.callback_url, "https://example.com/cb")
        self.assertEqual(reported_task.callback_secret, "sec")
        self.assertEqual(mock_cb.call_args.kwargs["status"], "failed")
        # The transient instance must never be persisted.
        self.assertFalse(Task.objects.filter(id=99999).exists())

    @patch("jobs.tasks.TASK_LOOKUP_DELAY_SECONDS", 0)
    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_missing_task_row_without_provider_is_not_reported(self, mock_load, mock_cb):
        """No provider in the payload — log and give up rather than guess."""
        mock_load.return_value = self._make_payload()

        from jobs.tasks import execute_task

        execute_task(99999)

        mock_cb.assert_not_called()

    @patch("jobs.tasks.TASK_LOOKUP_DELAY_SECONDS", 0)
    def test_fetch_task_retries_until_the_row_is_visible(self):
        """A row committed by another process just after the job was published."""
        from jobs.tasks import TASK_LOOKUP_ATTEMPTS, _fetch_task

        task = self._create_task()
        real_get = Task.objects.get
        calls = {"n": 0}

        def flaky_get(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise Task.DoesNotExist()
            return real_get(*args, **kwargs)

        with patch.object(Task.objects, "get", side_effect=flaky_get):
            found = _fetch_task(task.id)

        self.assertIsNotNone(found)
        self.assertEqual(found.id, task.id)
        self.assertEqual(calls["n"], 3)
        self.assertLessEqual(calls["n"], TASK_LOOKUP_ATTEMPTS)

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.ensure_sandbox_image")
    @patch("jobs.tasks.read_agent_result")
    @patch("jobs.tasks.run_agent_in_container")
    @patch("jobs.tasks.clone_repo_in_container")
    @patch("jobs.tasks.start_generic_sandbox_container")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_status_transitions(
        self, mock_load, mock_container, mock_clone, mock_run, mock_result, mock_ensure, mock_cb
    ):
        """Verify the full status transition sequence on success."""
        task = self._create_task()
        mock_load.return_value = self._make_payload()

        status_log = []

        original_save = Task.save

        def tracking_save(self_task, *args, **kwargs):
            status_log.append(self_task.status)
            original_save(self_task, *args, **kwargs)

        mock_container.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_container.return_value.__exit__ = MagicMock(return_value=False)
        mock_result.return_value = self._make_agent_result()

        from jobs.tasks import execute_task

        with patch.object(Task, "save", tracking_save):
            execute_task(task.id)

        self.assertIn("provisioning", status_log)
        self.assertIn("cloning", status_log)
        self.assertIn("running", status_log)
        self.assertIn("done", status_log)

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.ensure_sandbox_image")
    @patch("jobs.tasks.read_agent_result")
    @patch("jobs.tasks.run_agent_in_container")
    @patch("jobs.tasks.clone_repo_in_container")
    @patch("jobs.tasks.start_generic_sandbox_container")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_agent_failure_with_callback_attempt(
        self, mock_load, mock_container, mock_clone, mock_run, mock_result, mock_ensure, mock_cb
    ):
        """Agent fails but attempted callback — Gateway still falls back."""
        task = self._create_task()
        mock_load.return_value = self._make_payload()
        mock_container.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_container.return_value.__exit__ = MagicMock(return_value=False)
        mock_result.return_value = AgentResult(
            status="failed",
            branch_name="Jiffy/attempt",
            pr_url=None,
            programming_language=None,
            summary=None,
            technical_report=None,
            error_message="Could not install dependency.",
            model=None,
            callback={"attempted": True, "succeeded": False, "error": "HTTP 500"},
        )

        from jobs.tasks import execute_task

        execute_task(task.id)

        task.refresh_from_db()
        self.assertEqual(task.status, "failed")
        self.assertIn("Could not install dependency.", task.error_message)
        mock_cb.assert_called_once()


# ---------------------------------------------------------------------------
# execute_task logging
# ---------------------------------------------------------------------------


class ExecuteTaskLoggingTest(TestCase):
    """Verify that execute_task emits structured, ordered log lines."""

    def _create_task(self, **kwargs):
        defaults = {
            "provider": "github",
            "repo_url": "https://github.com/user/repo",
            "issue_external_id": "100",
            "callback_url": "https://example.com/cb",
            "callback_secret": "sec",
            "status": "queued",
        }
        defaults.update(kwargs)
        return Task.objects.create(**defaults)

    def _make_payload(self, token="ghp_SECRET123"):
        return {
            "repo": {"url": "https://github.com/user/repo", "token": token},
            "issue": {"text": "Fix the thing", "external_issue_id": "100"},
            "callback": {"url": "https://example.com/cb", "secret": "sec"},
        }

    def _make_agent_result(self, **overrides):
        result = {
            "status": "done",
            "branch_name": "Jiffy/fix-thing",
            "pr_url": "https://github.com/user/repo/pull/1",
            "programming_language": "python",
            "summary": "Fixed.",
            "technical_report": None,
            "error_message": None,
            "model": None,
            "callback": {"attempted": True, "succeeded": True, "error": None},
        }
        result.update(overrides)
        return AgentResult(**{k: v for k, v in result.items() if k in AgentResult._fields})

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.ensure_sandbox_image")
    @patch("jobs.tasks.read_agent_result")
    @patch("jobs.tasks.run_agent_in_container")
    @patch("jobs.tasks.clone_repo_in_container")
    @patch("jobs.tasks.start_generic_sandbox_container")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_log_output_is_ordered_and_readable(
        self, mock_load, mock_container, mock_clone, mock_run,
        mock_result, mock_ensure, mock_cb,
    ):
        task = self._create_task()
        mock_load.return_value = self._make_payload()
        mock_container.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_container.return_value.__exit__ = MagicMock(return_value=False)
        mock_result.return_value = self._make_agent_result()

        from jobs.tasks import execute_task

        with self.assertLogs("jobs.tasks", level="INFO") as cm:
            execute_task(task.id)

        messages = [r.getMessage() for r in cm.records]

        for msg in messages:
            self.assertIn(f"[{task.id}|github]", msg, f"Missing task_id/provider prefix: {msg}")

        status_keywords = [
            "Task started",
            "Checking sandbox image",
            "provisioning",
            "cloning",
            "running",
            "Agent result: done",
            "Task completed",
        ]
        found = []
        for kw in status_keywords:
            for msg in messages:
                if kw in msg:
                    found.append(kw)
                    break
        self.assertEqual(found, status_keywords, f"Log order mismatch: {found}")

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.ensure_sandbox_image")
    @patch("jobs.tasks.read_agent_result")
    @patch("jobs.tasks.run_agent_in_container")
    @patch("jobs.tasks.clone_repo_in_container")
    @patch("jobs.tasks.start_generic_sandbox_container")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_token_never_appears_in_logs(
        self, mock_load, mock_container, mock_clone, mock_run,
        mock_result, mock_ensure, mock_cb,
    ):
        secret_token = "ghp_SUPERTOKEN_12345"
        task = self._create_task()
        mock_load.return_value = self._make_payload(token=secret_token)
        mock_container.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_container.return_value.__exit__ = MagicMock(return_value=False)
        mock_result.return_value = self._make_agent_result()

        from jobs.tasks import execute_task

        with self.assertLogs("jobs.tasks", level="DEBUG") as cm:
            execute_task(task.id)

        for record in cm.records:
            self.assertNotIn(
                secret_token,
                record.getMessage(),
                f"Token leaked in log: {record.getMessage()}",
            )

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.ensure_sandbox_image")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_failure_path_logs_error_message(
        self, mock_load, mock_ensure, mock_cb,
    ):
        task = self._create_task()
        mock_load.return_value = self._make_payload()

        with patch(
            "jobs.tasks.start_generic_sandbox_container"
        ) as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(
                side_effect=ContainerError("boom")
            )
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

            from jobs.tasks import execute_task

            with self.assertLogs("jobs.tasks", level="ERROR") as cm:
                execute_task(task.id)

        messages = [r.getMessage() for r in cm.records]
        error_msgs = [m for m in messages if "boom" in m]
        self.assertTrue(error_msgs, "Error message 'boom' not found in ERROR logs")

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.ensure_sandbox_image")
    @patch("jobs.tasks.read_agent_result")
    @patch("jobs.tasks.run_agent_in_container")
    @patch("jobs.tasks.clone_repo_in_container")
    @patch("jobs.tasks.start_generic_sandbox_container")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_provider_in_every_log_line(
        self, mock_load, mock_container, mock_clone, mock_run,
        mock_result, mock_ensure, mock_cb,
    ):
        """Every log line for a task must include the provider tag."""
        task = self._create_task(provider="gitlab")
        mock_load.return_value = self._make_payload()
        mock_container.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_container.return_value.__exit__ = MagicMock(return_value=False)
        mock_result.return_value = self._make_agent_result()

        from jobs.tasks import execute_task

        with self.assertLogs("jobs.tasks", level="INFO") as cm:
            execute_task(task.id)

        for record in cm.records:
            self.assertIn(
                "|gitlab",
                record.getMessage(),
                f"Provider tag missing in log: {record.getMessage()}",
            )

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.ensure_sandbox_image")
    @patch("jobs.tasks.read_agent_result")
    @patch("jobs.tasks.run_agent_in_container")
    @patch("jobs.tasks.clone_repo_in_container")
    @patch("jobs.tasks.start_generic_sandbox_container")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_failed_agent_result_logs_warning(
        self, mock_load, mock_container, mock_clone, mock_run,
        mock_result, mock_ensure, mock_cb,
    ):
        """A failed agent result should log at WARNING, not INFO."""
        task = self._create_task()
        mock_load.return_value = self._make_payload()
        mock_container.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_container.return_value.__exit__ = MagicMock(return_value=False)
        mock_result.return_value = AgentResult(
            status="failed",
            branch_name=None,
            pr_url=None,
            programming_language=None,
            summary=None,
            technical_report=None,
            error_message="Something went wrong",
            model=None,
            callback={"attempted": False, "succeeded": False, "error": "No callback information available"},
        )

        from jobs.tasks import execute_task

        with self.assertLogs("jobs.tasks", level="WARNING") as cm:
            execute_task(task.id)

        messages = [r.getMessage() for r in cm.records]
        failed_msgs = [m for m in messages if "Agent result: failed" in m]
        self.assertTrue(failed_msgs, "No WARNING log for failed agent result")
        self.assertIn("Something went wrong", failed_msgs[0])

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.ensure_sandbox_image")
    @patch("jobs.tasks.read_agent_result")
    @patch("jobs.tasks.run_agent_in_container")
    @patch("jobs.tasks.clone_repo_in_container")
    @patch("jobs.tasks.start_generic_sandbox_container")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_callback_secret_never_in_logs(
        self, mock_load, mock_container, mock_clone, mock_run,
        mock_result, mock_ensure, mock_cb,
    ):
        """The callback secret must never appear in log output."""
        secret = "super_secret_callback_key_abc"
        task = self._create_task(callback_secret=secret)
        mock_load.return_value = self._make_payload()
        mock_container.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_container.return_value.__exit__ = MagicMock(return_value=False)
        mock_result.return_value = self._make_agent_result()

        from jobs.tasks import execute_task

        with self.assertLogs("jobs.tasks", level="DEBUG") as cm:
            execute_task(task.id)

        for record in cm.records:
            self.assertNotIn(
                secret,
                record.getMessage(),
                f"Callback secret leaked in log: {record.getMessage()}",
            )


# ---------------------------------------------------------------------------
# Sandbox resource limits
# ---------------------------------------------------------------------------


class ParseMemoryBytesTest(TestCase):
    def test_parses_suffixes(self):
        self.assertEqual(parse_memory_bytes("512m"), 512 * 1024 ** 2)
        self.assertEqual(parse_memory_bytes("2G"), 2 * 1024 ** 3)
        self.assertEqual(parse_memory_bytes("1024k"), 1024 * 1024)
        self.assertEqual(parse_memory_bytes("2048b"), 2048)

    def test_parses_bare_numbers_and_ints(self):
        self.assertEqual(parse_memory_bytes("1048576"), 1048576)
        self.assertEqual(parse_memory_bytes(1048576), 1048576)

    def test_unlimited_sentinel(self):
        self.assertEqual(parse_memory_bytes("-1"), -1)

    def test_invalid_returns_none(self):
        self.assertIsNone(parse_memory_bytes("plenty"))
        self.assertIsNone(parse_memory_bytes(""))
        self.assertIsNone(parse_memory_bytes(None))


class ResolveMemoryLimitsTest(TestCase):
    @override_settings(SANDBOX_MEMORY_LIMIT="2g", SANDBOX_MEMORY_SWAP_LIMIT="4g")
    def test_valid_pair_passed_through(self):
        mem, swap = resolve_memory_limits()
        self.assertEqual(mem, "2g")
        self.assertEqual(swap, 4 * 1024 ** 3)

    @override_settings(SANDBOX_MEMORY_LIMIT="2g", SANDBOX_MEMORY_SWAP_LIMIT="-1")
    def test_unlimited_swap_passed_through(self):
        mem, swap = resolve_memory_limits()
        self.assertEqual(mem, "2g")
        self.assertEqual(swap, -1)

    @override_settings(SANDBOX_MEMORY_LIMIT="2g", SANDBOX_MEMORY_SWAP_LIMIT="1g")
    def test_swap_below_memory_warns_and_falls_back(self):
        with self.assertLogs("jobs.execution.container", level="WARNING") as cm:
            mem, swap = resolve_memory_limits()
        self.assertEqual(mem, "2g")
        self.assertEqual(swap, 4 * 1024 ** 3)  # 2x the memory limit
        self.assertIn("SANDBOX_MEMORY_SWAP_LIMIT", cm.output[0])

    @override_settings(SANDBOX_MEMORY_LIMIT="2g", SANDBOX_MEMORY_SWAP_LIMIT="lots")
    def test_unparseable_swap_warns_and_falls_back(self):
        with self.assertLogs("jobs.execution.container", level="WARNING"):
            _, swap = resolve_memory_limits()
        self.assertEqual(swap, 4 * 1024 ** 3)

    @override_settings(SANDBOX_MEMORY_LIMIT="huge", SANDBOX_MEMORY_SWAP_LIMIT="4g")
    def test_unparseable_memory_falls_back_to_default(self):
        with self.assertLogs("jobs.execution.container", level="WARNING"):
            mem, swap = resolve_memory_limits()
        self.assertEqual(mem, "2g")
        self.assertEqual(swap, 4 * 1024 ** 3)


class ResolveCpuLimitTest(TestCase):
    @override_settings(SANDBOX_CPU_LIMIT="1.5")
    def test_fractional_cores(self):
        self.assertEqual(resolve_cpu_nano_cpus(), 1_500_000_000)

    @override_settings(SANDBOX_CPU_LIMIT="2")
    def test_whole_cores(self):
        self.assertEqual(resolve_cpu_nano_cpus(), 2_000_000_000)

    @override_settings(SANDBOX_CPU_LIMIT="all-of-them")
    def test_invalid_falls_back_with_warning(self):
        with self.assertLogs("jobs.execution.container", level="WARNING"):
            self.assertEqual(resolve_cpu_nano_cpus(), 1_500_000_000)

    @override_settings(SANDBOX_CPU_LIMIT="0")
    def test_zero_falls_back_with_warning(self):
        with self.assertLogs("jobs.execution.container", level="WARNING"):
            self.assertEqual(resolve_cpu_nano_cpus(), 1_500_000_000)


class WorkerConcurrencyTest(TestCase):
    """The concurrency setting must actually reach the worker, not just docs."""

    def test_setting_is_read_by_celery_app(self):
        from django.conf import settings as django_settings

        from config.celery import app

        self.assertEqual(
            app.conf.worker_concurrency,
            django_settings.CELERY_WORKER_CONCURRENCY,
        )

    def test_default_is_one(self):
        from django.conf import settings as django_settings

        self.assertEqual(django_settings.CELERY_WORKER_CONCURRENCY, 1)


class PackageManagerLimitsTest(TestCase):
    @override_settings(SANDBOX_NODE_MAX_OLD_SPACE_MB="")
    def test_heap_derived_from_memory_limit(self):
        self.assertEqual(resolve_node_heap_mb("2g"), 1024)

    @override_settings(SANDBOX_NODE_MAX_OLD_SPACE_MB="")
    def test_heap_has_floor(self):
        self.assertEqual(resolve_node_heap_mb("256m"), 512)

    @override_settings(SANDBOX_NODE_MAX_OLD_SPACE_MB="1536")
    def test_explicit_heap_wins(self):
        self.assertEqual(resolve_node_heap_mb("2g"), 1536)

    @override_settings(SANDBOX_NODE_MAX_OLD_SPACE_MB="", SANDBOX_PACKAGE_CONCURRENCY=2)
    def test_env_caps_memory_and_parallelism(self):
        env = build_package_manager_env("2g")
        self.assertEqual(env["NODE_OPTIONS"], "--max-old-space-size=1024")
        self.assertEqual(env["npm_config_maxsockets"], "2")
        self.assertEqual(env["npm_config_jobs"], "2")
        self.assertEqual(env["CARGO_BUILD_JOBS"], "2")
        self.assertEqual(env["MAKEFLAGS"], "-j2")
        self.assertEqual(env["GOMAXPROCS"], "2")

    @override_settings(SANDBOX_AGENT_ENV_PASSTHROUGH=("OPEN_API_BASE_URL", "OPEN_API_KEY"))
    def test_provider_env_forwarded(self):
        with patch.dict(
            os.environ,
            {"OPEN_API_BASE_URL": "https://llm.example/v1", "OPEN_API_KEY": "sk-test"},
        ):
            env = build_agent_provider_env()
        self.assertEqual(
            env,
            {"OPEN_API_BASE_URL": "https://llm.example/v1", "OPEN_API_KEY": "sk-test"},
        )

    @override_settings(SANDBOX_AGENT_ENV_PASSTHROUGH=("OPEN_API_BASE_URL", "OPEN_API_KEY"))
    def test_provider_env_skips_blank_values(self):
        with patch.dict(
            os.environ,
            {"OPEN_API_BASE_URL": "https://llm.example/v1", "OPEN_API_KEY": "   "},
        ):
            env = build_agent_provider_env()
        self.assertEqual(env, {"OPEN_API_BASE_URL": "https://llm.example/v1"})

    @override_settings(SANDBOX_AGENT_ENV_PASSTHROUGH=("OPEN_API_BASE_URL", "OPEN_API_KEY"))
    def test_provider_env_absent_is_not_an_error(self):
        """The provider vars are optional — unset means "forward nothing"."""
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(build_agent_provider_env(), {})

    @override_settings(SANDBOX_PACKAGE_CONCURRENCY=1)
    def test_pnpm_config_rewritten(self):
        container = MagicMock()
        container.exec_run.return_value = (0, (b"", b""))
        _apply_pnpm_limits(container, task_id=3)
        script = container.exec_run.call_args.kwargs["cmd"][2]
        self.assertIn("networkConcurrency", script)
        self.assertIn("childConcurrency", script)
        self.assertIn("/home/jiffy/.config/pnpm/config.yaml", script)

    def test_pnpm_config_failure_is_not_fatal(self):
        container = MagicMock()
        container.exec_run.return_value = (1, (b"", b"no such file"))
        with self.assertLogs("jobs.execution.container", level="WARNING"):
            _apply_pnpm_limits(container, task_id=3)


# ---------------------------------------------------------------------------
# Network egress restriction
# ---------------------------------------------------------------------------


class NetworkRestrictionTest(TestCase):
    """Unit tests for the sandbox network egress restriction."""

    def test_effective_allowlist_defaults(self):
        allowlist = _effective_network_allowlist()
        self.assertIn("pypi.org", allowlist)
        self.assertIn("registry.npmjs.org", allowlist)
        self.assertIn("github.com", allowlist)
        self.assertIn("crates.io", allowlist)

    @override_settings(SANDBOX_NETWORK_ALLOWLIST_EXTRA=["git.example.com", "llm.example.com"])
    def test_effective_allowlist_appends_extra(self):
        allowlist = _effective_network_allowlist()
        self.assertIn("git.example.com", allowlist)
        self.assertIn("llm.example.com", allowlist)
        self.assertIn("pypi.org", allowlist)

    @override_settings(
        SANDBOX_NETWORK_ALLOWLIST=["one.example.com"],
        SANDBOX_NETWORK_ALLOWLIST_EXTRA=["two.example.com", "one.example.com"],
    )
    def test_effective_allowlist_dedups_and_keeps_order(self):
        allowlist = _effective_network_allowlist()
        self.assertEqual(allowlist, ["one.example.com", "two.example.com"])

    @override_settings(
        SANDBOX_NETWORK_ALLOWLIST=["Mixed.Case.Example.com"],
        SANDBOX_NETWORK_ALLOWLIST_EXTRA=["mixed.case.example.com"],
    )
    def test_effective_allowlist_normalises_case(self):
        allowlist = _effective_network_allowlist()
        self.assertEqual(allowlist, ["mixed.case.example.com"])

    def test_script_allows_default_hosts_and_drops_rest(self):
        script = _build_network_restriction_script(["pypi.org", "github.com"])
        self.assertIn("iptables -P OUTPUT DROP", script)
        self.assertIn("getent ahostsv4 'pypi.org'", script)
        self.assertIn("getent ahostsv4 'github.com'", script)
        self.assertIn("-o lo -j ACCEPT", script)
        self.assertIn("--ctstate ESTABLISHED,RELATED -j ACCEPT", script)
        # DNS to Docker's embedded resolver is allowed so allowlisted hosts resolve.
        self.assertIn("--dport 53 -d 127.0.0.11 -j ACCEPT", script)

    def test_script_escapes_hosts(self):
        script = _build_network_restriction_script(["a'b.com"])
        self.assertIn("getent ahostsv4 'a'\\''b.com'", script)

    def test_apply_network_restriction_success(self):
        container = MagicMock()
        container.short_id = "abc123"
        container.exec_run.return_value = (0, (b"ok", b""))
        _apply_network_restriction(container, ["pypi.org"], task_id=7)
        container.exec_run.assert_called_once()
        _, kwargs = container.exec_run.call_args
        self.assertEqual(kwargs["user"], "root")
        self.assertTrue(kwargs["demux"])

    def test_apply_network_restriction_failure_raises(self):
        container = MagicMock()
        container.short_id = "abc123"
        container.exec_run.return_value = (1, (b"", b"iptables: Permission denied"))
        with self.assertRaises(ContainerError) as ctx:
            _apply_network_restriction(container, ["pypi.org"], task_id=7)
        self.assertIn("Failed to apply sandbox network restriction", str(ctx.exception))
        self.assertIn("Permission denied", str(ctx.exception))

    def _mock_container_start(self, docker_client):
        client = MagicMock()
        docker_client.return_value = client
        container = MagicMock()
        container.short_id = "abc123"
        container.id = "a" * 64
        container.exec_run.return_value = (0, (b"ok", b""))
        client.containers.run.return_value = container
        return client, container

    @patch("jobs.execution.container.get_docker_client")
    def test_start_container_restricted_by_default(self, mock_client):
        client, container = self._mock_container_start(mock_client)

        with override_settings(SANDBOX_CLEANUP=False):
            with start_generic_sandbox_container(1, {"REPO_TOKEN": "tok"}) as started:
                self.assertEqual(started, container)

        run_kwargs = client.containers.run.call_args
        self.assertEqual(run_kwargs.args[0], "jiffy-sandbox:1.2.0")
        self.assertEqual(run_kwargs.kwargs["cap_add"], ["NET_ADMIN"])
        env = run_kwargs.kwargs["environment"]
        self.assertEqual(env["JIFFY_SANDBOX_NETWORK_RESTRICTED"], "true")
        self.assertIn("pypi.org", env["JIFFY_SANDBOX_NETWORK_ALLOWLIST"])
        # Restriction rules applied before yield via a root exec.
        root_execs = [c for c in container.exec_run.call_args_list if c.kwargs.get("user") == "root"]
        self.assertEqual(len(root_execs), 1)

    @patch("jobs.execution.container.get_docker_client")
    def test_start_container_applies_resource_limits(self, mock_client):
        client, container = self._mock_container_start(mock_client)

        with override_settings(
            SANDBOX_CLEANUP=False,
            SANDBOX_MEMORY_LIMIT="3g",
            SANDBOX_MEMORY_SWAP_LIMIT="6g",
            SANDBOX_CPU_LIMIT="1.5",
            SANDBOX_NODE_MAX_OLD_SPACE_MB="",
            SANDBOX_PACKAGE_CONCURRENCY=2,
        ):
            with start_generic_sandbox_container(1, {"REPO_TOKEN": "tok"}):
                pass

        kwargs = client.containers.run.call_args.kwargs
        self.assertEqual(kwargs["mem_limit"], "3g")
        self.assertEqual(kwargs["memswap_limit"], 6 * 1024 ** 3)
        self.assertEqual(kwargs["nano_cpus"], 1_500_000_000)
        # cpuset_cpus pinned a core index rather than capping share — gone.
        self.assertNotIn("cpuset_cpus", kwargs)
        # Package-manager caps are in the container env from the start.
        self.assertEqual(kwargs["environment"]["NODE_OPTIONS"], "--max-old-space-size=1536")
        self.assertEqual(kwargs["environment"]["npm_config_maxsockets"], "2")

    @patch("jobs.execution.container.get_docker_client")
    def test_start_container_unrestricted_skips_cap_and_rules(self, mock_client):
        client, container = self._mock_container_start(mock_client)

        with override_settings(SANDBOX_CLEANUP=False, SANDBOX_NETWORK_RESTRICTED=False):
            with start_generic_sandbox_container(1, {"REPO_TOKEN": "tok"}) as started:
                self.assertEqual(started, container)

        run_kwargs = client.containers.run.call_args
        self.assertNotIn("cap_add", run_kwargs.kwargs)
        env = run_kwargs.kwargs["environment"]
        self.assertEqual(env["JIFFY_SANDBOX_NETWORK_RESTRICTED"], "false")

    @patch("jobs.execution.container.get_docker_client")
    def test_start_container_restriction_failure_fails_closed(self, mock_client):
        client = MagicMock()
        mock_client.return_value = client
        container = MagicMock()
        container.short_id = "abc123"
        container.id = "a" * 64
        container.exec_run.return_value = (1, (b"", b"iptables: Permission denied"))
        client.containers.run.return_value = container

        with override_settings(SANDBOX_CLEANUP=False):
            with self.assertRaises(ContainerError) as ctx:
                with start_generic_sandbox_container(1, {"REPO_TOKEN": "tok"}):
                    self.fail("should not yield when restriction cannot be applied")

        self.assertIn("Failed to apply sandbox network restriction", str(ctx.exception))

    @patch("jobs.execution.container.get_docker_client")
    def test_start_container_logs_restriction_state(self, mock_client):
        mock_client.return_value = MagicMock()
        container = MagicMock()
        container.short_id = "abc123"
        container.id = "a" * 64
        container.exec_run.return_value = (0, (b"ok", b""))
        mock_client.return_value.containers.run.return_value = container

        with override_settings(SANDBOX_CLEANUP=False):
            with self.assertLogs("jobs.execution.container", level="INFO") as cm:
                with start_generic_sandbox_container(1, {"REPO_TOKEN": "tok"}):
                    pass

        messages = [r.getMessage() for r in cm.records]
        active = [m for m in messages if "Network restriction ACTIVE" in m]
        self.assertTrue(active, "Expected an ACTIVE restriction log line")
        self.assertIn("pypi.org", active[0])

        with override_settings(SANDBOX_CLEANUP=False, SANDBOX_NETWORK_RESTRICTED=False):
            with self.assertLogs("jobs.execution.container", level="INFO") as cm:
                with start_generic_sandbox_container(1, {"REPO_TOKEN": "tok"}):
                    pass

        messages = [r.getMessage() for r in cm.records]
        disabled = [m for m in messages if "Network restriction DISABLED" in m]
        self.assertTrue(disabled, "Expected a DISABLED restriction log line")


class AgentQuestionTest(TestCase):
    """An agent that has a question must get it back onto the issue thread.

    Nothing about the run is interactive, so a question the agent cannot post
    is a question nobody ever sees.  A ``"question"`` result parks the task on
    ``needs_input`` and relays the text as a reply — the answer arrives later
    as a brand-new task.
    """

    def _create_task(self, **kwargs):
        defaults = {
            "provider": "github",
            "repo_url": "https://github.com/user/repo",
            "issue_external_id": "100",
            "callback_url": "https://example.com/cb",
            "callback_secret": "sec",
            "status": "queued",
        }
        defaults.update(kwargs)
        return Task.objects.create(**defaults)

    def _container_with_result(self, result: dict):
        container = MagicMock()
        container.short_id = "abc123"
        container.exec_run.return_value = (
            0,
            (json.dumps(result).encode(), b""),
        )
        return container

    def test_question_result_is_parsed(self):
        container = self._container_with_result({
            "status": "question",
            "branch_name": "Jiffy/add-cache",
            "summary": "Scaffolding pushed.",
            "question": "Redis or in-process cache?",
            "callback": {"attempted": True, "succeeded": True, "error": None},
        })
        result = read_agent_result(container)
        self.assertEqual(result.status, "question")
        self.assertEqual(result.question, "Redis or in-process cache?")

    def test_question_status_without_a_question_is_a_failure(self):
        """A "question" nobody can read is worse than an honest failure."""
        container = self._container_with_result({
            "status": "question",
            "question": "   ",
            "callback": {"attempted": True, "succeeded": True, "error": None},
        })
        result = read_agent_result(container)
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.question)
        self.assertIn("question", result.error_message)

    def _instructions(self, text="Do the thing"):
        return build_agent_instructions({
            "repo": {"url": "https://github.com/user/repo"},
            "issue": {"text": text, "external_issue_id": "1"},
            "callback": {"url": "https://example.com/cb", "secret": "sec"},
        })

    def test_instructions_tell_the_agent_how_to_ask(self):
        instructions = self._instructions()
        self.assertIn("## Asking a Question", instructions)
        self.assertIn('`"question"`', instructions)
        self.assertIn("❓ Jiffy has a question before continuing.", instructions)

    def test_instructions_make_asking_a_last_resort(self):
        """Asking costs a human round trip, so the prompt must push back on it."""
        instructions = self._instructions()
        section = instructions[instructions.index("## Asking a Question"):]
        section = section[: section.index("## Callback Delivery")]
        for expected in (
            "Last Resort Only",
            "Searched the repository",
            "does not depend on the answer",
            "reasonable default",
            "write down the assumption",
        ):
            self.assertIn(expected, section)

    def test_instructions_forbid_asking_about_a_truncated_prompt(self):
        """The one question never worth asking: "re-send me the task"."""
        instructions = self._instructions()
        self.assertIn("never ask the requester to re-send", instructions)
        self.assertIn("looks short, cut off, or incomplete", instructions)
        self.assertIn(INSTRUCTIONS_PATH, instructions)

    def test_issue_text_is_fenced_by_integrity_markers(self):
        """Seeing the end marker is how the agent knows nothing was truncated."""
        instructions = self._instructions("Fix the login bug")
        fenced = instructions.split(ISSUE_BEGIN_MARKER)[1].split(ISSUE_END_MARKER)[0]
        self.assertEqual(fenced.strip(), "Fix the login bug")

    def test_a_huge_thread_is_still_fully_fenced(self):
        body = "line of issue text\n" * 100_000
        instructions = self._instructions(body)
        fenced = instructions.split(ISSUE_BEGIN_MARKER)[1].split(ISSUE_END_MARKER)[0]
        self.assertEqual(fenced.strip(), body.strip())

    def test_empty_issue_text_is_flagged_by_the_gateway(self):
        """An empty request is a Gateway/edge bug, not something to ask about."""
        with self.assertLogs("jobs.execution.agent", level="WARNING") as cm:
            _extract_issue_text({"issue": {"text": "   ", "external_issue_id": "55"}})
        self.assertIn("produced no text", "".join(cm.output))

    def test_question_body_carries_the_tag(self):
        body = format_callback_body(
            task_id=3, status="question", question="Which cache backend?"
        )
        self.assertTrue(body.startswith(QUESTION_TAG))

    def test_result_bodies_do_not_carry_the_question_tag(self):
        for status in ("done", "failed"):
            body = format_callback_body(
                task_id=3, status=status, summary="ok", error_message="bad"
            )
            self.assertNotIn(QUESTION_TAG, body)

    def test_instructions_require_the_tag_on_the_question_comment(self):
        instructions = self._instructions()
        self.assertIn(f"{QUESTION_TAG} Task #<task_id>: ❓", instructions)
        self.assertIn("tag is mandatory", instructions)

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.ensure_sandbox_image")
    @patch("jobs.tasks.read_agent_result")
    @patch("jobs.tasks.run_agent_in_container")
    @patch("jobs.tasks.clone_repo_in_container")
    @patch("jobs.tasks.start_generic_sandbox_container")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_question_parks_the_task_and_falls_back_to_the_gateway(
        self, mock_load, mock_container, mock_clone, mock_run, mock_result, mock_ensure, mock_cb
    ):
        task = self._create_task()
        mock_load.return_value = {
            "repo": {"url": "https://github.com/user/repo", "token": "ghp_test"},
            "issue": {"text": "Do the thing", "external_issue_id": "100"},
            "callback": {"url": "https://example.com/cb", "secret": "sec"},
        }
        mock_container.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_container.return_value.__exit__ = MagicMock(return_value=False)
        mock_result.return_value = AgentResult(
            status="question",
            branch_name="Jiffy/add-cache",
            pr_url=None,
            programming_language="python",
            summary="Scaffolding pushed.",
            technical_report=None,
            error_message=None,
            model="test/model",
            callback={"attempted": False, "succeeded": False, "error": "no network"},
            question="Redis or in-process cache?",
        )

        from jobs.tasks import execute_task

        execute_task(task.id)

        task.refresh_from_db()
        self.assertEqual(task.status, "needs_input")
        self.assertEqual(task.question, "Redis or in-process cache?")
        self.assertEqual(task.branch_name, "Jiffy/add-cache")
        self.assertIsNone(task.error_message)

        mock_cb.assert_called_once()
        kwargs = mock_cb.call_args.kwargs
        self.assertEqual(kwargs["status"], "question")
        self.assertEqual(kwargs["question"], "Redis or in-process cache?")

    @patch("jobs.tasks.send_fallback_callback")
    @patch("jobs.tasks.ensure_sandbox_image")
    @patch("jobs.tasks.read_agent_result")
    @patch("jobs.tasks.run_agent_in_container")
    @patch("jobs.tasks.clone_repo_in_container")
    @patch("jobs.tasks.start_generic_sandbox_container")
    @patch("jobs.tasks.load_payload_from_redis")
    def test_agent_delivered_question_skips_the_gateway_callback(
        self, mock_load, mock_container, mock_clone, mock_run, mock_result, mock_ensure, mock_cb
    ):
        task = self._create_task()
        mock_load.return_value = {
            "repo": {"url": "https://github.com/user/repo", "token": "ghp_test"},
            "issue": {"text": "Do the thing", "external_issue_id": "100"},
            "callback": {"url": "https://example.com/cb", "secret": "sec"},
        }
        mock_container.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_container.return_value.__exit__ = MagicMock(return_value=False)
        mock_result.return_value = AgentResult(
            status="question",
            branch_name=None,
            pr_url=None,
            programming_language=None,
            summary=None,
            technical_report=None,
            error_message=None,
            model="test/model",
            callback={"attempted": True, "succeeded": True, "error": None},
            question="Which environment should this target?",
        )

        from jobs.tasks import execute_task

        execute_task(task.id)

        task.refresh_from_db()
        self.assertEqual(task.status, "needs_input")
        mock_cb.assert_not_called()
