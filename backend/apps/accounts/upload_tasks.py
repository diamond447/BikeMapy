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
    deleted, _ = ActivityUpload.objects.filter(created_at__lt=cutoff).delete()
    # Keep batch result metadata for auditability, but remove orphaned empty
    # batches left by interrupted uploads.
    orphaned, _ = ActivityUploadBatch.objects.filter(
        created_at__lt=cutoff, files__isnull=True
    ).delete()
    return {"uploads": deleted, "batches": orphaned}
