"""Tests for ingestion endpoints."""

import json
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, TestCase

from apps.ingestion.serializers import IngestionPayloadSerializer
from apps.ingestion.views import (
    GiteaIngestView,
    GitHubIngestView,
    GitLabIngestView,
    _store_payload,
)
from jobs.models import Task

AUTH_HEADER = "HTTP_X_JIFFY_TOKEN"


class TestGitHubIngest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.secret = "test-github-secret"
        self.payload = {
            "repo": {
                "url": "https://github.com/user/repo",
                "token": "ghp_test_token",
            },
            "issue": {
                "text": "Fix the bug in module X",
                "external_issue_id": "123",
            },
            "callback": {
                "url": "https://example.com/callback",
                "secret": "callback-secret-123",
            },
        }

    @patch.dict("os.environ", {"GITHUB_INGEST_TOKEN": "test-github-secret"})
    @patch("apps.ingestion.views.execute_task")
    @patch("apps.ingestion.views.get_redis")
    @patch("apps.ingestion.views.transaction")
    def test_valid_request_returns_202(self, mock_transaction, mock_redis, mock_task):
        mock_redis.return_value = MagicMock(set=MagicMock(return_value=True))
        mock_task.apply_async.return_value = MagicMock(id="celery-123")
        mock_transaction.on_commit.side_effect = lambda cb: cb()
        mock_transaction.atomic.return_value.__enter__ = MagicMock()
        mock_transaction.atomic.return_value.__exit__ = MagicMock(return_value=False)

        body = json.dumps(self.payload).encode()
        request = self.factory.post(
            "/api/github/ingestion",
            data=body,
            content_type="application/json",
            **{AUTH_HEADER: "test-github-secret"},
        )

        response = GitHubIngestView.as_view()(request)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], "queued")
        self.assertEqual(Task.objects.count(), 1)
        task = Task.objects.first()
        self.assertEqual(task.provider, "github")
        self.assertEqual(task.repo_url, self.payload["repo"]["url"])
        mock_task.apply_async.assert_called_once()

    @patch.dict("os.environ", {"GITHUB_INGEST_TOKEN": "test-github-secret"})
    @patch("apps.ingestion.views.execute_task")
    @patch("apps.ingestion.views.get_redis")
    def test_invalid_token_returns_401(self, mock_redis, mock_task):
        body = json.dumps(self.payload).encode()
        request = self.factory.post(
            "/api/github/ingestion",
            data=body,
            content_type="application/json",
            **{AUTH_HEADER: "wrong"},
        )

        response = GitHubIngestView.as_view()(request)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(Task.objects.count(), 0)

    @patch.dict("os.environ", {"GITHUB_INGEST_TOKEN": "test-github-secret"})
    @patch("apps.ingestion.views.execute_task")
    @patch("apps.ingestion.views.get_redis")
    def test_missing_token_returns_401(self, mock_redis, mock_task):
        body = json.dumps(self.payload).encode()
        request = self.factory.post(
            "/api/github/ingestion",
            data=body,
            content_type="application/json",
        )

        response = GitHubIngestView.as_view()(request)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(Task.objects.count(), 0)

    @patch.dict("os.environ", {"GITHUB_INGEST_TOKEN": "test-github-secret"})
    @patch("apps.ingestion.views.execute_task")
    @patch("apps.ingestion.views.get_redis")
    def test_missing_fields_returns_400(self, mock_redis, mock_task):
        incomplete_payload = {"repo": {"url": "https://github.com/user/repo"}}
        body = json.dumps(incomplete_payload).encode()
        request = self.factory.post(
            "/api/github/ingestion",
            data=body,
            content_type="application/json",
            **{AUTH_HEADER: "test-github-secret"},
        )

        response = GitHubIngestView.as_view()(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Task.objects.count(), 0)

    @patch.dict("os.environ", {"GITHUB_INGEST_TOKEN": "test-github-secret"})
    @patch("apps.ingestion.views.execute_task")
    @patch("apps.ingestion.views.get_redis")
    def test_invalid_url_returns_400(self, mock_redis, mock_task):
        invalid_payload = {
            "repo": {
                "url": "not-a-url",
                "token": "ghp_test_token",
            },
            "issue": {
                "text": "Fix the bug in module X",
                "external_issue_id": "123",
            },
            "callback": {
                "url": "https://example.com/callback",
                "secret": "callback-secret-123",
            },
        }
        body = json.dumps(invalid_payload).encode()
        request = self.factory.post(
            "/api/github/ingestion",
            data=body,
            content_type="application/json",
            **{AUTH_HEADER: "test-github-secret"},
        )

        response = GitHubIngestView.as_view()(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Task.objects.count(), 0)

    @patch.dict("os.environ", {"GITHUB_INGEST_TOKEN": "test-github-secret"})
    @patch("apps.ingestion.views.execute_task")
    @patch("apps.ingestion.views.get_redis")
    def test_non_dict_payload_returns_400(self, mock_redis, mock_task):
        body = json.dumps(["not", "a", "dict"]).encode()
        request = self.factory.post(
            "/api/github/ingestion",
            data=body,
            content_type="application/json",
            **{AUTH_HEADER: "test-github-secret"},
        )

        response = GitHubIngestView.as_view()(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Task.objects.count(), 0)

    @patch.dict("os.environ", {"GITHUB_INGEST_TOKEN": "test-github-secret"})
    @patch("apps.ingestion.views.execute_task")
    @patch("apps.ingestion.views.get_redis")
    @patch("apps.ingestion.views.transaction")
    def test_duplicate_delivery_returns_202_no_new_task(self, mock_transaction, mock_redis, mock_task):
        mock_redis_instance = MagicMock()
        mock_redis_instance.set.side_effect = [True, None, None]
        mock_redis.return_value = mock_redis_instance
        mock_task.apply_async.return_value = MagicMock(id="celery-123")
        mock_transaction.on_commit.side_effect = lambda cb: cb()
        mock_transaction.atomic.return_value.__enter__ = MagicMock()
        mock_transaction.atomic.return_value.__exit__ = MagicMock(return_value=False)

        body = json.dumps(self.payload).encode()
        request1 = self.factory.post(
            "/api/github/ingestion",
            data=body,
            content_type="application/json",
            **{AUTH_HEADER: "test-github-secret"},
        )
        response1 = GitHubIngestView.as_view()(request1)
        self.assertEqual(response1.status_code, 202)

        request2 = self.factory.post(
            "/api/github/ingestion",
            data=body,
            content_type="application/json",
            **{AUTH_HEADER: "test-github-secret"},
        )
        response2 = GitHubIngestView.as_view()(request2)
        self.assertEqual(response2.status_code, 202)
        self.assertEqual(Task.objects.count(), 1)


