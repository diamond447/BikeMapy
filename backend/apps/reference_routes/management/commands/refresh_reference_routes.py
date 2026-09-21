"""Operator command for bounded reference-route refreshes and validation."""

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from django.core.management.base import BaseCommand, CommandError

from apps.reference_routes.models import ReferenceCollection, ReferenceSourceKind
from apps.reference_routes.services import (
    _parse_utc_timestamp,
    blocked_via_czechia_import,
    import_osm_snapshot,
)


class Command(BaseCommand):
    help = "Import one manifest-verified OSM snapshot or report a blocked Via Czechia refresh."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--collection", required=True)
        parser.add_argument("--payload-file", type=Path)
        parser.add_argument("--manifest", type=Path)
        parser.add_argument("--validation-test-mode", action="store_true")

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
        manifest_file = options.get("manifest")
        if payload_file is None or manifest_file is None:
            raise CommandError(
                "OSM refresh requires --payload-file and --manifest; network fetching is bounded"
            )
        try:
            payload = payload_file.read_bytes()
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CommandError(f"Could not read source snapshot: {exc}") from exc
        if not isinstance(manifest, dict):
            raise CommandError("OSM manifest must be a JSON object")
        required = {
            "endpoint",
            "query",
            "retrieved_at",
            "source_timestamp",
            "http_status",
            "http_headers",
            "http_header_absence",
            "sha256",
            "validation_sample",
            "selected_relation_ids",
            "expected_relation_count",
            "expected_way_count",
            "expected_node_count",
            "relation_version",
            "relation_changeset",
            "relation_timestamp",
        }
        missing = sorted(required - set(manifest))
        if missing:
            raise CommandError(f"OSM manifest is missing required fields: {', '.join(missing)}")
        validation_sample = manifest["validation_sample"]
        if not isinstance(validation_sample, bool):
            raise CommandError("OSM manifest validation_sample must be boolean")
        validation_test_mode = bool(options.get("validation_test_mode"))
        if validation_sample and not validation_test_mode:
            raise CommandError("Validation-only manifests require --validation-test-mode")
        if validation_test_mode and not validation_sample:
            raise CommandError("--validation-test-mode requires a validation-only manifest")
        if validation_sample:
            sample_required = {"selection_rationale", "selection_date", "direct_tag_eligibility"}
            sample_missing = sorted(sample_required - set(manifest))
            if sample_missing:
                raise CommandError(
                    "Validation manifest is missing selection evidence: "
                    + ", ".join(sample_missing)
                )
            if (
                not isinstance(manifest["selection_rationale"], str)
                or not manifest["selection_rationale"].strip()
            ):
                raise CommandError("Validation selection rationale is required")
            try:
                _parse_utc_timestamp(manifest["selection_date"], field="selection_date")
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            eligibility = manifest["direct_tag_eligibility"]
            if (
                not isinstance(eligibility, dict)
                or not all(
                    eligibility.get(field) for field in ("route", "ref", "network", "operator")
                )
                or eligibility.get("within_czechia") is not True
            ):
                raise CommandError("Direct validation tag/Czechia eligibility evidence is invalid")
        else:
            discovery_required = {
                "discovery_mechanism",
                "discovery_query_or_extract_id",
                "discovery_executed_at",
                "discovery_result_sha256",
                "discovery_artifact_url",
                "discovery_selected_relation_ids",
                "discovery_result_payload",
            }
            discovery_missing = sorted(discovery_required - set(manifest))
            if discovery_missing:
                raise CommandError(
                    "Production manifest is missing discovery evidence: "
                    + ", ".join(discovery_missing)
                )
        ids = manifest["selected_relation_ids"]
        headers = manifest["http_headers"]
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(value, int) or isinstance(value, bool) for value in ids)
            or len(set(ids)) != len(ids)
            or manifest["expected_relation_count"] != len(ids)
        ):
            raise CommandError("OSM manifest relation IDs/count are invalid")
        if isinstance(manifest["expected_relation_count"], bool) or not isinstance(
            manifest["expected_relation_count"], int
        ):
            raise CommandError("OSM manifest relation count is invalid")
        if not isinstance(headers, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items()
        ):
            raise CommandError("OSM manifest HTTP headers are invalid")
        try:
            retrieved_at = _parse_utc_timestamp(manifest["retrieved_at"], field="retrieved_at")
            http_status = int(manifest["http_status"])
        except (TypeError, ValueError) as exc:
            raise CommandError("OSM manifest timestamp or HTTP status is invalid") from exc
        endpoint = urlsplit(str(manifest["endpoint"]))
        expected_endpoint = f"/api/0.6/relation/{ids[0]}/full.json"
        if (
            endpoint.scheme != "https"
            or endpoint.hostname != "www.openstreetmap.org"
            or endpoint.port is not None
            or endpoint.path != expected_endpoint
            or endpoint.query
            or endpoint.fragment
            or str(manifest["query"]) != f"GET {expected_endpoint}"
        ):
            raise CommandError("OSM manifest endpoint/query is not the exact reviewed API request")
        if not 200 <= http_status < 300:
            raise CommandError("OSM manifest HTTP status must be successful")
        try:
            result = import_osm_snapshot(
                collection=collection,
                payload=payload,
                endpoint=str(manifest["endpoint"]),
                query_text=str(manifest["query"]),
                retrieved_at=retrieved_at,
                response_metadata={
                    "complete": True,
                    "expected_relation_count": manifest["expected_relation_count"],
                    "expected_relation_ids": ids,
                    "expected_way_count": manifest["expected_way_count"],
                    "expected_node_count": manifest["expected_node_count"],
                    "relation_version": manifest["relation_version"],
                    "relation_changeset": manifest["relation_changeset"],
                    "relation_timestamp": manifest["relation_timestamp"],
                    "http_status": http_status,
                    "http_headers": headers,
                    "http_header_absence": manifest["http_header_absence"],
                    "raw_sha256": manifest["sha256"],
                    "source_timestamp": manifest["source_timestamp"],
                    "validation_sample": validation_sample,
                    "validation_test_mode": validation_test_mode,
                    "production_import": not validation_sample,
                    "import_mode": "validation" if validation_sample else "production",
                    "discovery_mechanism": manifest.get("discovery_mechanism"),
                    "discovery_query_or_extract_id": manifest.get("discovery_query_or_extract_id"),
                    "discovery_executed_at": manifest.get("discovery_executed_at"),
                    "discovery_result_sha256": manifest.get("discovery_result_sha256"),
                    "discovery_artifact_url": manifest.get("discovery_artifact_url"),
                    "discovery_result_payload": manifest.get("discovery_result_payload"),
                    "discovery_selected_relation_ids": manifest.get(
                        "discovery_selected_relation_ids"
                    ),
                },
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True, default=str))
