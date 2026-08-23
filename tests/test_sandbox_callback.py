"""Tests for the deterministic callback wrapper the sandbox runs.

The wrapper is loaded from its file rather than imported as a package module:
it ships into the container as a standalone stdlib-only script, and these
tests exercise it exactly as the sandbox does.
"""

import importlib.util
import io
import json
import tarfile
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TestCase

from jobs.callback_retry import (
    BASE_DELAY_SECONDS,
    MAX_ATTEMPTS,
    delay_before_attempt,
    is_success,
    is_transient_status,
    policy_for_sandbox,
)
from jobs.callback_specs import build_sandbox_callback_config, get_callback_spec
from jobs.execution.container import (
    CALLBACK_CONFIG_PATH,
    CALLBACK_SCRIPT_PATH,
    SANDBOX_CALLBACK_SCRIPT_SOURCE,
    run_agent_in_container,
    stage_callback_wrapper,
)
from jobs.execution.exceptions import ContainerError


def _load_wrapper():
    """Import the staged script from disk, the way the sandbox runs it."""
    spec = importlib.util.spec_from_file_location(
        "jiffy_callback_under_test", SANDBOX_CALLBACK_SCRIPT_SOURCE
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wrapper = _load_wrapper()


def _http_error(code):
    return urllib.error.HTTPError(
        url="https://example.com/cb", code=code, msg="err", hdrs=None, fp=None
    )


def _ok(code=201):
    response = MagicMock()
    response.getcode.return_value = code
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    return response


class RetryPolicyTest(TestCase):
    """The policy both senders share."""

    def test_success_is_anything_below_300(self):
        self.assertTrue(is_success(200))
        self.assertTrue(is_success(201))
        self.assertFalse(is_success(400))

    def test_server_errors_and_busy_signals_are_transient(self):
        for code in (500, 502, 503, 504, 408, 429):
            self.assertTrue(is_transient_status(code), code)

    def test_client_errors_are_permanent(self):
        for code in (400, 401, 403, 404, 409, 422):
            self.assertFalse(is_transient_status(code), code)

    def test_success_is_never_transient(self):
        self.assertFalse(is_transient_status(200))

    def test_backoff_is_exponential_from_the_existing_delay(self):
        self.assertEqual(delay_before_attempt(1), 0.0)
        self.assertEqual(delay_before_attempt(2), BASE_DELAY_SECONDS)
        self.assertEqual(delay_before_attempt(3), BASE_DELAY_SECONDS * 2)

    def test_total_wait_stays_well_inside_the_run_budget(self):
        total = sum(delay_before_attempt(a) for a in range(1, MAX_ATTEMPTS + 1))
        self.assertLess(total, 60)


class WrapperDeliveryTest(TestCase):
    """Delivery, retries, and the outcome the wrapper reports."""

    def _config(self, **overrides):
        config = {
            "method": "POST",
            "url": "https://example.com/cb",
            "headers": {"Authorization": "Bearer sec"},
            "body_format": "json",
            "body_text_field": "body",
        }
        config.update(policy_for_sandbox())
        config.update(overrides)
        return config

    def _deliver(self, side_effect, **overrides):
        with patch.object(wrapper.urllib.request, "urlopen") as urlopen, \
                patch.object(wrapper.time, "sleep") as sleep:
            urlopen.side_effect = side_effect
            result = wrapper.deliver(self._config(**overrides), "the report")
        return result, urlopen, sleep

    def test_first_attempt_success_does_not_retry(self):
        result, urlopen, sleep = self._deliver([_ok()])
        self.assertEqual(result, {"attempted": True, "succeeded": True, "error": None})
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_transient_failure_is_retried_then_succeeds(self):
        result, urlopen, sleep = self._deliver([_http_error(503), _ok()])
        self.assertTrue(result["succeeded"])
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(BASE_DELAY_SECONDS)

    def test_connection_error_is_retried(self):
        result, urlopen, _ = self._deliver(
            [urllib.error.URLError("connection refused"), _ok()]
        )
        self.assertTrue(result["succeeded"])
        self.assertEqual(urlopen.call_count, 2)

    def test_timeout_is_retried(self):
        result, urlopen, _ = self._deliver([TimeoutError("timed out"), _ok()])
        self.assertTrue(result["succeeded"])
        self.assertEqual(urlopen.call_count, 2)

    def test_client_error_fails_immediately(self):
        """A 4xx answers the same way however often it is asked."""
        for code in (400, 401, 403, 404, 422):
            result, urlopen, sleep = self._deliver([_http_error(code)])
            self.assertFalse(result["succeeded"])
            self.assertEqual(urlopen.call_count, 1, code)
            sleep.assert_not_called()
            self.assertIn("client error", result["error"])
            self.assertIn(str(code), result["error"])

    def test_retryable_client_statuses_are_retried(self):
        for code in (408, 429):
            result, urlopen, _ = self._deliver([_http_error(code), _ok()])
            self.assertTrue(result["succeeded"], code)
            self.assertEqual(urlopen.call_count, 2, code)

    def test_exhaustion_reports_the_last_error(self):
        result, urlopen, sleep = self._deliver([_http_error(500)] * MAX_ATTEMPTS)
        self.assertEqual(
            result,
            {
                "attempted": True,
                "succeeded": False,
                "error": "all 3 attempts failed; last error: HTTP 500",
            },
        )
        self.assertEqual(urlopen.call_count, MAX_ATTEMPTS)
        self.assertEqual(sleep.call_count, MAX_ATTEMPTS - 1)

    def test_backoff_between_attempts_is_exponential(self):
        _, _, sleep = self._deliver([_http_error(500)] * MAX_ATTEMPTS)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [BASE_DELAY_SECONDS, BASE_DELAY_SECONDS * 2],
        )

    def test_it_never_waits_forever(self):
        """Every attempt carries the configured per-request timeout."""
        with patch.object(wrapper.urllib.request, "urlopen") as urlopen, \
                patch.object(wrapper.time, "sleep"):
            urlopen.return_value = _ok()
            wrapper.deliver(self._config(timeout_seconds=7), "the report")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 7)

    def test_attempts_are_logged_individually(self):
        with patch.object(wrapper.urllib.request, "urlopen") as urlopen, \
                patch.object(wrapper.time, "sleep"), \
                patch.object(wrapper.sys, "stderr", new=io.StringIO()) as err:
            urlopen.side_effect = [_http_error(500), _ok()]
            wrapper.deliver(self._config(), "the report")
        lines = err.getvalue()
        self.assertIn("attempt 1/3", lines)
        self.assertIn("HTTP 500", lines)
        self.assertIn("attempt 2/3", lines)
        self.assertIn("delivered on attempt 2/3", lines)

    def test_exhaustion_is_logged(self):
        with patch.object(wrapper.urllib.request, "urlopen") as urlopen, \
                patch.object(wrapper.time, "sleep"), \
                patch.object(wrapper.sys, "stderr", new=io.StringIO()) as err:
            urlopen.side_effect = [_http_error(500)] * MAX_ATTEMPTS
            wrapper.deliver(self._config(), "the report")
        self.assertIn("all 3 attempts failed — giving up", err.getvalue())


