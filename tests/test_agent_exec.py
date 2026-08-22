"""Tests for how the agent process is executed inside the sandbox container."""

import io
import tarfile
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from jobs.execution.container import (
    MAX_INLINE_INSTRUCTIONS_BYTES,
    build_agent_command,
    run_agent_in_container,
    write_instructions_file,
)
from jobs.execution.exceptions import ContainerError


class RunAgentInContainerTest(TestCase):
    """The agent exec is started detached and polled to completion.

    A blocking exec used to hold one Docker API connection open for the whole
    run, which the socket proxy cut at 10 minutes — the caller then saw an
    exec that was still "Running" with a null exit code ("exited with code
    None") even though the agent was alive.  These tests pin the detached
    behaviour and the Gateway-enforced timeout that replaced it.
    """

    def _container(self, inspect_results):
        container = MagicMock()
        container.short_id = "abc123"
        # Serves both the opencode-config read and the instructions write.
        container.exec_run.return_value = (0, (b'{"model": "test/model"}', b""))
        api = container.client.api
        api.exec_create.return_value = {"Id": "exec-1"}
        api.exec_inspect.side_effect = list(inspect_results)
        return container

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
        container = MagicMock()
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
        # Nothing may go through a shell: that is what imposed the old limit.
        container.exec_run.assert_not_called()

    def test_shell_metacharacters_survive_verbatim(self):
        container = MagicMock()
        text = "quotes ' \" and $(rm -rf /) and \\backslash\\ and\nnewlines"

        write_instructions_file(container, text)

        _, archive = container.put_archive.call_args.args
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            body = tar.extractfile(tar.getmember("jiffy_instructions.txt")).read()
        self.assertEqual(body.decode("utf-8"), text)

    def test_upload_rejection_raises_container_error(self):
        container = MagicMock()
        container.put_archive.return_value = False
        with self.assertRaises(ContainerError):
            write_instructions_file(container, "hello")

    def test_small_instructions_are_passed_inline(self):
        cmd = build_agent_command(1024)
        self.assertIn('opencode run --auto "$(cat /tmp/jiffy_instructions.txt)"', cmd)

    def test_oversized_instructions_are_passed_by_path(self):
        cmd = build_agent_command(MAX_INLINE_INSTRUCTIONS_BYTES + 1)
        self.assertNotIn("$(cat", cmd)
        self.assertIn("/tmp/jiffy_instructions.txt", cmd)
        # The prompt argv must stay well under the kernel's per-argument cap.
        self.assertLess(len(cmd.encode("utf-8")), 4096)

    def test_a_multi_megabyte_thread_still_runs(self):
        """End-to-end: a huge thread reaches the agent instead of blowing up."""
        container = MagicMock()
        container.short_id = "abc123"
        container.exec_run.return_value = (0, (b'{"model": "test/model"}', b""))
        api = container.client.api
        api.exec_create.return_value = {"Id": "exec-1"}
        api.exec_inspect.side_effect = [{"Running": False, "ExitCode": 0}]

        instructions = "y" * (2 * 1024 * 1024)
        run_agent_in_container(container, instructions, task_id=7)

        _, archive = container.put_archive.call_args.args
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            staged = tar.extractfile(tar.getmember("jiffy_instructions.txt")).read()
        self.assertEqual(len(staged), len(instructions.encode("utf-8")))
        run_cmd = api.exec_create.call_args.kwargs["cmd"][3]
        self.assertLess(len(run_cmd.encode("utf-8")), 4096)
