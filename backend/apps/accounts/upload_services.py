"""Safe FIT/GPX/TCX normalization for provider-neutral activity uploads."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import tempfile
import zipfile
from datetime import UTC, datetime
from typing import Any, BinaryIO, cast
from uuid import uuid4

from defusedxml import ElementTree  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]
from django.core.files import File
from django.db import IntegrityError, connection, transaction
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
UPLOAD_SPOOL_MEMORY = 1024 * 1024
SUPPORTED_SUFFIXES = frozenset({".fit", ".gpx", ".tcx"})
_FIT_CRC_TABLE = (
    0x00,
    0xCC,
    0xD9,
    0x15,
    0xF1,
    0x3D,
    0x28,
    0xE4,
    0xA2,
    0x6E,
    0x7B,
    0xB7,
    0x53,
    0x9F,
    0x8A,
    0x46,
)


class UploadError(ValueError):
    def __init__(self, detail: str, *, code: str = "invalid_file") -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


def _fit_crc(data: bytes) -> int:
    crc = 0
    for value in data:
        nibble = (crc ^ value) & 0x0F
        crc = (crc >> 4) ^ _FIT_CRC_TABLE[nibble]
        nibble = (crc ^ (value >> 4)) & 0x0F
        crc = (crc >> 4) ^ _FIT_CRC_TABLE[nibble]
    return crc


def _fit_crc_update(crc: int, data: bytes) -> int:
    for value in data:
        nibble = (crc ^ value) & 0x0F
        crc = (crc >> 4) ^ _FIT_CRC_TABLE[nibble]
        nibble = (crc ^ (value >> 4)) & 0x0F
        crc = (crc >> 4) ^ _FIT_CRC_TABLE[nibble]
    return crc


def _reconstruct_compressed_timestamp(last_timestamp: int, offset: int) -> int:
    """Expand FIT's five-bit timestamp offset against the previous timestamp."""

    timestamp = (last_timestamp & ~0x1F) | offset
    if timestamp < last_timestamp:
        timestamp += 0x20
    return timestamp


class _LimitedReader:
    """File-like adapter that enforces a maximum XML payload size."""

    def __init__(self, source: BinaryIO, limit: int) -> None:
        self.source = source
        self.limit = limit
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        size = self.limit - self.total + 1 if size < 0 else min(size, self.limit - self.total + 1)
        if size <= 0:
            raise UploadError("The file exceeds the size limit.", code="file_too_large")
        chunk = self.source.read(size)
        self.total += len(chunk)
        if self.total > self.limit:
            raise UploadError("The file exceeds the size limit.", code="file_too_large")
        return chunk


class _FitReader:
    """Bounded FIT data reader that keeps the data checksum incremental."""

    def __init__(self, source: BinaryIO, size: int, crc: int) -> None:
        self.source = source
        self.remaining = size
        self.crc = crc

    def read(self, size: int) -> bytes:
        if size < 0 or size > self.remaining:
            raise UploadError("The FIT data record is truncated.", code="invalid_fit")
        value = self.source.read(size)
        if len(value) != size:
            raise UploadError("The FIT data record is truncated.", code="invalid_fit")
        self.remaining -= size
        self.crc = _fit_crc_update(self.crc, value)
        return value


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