class TestGitLabIngest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.token = "test-gitlab-token"
        self.payload = {
            "repo": {
                "url": "https://gitlab.com/user/repo",
                "token": "glpat-test-token",
            },
            "issue": {
                "text": "Add feature Y",
                "external_issue_id": "456",
            },
            "callback": {
                "url": "https://example.com/callback",
                "secret": "callback-secret-456",
            },
        }

    @patch.dict("os.environ", {"GITLAB_INGEST_TOKEN": "test-gitlab-token"})
    @patch("apps.ingestion.views.execute_task")
    @patch("apps.ingestion.views.get_redis")
    @patch("apps.ingestion.views.transaction")
    def test_valid_request_returns_202(self, mock_transaction, mock_redis, mock_task):
        mock_redis.return_value = MagicMock(set=MagicMock(return_value=True))
        mock_task.apply_async.return_value = MagicMock(id="celery-456")
        mock_transaction.on_commit.side_effect = lambda cb: cb()
        mock_transaction.atomic.return_value.__enter__ = MagicMock()
        mock_transaction.atomic.return_value.__exit__ = MagicMock(return_value=False)

        body = json.dumps(self.payload).encode()
        request = self.factory.post(
            "/api/gitlab/ingestion",
            data=body,
            content_type="application/json",
            **{AUTH_HEADER: "test-gitlab-token"},
        )

        response = GitLabIngestView.as_view()(request)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(Task.objects.count(), 1)
        task = Task.objects.first()
        self.assertEqual(task.provider, "gitlab")

    @patch.dict("os.environ", {"GITLAB_INGEST_TOKEN": "test-gitlab-token"})
    @patch("apps.ingestion.views.execute_task")
    @patch("apps.ingestion.views.get_redis")
    def test_invalid_token_returns_401(self, mock_redis, mock_task):
        body = json.dumps(self.payload).encode()
        request = self.factory.post(
            "/api/gitlab/ingestion",
            data=body,
            content_type="application/json",
            **{AUTH_HEADER: "wrong-token"},
        )

        response = GitLabIngestView.as_view()(request)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(Task.objects.count(), 0)

    @patch.dict("os.environ", {"GITLAB_INGEST_TOKEN": "test-gitlab-token"})
    @patch("apps.ingestion.views.execute_task")
    @patch("apps.ingestion.views.get_redis")
    def test_missing_token_returns_401(self, mock_redis, mock_task):
        body = json.dumps(self.payload).encode()
        request = self.factory.post(
            "/api/gitlab/ingestion",
            data=body,
            content_type="application/json",
        )

        response = GitLabIngestView.as_view()(request)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(Task.objects.count(), 0)

    @patch.dict("os.environ", {"GITLAB_INGEST_TOKEN": "test-gitlab-token"})
    @patch("apps.ingestion.views.execute_task")
    @patch("apps.ingestion.views.get_redis")
    @patch("apps.ingestion.views.transaction")
    def test_duplicate_delivery_returns_202(self, mock_transaction, mock_redis, mock_task):
        mock_redis_instance = MagicMock()
        mock_redis_instance.set.side_effect = [True, None, None]
        mock_redis.return_value = mock_redis_instance
        mock_task.apply_async.return_value = MagicMock(id="celery-456")
        mock_transaction.on_commit.side_effect = lambda cb: cb()
        mock_transaction.atomic.return_value.__enter__ = MagicMock()
        mock_transaction.atomic.return_value.__exit__ = MagicMock(return_value=False)

        body = json.dumps(self.payload).encode()
        request1 = self.factory.post(
            "/api/gitlab/ingestion",
            data=body,
            content_type="application/json",
            **{AUTH_HEADER: "test-gitlab-token"},
        )
        GitLabIngestView.as_view()(request1)

        request2 = self.factory.post(
            "/api/gitlab/ingestion",
            data=body,
            content_type="application/json",
            **{AUTH_HEADER: "test-gitlab-token"},
        )
        response2 = GitLabIngestView.as_view()(request2)
        self.assertEqual(response2.status_code, 202)
        self.assertEqual(Task.objects.count(), 1)


