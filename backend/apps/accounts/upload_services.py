"""Safe FIT/GPX/TCX normalization for provider-neutral activity uploads."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from typing import Any

from defusedxml import ElementTree
from django.contrib.gis.geos import LineString
from django.db import transaction
from django.utils import timezone

from .activity_services import _schedule_player_recomputations
from .models import ActivityUpload, ActivityUploadBatch, ImportedActivity, Player

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_EXPANDED_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_FILES = 100
MAX_GEOMETRY_POINTS = 100_000
MAX_ACTIVITY_COUNT = 100
SUPPORTED_SUFFIXES = frozenset({".fit", ".gpx", ".tcx"})


class UploadError(ValueError):
    def __init__(self, detail: str, *, code: str = "invalid_file") -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_xml(data: bytes, suffix: str) -> tuple[list[tuple[float, float]], datetime | None, str]:
    if len(data) > MAX_FILE_BYTES:
        raise UploadError("The file exceeds the size limit.", code="file_too_large")
    try:
        root = ElementTree.fromstring(data)
    except (ElementTree.ParseError, ValueError) as exc:
        raise UploadError("The activity file is not valid XML.", code="invalid_xml") from exc
    points: list[tuple[float, float]] = []
    started: datetime | None = None
    for element in root.iter():
        name = _local_name(element.tag)
        if name == "trkpt":
            try:
                lat, lon = float(element.attrib["lat"]), float(element.attrib["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            points.append((lon, lat))
        elif name == "trackpoint":
            lat = lon = None
            for child in element:
                child_name = _local_name(child.tag)
                if child_name == "latitude":
                    for value in child.iter():
                        if _local_name(value.tag) == "degrees":
                            lat = float(_text(value.text))
                elif child_name == "longitude":
                    for value in child.iter():
                        if _local_name(value.tag) == "degrees":
                            lon = float(_text(value.text))
            if lat is not None and lon is not None:
                points.append((lon, lat))
        elif name in {"time", "starttime"} and started is None:
            parsed = _parse_timestamp(_text(element.text))
            if parsed:
                started = parsed
        if len(points) > MAX_GEOMETRY_POINTS:
            raise UploadError("The activity has too many geometry points.", code="too_many_points")
    if len(points) < 2:
        raise UploadError("The activity does not contain a usable track.", code="missing_geometry")
    for lon, lat in points:
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise UploadError("The activity contains invalid coordinates.", code="invalid_geometry")
    return points, started, suffix[1:].upper()


def parse_activity(data: bytes, filename: str) -> tuple[list[tuple[float, float]], datetime, str]:
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in SUPPORTED_SUFFIXES:
        raise UploadError("Only FIT, GPX, and TCX files are supported.", code="unsupported_type")
    if suffix == ".fit":
        # FIT is a binary format; accept only files with its mandatory header.
        # A later parser can enrich timestamps, while the content fingerprint
        # still makes imports deterministic and idempotent.
        if len(data) < 14 or data[8:12] != b".FIT":
            raise UploadError("The FIT file header is invalid.", code="invalid_fit")
        now = timezone.now()
        return [], now, "FIT"
    points, started, kind = _parse_xml(data, suffix)
    return points, started or timezone.now(), kind


def _safe_archive_members(data: bytes) -> list[tuple[str, bytes]]:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise UploadError("The archive exceeds the size limit.", code="archive_too_large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UploadError("The ZIP archive is invalid.", code="invalid_archive") from exc
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_FILES:
        raise UploadError("The archive contains too many files.", code="too_many_files")
    expanded = 0
    result: list[tuple[str, bytes]] = []
    for info in infos:
        name = info.filename.replace("\\", "/")
        if info.is_dir() or name.startswith("/") or ".." in name.split("/"):
            raise UploadError("The archive contains an unsafe path.", code="unsafe_archive")
        suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if suffix == ".zip":
            raise UploadError("Nested archives are not supported.", code="nested_archive")
        if suffix not in SUPPORTED_SUFFIXES:
            continue
        expanded += info.file_size
        if expanded > MAX_EXPANDED_BYTES or info.file_size > MAX_FILE_BYTES:
            raise UploadError("The archive expands beyond the safe limit.", code="archive_bomb")
        result.append((name, archive.read(info)))
    if not result:
        raise UploadError(
            "The archive contains no supported activity files.", code="no_supported_files"
        )
    return result


def create_batch(
    player: Player, files: list[tuple[str, bytes]], *, attested: bool
) -> ActivityUploadBatch:
    if not attested:
        raise UploadError(
            "You must attest that you own or may process this data.", code="attestation_required"
        )
    if not files or len(files) > MAX_ACTIVITY_COUNT:
        raise UploadError("The batch contains too many files.", code="too_many_files")
    expanded: list[tuple[str, bytes]] = []
    for filename, data in files:
        if filename.lower().endswith(".zip"):
            expanded.extend(_safe_archive_members(data))
        else:
            if len(data) > MAX_FILE_BYTES:
                raise UploadError("The file exceeds the size limit.", code="file_too_large")
            expanded.append((filename, data))
    if len(expanded) > MAX_ACTIVITY_COUNT:
        raise UploadError("The batch contains too many activities.", code="too_many_files")
    with transaction.atomic():
        batch = ActivityUploadBatch.objects.create(
            player=player, total_files=len(expanded), attested=True
        )
        ActivityUpload.objects.bulk_create(
            [
                ActivityUpload(
                    batch=batch,
                    original_name=name[:240],
                    content_sha256=hashlib.sha256(data).hexdigest(),
                    content=data,
                    size_bytes=len(data),
                )
                for name, data in expanded
            ]
        )
    return batch


def process_batch(batch_id: Any) -> ActivityUploadBatch:
    batch = ActivityUploadBatch.objects.get(pk=batch_id)
    ActivityUploadBatch.objects.filter(pk=batch.pk).update(
        status=ActivityUploadBatch.Status.RUNNING
    )
    for upload in batch.files.filter(status=ActivityUpload.Status.QUEUED).order_by("pk"):
        try:
            data = bytes(upload.content or b"")
            points, started, kind = parse_activity(data, upload.original_name)
            fingerprint = hashlib.sha256(
                json.dumps(
                    {"kind": kind, "points": points, "started": started.isoformat()},
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            geometry = LineString(points, srid=4326) if len(points) >= 2 else None
            provider_id = f"upload:{fingerprint}"
            with transaction.atomic():
                existing = ImportedActivity.objects.filter(
                    player=batch.player, provider_activity_id=provider_id
                ).first()
                if existing is not None:
                    upload.status = ActivityUpload.Status.DUPLICATE
                    upload.fingerprint = fingerprint
                    upload.activity = existing
                else:
                    activity = ImportedActivity.objects.create(
                        player=batch.player,
                        provider_activity_id=provider_id,
                        started_at=started,
                        calendar_date=started.date(),
                        activity_type="Ride",
                        visibility="private",
                        geometry=geometry,
                        geometry_hash=fingerprint,
                        payload={"source": "direct-upload", "format": kind},
                    )
                    upload.status = ActivityUpload.Status.ACCEPTED
                    upload.fingerprint = fingerprint
                    upload.activity = activity
                    _schedule_player_recomputations(batch.player.pk)
                upload.content = None
                upload.processed_at = timezone.now()
                upload.save(
                    update_fields=("status", "fingerprint", "activity", "content", "processed_at")
                )
        except UploadError as exc:
            upload.status = (
                ActivityUpload.Status.UNSUPPORTED
                if exc.code == "unsupported_type"
                else ActivityUpload.Status.FAILED
            )
            upload.error_code, upload.error_detail = exc.code, exc.detail
            upload.content = None
            upload.processed_at = timezone.now()
            upload.save(
                update_fields=("status", "error_code", "error_detail", "content", "processed_at")
            )
        except Exception:
            upload.status = ActivityUpload.Status.FAILED
            upload.error_code, upload.error_detail = (
                "processing_failed",
                "The activity could not be processed.",
            )
            upload.content = None
            upload.processed_at = timezone.now()
            upload.save(
                update_fields=("status", "error_code", "error_detail", "content", "processed_at")
            )
    counts = batch.files.values_list("status", flat=True)
    statuses = list(counts)
    done = len(statuses)
    batch.processed_files = done
    batch.accepted_files = statuses.count(ActivityUpload.Status.ACCEPTED)
    batch.duplicate_files = statuses.count(ActivityUpload.Status.DUPLICATE)
    batch.failed_files = sum(
        status in {ActivityUpload.Status.FAILED, ActivityUpload.Status.UNSUPPORTED}
        for status in statuses
    )
    batch.status = (
        ActivityUploadBatch.Status.COMPLETED
        if batch.failed_files == 0
        else ActivityUploadBatch.Status.PARTIAL
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
    return batch