def _parse_xml(
    data: bytes | BinaryIO, suffix: str
) -> tuple[list[tuple[float, float]], datetime | None, str]:
    source = io.BytesIO(data) if isinstance(data, bytes) else _LimitedReader(data, MAX_FILE_BYTES)
    try:
        events = ElementTree.iterparse(source, events=("start", "end"))
        root_name = ""
        points: list[tuple[float, float]] = []
        has_track = False
        track_type = ""
        sports: set[str] = set()
        started: datetime | None = None
        trackpoint_depth = 0
        for event, element in events:
            name = _local_name(element.tag)
            if event == "start":
                if not root_name:
                    root_name = name
                if name == "activity":
                    sports.update(
                        str(value).strip().lower()
                        for key, value in element.attrib.items()
                        if _local_name(key) == "sport"
                    )
                if name == "trackpoint":
                    trackpoint_depth += 1
                continue
            if name == "trkpt":
                has_track = True
                try:
                    lat, lon = float(element.attrib["lat"]), float(element.attrib["lon"])
                except (KeyError, TypeError, ValueError):
                    lat, lon = None, None
                if lat is not None and lon is not None:
                    points.append((lon, lat))
            elif name == "type" and not track_type:
                track_type = _text(element.text).lower()
            elif name == "trackpoint":
                has_track = True
                lat_value: float | None = None
                lon_value: float | None = None
                for child in element.iter():
                    child_name = _local_name(child.tag)
                    if child_name in {"latitude", "position"}:
                        for value in child.iter():
                            if _local_name(value.tag) in {"latitudedegrees", "degrees"}:
                                try:
                                    lat_value = float(_text(value.text))
                                except ValueError:
                                    lat_value = None
                    if child_name in {"longitude", "position"}:
                        for value in child.iter():
                            if _local_name(value.tag) in {"longitudedegrees", "degrees"}:
                                try:
                                    lon_value = float(_text(value.text))
                                except ValueError:
                                    lon_value = None
                if lat_value is not None and lon_value is not None:
                    points.append((lon_value, lat_value))
                trackpoint_depth -= 1
            elif name in {"time", "starttime"} and started is None:
                parsed = _parse_timestamp(_text(element.text))
                if parsed:
                    started = parsed
            if len(points) > MAX_GEOMETRY_POINTS:
                raise UploadError(
                    "The activity has too many geometry points.", code="too_many_points"
                )
            if trackpoint_depth == 0:
                element.clear()
    except (DefusedXmlException, ElementTree.ParseError, ValueError) as exc:
        raise UploadError("The activity file is not valid XML.", code="invalid_xml") from exc
    expected_root = {".gpx": "gpx", ".tcx": "trainingcenterdatabase"}[suffix]
    if root_name != expected_root:
        raise UploadError("The file content does not match its type.", code="type_mismatch")
    if suffix == ".tcx":
        if not sports or not sports.issubset({"biking", "cycling", "bike"}):
            raise UploadError("Only cycling activities are supported.", code="non_cycling")
    if suffix == ".gpx" and track_type not in {"bike", "biking", "cycling"}:
        raise UploadError("Only cycling activities are supported.", code="non_cycling")
    if suffix == ".gpx" and not has_track:
        raise UploadError("The activity does not contain a cycling track.", code="non_cycling")
    if len(points) < 2:
        raise UploadError("The activity does not contain a usable track.", code="missing_geometry")
    for lon, lat in points:
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise UploadError("The activity contains invalid coordinates.", code="invalid_geometry")
    return points, started, suffix[1:].upper()