class WrapperBodyTest(TestCase):
    """The wire body must match what the provider's endpoint expects."""

    def test_json_providers_get_the_text_wrapped(self):
        body = wrapper.build_wire_body(
            {"body_format": "json", "body_text_field": "body"}, "hello ✅"
        )
        self.assertEqual(json.loads(body.decode("utf-8")), {"body": "hello ✅"})

    def test_text_providers_get_raw_utf8(self):
        body = wrapper.build_wire_body({"body_format": "text"}, "hello ✅")
        self.assertEqual(body, "hello ✅".encode("utf-8"))


class WrapperCliTest(TestCase):
    """Exit codes and stdout, which are the agent's whole interface to it."""

    def _run(self, tmp_path, side_effect, body="the report"):
        config_file = tmp_path / "config.json"
        config = {
            "method": "POST",
            "url": "https://example.com/cb",
            "headers": {},
            "body_format": "json",
            "body_text_field": "body",
        }
        config.update(policy_for_sandbox())
        config_file.write_text(json.dumps(config), encoding="utf-8")
        body_file = tmp_path / "body.md"
        body_file.write_text(body, encoding="utf-8")

        with patch.object(wrapper.urllib.request, "urlopen") as urlopen, \
                patch.object(wrapper.time, "sleep"), \
                patch.object(wrapper.sys, "stdout", new=io.StringIO()) as out, \
                patch.object(wrapper.sys, "stderr", new=io.StringIO()):
            urlopen.side_effect = side_effect
            code = wrapper.main(
                ["--body-file", str(body_file), "--config", str(config_file)]
            )
        return code, out.getvalue()

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def test_success_exits_zero_and_prints_the_callback_object(self):
        code, out = self._run(self.tmp_path, [_ok()])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out), {"attempted": True, "succeeded": True, "error": None}
        )

    def test_exhaustion_exits_one_but_still_prints_a_usable_object(self):
        code, out = self._run(self.tmp_path, [_http_error(500)] * MAX_ATTEMPTS)
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertTrue(payload["attempted"])
        self.assertFalse(payload["succeeded"])
        self.assertIn("attempts failed", payload["error"])

    def test_missing_config_exits_two_without_posting(self):
        body_file = self.tmp_path / "body.md"
        body_file.write_text("x", encoding="utf-8")
        with patch.object(wrapper.urllib.request, "urlopen") as urlopen, \
                patch.object(wrapper.sys, "stderr", new=io.StringIO()):
            code = wrapper.main(
                ["--body-file", str(body_file), "--config", "/nonexistent.json"]
            )
        self.assertEqual(code, 2)
        urlopen.assert_not_called()

    def test_blank_body_is_refused(self):
        code, out = self._run(self.tmp_path, [_ok()], body="   \n")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")


