"""Operator-triggered, bounded reference-route refresh task."""

from __future__ import annotations

from typing import Any

from celery import shared_task  # type: ignore[import-untyped]

from .models import ReferenceCollection, ReferenceSourceKind
from .services import blocked_via_czechia_import, import_osm_snapshot


@shared_task(name="bikemapy.reference_routes.refresh")  # type: ignore[untyped-decorator]
def refresh_reference_routes(
    collection_slug: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Refresh from an operator-supplied cached snapshot; never scrapes blocked sources."""

    collection = ReferenceCollection.objects.get(slug=collection_slug)
    if collection.source_kind == ReferenceSourceKind.VIA_CZECHIA:
        return blocked_via_czechia_import(collection=collection)
    if payload is None:
        return {"status": "failed", "reason": "An operator-supplied OSM snapshot is required"}
    return import_osm_snapshot(collection=collection, payload=payload)