def _parse_fit(data: bytes | BinaryIO) -> tuple[list[tuple[float, float]], datetime | None, str]:
    """Read the standard FIT record position fields without third-party code.

    FIT files are binary protocol streams.  Only the definition/data records
    needed for cycling geometry and timestamps are decoded; unknown fields are
    skipped by their declared size, keeping malformed input bounded.
    """

    source = io.BytesIO(data) if isinstance(data, bytes) else data
    header_prefix = source.read(12)
    if len(header_prefix) != 12 or header_prefix[8:12] != b".FIT":
        raise UploadError("The FIT file header is invalid.", code="invalid_fit")
    header_size = header_prefix[0]
    if header_size < 12:
        raise UploadError("The FIT file header is invalid.", code="invalid_fit")
    header = header_prefix + source.read(header_size - 12)
    if len(header) != header_size:
        raise UploadError("The FIT file header is invalid.", code="invalid_fit")
    data_size = struct.unpack_from("<I", header, 4)[0]
    if data_size <= 0 or data_size > MAX_FILE_BYTES:
        raise UploadError("The FIT file size is invalid.", code="invalid_fit")
    if header_size >= 14:
        expected_header_crc = struct.unpack_from("<H", header, header_size - 2)[0]
        if _fit_crc(header[: header_size - 2]) != expected_header_crc:
            raise UploadError("The FIT header checksum is invalid.", code="invalid_fit")
    fit_data = _FitReader(source, data_size, _fit_crc(header))
    definitions: dict[int, tuple[str, int, list[tuple[int, int, int]], list[int]]] = {}
    points: list[tuple[float, float]] = []
    started: datetime | None = None
    sizes = {0x01: 1, 0x02: 1, 0x83: 2, 0x84: 2, 0x85: 4, 0x86: 4, 0x07: 1, 0x0D: 4}
    last_timestamp: int | None = None
    sport: int | None = None

    def read_byte() -> int:
        return fit_data.read(1)[0]

    while fit_data.remaining:
        record_header = read_byte()
        compressed_timestamp = bool(record_header & 0x80)
        local_number = (
            ((record_header >> 5) & 0x03) if compressed_timestamp else record_header & 0x0F
        )
        compressed_offset = record_header & 0x1F
        if not compressed_timestamp and record_header & 0x40:
            fit_data.read(1)  # reserved
            architecture = read_byte()
            endian = ">" if architecture else "<"
            global_number = struct.unpack(f"{endian}H", fit_data.read(2))[0]
            field_count = read_byte()
            fields: list[tuple[int, int, int]] = []
            for _ in range(field_count):
                field_number, field_size, base_type = fit_data.read(3)
                fields.append((field_number, field_size, base_type))
            developer_sizes: list[int] = []
            if record_header & 0x20:
                developer_count = read_byte()
                for _ in range(developer_count):
                    fit_data.read(1)  # developer field number
                    developer_sizes.append(read_byte())
                    fit_data.read(1)  # developer data index
            definitions[local_number] = (endian, global_number, fields, developer_sizes)
            continue
        definition = definitions.get(local_number)
        if definition is None:
            raise UploadError("The FIT data record has no definition.", code="invalid_fit")
        endian, global_number, fields, developer_sizes = definition
        values: dict[int, int] = {}
        for field_number, field_size, base_type in fields:
            # A compressed timestamp header replaces the timestamp field in
            # the data message; its bytes are omitted from the payload even
            # though field 253 remains in the definition.
            if compressed_timestamp and field_number == 253:
                continue
            raw = fit_data.read(field_size)
            base_type &= 0x9F
            if base_type not in sizes or sizes[base_type] != field_size:
                continue
            if base_type in {0x01, 0x02, 0x07, 0x0D}:
                values[field_number] = (
                    raw[0] if field_size == 1 else struct.unpack_from(f"{endian}I", raw)[0]
                )
            elif base_type == 0x83:
                values[field_number] = struct.unpack_from(f"{endian}h", raw)[0]
            elif base_type == 0x84:
                values[field_number] = struct.unpack_from(f"{endian}H", raw)[0]
            elif base_type == 0x85:
                values[field_number] = struct.unpack_from(f"{endian}i", raw)[0]
            else:
                values[field_number] = struct.unpack_from(f"{endian}I", raw)[0]
        for developer_size in developer_sizes:
            fit_data.read(developer_size)
        if compressed_timestamp:
            if last_timestamp is None:
                raise UploadError("The FIT compressed timestamp has no base.", code="invalid_fit")
            values[253] = _reconstruct_compressed_timestamp(last_timestamp, compressed_offset)
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
    checksum = source.read(2)
    if len(checksum) != 2 or struct.unpack("<H", checksum)[0] != fit_data.crc:
        raise UploadError("The FIT data checksum is invalid.", code="invalid_fit")
    # Session sport is mandatory evidence. FIT enum 2 is cycling; geometry
    # alone is not enough to classify an upload as a ride.
    if sport != 2:
        raise UploadError("Only cycling activities are supported.", code="non_cycling")
    if len(points) < 2:
        raise UploadError("The activity does not contain a usable track.", code="missing_geometry")
    return points, started, "FIT"


