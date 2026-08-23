"""Tests for how the agent process is executed inside the sandbox container."""

import json
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from jobs.execution.agent import (
    ISSUE_BEGIN_MARKER,
    ISSUE_END_MARKER,
    MAX_INLINE_ISSUE_TEXT_BYTES,
    build_agent_instructions,
    build_task_document,
)
from jobs.execution.container import (
    PROMPT_PATH,
    TASK_JSON_PATH,
    build_agent_command,
    run_agent_in_container,
    write_task_document,
)
from jobs.execution.exceptions import ContainerError
from tests.support import sandbox_container_mock, staged_files


class RunAgentInContainerTest(TestCase):
    """The agent exec is started detached and polled to completion.

    A blocking exec used to hold one Docker API connection open for the whole
    run, which the socket proxy cut at 10 minutes — the caller then saw an
    exec that was still "Running" with a null exit code ("exited with code
    None") even though the agent was alive.  These tests pin the detached
    behaviour and the Gateway-enforced timeout that replaced it.
    """

    def _container(self, inspect_results):
        return sandbox_container_mock(inspect_results)

    def test_exec_is_detached_and_polled(self):
        container = self._container([
            {"Running": True, "ExitCode": None},
            {"Running": False, "ExitCode": 0},
        ])
        with patch("jobs.execution.container.time.sleep"):
            run_agent_in_container(container, "do the thing", task_id=1)

        api = container.client.api
        api.exec_start.assert_called_once_with("exec-1", detach=True)
        self.assertEqual(api.exec_inspect.call_count, 2)
        self.assertEqual(api.exec_create.call_args.kwargs["workdir"], "/workspace")
        self.assertEqual(api.exec_create.call_args.kwargs["cmd"][:3], ["bash", "-l", "-c"])

    def test_nonzero_exit_code_raises(self):
        container = self._container([{"Running": False, "ExitCode": 137}])
        with self.assertRaises(ContainerError) as ctx:
            run_agent_in_container(container, "do the thing", task_id=1)
        self.assertIn("137", str(ctx.exception))

    def test_timeout_raises_instead_of_hanging(self):
        container = self._container([{"Running": True, "ExitCode": None}] * 10)
        with patch("jobs.execution.container.time.sleep"), \
                patch("jobs.execution.container.time.monotonic", side_effect=[0, 10, 4000]):
            with self.assertRaises(ContainerError) as ctx:
                run_agent_in_container(container, "do the thing", task_id=1, timeout_seconds=3600)
        self.assertIn("did not finish within 3600s", str(ctx.exception))

    def test_finished_without_exit_code_is_reported_clearly(self):
        container = self._container([{"Running": False, "ExitCode": None, "Status": "unknown"}])
        with self.assertRaises(ContainerError) as ctx:
            run_agent_in_container(container, "do the thing", task_id=1)
        self.assertIn("no exit code", str(ctx.exception))

    @override_settings(SANDBOX_AGENT_TIMEOUT_SECONDS=120)
    def test_timeout_comes_from_settings(self):
        container = self._container([{"Running": True, "ExitCode": None}] * 10)
        with patch("jobs.execution.container.time.sleep"), \
                patch("jobs.execution.container.time.monotonic", side_effect=[0, 10, 500]):
            with self.assertRaises(ContainerError) as ctx:
                run_agent_in_container(container, "do the thing", task_id=1)
        self.assertIn("did not finish within 120s", str(ctx.exception))