class TestGiteaIngest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.secret = "test-gitea-secret"
        self.payload = {
            "repo": {
                "url": "https://gitea.com/user/repo",
                "token": "gitea_test_token",
            },
            "issue": {
                "text": "Refactor module Z",
                "external_issue_id": "789",
            },
            "callback": {
                "url": "https://example.com/callback",
                "secret": "callback-secret-789",
            },
        }

    @patch.dict("os.environ", {"GITEA_INGEST_TOKEN": "test-gitea-secret"})
    @patch("apps.ingestion.views.execute_task")
    @patch("apps.ingestion.views.get_redis")
    @patch("apps.ingestion.views.transaction")
    def test_valid_request_returns_202(self, mock_transaction, mock_redis, mock_task):
        mock_redis.return_value = MagicMock(set=MagicMock(return_value=True))
        mock_task.apply_async.return_value = MagicMock(id="celery-789")
        mock_transaction.on_commit.side_effect = lambda cb: cb()
        mock_transaction.atomic.return_value.__enter__ = MagicMock()
        mock_transaction.atomic.return_value.__exit__ = MagicMock(return_value=False)

        body = json.dumps(self.payload).encode()
        request = self.factory.post(
            "/api/gitea/ingestion",
            data=body,
            content_type="application/json",
            **{AUTH_HEADER: "test-gitea-secret"},
        )

        response = GiteaIngestView.as_view()(request)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(Task.objects.count(), 1)
        task = Task.objects.first()
        self.assertEqual(task.provider, "gitea")

    @patch.dict("os.environ", {"GITEA_INGEST_TOKEN": "test-gitea-secret"})
    @patch("apps.ingestion.views.execute_task")
    @patch("apps.ingestion.views.get_redis")
    def test_invalid_token_returns_401(self, mock_redis, mock_task):
        body = json.dumps(self.payload).encode()
        request = self.factory.post(
            "/api/gitea/ingestion",
            data=body,
            content_type="application/json",
            **{AUTH_HEADER: "wrong"},
        )

        response = GiteaIngestView.as_view()(request)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(Task.objects.count(), 0)

    @patch.dict("os.environ", {"GITEA_INGEST_TOKEN": "test-gitea-secret"})
    @patch("apps.ingestion.views.execute_task")
    @patch("apps.ingestion.views.get_redis")
    def test_missing_token_returns_401(self, mock_redis, mock_task):
        body = json.dumps(self.payload).encode()
        request = self.factory.post(
            "/api/gitea/ingestion",
            data=body,
            content_type="application/json",
        )

        response = GiteaIngestView.as_view()(request)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(Task.objects.count(), 0)

    @patch.dict("os.environ", {"GITEA_INGEST_TOKEN": "test-gitea-secret"})
    @patch("apps.ingestion.views.execute_task")
    @patch("apps.ingestion.views.get_redis")
    @patch("apps.ingestion.views.transaction")
    def test_duplicate_delivery_returns_202(self, mock_transaction, mock_redis, mock_task):
        mock_redis_instance = MagicMock()
        mock_redis_instance.set.side_effect = [True, None, None]
        mock_redis.return_value = mock_redis_instance
        mock_task.apply_async.return_value = MagicMock(id="celery-789")
        mock_transaction.on_commit.side_effect = lambda cb: cb()
        mock_transaction.atomic.return_value.__enter__ = MagicMock()
        mock_transaction.atomic.return_value.__exit__ = MagicMock(return_value=False)

        body = json.dumps(self.payload).encode()
        request1 = self.factory.post(
            "/api/gitea/ingestion",
            data=body,
            content_type="application/json",
            **{AUTH_HEADER: "test-gitea-secret"},
        )
        GiteaIngestView.as_view()(request1)

        request2 = self.factory.post(
            "/api/gitea/ingestion",
            data=body,
            content_type="application/json",
            **{AUTH_HEADER: "test-gitea-secret"},
        )
        response2 = GiteaIngestView.as_view()(request2)
        self.assertEqual(response2.status_code, 202)
        self.assertEqual(Task.objects.count(), 1)