def parse_activity(
    data: bytes | BinaryIO, filename: str
) -> tuple[list[tuple[float, float]], datetime, str]:
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in SUPPORTED_SUFFIXES:
        raise UploadError("Only FIT, GPX, and TCX files are supported.", code="unsupported_type")
    if isinstance(data, bytes):
        prefix = data[:12]
    else:
        position = data.tell()
        prefix = data.read(12)
        data.seek(position)
    if suffix == ".fit":
        if len(prefix) < 12 or prefix[8:12] != b".FIT":
            raise UploadError("The file content does not match its type.", code="type_mismatch")
        points, started, kind = _parse_fit(data)
        return points, started or timezone.now(), kind
    if not prefix.lstrip().startswith(b"<"):
        raise UploadError("The file content does not match its type.", code="type_mismatch")
    points, started, kind = _parse_xml(data, suffix)
    return points, started or timezone.now(), kind


UploadPayload = bytes | BinaryIO


def _payload_stream(payload: UploadPayload) -> tuple[BinaryIO, bool]:
    if isinstance(payload, bytes):
        return io.BytesIO(payload), True
    try:
        payload.seek(0)
    except (AttributeError, OSError) as exc:
        raise UploadError("The uploaded file cannot be read safely.", code="invalid_file") from exc
    return payload, False


def _payload_size(payload: UploadPayload) -> int:
    if isinstance(payload, bytes):
        return len(payload)
    try:
        position = payload.tell()
        payload.seek(0, 2)
        size = payload.tell()
        payload.seek(position)
        return size
    except (AttributeError, OSError) as exc:
        raise UploadError("The uploaded file cannot be read safely.", code="invalid_file") from exc


def _archive_infos(payload: UploadPayload) -> tuple[list[zipfile.ZipInfo], int, bool]:
    source, close_source = _payload_stream(payload)
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        if close_source:
            source.close()
        raise UploadError("The ZIP archive is invalid.", code="invalid_archive") from exc
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_FILES:
        archive.close()
        if close_source:
            source.close()
        raise UploadError("The archive contains too many files.", code="too_many_files")
    expanded = 0
    supported = False
    for info in infos:
        name = info.filename.replace("\\", "/")
        is_symlink = (info.external_attr >> 16) & 0o170000 == 0o120000
        if (
            info.is_dir()
            or is_symlink
            or name.startswith("/")
            or name.split("/", 1)[0].endswith(":")
            or ".." in name.split("/")
        ):
            archive.close()
            if close_source:
                source.close()
            raise UploadError("The archive contains an unsafe path.", code="unsafe_archive")
        suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if suffix == ".zip":
            archive.close()
            if close_source:
                source.close()
            raise UploadError("Nested archives are not supported.", code="nested_archive")
        expanded += info.file_size
        if expanded > MAX_EXPANDED_BYTES or info.file_size > MAX_FILE_BYTES:
            archive.close()
            if close_source:
                source.close()
            raise UploadError("The archive expands beyond the safe limit.", code="archive_bomb")
        if suffix in SUPPORTED_SUFFIXES:
            supported = True
    archive.close()
    if close_source:
        source.close()
    return infos, expanded, supported