class TaskStagingTest(TestCase):
    """The task travels by file, so nothing about it depends on argv size."""

    def _payload(self, body):
        return {
            "repo": {"url": "https://github.com/user/repo"},
            "issue": {
                "turns": [
                    {
                        "role": "user",
                        "author": "a",
                        "body": body,
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "external_issue_id": "42",
            },
            "callback": {"url": "https://example.com/cb", "secret": "sec"},
        }

    def _run(self, body):
        payload = self._payload(body)
        container = sandbox_container_mock([{"Running": False, "ExitCode": 0}])
        run_agent_in_container(
            container,
            build_agent_instructions(payload),
            task_id=1,
            task_document=build_task_document(payload, task_id=1),
        )
        return container, staged_files(container)

    def test_task_json_carries_the_whole_thread(self):
        body = "Fix the deploy script.\n" + ("history line\n" * 20000)
        _, staged = self._run(body)
        document = json.loads(staged[TASK_JSON_PATH])
        self.assertIn(body, document["issue_text"])
        self.assertEqual(
            document["issue_text_bytes"], len(document["issue_text"].encode("utf-8"))
        )

    def test_task_json_is_written_as_a_tar_upload_not_through_a_shell(self):
        container = sandbox_container_mock()
        write_task_document(container, {"issue_text": "x' \" $(rm -rf /)"})
        container.put_archive.assert_called_once()
        for call in container.exec_run.call_args_list:
            self.assertEqual(call.kwargs["cmd"][0], "stat")

    def test_task_json_carries_no_credentials(self):
        payload = self._payload("do it")
        payload["repo"]["token"] = "ghp_secret"
        document = build_task_document(payload, task_id=1)
        self.assertNotIn("ghp_secret", json.dumps(document))
        self.assertNotIn("sec", json.dumps(document.get("callback", {})))

    def test_prompt_stays_under_the_argv_cap_whatever_the_thread(self):
        """The prompt grows with the thread but is bounded well under 128 KiB."""
        _, huge = self._run("Fix it.\n" + ("history line\n" * 100000))
        self.assertLess(len(huge[PROMPT_PATH].encode("utf-8")), 110 * 1024)
        self.assertLessEqual(
            len(huge[PROMPT_PATH].encode("utf-8")),
            MAX_INLINE_ISSUE_TEXT_BYTES + 32 * 1024,
        )

    def test_the_command_stays_tiny_whatever_the_thread(self):
        cmd = build_agent_command()
        self.assertIn(f'"$(cat {PROMPT_PATH})"', cmd)
        self.assertLess(len(cmd.encode("utf-8")), 4096)


class IssueSectionTest(TestCase):
    """The request must always be in the prompt, and never look truncated.

    Two reported failures came from breaking this: an empty fenced region made
    the agent answer "the message appears to be cut off", and a bare pointer to
    a file made it answer "what would you like me to work on?".
    """

    def _instructions(self, body):
        return build_agent_instructions({
            "repo": {"url": "https://github.com/user/repo"},
            "issue": {
                "turns": [
                    {
                        "role": "user",
                        "author": "a",
                        "body": body,
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "external_issue_id": "42",
            },
            "callback": {"url": "https://example.com/cb", "secret": "sec"},
        })

    def _fenced(self, body):
        instructions = self._instructions(body)
        return instructions.split(ISSUE_BEGIN_MARKER)[1].split(ISSUE_END_MARKER)[0]

    def test_a_small_request_is_inlined_whole(self):
        self.assertIn("Fix the deploy script", self._fenced("Fix the deploy script"))

    def test_a_large_request_is_still_inlined(self):
        """Never a bare pointer: the agent cannot skip what is in front of it."""
        fenced = self._fenced("Fix the deploy script.\n" + ("history line\n" * 40000))
        self.assertIn("Fix the deploy script.", fenced)
        self.assertGreater(len(fenced.encode("utf-8")), 1000)

    def test_elision_keeps_both_the_request_and_the_latest_comment(self):
        body = (
            "THE ORIGINAL REQUEST: add a readiness probe\n"
            + ("history line\n" * 40000)
            + "LATEST COMMENT: also update the docs\n"
        )
        fenced = self._fenced(body)
        self.assertIn("THE ORIGINAL REQUEST: add a readiness probe", fenced)
        self.assertIn("LATEST COMMENT: also update the docs", fenced)

    def test_elision_says_what_it_left_out_and_where_to_find_it(self):
        fenced = self._fenced("Fix it.\n" + ("history line\n" * 40000))
        self.assertIn("omitted here", fenced)
        self.assertIn(TASK_JSON_PATH, fenced)

    def test_the_fenced_region_is_never_empty(self):
        for body in ("x", "short", "Fix it.\n" + ("history line\n" * 40000)):
            self.assertTrue(self._fenced(body).strip(), "issue region rendered empty")

    def test_the_prompt_stays_under_the_argv_limit(self):
        """MAX_ARG_STRLEN is 128 KiB; leave a clear margin under it."""
        instructions = self._instructions("Fix it.\n" + ("history line\n" * 100000))
        self.assertLess(len(instructions.encode("utf-8")), 110 * 1024)

    def test_the_contract_survives_at_every_size(self):
        for body in ("tiny", "Fix it.\n" + ("history line\n" * 40000)):
            instructions = self._instructions(body)
            for section in (
                "## Callback Delivery",
                "## Required Final Output",
                ".jiffy_result.json",
                "## When Something Is Unclear",
            ):
                self.assertIn(section, instructions)


class StagedFileVerificationTest(TestCase):
    """The Gateway must prove what landed, not infer it from behaviour."""

    def test_a_truncated_upload_fails_the_task(self):
        container = sandbox_container_mock()

        def _short_stat(cmd=None, **kwargs):
            if cmd and cmd[0] == "stat":
                return 0, (b"5", b"")
            return 0, (b'{"model": "m"}', b"")

        container.exec_run.side_effect = _short_stat

        with self.assertRaises(ContainerError) as ctx:
            write_task_document(container, {"issue_text": "a much longer text"})
        self.assertIn("truncated prompt", str(ctx.exception))

    def test_a_missing_file_fails_the_task(self):
        container = sandbox_container_mock()

        def _missing(cmd=None, **kwargs):
            if cmd and cmd[0] == "stat":
                return 1, (b"", b"No such file or directory")
            return 0, (b'{"model": "m"}', b"")

        container.exec_run.side_effect = _missing

        with self.assertRaises(ContainerError) as ctx:
            write_task_document(container, {"issue_text": "text"})
        self.assertIn("missing after upload", str(ctx.exception))

    def test_handoff_is_logged_with_sizes_and_a_hash(self):
        container = sandbox_container_mock([{"Running": False, "ExitCode": 0}])
        with self.assertLogs("jobs.execution.container", level="INFO") as cm:
            run_agent_in_container(
                container,
                "the prompt",
                task_id=3,
                task_document={"issue_text": "the request", "issue_text_inlined": True},
            )
        message = "\n".join(cm.output)
        self.assertIn("Task handed off", message)
        self.assertIn("sha256:", message)
        self.assertIn("inlined", message)

    def test_a_by_reference_handoff_is_logged_as_such(self):
        container = sandbox_container_mock([{"Running": False, "ExitCode": 0}])
        with self.assertLogs("jobs.execution.container", level="INFO") as cm:
            run_agent_in_container(
                container,
                "the prompt",
                task_id=3,
                task_document={"issue_text": "x" * 100, "issue_text_inlined": False},
            )
        self.assertIn("by reference", "\n".join(cm.output))


class MissingResultDiagnosticsTest(TestCase):
    """A run that ends with no result file must leave a trace of why."""

    def _container_without_result(self, log_output=b"The message appears to be cut off."):
        container = sandbox_container_mock()
        container.exec_run.side_effect = lambda cmd=None, **kw: (1, (b"", b"No such file"))
        container.logs.return_value = log_output
        return container

    def test_agent_output_is_kept_when_the_contract_is_missing(self):
        from jobs.execution.agent import read_agent_result

        container = self._container_without_result()
        with self.assertLogs("jobs.execution.agent", level="WARNING") as cm:
            result = read_agent_result(container)

        self.assertEqual(result.status, "failed")
        message = "\n".join(cm.output)
        self.assertIn("ended without a result file", message)
        self.assertIn("The message appears to be cut off.", message)

    def test_a_silent_run_is_reported_as_silent(self):
        from jobs.execution.agent import read_agent_result

        container = self._container_without_result(log_output=b"   ")
        with self.assertLogs("jobs.execution.agent", level="WARNING") as cm:
            read_agent_result(container)
        self.assertIn("no output at all", "\n".join(cm.output))

    def test_unreadable_logs_never_mask_the_real_failure(self):
        from jobs.execution.agent import read_agent_result

        container = self._container_without_result()
        container.logs.side_effect = RuntimeError("docker gone")
        with self.assertLogs("jobs.execution.agent", level="WARNING"):
            result = read_agent_result(container)
        self.assertEqual(result.status, "failed")
        self.assertIn("did not produce the required output contract", result.error_message)


class ResultContractProminenceTest(TestCase):
    """The result file must be impossible to miss, not a footnote."""

    def _instructions(self):
        return build_agent_instructions({
            "repo": {"url": "https://github.com/user/repo"},
            "issue": {"text": "Do the thing", "external_issue_id": "1"},
            "callback": {"url": "https://example.com/cb", "secret": "sec"},
        })

    def test_the_result_file_is_a_numbered_step(self):
        instructions = self._instructions()
        self.assertIn("9. **Write the result file**", instructions)

    def test_the_agent_is_told_chat_output_reaches_nobody(self):
        collapsed = " ".join(self._instructions().split())
        self.assertIn("Nobody is reading your chat output", collapsed)
        self.assertIn("Answering in chat instead of doing those is the same as doing nothing", collapsed)