class TestStorePayload(TestCase):
    """The Redis payload must survive datetimes produced by DateTimeField."""

    @patch("apps.ingestion.views.get_redis")
    def test_stores_turn_datetimes_as_iso_strings(self, mock_redis):
        serializer = IngestionPayloadSerializer(
            data={
                "repo": {"url": "https://github.com/user/repo", "token": "tok"},
                "issue": {
                    "turns": [
                        {"role": "user", "author": "alice", "body": "Fix it", "created_at": "2025-01-01T00:00:00Z"},
                    ],
                    "external_issue_id": "42",
                },
                "callback": {"url": "https://example.com/cb", "secret": "s"},
            }
        )
        self.assertTrue(serializer.is_valid(), msg=serializer.errors)

        _store_payload(1, serializer.validated_data)

        stored = json.loads(mock_redis.return_value.set.call_args[0][1])
        self.assertEqual(stored["issue"]["turns"][0]["created_at"], "2025-01-01T00:00:00Z")


class TestIngestionPayloadSerializer(TestCase):
    """Tests for the IngestionPayloadSerializer, specifically the turns field."""

    def test_valid_turns_payload(self):
        payload = {
            "repo": {"url": "https://github.com/user/repo", "token": "tok"},
            "issue": {
                "turns": [
                    {"role": "user", "author": "alice", "body": "Fix it", "created_at": "2025-01-01T00:00:00Z"},
                    {"role": "agent", "author": "jiffy-bot", "body": "On it!", "created_at": "2025-01-01T01:00:00Z"},
                ],
                "external_issue_id": "42",
            },
            "callback": {"url": "https://example.com/cb", "secret": "s"},
        }
        serializer = IngestionPayloadSerializer(data=payload)
        self.assertTrue(serializer.is_valid(), msg=serializer.errors)

    def test_valid_turn_with_blank_body(self):
        """A GitHub issue opened with a title but no description sends body: ''."""
        payload = {
            "repo": {"url": "https://github.com/user/repo", "token": "tok"},
            "issue": {
                "turns": [
                    {"role": "user", "author": "alice", "body": "", "created_at": "2025-01-01T00:00:00Z"},
                ],
                "external_issue_id": "42",
            },
            "callback": {"url": "https://example.com/cb", "secret": "s"},
        }
        serializer = IngestionPayloadSerializer(data=payload)
        self.assertTrue(serializer.is_valid(), msg=serializer.errors)

    def test_valid_legacy_text_payload(self):
        payload = {
            "repo": {"url": "https://github.com/user/repo", "token": "tok"},
            "issue": {"text": "Fix the bug", "external_issue_id": "42"},
            "callback": {"url": "https://example.com/cb", "secret": "s"},
        }
        serializer = IngestionPayloadSerializer(data=payload)
        self.assertTrue(serializer.is_valid(), msg=serializer.errors)

    def test_invalid_no_text_no_turns(self):
        payload = {
            "repo": {"url": "https://github.com/user/repo", "token": "tok"},
            "issue": {"external_issue_id": "42"},
            "callback": {"url": "https://example.com/cb", "secret": "s"},
        }
        serializer = IngestionPayloadSerializer(data=payload)
        self.assertFalse(serializer.is_valid())

    def test_invalid_turn_missing_role(self):
        payload = {
            "repo": {"url": "https://github.com/user/repo", "token": "tok"},
            "issue": {
                "turns": [
                    {"author": "alice", "body": "Fix it", "created_at": "2025-01-01T00:00:00Z"},
                ],
                "external_issue_id": "42",
            },
            "callback": {"url": "https://example.com/cb", "secret": "s"},
        }
        serializer = IngestionPayloadSerializer(data=payload)
        self.assertFalse(serializer.is_valid())

    def test_invalid_turn_bad_role(self):
        payload = {
            "repo": {"url": "https://github.com/user/repo", "token": "tok"},
            "issue": {
                "turns": [
                    {"role": "admin", "author": "alice", "body": "Fix it", "created_at": "2025-01-01T00:00:00Z"},
                ],
                "external_issue_id": "42",
            },
            "callback": {"url": "https://example.com/cb", "secret": "s"},
        }
        serializer = IngestionPayloadSerializer(data=payload)
        self.assertFalse(serializer.is_valid())

    def test_turns_and_text_both_valid(self):
        payload = {
            "repo": {"url": "https://github.com/user/repo", "token": "tok"},
            "issue": {
                "turns": [
                    {"role": "user", "author": "alice", "body": "Fix it", "created_at": "2025-01-01T00:00:00Z"},
                ],
                "text": "Legacy text",
                "external_issue_id": "42",
            },
            "callback": {"url": "https://example.com/cb", "secret": "s"},
        }
        serializer = IngestionPayloadSerializer(data=payload)
        self.assertTrue(serializer.is_valid(), msg=serializer.errors)


