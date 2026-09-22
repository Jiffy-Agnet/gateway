"""Tests for how the agent process is executed inside the sandbox container."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from jobs.execution.agent import (
    ISSUE_BEGIN_MARKER,
    ISSUE_END_MARKER,
    MAX_INLINE_ISSUE_TEXT_BYTES,
    build_agent_instructions,
    build_system_prompt,
    build_task_document,
)
from jobs.execution.container import (
    PROMPT_PATH,
    SYSTEM_PROMPT_PATH,
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
        self.assertIn(f"< {PROMPT_PATH}", cmd)
        self.assertLess(len(cmd.encode("utf-8")), 4096)


class StdinDeliveryTest(TestCase):
    """The user prompt is delivered via stdin, never through argv.

    ``opencode run`` merges piped stdin into its message (resolveRunInput in
    the CLI source), so a stdin redirect sidesteps the kernel's 128 KiB
    MAX_ARG_STRLEN cap on a single argv entry — the limit that used to make
    every large thread fail with exit 126 "Argument list too long".
    """

    def test_the_command_never_expands_the_prompt_into_argv(self):
        cmd = build_agent_command()
        self.assertIn('opencode run --auto < ', cmd)
        self.assertNotIn('$(cat', cmd)
        self.assertNotIn('JIFFY_PROMPT', cmd)

    def test_the_command_logs_the_byte_count_before_the_agent_starts(self):
        cmd = build_agent_command()
        self.assertIn('bytes of prompt to opencode', cmd)
        self.assertIn('wc -c', cmd)

    def test_a_huge_prompt_is_delivered_verbatim_whatever_its_size(self):
        """The 80 KiB inline cap is a context budget, not a delivery limit.

        A prompt past the old 128 KiB argv cap — which used to die with exit
        126 "Argument list too long" — must stage whole and pass the
        byte-exact delivery check: stdin has no MAX_ARG_STRLEN.
        """
        instructions = "Fix it. " * 30_000  # ~240 KiB
        self.assertGreater(len(instructions.encode("utf-8")), 128 * 1024)

        container = sandbox_container_mock([{"Running": False, "ExitCode": 0}])
        run_agent_in_container(container, instructions, task_id=1)
        staged = staged_files(container)
        self.assertEqual(staged[PROMPT_PATH], instructions)


class SystemPromptStagingTest(TestCase):
    """The standing contract travels as a file and is loaded via the config."""

    def test_system_prompt_is_staged_when_provided(self):
        container = sandbox_container_mock([{"Running": False, "ExitCode": 0}])
        run_agent_in_container(
            container,
            "the task",
            task_id=1,
            system_prompt="the standing contract",
        )
        staged = staged_files(container)
        self.assertIn(SYSTEM_PROMPT_PATH, staged)
        self.assertEqual(staged[SYSTEM_PROMPT_PATH], "the standing contract")
        self.assertIn(PROMPT_PATH, staged)

    def test_system_prompt_is_optional(self):
        """Runs without one still work (the contract is additive, not required)."""
        container = sandbox_container_mock([{"Running": False, "ExitCode": 0}])
        run_agent_in_container(container, "the task", task_id=1)
        self.assertNotIn(SYSTEM_PROMPT_PATH, staged_files(container))

    def test_system_prompt_is_agent_agnostic_and_issue_free(self):
        """The contract must not restate the request or embed per-issue content."""
        system_prompt = build_system_prompt({
            "repo": {"url": "https://github.com/user/repo"},
            "issue": {"text": "Do the thing", "external_issue_id": "1"},
            "callback": {"url": "https://example.com/cb", "secret": "sec"},
        })
        self.assertNotIn("Do the thing", system_prompt)
        self.assertNotIn(ISSUE_BEGIN_MARKER, system_prompt)
        for section in (
            "## Working Directory",
            "## When Something Is Unclear",
            "## Callback Delivery",
            "## Required Final Output",
            ".jiffy_result.json",
        ):
            self.assertIn(section, system_prompt)


class OpencodeConfigInjectionTest(TestCase):
    """The injected config must register the staged system prompt."""

    def _inject_with_root_config(self, root_config_text, container=None):
        """Inject with a controlled project-root opencode.json; return the staged file."""
        container = container or sandbox_container_mock()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(root_config_text)
            tmp = fh.name
        self.addCleanup(os.unlink, tmp)
        with patch("jobs.execution.container._project_opencode_json_path") as fake_path:
            fake_path.return_value = Path(tmp)
            from jobs.execution.container import _inject_opencode_config

            _inject_opencode_config(container, task_id=1)
        staged = staged_files(container)
        return staged.get("/home/jiffy/.config/opencode/opencode.json")

    def test_config_registers_the_staged_system_prompt(self):
        staged = self._inject_with_root_config('{"model": "opencode/m"}')
        config = json.loads(staged)
        self.assertEqual(config["instructions"][0], SYSTEM_PROMPT_PATH)
        self.assertEqual(config["model"], "opencode/m")

    def test_existing_instructions_are_kept_after_ours(self):
        root = json.dumps({"instructions": ["CONTRIBUTING.md"]})
        config = json.loads(self._inject_with_root_config(root))
        self.assertEqual(
            config["instructions"],
            [SYSTEM_PROMPT_PATH, "CONTRIBUTING.md"],
        )

    def test_a_duplicate_registration_is_not_added_twice(self):
        root = json.dumps({"instructions": [SYSTEM_PROMPT_PATH]})
        config = json.loads(self._inject_with_root_config(root))
        self.assertEqual(config["instructions"], [SYSTEM_PROMPT_PATH])

    def test_an_unparseable_root_config_fails_closed_when_restricted(self):
        # An empty or corrupt config silently drops model and plugin, and the
        # agent then runs against whatever default its credentials resolve to.
        for text in ("", "{not json", "[]"):
            with self.subTest(text=text):
                with self.assertRaises(ContainerError) as ctx:
                    self._inject_with_root_config(text)
                self.assertIn("opencode.json", str(ctx.exception))

    def test_an_unparseable_root_config_still_registers_the_contract(self):
        with override_settings(SANDBOX_NETWORK_RESTRICTED=False):
            with self.assertLogs("jobs.execution.container", level="WARNING"):
                staged = self._inject_with_root_config("{not json")
        config = json.loads(staged)
        self.assertEqual(config["instructions"], [SYSTEM_PROMPT_PATH])

    def test_injection_failure_fails_closed_when_restricted(self):
        container = sandbox_container_mock()
        container.put_archive.side_effect = None
        container.put_archive.return_value = False
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{}")
            tmp = fh.name
        self.addCleanup(os.unlink, tmp)
        with patch("jobs.execution.container._project_opencode_json_path") as fake_path:
            fake_path.return_value = Path(tmp)
            from jobs.execution.container import _inject_opencode_config

            with self.assertRaises(ContainerError) as ctx:
                _inject_opencode_config(container, task_id=1)
        self.assertIn("Failed to inject OpenCode config", str(ctx.exception))

    def test_a_missing_root_config_fails_closed_when_restricted(self):
        from jobs.execution.container import _inject_opencode_config

        container = sandbox_container_mock()
        with patch("jobs.execution.container._project_opencode_json_path") as fake_path:
            fake_path.return_value = Path("/nonexistent/opencode.json")
            with self.assertRaises(ContainerError) as ctx:
                _inject_opencode_config(container, task_id=1)
        self.assertIn("opencode.json not found", str(ctx.exception))

    def test_a_missing_root_config_warns_when_unrestricted(self):
        from jobs.execution.container import _inject_opencode_config

        container = sandbox_container_mock()
        with override_settings(SANDBOX_NETWORK_RESTRICTED=False):
            with patch("jobs.execution.container._project_opencode_json_path") as fake_path:
                fake_path.return_value = Path("/nonexistent/opencode.json")
                with self.assertLogs("jobs.execution.container", level="WARNING"):
                    _inject_opencode_config(container, task_id=1)
        # Nothing staged, but the run is not failed: debugging mode.
        self.assertEqual(
            staged_files(container).get("/home/jiffy/.config/opencode/opencode.json"),
            None,
        )


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
        """The request may be elided; the contract never is — it is a separate file."""
        system_prompt = build_system_prompt({
            "repo": {"url": "https://github.com/user/repo"},
            "issue": {"text": "Do the thing", "external_issue_id": "1"},
            "callback": {"url": "https://example.com/cb", "secret": "sec"},
        })
        for section in (
            "## Callback Delivery",
            "## Required Final Output",
            ".jiffy_result.json",
            "## When Something Is Unclear",
        ):
            self.assertIn(section, system_prompt)
        for body in ("tiny", "Fix it.\n" + ("history line\n" * 40000)):
            instructions = self._instructions(body)
            self.assertIn("standing procedure", instructions)


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
                task_document={"issue_text": "the request", "issue_text_elided_in_prompt": False},
            )
        message = "\n".join(cm.output)
        self.assertIn("Task handed off", message)
        self.assertIn("sha256:", message)
        self.assertIn("via stdin", message)

    def test_a_by_reference_handoff_is_logged_as_such(self):
        container = sandbox_container_mock([{"Running": False, "ExitCode": 0}])
        with self.assertLogs("jobs.execution.container", level="INFO") as cm:
            run_agent_in_container(
                container,
                "the prompt",
                task_id=3,
                task_document={"issue_text": "x" * 100, "issue_text_elided_in_prompt": True},
            )
        self.assertIn("elided", "\n".join(cm.output))


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

    def _payload(self):
        return {
            "repo": {"url": "https://github.com/user/repo"},
            "issue": {"text": "Do the thing", "external_issue_id": "1"},
            "callback": {"url": "https://example.com/cb", "secret": "sec"},
        }

    def test_the_result_file_is_a_numbered_step(self):
        system_prompt = build_system_prompt(self._payload())
        self.assertIn("9. **Write the result file**", system_prompt)

    def test_the_agent_is_told_chat_output_reaches_nobody(self):
        collapsed = " ".join(build_system_prompt(self._payload()).split())
        self.assertIn("Nobody is reading your chat output", collapsed)
        self.assertIn("Answering in chat instead of doing those is the same as doing nothing", collapsed)


class PromptDeliveryVerificationTest(TestCase):
    """Staging proves the file landed; this proves the agent gets it.

    What the agent consumes is a stdin redirect inside a login shell. The
    byte-exact read-back catches a truncated or missing file before the agent
    starts — an empty prompt surfaces only as the agent answering "what would
    you like me to work on?", the same symptom as a dozen unrelated faults.
    """

    def _container(self, delivered_bytes=None):
        container = sandbox_container_mock([{"Running": False, "ExitCode": 0}])
        if delivered_bytes is not None:
            inner = container.exec_run.side_effect

            def _exec_run(cmd=None, **kwargs):
                if cmd and "wc -c" in cmd[-1]:
                    return 0, (str(delivered_bytes).encode(), b"")
                return inner(cmd=cmd, **kwargs)

            container.exec_run.side_effect = _exec_run
        return container

    def test_a_prompt_that_survives_delivery_is_accepted(self):
        container = self._container()
        with self.assertLogs("jobs.execution.container", level="INFO") as cm:
            run_agent_in_container(container, "the whole prompt", task_id=1)
        self.assertIn("Prompt delivery verified", "\n".join(cm.output))

    def test_an_empty_delivery_fails_the_task(self):
        """The exact fault behind "what would you like me to work on?"."""
        container = self._container(delivered_bytes=0)
        with self.assertRaises(ContainerError) as ctx:
            run_agent_in_container(container, "the whole prompt", task_id=1)
        self.assertIn("does not survive delivery", str(ctx.exception))

    def test_a_partial_delivery_fails_the_task(self):
        container = self._container(delivered_bytes=5)
        with self.assertRaises(ContainerError) as ctx:
            run_agent_in_container(container, "the whole prompt", task_id=1)
        self.assertIn("incomplete or empty task", str(ctx.exception))

    def test_the_agent_never_starts_on_a_broken_delivery(self):
        container = self._container(delivered_bytes=0)
        with self.assertRaises(ContainerError):
            run_agent_in_container(container, "the whole prompt", task_id=1)
        container.client.api.exec_create.assert_not_called()

    def test_trailing_newlines_are_not_counted_as_loss(self):
        """Delivery is verbatim stdin, so newlines are not stripped anywhere."""
        container = sandbox_container_mock([{"Running": False, "ExitCode": 0}])
        run_agent_in_container(container, "prompt text\n\n\n", task_id=1)
        container.client.api.exec_create.assert_called_once()

    def test_the_command_logs_what_it_hands_over(self):
        cmd = build_agent_command()
        self.assertIn("bytes of prompt to opencode", cmd)
        self.assertIn('opencode run --auto <', cmd)


class PromptShapeTest(TestCase):
    """A small model must meet the task before any boilerplate."""

    def _instructions(self):
        return build_agent_instructions({
            "repo": {"url": "https://github.com/user/repo"},
            "issue": {"text": "Add a health-check tool", "external_issue_id": "1"},
            "callback": {"url": "https://example.com/cb", "secret": "sec"},
        })

    def test_the_task_is_the_first_thing_in_the_prompt(self):
        instructions = self._instructions()
        self.assertTrue(instructions.startswith("# YOUR TASK"))

    def test_the_request_comes_before_the_procedure(self):
        """The request is read first; the procedure is pointed at, after it."""
        instructions = self._instructions()
        self.assertLess(
            instructions.index(ISSUE_END_MARKER),
            instructions.index("standing procedure"),
        )
        self.assertLess(
            instructions.index(ISSUE_END_MARKER),
            instructions.index(SYSTEM_PROMPT_PATH),
        )

    def test_the_request_appears_within_the_first_lines(self):
        """Not buried under a wall of preamble."""
        instructions = self._instructions()
        head = "\n".join(instructions.splitlines()[:12])
        self.assertIn(ISSUE_BEGIN_MARKER, head)

    def test_the_header_forbids_asking_what_to_work_on(self):
        collapsed = " ".join(self._instructions().split())
        self.assertIn("Do not reply asking what to work on", collapsed)
