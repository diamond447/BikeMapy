"""Operator-triggered, bounded reference-route refresh task."""

from __future__ import annotations

from celery import shared_task  # type: ignore[import-untyped]

from .models import ReferenceImport
from .services import import_osm_snapshot


@shared_task(name="bikemapy.reference_routes.refresh")  # type: ignore[untyped-decorator]
def refresh_reference_routes(reference_import_id: int) -> dict[str, object]:
    """Process a stored snapshot by ID; raw bytes never travel through Celery."""

    source_import = ReferenceImport.objects.select_related("collection").get(pk=reference_import_id)
    if source_import.status != "discovered":
        return {"status": "unchanged", "import_id": source_import.pk}
    return import_osm_snapshot(
        collection=source_import.collection,
        payload=bytes(source_import.raw_response),
        endpoint=source_import.endpoint,
        query_text=source_import.query_text,
        retrieved_at=source_import.retrieved_at,
        response_metadata=source_import.response_metadata,
        stored_import_id=source_import.pk,
    )
