"""Celery workers for direct activity batches."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]
from django.conf import settings
from django.utils import timezone

from .upload_services import process_batch

logger = logging.getLogger(__name__)


@shared_task(
    name="bikemapy.accounts.process_activity_upload_batch",
    soft_time_limit=300,
    time_limit=360,
)  # type: ignore[untyped-decorator]
def process_activity_upload_batch_task(batch_id: str) -> dict[str, Any]:
    from .models import ActivityUploadBatch

    try:
        batch = process_batch(batch_id)
    except ActivityUploadBatch.DoesNotExist:
        return {"batch_id": batch_id, "status": "deleted", "processed": 0}
    return {"batch_id": str(batch.pk), "status": batch.status, "processed": batch.processed_files}


@shared_task(name="bikemapy.accounts.cleanup_expired_activity_uploads")  # type: ignore[untyped-decorator]
def cleanup_expired_activity_uploads_task() -> dict[str, int]:
    """Delete transient raw upload payloads after the documented retention window."""

    from .models import ActivityUpload, ActivityUploadBatch, ActivityUploadDeletion

    hours = int(getattr(settings, "ACTIVITY_UPLOAD_MAX_RETENTION_HOURS", 24))
    cutoff = timezone.now() - timedelta(hours=hours)
    stale = ActivityUpload.objects.filter(
        created_at__lt=cutoff,
        status__in=(ActivityUpload.Status.QUEUED, ActivityUpload.Status.PROCESSING),
        content_path__isnull=False,
    ).exclude(content_path="")
    batch_ids = list(stale.values_list("batch_id", flat=True).distinct())
    expired = list(stale)
    cleared = 0
    for upload in expired:
        if upload.content_path:
            ActivityUploadDeletion.objects.get_or_create(storage_key=upload.content_path.name)
        upload.content = None
        upload.content_path = None
        upload.status = ActivityUpload.Status.FAILED
        upload.error_code = "retention_expired"
        upload.error_detail = "The upload expired before processing."
        upload.processed_at = timezone.now()
        upload.save(
            update_fields=(
                "content",
                "content_path",
                "status",
                "error_code",
                "error_detail",
                "processed_at",
            )
        )
        cleared += 1
    for batch in ActivityUploadBatch.objects.filter(pk__in=batch_ids):
        statuses = list(batch.files.values_list("status", flat=True))
        terminal = [
            status
            for status in statuses
            if status
            in {
                ActivityUpload.Status.ACCEPTED,
                ActivityUpload.Status.DUPLICATE,
                ActivityUpload.Status.FAILED,
                ActivityUpload.Status.UNSUPPORTED,
            }
        ]
        batch.processed_files = len(terminal)
        batch.accepted_files = terminal.count(ActivityUpload.Status.ACCEPTED)
        batch.duplicate_files = terminal.count(ActivityUpload.Status.DUPLICATE)
        batch.failed_files = sum(
            status in {ActivityUpload.Status.FAILED, ActivityUpload.Status.UNSUPPORTED}
            for status in terminal
        )
        if len(terminal) == len(statuses):
            batch.status = (
                ActivityUploadBatch.Status.COMPLETED
                if batch.failed_files == 0
                else ActivityUploadBatch.Status.PARTIAL
                if batch.accepted_files or batch.duplicate_files
                else ActivityUploadBatch.Status.FAILED
            )
            batch.completed_at = timezone.now()
        batch.save(
            update_fields=(
                "status",
                "processed_files",
                "accepted_files",
                "duplicate_files",
                "failed_files",
                "completed_at",
                "updated_at",
            )
        )
    # Keep per-file result rows and batch metadata for owner-visible audit
    # history; only transient content is removed.
    deletions = retry_activity_upload_deletions_task()
    orphans = reconcile_orphan_activity_uploads_task()
    return {
        "uploads": cleared,
        "batches": 0,
        "deletions_deleted": deletions["deleted"],
        "deletions_failed": deletions["failed"],
        "deletion_records_purged": deletions["purged"],
        "orphans_queued": orphans["queued"],
    }


@shared_task(name="bikemapy.accounts.retry_activity_upload_deletions")  # type: ignore[untyped-decorator]
def retry_activity_upload_deletions_task(limit: int = 100) -> dict[str, int]:
    """Retry durable deletions and leave failures visible to owner operators."""

    from .models import ActivityUpload, ActivityUploadDeletion

    now = timezone.now()
    deleted = failed = 0
    candidates = ActivityUploadDeletion.objects.filter(
        status__in=(ActivityUploadDeletion.Status.PENDING, ActivityUploadDeletion.Status.FAILED),
        next_attempt_at__lte=now,
    ).order_by("created_at", "pk")[: max(1, limit)]
    for candidate in candidates:
        try:
            ActivityUpload.content_path.field.storage.delete(candidate.storage_key)
        except Exception as exc:
            attempts = candidate.attempts + 1
            delay = min(3600, 30 * 2 ** min(attempts - 1, 7))
            ActivityUploadDeletion.objects.filter(pk=candidate.pk).update(
                status=ActivityUploadDeletion.Status.FAILED,
                attempts=attempts,
                next_attempt_at=now + timedelta(seconds=delay),
                last_error=type(exc).__name__[:240],
            )
            logger.error(
                "activity_upload_storage_deletion_failed",
                extra={"deletion_id": candidate.pk, "attempts": attempts},
            )
            failed += 1
            continue
        ActivityUploadDeletion.objects.filter(pk=candidate.pk).update(
            status=ActivityUploadDeletion.Status.DELETED,
            attempts=candidate.attempts + 1,
            last_error="",
            deleted_at=now,
        )
        deleted += 1
    retention_cutoff = now - timedelta(days=30)
    purged, _ = ActivityUploadDeletion.objects.filter(
        status=ActivityUploadDeletion.Status.DELETED,
        deleted_at__lt=retention_cutoff,
    ).delete()
    return {"deleted": deleted, "failed": failed, "purged": purged}


@shared_task(name="bikemapy.accounts.reconcile_orphan_activity_uploads")  # type: ignore[untyped-decorator]
def reconcile_orphan_activity_uploads_task(limit: int = 100) -> dict[str, int]:
    """Queue aged storage objects which are not referenced by any upload row."""

    from .models import ActivityUpload, ActivityUploadDeletion

    root = str(settings.ACTIVITY_UPLOAD_ROOT)
    if not os.path.isdir(root):
        return {"queued": 0}
    cutoff = timezone.now() - timedelta(hours=1)
    referenced = set(
        ActivityUpload.objects.filter(content_path__isnull=False)
        .exclude(content_path="")
        .values_list("content_path", flat=True)
    )
    queued = 0
    for directory, _subdirs, files in os.walk(root):
        for filename in files:
            absolute = os.path.join(directory, filename)
            key = os.path.relpath(absolute, root).replace(os.sep, "/")
            if key in referenced or ActivityUploadDeletion.objects.filter(storage_key=key).exists():
                continue
            modified = datetime.fromtimestamp(os.path.getmtime(absolute), tz=UTC)
            if modified >= cutoff:
                continue
            ActivityUploadDeletion.objects.get_or_create(storage_key=key)
            queued += 1
            if queued >= max(1, limit):
                return {"queued": queued}
    return {"queued": queued}