class CallbackConfigTest(TestCase):
    """The config the Gateway stages must describe the endpoint and the policy."""

    def test_config_carries_endpoint_and_policy(self):
        config = build_sandbox_callback_config(
            get_callback_spec("github"),
            callback_url="https://api.github.com/repos/u/r/issues/1/comments",
            callback_secret="sec",
        )
        self.assertEqual(config["method"], "POST")
        self.assertEqual(config["body_format"], "json")
        self.assertEqual(config["headers"]["Authorization"], "Bearer sec")
        self.assertEqual(config["headers"]["Accept"], "application/vnd.github+json")
        self.assertEqual(config["max_attempts"], MAX_ATTEMPTS)
        self.assertEqual(config["base_delay_seconds"], BASE_DELAY_SECONDS)

    def test_text_providers_are_described_as_text(self):
        config = build_sandbox_callback_config(
            get_callback_spec("gitlab"), callback_url="https://gl/cb", callback_secret="s"
        )
        self.assertEqual(config["body_format"], "text")


class StagingTest(TestCase):
    """The wrapper has to actually reach the container."""

    def _staged(self, container):
        staged = {}
        for call in container.put_archive.call_args_list:
            directory, archive = call.args
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                for member in tar.getmembers():
                    path = f"{directory.rstrip('/')}/{member.name}"
                    staged[path] = tar.extractfile(member).read().decode("utf-8")
        return staged

    def test_script_and_config_are_uploaded(self):
        container = MagicMock()
        config = build_sandbox_callback_config(
            get_callback_spec("github"), callback_url="https://x/cb", callback_secret="s"
        )
        stage_callback_wrapper(container, config, task_id=1)

        staged = self._staged(container)
        self.assertIn(CALLBACK_SCRIPT_PATH, staged)
        self.assertIn(CALLBACK_CONFIG_PATH, staged)
        self.assertEqual(
            staged[CALLBACK_SCRIPT_PATH],
            SANDBOX_CALLBACK_SCRIPT_SOURCE.read_text(encoding="utf-8"),
        )
        self.assertEqual(json.loads(staged[CALLBACK_CONFIG_PATH])["url"], "https://x/cb")

    def test_script_is_staged_executable(self):
        container = MagicMock()
        stage_callback_wrapper(container, {"url": "https://x/cb"}, task_id=1)
        directory, archive = container.put_archive.call_args_list[0].args
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            self.assertEqual(tar.getmembers()[0].mode, 0o755)

    def test_upload_rejection_raises(self):
        container = MagicMock()
        container.put_archive.return_value = False
        with self.assertRaises(ContainerError):
            stage_callback_wrapper(container, {"url": "https://x/cb"}, task_id=1)

    def test_run_stages_the_wrapper_before_the_agent_starts(self):
        container = MagicMock()
        container.short_id = "abc123"
        container.exec_run.return_value = (0, (b'{"model": "m"}', b""))
        api = container.client.api
        api.exec_create.return_value = {"Id": "exec-1"}
        api.exec_inspect.side_effect = [{"Running": False, "ExitCode": 0}]

        config = build_sandbox_callback_config(
            get_callback_spec("github"), callback_url="https://x/cb", callback_secret="s"
        )
        run_agent_in_container(container, "do it", task_id=1, callback_config=config)

        self.assertIn(CALLBACK_SCRIPT_PATH, self._staged(container))

    def test_run_without_a_config_stages_only_the_instructions(self):
        """An unknown provider still runs; the Gateway fallback covers reporting."""
        container = MagicMock()
        container.short_id = "abc123"
        container.exec_run.return_value = (0, (b'{"model": "m"}', b""))
        api = container.client.api
        api.exec_create.return_value = {"Id": "exec-1"}
        api.exec_inspect.side_effect = [{"Running": False, "ExitCode": 0}]

        run_agent_in_container(container, "do it", task_id=1)

        self.assertNotIn(CALLBACK_SCRIPT_PATH, self._staged(container))


