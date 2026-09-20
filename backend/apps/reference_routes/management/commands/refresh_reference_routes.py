"""Operator command for bounded reference-route refreshes and validation."""

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.reference_routes.models import ReferenceCollection, ReferenceSourceKind
from apps.reference_routes.services import blocked_via_czechia_import, import_osm_snapshot


class Command(BaseCommand):
    help = "Import one cached OSM snapshot or report a blocked Via Czechia refresh."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--collection", required=True)
        parser.add_argument("--payload-file", type=Path)

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            collection = ReferenceCollection.objects.get(slug=options["collection"])
        except ReferenceCollection.DoesNotExist as exc:
            raise CommandError("Unknown reference-route collection") from exc
        if collection.source_kind == ReferenceSourceKind.VIA_CZECHIA:
            self.stdout.write(
                json.dumps(blocked_via_czechia_import(collection=collection), sort_keys=True)
            )
            return
        payload_file = options.get("payload_file")
        if payload_file is None:
            raise CommandError(
                "OSM refresh requires --payload-file; network fetching is intentionally bounded"
            )
        try:
            payload = payload_file.read_bytes()
            json.loads(payload)
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Could not read source snapshot: {exc}") from exc
        self.stdout.write(
            json.dumps(
                import_osm_snapshot(collection=collection, payload=payload),
                sort_keys=True,
                default=str,
            )
        )
