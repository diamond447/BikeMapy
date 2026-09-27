"""Celery workers for direct activity batches."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]
from django.conf import settings
from django.utils import timezone

from .upload_services import process_batch


@shared_task(
    name="bikemapy.accounts.process_activity_upload_batch",
    soft_time_limit=300,
    time_limit=360,
)  # type: ignore[untyped-decorator]
def process_activity_upload_batch_task(batch_id: str) -> dict[str, Any]:
    batch = process_batch(batch_id)
    return {"batch_id": str(batch.pk), "status": batch.status, "processed": batch.processed_files}


@shared_task(name="bikemapy.accounts.cleanup_expired_activity_uploads")  # type: ignore[untyped-decorator]
def cleanup_expired_activity_uploads_task() -> dict[str, int]:
    """Delete transient raw upload payloads after the documented retention window."""

    from .models import ActivityUpload, ActivityUploadBatch

    hours = int(getattr(settings, "ACTIVITY_UPLOAD_MAX_RETENTION_HOURS", 24))
    cutoff = timezone.now() - timedelta(hours=hours)
    stale = ActivityUpload.objects.filter(
        created_at__lt=cutoff,
        status__in=(ActivityUpload.Status.QUEUED, ActivityUpload.Status.PROCESSING),
    )
    batch_ids = list(stale.values_list("batch_id", flat=True).distinct())
    expired = list(stale)
    cleared = 0
    for upload in expired:
        if upload.content_path:
            upload.content_path.delete(save=False)
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
    return {"uploads": cleared, "batches": 0}
