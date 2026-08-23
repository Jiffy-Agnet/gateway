"""Tests for how the agent process is executed inside the sandbox container."""

import io
import tarfile
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from jobs.execution.agent import build_inline_instructions
from jobs.execution.container import (
    INLINE_PROMPT_PATH,
    INSTRUCTIONS_PATH,
    MAX_INLINE_INSTRUCTIONS_BYTES,
    build_agent_command,
    run_agent_in_container,
    write_instructions_file,
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


class InstructionsStagingTest(TestCase):
    """The instructions text must not be capped by any shell/argv limit.

    An issue thread is the whole conversation — issue body plus every comment —
    so it can be far larger than the kernel's per-argument ceiling
    (MAX_ARG_STRLEN, 128 KiB).  Staging it through a shell command, as the
    Gateway used to, made a large thread fail with "Argument list too long".
    """

    def test_instructions_are_uploaded_not_echoed_through_a_shell(self):
        container = sandbox_container_mock()
        text = "x" * (4 * 1024 * 1024)

        write_instructions_file(container, text)

        container.put_archive.assert_called_once()
        path, archive = container.put_archive.call_args.args
        self.assertEqual(path, "/tmp")
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            member = tar.getmember("jiffy_instructions.txt")
            self.assertEqual(
                tar.extractfile(member).read().decode("utf-8"), text
            )
        # The only shell command is the size read-back; the content itself
        # never goes through one — that is what imposed the old limit.
        for call in container.exec_run.call_args_list:
            self.assertEqual(call.kwargs["cmd"][0], "stat")

    def test_shell_metacharacters_survive_verbatim(self):
        container = sandbox_container_mock()
        text = "quotes ' \" and $(rm -rf /) and \\backslash\\ and\nnewlines"

        write_instructions_file(container, text)

        _, archive = container.put_archive.call_args.args
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            body = tar.extractfile(tar.getmember("jiffy_instructions.txt")).read()
        self.assertEqual(body.decode("utf-8"), text)

    def test_upload_rejection_raises_container_error(self):
        container = sandbox_container_mock()
        container.put_archive.side_effect = None
        container.put_archive.return_value = False
        with self.assertRaises(ContainerError):
            write_instructions_file(container, "hello")

    def test_prompt_is_read_from_a_file_not_embedded_in_the_command(self):
        """The command is an argv entry too — inlining the text would cap it."""
        cmd = build_agent_command()
        self.assertIn(f'"$(cat {INLINE_PROMPT_PATH})"', cmd)
        self.assertLess(len(cmd.encode("utf-8")), 4096)

    def test_a_multi_megabyte_thread_still_runs(self):
        """End-to-end: a huge thread reaches the agent instead of blowing up."""
        container = sandbox_container_mock([{"Running": False, "ExitCode": 0}])

        instructions = "y" * (2 * 1024 * 1024)
        run_agent_in_container(container, instructions, task_id=7)

        staged = staged_files(container)
        self.assertEqual(
            len(staged[INSTRUCTIONS_PATH].encode("utf-8")),
            len(instructions.encode("utf-8")),
        )
        run_cmd = container.client.api.exec_create.call_args.kwargs["cmd"][3]
        self.assertLess(len(run_cmd.encode("utf-8")), 4096)


class InlinePromptTest(TestCase):
    """`opencode run` reads its prompt from argv only, so the argv copy has to
    fit — but it must never be a bare pointer the agent has to act on faith."""

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
                "external_issue_id": "1",
            },
            "callback": {"url": "https://example.com/cb", "secret": "sec"},
        }

    def test_a_prompt_that_fits_is_used_unchanged(self):
        from jobs.execution.agent import build_agent_instructions

        payload = self._payload("Fix the deploy script")
        full = build_agent_instructions(payload)
        inline = build_inline_instructions(payload, full, MAX_INLINE_INSTRUCTIONS_BYTES)
        self.assertEqual(inline, full)

    def test_an_oversized_prompt_is_trimmed_to_fit(self):
        from jobs.execution.agent import build_agent_instructions

        payload = self._payload("Fix the deploy script.\n" + ("history line\n" * 20000))
        full = build_agent_instructions(payload)
        self.assertGreater(len(full.encode("utf-8")), MAX_INLINE_INSTRUCTIONS_BYTES)

        inline = build_inline_instructions(payload, full, MAX_INLINE_INSTRUCTIONS_BYTES)
        self.assertLessEqual(
            len(inline.encode("utf-8")), MAX_INLINE_INSTRUCTIONS_BYTES
        )

    def test_the_trimmed_prompt_still_carries_the_start_of_the_request(self):
        """The old bare pointer left the agent with no task at all."""
        from jobs.execution.agent import build_agent_instructions

        payload = self._payload("Fix the deploy script.\n" + ("history line\n" * 20000))
        full = build_agent_instructions(payload)
        inline = build_inline_instructions(payload, full, MAX_INLINE_INSTRUCTIONS_BYTES)
        self.assertIn("Fix the deploy script.", inline)

    def test_the_trimmed_prompt_keeps_the_whole_contract(self):
        from jobs.execution.agent import build_agent_instructions

        payload = self._payload("Fix it.\n" + ("history line\n" * 20000))
        full = build_agent_instructions(payload)
        inline = build_inline_instructions(payload, full, MAX_INLINE_INSTRUCTIONS_BYTES)
        for section in (
            "## Callback Delivery",
            "## Required Final Output",
            ".jiffy_result.json",
            "## Asking a Question",
        ):
            self.assertIn(section, inline)

    def test_the_trimmed_prompt_says_where_the_rest_is(self):
        from jobs.execution.agent import build_agent_instructions

        payload = self._payload("Fix it.\n" + ("history line\n" * 20000))
        full = build_agent_instructions(payload)
        inline = build_inline_instructions(payload, full, MAX_INLINE_INSTRUCTIONS_BYTES)
        self.assertIn(INSTRUCTIONS_PATH, inline)
        self.assertIn("too large to include in full here", inline)
        self.assertIn("Do not ask anyone to re-send it", inline)

    def test_the_fenced_region_is_never_empty(self):
        """The exact symptom to prevent: markers present, nothing between them."""
        from jobs.execution.agent import (
            ISSUE_BEGIN_MARKER,
            ISSUE_END_MARKER,
            build_agent_instructions,
        )

        for body in ("short", "Fix it.\n" + ("history line\n" * 20000)):
            payload = self._payload(body)
            full = build_agent_instructions(payload)
            inline = build_inline_instructions(
                payload, full, MAX_INLINE_INSTRUCTIONS_BYTES
            )
            for rendered in (full, inline):
                fenced = rendered.split(ISSUE_BEGIN_MARKER)[1].split(ISSUE_END_MARKER)[0]
                self.assertTrue(fenced.strip(), "issue region rendered empty")

    def test_both_copies_are_staged_and_the_file_holds_everything(self):
        from jobs.execution.agent import build_agent_instructions

        payload = self._payload("Fix it.\n" + ("history line\n" * 20000))
        full = build_agent_instructions(payload)
        inline = build_inline_instructions(payload, full, MAX_INLINE_INSTRUCTIONS_BYTES)

        container = sandbox_container_mock([{"Running": False, "ExitCode": 0}])
        run_agent_in_container(
            container, full, task_id=1, inline_instructions=inline
        )

        staged = staged_files(container)
        self.assertEqual(staged[INSTRUCTIONS_PATH], full)
        self.assertEqual(staged[INLINE_PROMPT_PATH], inline)


