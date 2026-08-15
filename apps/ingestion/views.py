"""Ingestion endpoints for GitHub, GitLab, and Gitea webhooks."""

import json
import logging

import redis
from celery import uuid as celery_uuid
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ingestion.auth import get_ingest_secret, verify_ingest_token
from apps.ingestion.serializers import IngestionPayloadSerializer
from apps.ingestion.tasks import execute_task
from jobs.models import Task

logger = logging.getLogger(__name__)

_redis_client = None


def get_redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.REDIS_URL, protocol=2)
    return _redis_client


PAYLOAD_TTL_SECONDS = 4 * 60 * 60
LOCK_TTL_SECONDS = 300


def _acquire_lock(lock_key: str) -> bool:
    r = get_redis()
    return r.set(lock_key, "1", nx=True, ex=LOCK_TTL_SECONDS)


def _store_payload(task_id: int, payload: dict) -> None:
    r = get_redis()
    key = f"jiffy:task:{task_id}:payload"
    # DjangoJSONEncoder renders datetimes (e.g. each turn's created_at, parsed by
    # DateTimeField) back to ISO-8601 strings, which is what the worker expects.
    r.set(key, json.dumps(payload, cls=DjangoJSONEncoder), ex=PAYLOAD_TTL_SECONDS)


def _handle_ingestion(provider: str, data: dict) -> Response:
    if not isinstance(data, dict):
        return Response(
            {"error": "Invalid payload format. Expected a JSON object."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer = IngestionPayloadSerializer(data=data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    validated = serializer.validated_data
    lock_key = f"jiffy:lock:issue:{provider}:{validated['issue']['external_issue_id']}"
    if not _acquire_lock(lock_key):
        logger.info(
            "Duplicate delivery for %s issue %s — skipping",
            provider,
            validated["issue"]["external_issue_id"],
        )
        return Response({"status": "already_queued"}, status=status.HTTP_202_ACCEPTED)

    task_id = celery_uuid()

    with transaction.atomic():
        task = Task.objects.create(
            provider=provider,
            repo_url=validated["repo"]["url"],
            issue_external_id=validated["issue"]["external_issue_id"],
            callback_url=validated["callback"]["url"],
            callback_secret=validated["callback"]["secret"],
            status="queued",
            celery_task_id=task_id,
        )

        # ``provider`` rides along in Redis so the worker can still report back
        # to the issue thread if it cannot read the Task row.
        _store_payload(task.id, {**validated, "provider": provider})

        # Registered inside the transaction so the job is only published once
        # the row is actually committed and visible to the worker.
        transaction.on_commit(
            lambda: execute_task.apply_async(args=[task.id], task_id=task_id)
        )

    logger.info(
        "Ingested %s issue %s as task %d (celery=%s)",
        provider,
        validated["issue"]["external_issue_id"],
        task.id,
        task_id,
    )

    return Response({"task_id": task.id, "status": "queued"}, status=status.HTTP_202_ACCEPTED)


class GitHubIngestView(APIView):
    """Ingest webhook from GitHub.

    Auth: ``X-JIFFY-TOKEN `` header.
    """

    authentication_classes = []
    permission_classes = []
    serializer_class = IngestionPayloadSerializer

    def post(self, request: Request) -> Response:
        secret = get_ingest_secret("github")
        if not verify_ingest_token(request, secret):
            return Response({"error": "Invalid token"}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return Response({"error": "Invalid JSON"}, status=status.HTTP_400_BAD_REQUEST)
        return _handle_ingestion("github", data)


class GitLabIngestView(APIView):
    """Ingest webhook from GitLab.

    Auth: ``X-JIFFY-TOKEN `` header.
    """

    authentication_classes = []
    permission_classes = []
    serializer_class = IngestionPayloadSerializer

    def post(self, request: Request) -> Response:
        if not verify_ingest_token(request, get_ingest_secret("gitlab")):
            return Response({"error": "Invalid token"}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return Response({"error": "Invalid JSON"}, status=status.HTTP_400_BAD_REQUEST)
        return _handle_ingestion("gitlab", data)


class GiteaIngestView(APIView):
    """Ingest webhook from Gitea.

    Auth: ``X-JIFFY-TOKEN `` header.
    """

    authentication_classes = []
    permission_classes = []
    serializer_class = IngestionPayloadSerializer

    def post(self, request: Request) -> Response:
        if not verify_ingest_token(request, get_ingest_secret("gitea")):
            return Response({"error": "Invalid token"}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return Response({"error": "Invalid JSON"}, status=status.HTTP_400_BAD_REQUEST)
        return _handle_ingestion("gitea", data)
