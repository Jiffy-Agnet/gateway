"""Celery application configuration."""

import logging
import os

from celery import Celery
from celery.signals import worker_process_init, worker_ready

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")

import django

django.setup()

app = Celery("jiffy_gateway")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(packages=["jobs", "apps.ingestion"])

# Ensure task modules are imported so @shared_task registers with this app.
import jobs.tasks  # noqa: F401, E402

logger = logging.getLogger(__name__)


@worker_process_init.connect
def close_inherited_db_connections(**kwargs: object) -> None:
    """Drop DB connections inherited from the parent process after fork.

    A prefork child inherits whatever connection the main process had open.
    Reusing a SQLite connection across ``fork()`` is undefined behaviour and can
    serve stale reads — a Task row committed by the web process after the fork
    then looks like it does not exist.  Each child opens its own connection on
    first use instead.
    """
    from django.db import connections

    connections.close_all()


@worker_ready.connect
def on_worker_ready(**kwargs: object) -> None:
    """Run startup tasks after the worker is fully connected.

    1. Ensure the sandbox image exists (build if missing).
    2. Re-dispatch any tasks left in ``queued`` status from a previous run.

    The ``worker_ready`` signal fires once the worker has successfully
    connected to the broker and is about to start consuming.  This is
    the right place for startup recovery because:
    - The Django app is fully initialised (settings, DB, Docker daemon).
    - The broker connection is live, so ``apply_async`` will succeed.
    - It runs exactly once per worker process start.
    """
    from django.conf import settings

    from jobs.execution.container import ensure_sandbox_image
    from jobs.models import Task

    # --- 0. Which database is this worker actually reading? ------------------
    # If this path (or the row count) does not match the web service, ingested
    # tasks will never be found and every job dies with "Task not found in DB".
    db_path = settings.DATABASES["default"]["NAME"]
    try:
        total_tasks = Task.objects.count()
    except Exception:
        logger.exception("Worker startup: task database %s is not readable", db_path)
        total_tasks = -1
    logger.info(
        "Worker startup: task database=%s (%s task rows) — this must be the same "
        "file the web service writes to",
        db_path,
        total_tasks if total_tasks >= 0 else "unknown",
    )

    # --- 1. Sandbox image readiness ------------------------------------------
    # try:
    #     ensure_sandbox_image()
    # except Exception:
    #     logger.exception(
    #         "Worker startup: sandbox image check/build FAILED — "
    #         "tasks will fail at provisioning until this is resolved"
    #     )

    # --- 2. Re-dispatch orphaned queued tasks --------------------------------
    #
    # Known limitation: we cannot distinguish "genuinely orphaned" (the
    # worker crashed/restarted after the task was enqueued but before it
    # was picked up) from "still legitimately running" (a long-running
    # task that simply hasn't updated its status yet).  Re-enqueuing
    # a task that is still running will cause it to execute twice.
    #
    # Mitigation: ``acks_late=True`` + ``task_reject_on_worker_lost=True``
    # means the broker already re-delivers tasks when a worker dies.  The
    # startup recovery here mainly covers the case where *both* the broker
    # and worker were restarted (e.g. full host reboot) and the broker
    # lost unacked messages.  In that scenario no live worker holds the
    # task, so double-enqueue is not a concern.
    #
    # If this heuristic proves too aggressive in practice, consider adding
    # a ``started_at`` timestamp to the Task model and only re-dispatching
    # tasks whose ``started_at`` is older than a threshold (e.g. 2× the
    # expected max task duration).
    stale_tasks = Task.objects.filter(status="queued")
    count = stale_tasks.count()
    if count:
        from celery import uuid as celery_uuid
        from jobs.tasks import execute_task

        logger.info(
            "Worker startup: found %d orphaned queued task(s) — re-dispatching",
            count,
        )
        for task in stale_tasks.iterator():
            new_celery_id = celery_uuid()
            task.celery_task_id = new_celery_id
            task.save(update_fields=["celery_task_id"])
            execute_task.apply_async(args=[task.id], task_id=new_celery_id)
            logger.info(
                "Worker startup: re-dispatched task %d (provider=%s, issue=%s)",
                task.id,
                task.provider,
                task.issue_external_id,
            )
    else:
        logger.info("Worker startup: no orphaned queued tasks — nothing to re-dispatch")