class StagedFileVerificationTest(TestCase):
    """The Gateway must prove the prompt landed, not infer it from behaviour."""

    def test_a_truncated_upload_fails_the_task(self):
        container = sandbox_container_mock()

        def _short_stat(cmd=None, **kwargs):
            if cmd and cmd[0] == "stat":
                return 0, (b"5", b"")
            return 0, (b'{"model": "m"}', b"")

        container.exec_run.side_effect = _short_stat

        with self.assertRaises(ContainerError) as ctx:
            write_instructions_file(container, "a much longer instruction text")
        self.assertIn("truncated prompt", str(ctx.exception))

    def test_a_missing_file_fails_the_task(self):
        container = sandbox_container_mock()

        def _missing(cmd=None, **kwargs):
            if cmd and cmd[0] == "stat":
                return 1, (b"", b"No such file or directory")
            return 0, (b'{"model": "m"}', b"")

        container.exec_run.side_effect = _missing

        with self.assertRaises(ContainerError) as ctx:
            write_instructions_file(container, "instructions")
        self.assertIn("missing after upload", str(ctx.exception))

    def test_delivery_is_logged_with_sizes(self):
        container = sandbox_container_mock([{"Running": False, "ExitCode": 0}])
        with self.assertLogs("jobs.execution.container", level="INFO") as cm:
            run_agent_in_container(container, "do the thing", task_id=3)
        self.assertIn("Prompt delivered in full", "\n".join(cm.output))

    def test_a_trimmed_delivery_is_logged_as_such(self):
        container = sandbox_container_mock([{"Running": False, "ExitCode": 0}])
        with self.assertLogs("jobs.execution.container", level="WARNING") as cm:
            run_agent_in_container(
                container, "x" * 5000, task_id=3, inline_instructions="short"
            )
        self.assertIn("too large for argv", "\n".join(cm.output))