class InstructionsTest(TestCase):
    """The prompt must hand delivery to the wrapper, not describe a retry policy."""

    def _instructions(self):
        from jobs.execution.agent import build_agent_instructions

        return build_agent_instructions({
            "repo": {"url": "https://github.com/user/repo"},
            "issue": {"text": "Do the thing", "external_issue_id": "1"},
            "callback": {
                "url": "https://api.github.com/repos/u/r/issues/1/comments",
                "secret": "sec",
            },
        })

    def test_agent_is_told_to_run_the_wrapper(self):
        instructions = self._instructions()
        self.assertIn(f"python3 {CALLBACK_SCRIPT_PATH} --body-file", instructions)
        self.assertIn("Run the wrapper **exactly once**", instructions)

    def test_agent_is_forbidden_from_rolling_its_own_delivery(self):
        instructions = self._instructions()
        collapsed = " ".join(instructions.split())
        self.assertIn(
            "Do **not** write your own HTTP request, curl command, or retry loop",
            collapsed,
        )
        self.assertIn("Delivery policy is not yours to decide", collapsed)

    def test_prompt_carries_no_retry_decision_logic(self):
        """Retry is deterministic code; the prompt must not restate a policy."""
        instructions = self._instructions()
        for leaked in ("exponential", "backoff", "attempts", "5xx", "4xx"):
            self.assertNotIn(leaked, instructions.lower())

    def test_agent_copies_the_wrappers_verdict_verbatim(self):
        instructions = self._instructions()
        self.assertIn("Do **not** invent the `callback` object", instructions)
        self.assertIn("Use exactly what the wrapper", instructions)


class MissingWrapperSourceTest(TestCase):
    """A missing script must fail as a clear execution error, not a stray OSError."""

    def test_unreadable_source_raises_container_error(self):
        container = MagicMock()
        with patch(
            "jobs.execution.container.SANDBOX_CALLBACK_SCRIPT_SOURCE",
            Path("/nonexistent/jiffy_callback.py"),
        ):
            with self.assertRaises(ContainerError) as ctx:
                stage_callback_wrapper(container, {"url": "https://x/cb"}, task_id=1)
        self.assertIn("Callback wrapper source is missing", str(ctx.exception))
