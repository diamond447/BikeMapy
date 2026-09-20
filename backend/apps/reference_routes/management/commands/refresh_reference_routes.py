"""Operator command for bounded reference-route refreshes and validation."""

import json
from datetime import datetime
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
        parser.add_argument("--expected-relation-count", type=int)
        parser.add_argument("--expected-relation-id", action="append", type=int, default=[])
        parser.add_argument("--retrieved-at")
        parser.add_argument("--http-status", type=int, default=200)

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
        if options.get("expected_relation_count") is None or not options.get("retrieved_at"):
            raise CommandError(
                "OSM refresh requires expected relation count and retrieval timestamp"
            )
        try:
            payload = payload_file.read_bytes()
            json.loads(payload)
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Could not read source snapshot: {exc}") from exc
        self.stdout.write(
            json.dumps(
                import_osm_snapshot(
                    collection=collection,
                    payload=payload,
                    retrieved_at=datetime.fromisoformat(
                        options["retrieved_at"].replace("Z", "+00:00")
                    ),
                    response_metadata={
                        "complete": True,
                        "expected_relation_count": options["expected_relation_count"],
                        "expected_relation_ids": options["expected_relation_id"],
                        "http_status": options["http_status"],
                    },
                ),
                sort_keys=True,
                default=str,
            )
        )
