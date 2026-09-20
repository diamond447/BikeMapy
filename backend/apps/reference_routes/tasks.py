"""Operator-triggered, bounded reference-route refresh task."""

from __future__ import annotations

from celery import shared_task  # type: ignore[import-untyped]

from .models import ReferenceImport


@shared_task(name="bikemapy.reference_routes.refresh")  # type: ignore[untyped-decorator]
def refresh_reference_routes(reference_import_id: int) -> dict[str, object]:
    """Process a stored snapshot by ID; raw bytes never travel through Celery."""

    source_import = ReferenceImport.objects.select_related("collection").get(pk=reference_import_id)
    return {
        "status": source_import.status,
        "import_id": source_import.pk,
        "raw_response_sha256": source_import.raw_response_sha256,
    }