class TestLargeIssueIngestion(TestCase):
    """A large issue thread must be ingested whole, not rejected or clipped.

    Django caps a request body at ``DATA_UPLOAD_MAX_MEMORY_SIZE`` (2.5 MiB by
    default) and raises ``RequestDataTooBig`` from ``request.body`` — which
    would have silently capped how much of an issue could ever reach the
    agent.  Settings lift that cap; these tests pin it.
    """

    def setUp(self):
        self.factory = RequestFactory()

    def _payload(self, body: str):
        return {
            "repo": {"url": "https://github.com/user/repo", "token": "ghp_test"},
            "issue": {
                "turns": [
                    {
                        "role": "user",
                        "author": "someone",
                        "body": body,
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "external_issue_id": "999",
            },
            "callback": {
                "url": "https://example.com/callback",
                "secret": "callback-secret",
            },
        }

    def test_upload_cap_is_lifted(self):
        from django.conf import settings

        self.assertIsNone(settings.DATA_UPLOAD_MAX_MEMORY_SIZE)

    @patch.dict("os.environ", {"GITHUB_INGEST_TOKEN": "test-github-secret"})
    @patch("apps.ingestion.views.execute_task")
    @patch("apps.ingestion.views.get_redis")
    @patch("apps.ingestion.views.transaction")
    def test_multi_megabyte_thread_is_accepted_and_stored_whole(
        self, mock_transaction, mock_redis, mock_task
    ):
        redis_client = MagicMock(set=MagicMock(return_value=True))
        mock_redis.return_value = redis_client
        mock_transaction.on_commit.side_effect = lambda cb: cb()
        mock_transaction.atomic.return_value.__enter__ = MagicMock()
        mock_transaction.atomic.return_value.__exit__ = MagicMock(return_value=False)

        # Comfortably past Django's 2.5 MiB default.
        huge_body = "a very long comment. " * 200_000
        body = json.dumps(self._payload(huge_body)).encode()
        self.assertGreater(len(body), 4 * 1024 * 1024)

        request = self.factory.post(
            "/api/github/ingestion",
            data=body,
            content_type="application/json",
            **{AUTH_HEADER: "test-github-secret"},
        )
        response = GitHubIngestView.as_view()(request)

        self.assertEqual(response.status_code, 202)
        # The payload written to Redis must carry the whole thread, untruncated.
        stored = json.loads(
            [c for c in redis_client.set.call_args_list if "payload" in c.args[0]][0].args[1]
        )
        self.assertEqual(stored["issue"]["turns"][0]["body"], huge_body)
