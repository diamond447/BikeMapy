"""Safe FIT/GPX/TCX normalization for provider-neutral activity uploads."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import zipfile
from datetime import UTC, datetime
from typing import Any

from defusedxml import ElementTree  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]
from django.db import IntegrityError, transaction
from django.utils import timezone

from .activity_services import _schedule_player_recomputations
from .models import ActivityUpload, ActivityUploadBatch, ImportedActivity, Player

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_EXPANDED_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_FILES = 100
MAX_GEOMETRY_POINTS = 100_000
MAX_ACTIVITY_COUNT = 100
MAX_BATCH_EXPANDED_BYTES = 200 * 1024 * 1024
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
    except (DefusedXmlException, ElementTree.ParseError, ValueError) as exc:
        raise UploadError("The activity file is not valid XML.", code="invalid_xml") from exc
    root_name = _local_name(root.tag)
    expected_root = {".gpx": "gpx", ".tcx": "trainingcenterdatabase"}[suffix]
    if root_name != expected_root:
        raise UploadError("The file content does not match its type.", code="type_mismatch")
    if suffix == ".tcx":
        sports = {
            str(value).strip().lower()
            for element in root.iter()
            if _local_name(element.tag) == "activity"
            for key, value in element.attrib.items()
            if _local_name(key) == "sport"
        }
        if sports and not sports.issubset({"biking", "cycling", "bike"}):
            raise UploadError("Only cycling activities are supported.", code="non_cycling")
    points: list[tuple[float, float]] = []
    has_track = False
    track_type = ""
    started: datetime | None = None
    for element in root.iter():
        name = _local_name(element.tag)
        if name == "trkpt":
            has_track = True
            try:
                lat, lon = float(element.attrib["lat"]), float(element.attrib["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            points.append((lon, lat))
        elif name == "type" and track_type == "":
            track_type = _text(element.text).lower()
        elif name == "trackpoint":
            has_track = True
            lat_value: float | None = None
            lon_value: float | None = None
            for child in element:
                child_name = _local_name(child.tag)
                if child_name == "latitude":
                    for value in child.iter():
                        if _local_name(value.tag) == "degrees":
                            lat_value = float(_text(value.text))
                elif child_name == "longitude":
                    for value in child.iter():
                        if _local_name(value.tag) == "degrees":
                            lon_value = float(_text(value.text))
            if lat_value is not None and lon_value is not None:
                points.append((lon_value, lat_value))
        elif name in {"time", "starttime"} and started is None:
            parsed = _parse_timestamp(_text(element.text))
            if parsed:
                started = parsed
        if len(points) > MAX_GEOMETRY_POINTS:
            raise UploadError("The activity has too many geometry points.", code="too_many_points")
    if suffix == ".gpx" and track_type and track_type not in {"bike", "biking", "cycling"}:
        raise UploadError("Only cycling activities are supported.", code="non_cycling")
    if suffix == ".gpx" and not has_track:
        raise UploadError("The activity does not contain a cycling track.", code="non_cycling")
    if len(points) < 2:
        raise UploadError("The activity does not contain a usable track.", code="missing_geometry")
    for lon, lat in points:
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise UploadError("The activity contains invalid coordinates.", code="invalid_geometry")
    return points, started, suffix[1:].upper()


def _parse_fit(data: bytes) -> tuple[list[tuple[float, float]], datetime | None, str]:
    """Read the standard FIT record position fields without third-party code.

    FIT files are binary protocol streams.  Only the definition/data records
    needed for cycling geometry and timestamps are decoded; unknown fields are
    skipped by their declared size, keeping malformed input bounded.
    """

    if len(data) < 14 or data[8:12] != b".FIT":
        raise UploadError("The FIT file header is invalid.", code="invalid_fit")
    header_size = data[0]
    if header_size < 12 or header_size > len(data):
        raise UploadError("The FIT file header is invalid.", code="invalid_fit")
    data_size = struct.unpack_from("<I", data, 4)[0]
    end = header_size + data_size
    if data_size <= 0 or end > len(data) or end > header_size + MAX_FILE_BYTES:
        raise UploadError("The FIT file size is invalid.", code="invalid_fit")
    definitions: dict[int, tuple[str, int, list[tuple[int, int, int]]]] = {}
    points: list[tuple[float, float]] = []
    started: datetime | None = None
    cursor = header_size
    sizes = {0x01: 1, 0x02: 1, 0x83: 2, 0x84: 2, 0x85: 4, 0x86: 4, 0x07: 1, 0x0D: 4}
    last_timestamp: int | None = None
    sport: int | None = None
    while cursor < end:
        record_header = data[cursor]
        cursor += 1
        compressed_timestamp = bool(record_header & 0x80)
        local_number = (
            ((record_header >> 5) & 0x03)
            if compressed_timestamp
            else record_header & 0x0F
        )
        compressed_offset = record_header & 0x1F
        if not compressed_timestamp and record_header & 0x40:
            if cursor + 5 > end:
                raise UploadError("The FIT definition is truncated.", code="invalid_fit")
            cursor += 1  # reserved
            architecture = data[cursor]
            cursor += 1
            endian = ">" if architecture else "<"
            global_number = struct.unpack_from(f"{endian}H", data, cursor)[0]
            cursor += 2
            field_count = data[cursor]
            cursor += 1
            fields: list[tuple[int, int, int]] = []
            for _ in range(field_count):
                if cursor + 3 > end:
                    raise UploadError("The FIT definition is truncated.", code="invalid_fit")
                field_number, field_size, base_type = data[cursor : cursor + 3]
                cursor += 3
                fields.append((field_number, field_size, base_type))
            definitions[local_number] = (endian, global_number, fields)
            continue
        definition = definitions.get(local_number)
        if definition is None:
            raise UploadError("The FIT data record has no definition.", code="invalid_fit")
        endian, global_number, fields = definition
        values: dict[int, int] = {}
        for field_number, field_size, base_type in fields:
            if cursor + field_size > end:
                raise UploadError("The FIT data record is truncated.", code="invalid_fit")
            raw = data[cursor : cursor + field_size]
            cursor += field_size
            base_type &= 0x9F
            if base_type not in sizes or sizes[base_type] != field_size:
                continue
            if base_type in {0x01, 0x02, 0x07, 0x0D}:
                values[field_number] = (
                    raw[0]
                    if field_size == 1
                    else struct.unpack_from(f"{endian}I", raw)[0]
                )
            elif base_type == 0x83:
                values[field_number] = struct.unpack_from(f"{endian}h", raw)[0]
            elif base_type == 0x84:
                values[field_number] = struct.unpack_from(f"{endian}H", raw)[0]
            elif base_type == 0x86:
                values[field_number] = struct.unpack_from(f"{endian}i", raw)[0]
            else:
                values[field_number] = struct.unpack_from(f"{endian}I", raw)[0]
        if compressed_timestamp:
            if last_timestamp is None:
                raise UploadError("The FIT compressed timestamp has no base.", code="invalid_fit")
            timestamp = (last_timestamp & ~0x1F) | compressed_offset
            if timestamp <= last_timestamp:
                timestamp += 0x20
            values[253] = timestamp
        if global_number == 20 and 0 in values and 1 in values:
            if values[0] in {0x7FFFFFFF, -0x80000000} or values[1] in {0x7FFFFFFF, -0x80000000}:
                continue
            points.append((values[1] * 180.0 / 2**31, values[0] * 180.0 / 2**31))
            if len(points) > MAX_GEOMETRY_POINTS:
                raise UploadError(
                    "The activity has too many geometry points.", code="too_many_points"
                )
        if global_number == 18 and 5 in values:
            sport = values[5]
        if 253 in values:
            last_timestamp = values[253]
            if started is None:
                started = datetime.fromtimestamp(values[253] + 631065600, tz=UTC)
    # FIT sport enum 2 is cycling.  Older devices occasionally omit the
    # session message; in that case geometry remains the compatibility signal.
    if sport is not None and sport != 2:
        raise UploadError("Only cycling activities are supported.", code="non_cycling")
    if len(points) < 2:
        raise UploadError("The activity does not contain a usable track.", code="missing_geometry")
    return points, started, "FIT"


def parse_activity(data: bytes, filename: str) -> tuple[list[tuple[float, float]], datetime, str]:
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in SUPPORTED_SUFFIXES:
        raise UploadError("Only FIT, GPX, and TCX files are supported.", code="unsupported_type")
    if suffix == ".fit":
        if len(data) < 12 or data[8:12] != b".FIT":
            raise UploadError("The file content does not match its type.", code="type_mismatch")
        points, started, kind = _parse_fit(data)
        return points, started or timezone.now(), kind
    if not data.lstrip().startswith(b"<"):
        raise UploadError("The file content does not match its type.", code="type_mismatch")
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
            # Keep an explicit per-file result without reading arbitrary
            # unsupported payloads into memory.
            result.append((name, b""))
            continue
        expanded += info.file_size
        if expanded > MAX_EXPANDED_BYTES or info.file_size > MAX_FILE_BYTES:
            raise UploadError("The archive expands beyond the safe limit.", code="archive_bomb")
        with archive.open(info, "r") as source:
            chunks: list[bytes] = []
            read_size = 0
            while True:
                chunk = source.read(min(1024 * 1024, MAX_FILE_BYTES - read_size + 1))
                if not chunk:
                    break
                read_size += len(chunk)
                if read_size > MAX_FILE_BYTES:
                    raise UploadError(
                        "The archive expands beyond the safe limit.", code="archive_bomb"
                    )
                chunks.append(chunk)
            result.append((name, b"".join(chunks)))
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
    expanded_bytes = 0
    for filename, data in files:
        if filename.lower().endswith(".zip"):
            members = _safe_archive_members(data)
            expanded.extend(members)
            expanded_bytes += sum(len(payload) for _, payload in members)
        else:
            if len(data) > MAX_FILE_BYTES:
                raise UploadError("The file exceeds the size limit.", code="file_too_large")
            expanded.append((filename, data))
            expanded_bytes += len(data)
        if expanded_bytes > MAX_BATCH_EXPANDED_BYTES:
            raise UploadError("The batch exceeds the expanded size limit.", code="batch_too_large")
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
            # Source timestamps are mutable metadata and must not make the
            # same track appear to be a new activity on re-upload.
            normalized_points = [(round(lon, 7), round(lat, 7)) for lon, lat in points]
            fingerprint = hashlib.sha256(
                json.dumps(
                    {"kind": kind, "points": normalized_points},
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            # GeoDjango/GDAL is optional for import-time checks and SQLite
            # contract generation.  Only the worker path needs a GEOS object.
            if len(points) >= 2:
                from django.contrib.gis.geos import LineString

                geometry = LineString(points, srid=4326)
            else:
                geometry = None
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
                    try:
                        with transaction.atomic():
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
                    except IntegrityError:
                        # The unique provider id is the final race-safe
                        # duplicate gate when two workers process the same
                        # batch concurrently.
                        activity = ImportedActivity.objects.get(
                            player=batch.player, provider_activity_id=provider_id
                        )
                        upload.status = ActivityUpload.Status.DUPLICATE
                        upload.fingerprint = fingerprint
                        upload.activity = activity
                        upload.content = None
                        upload.processed_at = timezone.now()
                        upload.save(
                            update_fields=(
                                "status",
                                "fingerprint",
                                "activity",
                                "content",
                                "processed_at",
                            )
                        )
                        continue
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