def _stream_digest(source: BinaryIO, *, limit: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = source.read(min(UPLOAD_SPOOL_MEMORY, limit - size + 1))
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise UploadError("The file exceeds the size limit.", code="file_too_large")
        digest.update(chunk)
    return digest.hexdigest(), size


def _store_stream(upload: ActivityUpload, source: BinaryIO, *, limit: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with tempfile.SpooledTemporaryFile(max_size=UPLOAD_SPOOL_MEMORY, mode="w+b") as spool:
        while True:
            chunk = source.read(min(UPLOAD_SPOOL_MEMORY, limit - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise UploadError("The file exceeds the size limit.", code="file_too_large")
            digest.update(chunk)
            spool.write(chunk)
        spool.seek(0)
        storage_name = f"{uuid4().hex}.bin"
        try:
            upload.content_path.save(storage_name, File(spool), save=False)
        except Exception:
            # FileField.save can have written the object before propagating a
            # storage/backend error. Remove both the caller name and the
            # upload_to-expanded name when available.
            names = {storage_name}
            if upload.content_path.name:
                names.add(upload.content_path.name)
            for name in names:
                upload.content_path.storage.delete(name)
            upload.content_path = None
            raise
    return digest.hexdigest(), size


def _safe_archive_members(data: bytes) -> list[tuple[str, bytes]]:
    """Compatibility helper for callers that explicitly request materialization.

    The API and batch worker use the streaming archive path below; this helper
    remains intentionally bounded for legacy tests and management callers.
    """
    if len(data) > MAX_ARCHIVE_BYTES:
        raise UploadError("The archive exceeds the size limit.", code="archive_too_large")
    spool = tempfile.SpooledTemporaryFile(max_size=UPLOAD_SPOOL_MEMORY, mode="w+b")
    spool.write(data)
    spool.seek(0)
    try:
        archive = zipfile.ZipFile(spool)
    except zipfile.BadZipFile as exc:
        spool.close()
        raise UploadError("The ZIP archive is invalid.", code="invalid_archive") from exc
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_FILES:
        raise UploadError("The archive contains too many files.", code="too_many_files")
    expanded = 0
    result: list[tuple[str, bytes]] = []
    for info in infos:
        name = info.filename.replace("\\", "/")
        is_symlink = (info.external_attr >> 16) & 0o170000 == 0o120000
        if (
            info.is_dir()
            or is_symlink
            or name.startswith("/")
            or name.split("/", 1)[0].endswith(":")
            or ".." in name.split("/")
        ):
            raise UploadError("The archive contains an unsafe path.", code="unsafe_archive")
        suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if suffix == ".zip":
            raise UploadError("Nested archives are not supported.", code="nested_archive")
        expanded += info.file_size
        if expanded > MAX_EXPANDED_BYTES or info.file_size > MAX_FILE_BYTES:
            raise UploadError("The archive expands beyond the safe limit.", code="archive_bomb")
        if suffix not in SUPPORTED_SUFFIXES:
            # Keep an explicit per-file result without reading arbitrary
            # unsupported payloads into memory.
            result.append((name, b""))
            continue
        with archive.open(info, "r") as member_source:
            chunks: list[bytes] = []
            read_size = 0
            while True:
                chunk = member_source.read(min(1024 * 1024, MAX_FILE_BYTES - read_size + 1))
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
        archive.close()
        spool.close()
        raise UploadError(
            "The archive contains no supported activity files.", code="no_supported_files"
        )
    archive.close()
    spool.close()
    return result


def create_batch(
    player: Player, files: list[tuple[str, UploadPayload]], *, attested: bool
) -> ActivityUploadBatch:
    if not attested:
        raise UploadError(
            "You must attest that you own or may process this data.", code="attestation_required"
        )
    if not files or len(files) > MAX_ACTIVITY_COUNT:
        raise UploadError("The batch contains too many files.", code="too_many_files")
    planned: list[tuple[str, UploadPayload, zipfile.ZipInfo | None]] = []
    expanded_bytes = 0
    for filename, payload in files:
        if filename.lower().endswith(".zip"):
            archive_size = _payload_size(payload)
            if archive_size > MAX_ARCHIVE_BYTES:
                raise UploadError("The archive exceeds the size limit.", code="archive_too_large")
            infos, expanded, supported = _archive_infos(payload)
            if not supported:
                raise UploadError(
                    "The archive contains no supported activity files.", code="no_supported_files"
                )
            expanded_bytes += expanded
            source, close_source = _payload_stream(payload)
            try:
                archive = zipfile.ZipFile(source)
                for info in infos:
                    planned.append((info.filename.replace("\\", "/"), payload, info))
                archive.close()
            finally:
                if close_source:
                    source.close()
        else:
            size = _payload_size(payload)
            if size > MAX_FILE_BYTES:
                raise UploadError("The file exceeds the size limit.", code="file_too_large")
            planned.append((filename, payload, None))
            expanded_bytes += size
        if expanded_bytes > MAX_BATCH_EXPANDED_BYTES:
            raise UploadError("The batch exceeds the expanded size limit.", code="batch_too_large")
    if len(planned) > MAX_ACTIVITY_COUNT:
        raise UploadError("The batch contains too many activities.", code="too_many_files")
    staged: list[ActivityUpload] = []
    try:
        with transaction.atomic():
            batch = ActivityUploadBatch.objects.create(
                player=player, total_files=len(planned), attested=True
            )
            for name, payload, member_info in planned:
                suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
                upload = ActivityUpload(batch=batch, original_name=name[:240], size_bytes=0)
                zip_archive: zipfile.ZipFile | None = None
                archive_source: BinaryIO | None = None
                close_source = False
                if member_info is None:
                    source, close_source = _payload_stream(payload)
                else:
                    source, close_source = _payload_stream(payload)
                    archive_source = source
                    zip_archive = zipfile.ZipFile(source)
                    source = cast(BinaryIO, zip_archive.open(member_info, "r"))
                try:
                    if suffix in SUPPORTED_SUFFIXES:
                        digest, size = _store_stream(upload, source, limit=MAX_FILE_BYTES)
                        # Track immediately: model.save() and the enclosing
                        # transaction can both fail after storage succeeds.
                        staged.append(upload)
                    else:
                        digest, size = _stream_digest(source, limit=MAX_FILE_BYTES)
                        upload.content = b""
                    upload.content_sha256 = digest
                    upload.size_bytes = size
                    upload.save()
                finally:
                    if member_info is not None or close_source:
                        source.close()
                    if zip_archive is not None:
                        zip_archive.close()
                    if archive_source is not None and close_source:
                        archive_source.close()
                    elif archive_source is None and close_source:
                        source.close()
        return batch
    except Exception:
        for upload in staged:
            if upload.content_path:
                upload.content_path.delete(save=False)
        raise


def _open_upload_payload(upload: ActivityUpload) -> BinaryIO:
    if upload.content_path:
        return cast(BinaryIO, upload.content_path.open("rb"))
    if upload.content is not None:
        return io.BytesIO(bytes(upload.content))
    raise UploadError("The upload payload is no longer available.", code="missing_payload")


def _clear_upload_payload(upload: ActivityUpload) -> None:
    if upload.content_path:
        upload.content_path.delete(save=False)
    upload.content = None
    upload.content_path = None


def process_batch(batch_id: Any) -> ActivityUploadBatch:
    batch = ActivityUploadBatch.objects.get(pk=batch_id)
    ActivityUploadBatch.objects.filter(pk=batch.pk).update(
        status=ActivityUploadBatch.Status.RUNNING
    )
    for upload in batch.files.filter(status=ActivityUpload.Status.QUEUED).order_by("pk"):
        # Claim before reading transient content. A second worker skips the
        # row instead of turning an accepted result into a duplicate/failure.
        claimed = ActivityUpload.objects.filter(
            pk=upload.pk, status=ActivityUpload.Status.QUEUED
        ).update(status=ActivityUpload.Status.PROCESSING)
        if not claimed:
            continue
        upload.status = ActivityUpload.Status.PROCESSING
        try:
            with _open_upload_payload(upload) as data:
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
            if len(points) >= 2 and connection.vendor != "sqlite":
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
                    else:
                        upload.status = ActivityUpload.Status.ACCEPTED
                        upload.fingerprint = fingerprint
                        upload.activity = activity
                        _schedule_player_recomputations(batch.player.pk)
                _clear_upload_payload(upload)
                upload.processed_at = timezone.now()
                upload.save(
                    update_fields=(
                        "status",
                        "fingerprint",
                        "activity",
                        "content",
                        "content_path",
                        "processed_at",
                    )
                )
        except UploadError as exc:
            upload.status = (
                ActivityUpload.Status.UNSUPPORTED
                if exc.code == "unsupported_type"
                else ActivityUpload.Status.FAILED
            )
            upload.error_code, upload.error_detail = exc.code, exc.detail
            _clear_upload_payload(upload)
            upload.processed_at = timezone.now()
            upload.save(
                update_fields=(
                    "status",
                    "error_code",
                    "error_detail",
                    "content",
                    "content_path",
                    "processed_at",
                )
            )
        except Exception:
            upload.status = ActivityUpload.Status.FAILED
            upload.error_code, upload.error_detail = (
                "processing_failed",
                "The activity could not be processed.",
            )
            _clear_upload_payload(upload)
            upload.processed_at = timezone.now()
            upload.save(
                update_fields=(
                    "status",
                    "error_code",
                    "error_detail",
                    "content",
                    "content_path",
                    "processed_at",
                )
            )
        # Publish progress after every file, so a client can recover from a
        # worker interruption and distinguish a partial batch from a terminal
        # failure.  Counts are derived from committed rows, not worker memory.
        statuses_now = list(batch.files.values_list("status", flat=True))
        terminal_now = [
            status
            for status in statuses_now
            if status
            in {
                ActivityUpload.Status.ACCEPTED,
                ActivityUpload.Status.DUPLICATE,
                ActivityUpload.Status.FAILED,
                ActivityUpload.Status.UNSUPPORTED,
            }
        ]
        ActivityUploadBatch.objects.filter(pk=batch.pk).update(
            status=ActivityUploadBatch.Status.RUNNING,
            processed_files=len(terminal_now),
            accepted_files=terminal_now.count(ActivityUpload.Status.ACCEPTED),
            duplicate_files=terminal_now.count(ActivityUpload.Status.DUPLICATE),
            failed_files=sum(
                status in {ActivityUpload.Status.FAILED, ActivityUpload.Status.UNSUPPORTED}
                for status in terminal_now
            ),
        )
    counts = batch.files.values_list("status", flat=True)
    statuses = list(counts)
    terminal = [
        status
        for status in statuses
        if status
        in {
            ActivityUpload.Status.ACCEPTED,
            ActivityUpload.Status.DUPLICATE,
            ActivityUpload.Status.FAILED,
            ActivityUpload.Status.UNSUPPORTED,
        }
    ]
    batch.processed_files = len(terminal)
    batch.accepted_files = terminal.count(ActivityUpload.Status.ACCEPTED)
    batch.duplicate_files = terminal.count(ActivityUpload.Status.DUPLICATE)
    batch.failed_files = sum(
        status in {ActivityUpload.Status.FAILED, ActivityUpload.Status.UNSUPPORTED}
        for status in terminal
    )
    if len(terminal) < len(statuses):
        batch.status = ActivityUploadBatch.Status.RUNNING
        batch.completed_at = None
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
    if batch.failed_files == 0:
        batch.status = ActivityUploadBatch.Status.COMPLETED
    elif batch.accepted_files == 0 and batch.duplicate_files == 0:
        batch.status = ActivityUploadBatch.Status.FAILED
    else:
        batch.status = ActivityUploadBatch.Status.PARTIAL
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
