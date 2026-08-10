"""Tests for how the agent process is executed inside the sandbox container."""

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from jobs.execution.container import run_agent_in_container
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
