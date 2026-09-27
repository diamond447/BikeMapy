"""Celery workers for direct activity batches."""

from __future__ import annotations

from typing import Any

from celery import shared_task  # type: ignore[import-untyped]

from .upload_services import process_batch


@shared_task(name="bikemapy.accounts.process_activity_upload_batch")  # type: ignore[untyped-decorator]
def process_activity_upload_batch_task(batch_id: str) -> dict[str, Any]:
    batch = process_batch(batch_id)
    return {"batch_id": str(batch.pk), "status": batch.status, "processed": batch.processed_files}
