"""Safe GPX extraction, validation, and immutable catalogue import.

The exporter is intentionally hidden behind :class:`GpxExporter`.  Importing
this module therefore does not make the third-party client part of the
catalogue API, and tests can use a small in-memory exporter.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlparse
from xml.etree.ElementTree import Element

import httpx
from defusedxml import ElementTree  # type: ignore[import-untyped]
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from apps.catalogue.deduplication import classify_version, normalize_geometry
from apps.catalogue.models import LoopStatus, ProcessingStatus, RouteLifecycle, RouteVersion
from apps.catalogue.services import approve_version, record_route_version

from .models import (
    ExtractionAttempt,
    ExtractionStatus,
    OrphanPayloadCleanup,
    OrphanPayloadStatus,
)


class GpxExtractionError(Exception):
    """A source could not be safely converted into a technically valid GPX."""


class UnsafeGpxUrlError(GpxExtractionError):
    """The source URL or a redirect target is not safe to request."""


class GpxValidationError(GpxExtractionError):
    """The payload is not a usable GPX document."""


class GpxPayloadTooLarge(GpxExtractionError):
    """A response exceeded the hard byte cap while it was being streamed."""


class GpxExtractionTaskFailure(GpxExtractionError):
    """Terminal task error after a failed attempt has been durably recorded."""


class GpxExporter(Protocol):
    """Replaceable boundary for the Mapy GPX exporter."""

    def fetch_gpx(self, url: str) -> bytes | ExportedGpx: ...


@dataclass(frozen=True)
class ExportedGpx:
    """Exporter response with optional transport metadata."""

    content: bytes
    content_type: str = "application/gpx+xml"
    final_url: str = ""


@dataclass(frozen=True)
class ParsedGpx:
    """Normalized GPX data and optional metrics."""

    geometry: dict[str, Any]
    elevations: tuple[float | None, ...]
    distance_m: float
    ascent_m: float | None
    descent_m: float | None
    loop_status: str
    title: str


_MAPY_HOSTS = {"mapy.com", "mapy.cz"}
_ALLOWED_CONTENT_TYPES = {
    "application/gpx+xml",
    "application/xml",
    "text/xml",
    "application/octet-stream",
}
_EARTH_RADIUS_M = 6_371_008.8
_GPX_NAMESPACES = {
    "http://www.topografix.com/GPX/1/0",
    "http://www.topografix.com/GPX/1/1",
}


def _setting(name: str, default: Any) -> Any:
    return getattr(settings, name, default)


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    try:
        addresses = {
            str(item[4][0]) for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise UnsafeGpxUrlError(f"Could not resolve GPX source host: {hostname}") from exc
    public = tuple(sorted(address for address in addresses if _is_public_ip(address)))
    if not public or len(public) != len(addresses):
        raise UnsafeGpxUrlError(f"GPX source resolves to a private network: {hostname}")
    return public


def validate_gpx_url(url: str, *, check_dns: bool | None = None) -> str:
    """Validate a Mapy URL before any network request is made.

    Only HTTPS Mapy hosts are accepted.  DNS answers are checked as well so a
    compromised/overridden Mapy hostname cannot be used to reach local cloud
    metadata or private network services.
    """

    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise UnsafeGpxUrlError(f"Malformed GPX source URL: {url!r}") from exc
    host_parts = hostname.split(".")
    valid_host = hostname in _MAPY_HOSTS or (
        len(host_parts) == 3
        and host_parts[1] == "mapy"
        and host_parts[2] in {"com", "cz"}
        and (host_parts[0] == "www" or len(host_parts[0]) == 2 and host_parts[0].isalpha())
    )
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and port != 443)
        or not valid_host
    ):
        raise UnsafeGpxUrlError(f"Only HTTPS Mapy URLs are supported: {url}")
    if check_dns is None:
        check_dns = bool(_setting("GPX_DNS_CHECK", True))
    if check_dns:
        _public_addresses(hostname, port or 443)
    return url


def validate_gpx_content_type(content_type: str) -> None:
    """Reject non-XML responses before parsing them as GPX."""

    if not content_type:
        return
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type not in _ALLOWED_CONTENT_TYPES:
        raise GpxValidationError(f"Unsupported GPX response content type: {content_type!r}")


class MapyGpxExporterAdapter:
    """Adapter around ``mapy-gpx-exporter[frpc]`` with bounded retries."""

    def __init__(
        self,
        *,
        timeout: float | None = None,
        retries: int | None = None,
        backoff: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout = float(timeout if timeout is not None else _setting("GPX_TIMEOUT", 30.0))
        self.retries = max(1, int(retries if retries is not None else _setting("GPX_RETRIES", 3)))
        self.backoff = float(backoff if backoff is not None else _setting("GPX_BACKOFF", 0.5))
        self._sleep = sleep

    def fetch_gpx(self, url: str) -> ExportedGpx:
        validate_gpx_url(url)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                # Use the dependency's public resolver/exporter functions,
                # passing our guarded client into every FRPC, redirect, POI,
                # altitude, and GPX request.  The convenience client cannot
                # expose those transport controls.
                from mapy_gpx_exporter.exporter import export_gpx  # type: ignore[import-untyped]
                from mapy_gpx_exporter.resolver import (  # type: ignore[import-untyped]
                    resolve_short_link,
                )

                client = _GuardedHttpClient(
                    timeout=self.timeout,
                    max_bytes=int(_setting("GPX_MAX_BYTES", 10 * 1024 * 1024)),
                )
                try:
                    route = resolve_short_link(client, url)
                    requests_before_export = client.request_count
                    content = export_gpx(client, route)
                    response = client.last_response
                finally:
                    client.close()
                if not isinstance(content, bytes):
                    raise GpxExtractionError("The exporter returned a non-byte GPX payload")
                if route.resolution_method in {"local_decode", "local_waypoint"}:
                    # The dependency generated this XML locally from resolved
                    # route data; a preceding FRPC response is not the GPX
                    # response and its content type must not be reused.
                    return ExportedGpx(content=content)
                if client.request_count == requests_before_export:
                    raise GpxExtractionError("The exporter produced no GPX network response")
                if response is None:
                    return ExportedGpx(content=content)
                content_type = response.headers.get("content-type", "")
                if not content_type:
                    raise GpxValidationError("The exporter response has no Content-Type")
                validate_gpx_content_type(content_type)
                return ExportedGpx(
                    content=content,
                    content_type=content_type,
                    final_url=str(response.url),
                )
            except UnsafeGpxUrlError:
                raise
            except Exception as exc:  # exporter exceptions are diagnostics for the attempt
                last_error = exc
                if attempt + 1 < self.retries:
                    self._sleep(self.backoff * (2**attempt))
        raise GpxExtractionError(
            f"GPX exporter failed after {self.retries} attempts: {last_error}"
        ) from last_error


class _GuardedHttpClient:
    """Small ``httpx.Client``-compatible transport facade for the exporter.

    Upstream calls ``get`` and ``post`` and expects a buffered response.  We
    retain that API while reading each response in bounded chunks, rejecting
    every request URL immediately before connection and disabling automatic
    redirects.  The upstream resolver then explicitly follows redirects,
    causing this same guard to run for each hop.
    """

    def __init__(self, *, timeout: float, max_bytes: int) -> None:
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=PinnedMapyTransport(max_bytes=max_bytes),
        )
        self.max_bytes = max_bytes
        self.last_response: httpx.Response | None = None
        self.request_count = 0

    def close(self) -> None:
        self._client.close()

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._request("POST", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        validate_gpx_url(str(url))
        if kwargs.pop("follow_redirects", False):
            raise UnsafeGpxUrlError("Automatic redirects are disabled for GPX requests")
        chunks: list[bytes] = []
        size = 0
        self.request_count += 1
        with self._client.stream(method, url, follow_redirects=False, **kwargs) as response:
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > self.max_bytes:
                    raise GpxPayloadTooLarge(
                        f"GPX response exceeds the {self.max_bytes}-byte limit"
                    )
                chunks.append(chunk)
            bounded = httpx.Response(
                response.status_code,
                headers=response.headers,
                content=b"".join(chunks),
                # The transport connects to an IP, but callers must observe
                # the validated logical URL (including query parameters).
                request=httpx.Request(
                    response.request.method,
                    response.request.url.copy_with(host=urlparse(str(url)).hostname),
                    headers=response.request.headers,
                ),
            )
        self.last_response = bounded
        return bounded


class PinnedMapyTransport(httpx.BaseTransport):
    """HTTP transport that connects to the validated DNS answer itself."""

    def __init__(self, *, max_bytes: int) -> None:
        self._transports: dict[tuple[str, str], httpx.HTTPTransport] = {}
        self.max_bytes = max_bytes

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        original_host = request.url.host
        validate_gpx_url(str(request.url))
        addresses = _public_addresses(original_host, request.url.port or 443)
        # The address is resolved immediately before the underlying transport
        # connects.  HTTPX therefore cannot perform another hostname lookup.
        pinned_url = request.url.copy_with(host=addresses[0])
        headers = request.headers.copy()
        headers["host"] = original_host
        pinned_request = httpx.Request(
            request.method,
            pinned_url,
            headers=headers,
            content=request.content,
            extensions={**request.extensions, "sni_hostname": original_host},
        )
        transport_key = (original_host, addresses[0])
        transport = self._transports.get(transport_key)
        if transport is None:
            transport = httpx.HTTPTransport(trust_env=False, retries=0)
            self._transports[transport_key] = transport
        response = transport.handle_request(pinned_request)
        return response

    def close(self) -> None:
        for transport in self._transports.values():
            transport.close()
        self._transports.clear()


def _local_name(element: Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower() if isinstance(element.tag, str) else ""


def _namespace(element: Element) -> str:
    return (
        element.tag[1:].split("}", 1)[0]
        if isinstance(element.tag, str) and element.tag.startswith("{")
        else ""
    )


def _gpx_element(element: Element, name: str, namespace: str) -> bool:
    return _local_name(element) == name and _namespace(element) == namespace


def _coordinate(raw: str, axis: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise GpxValidationError(f"Invalid {axis} coordinate: {raw!r}") from exc
    limit = 90.0 if axis == "latitude" else 180.0
    if not math.isfinite(value) or not -limit <= value <= limit:
        raise GpxValidationError(f"Invalid {axis} coordinate: {raw!r}")
    return round(value, 7)


def _optional_elevation(point: Element, namespace: str) -> float | None:
    for child in point:
        if not _gpx_element(child, "ele", namespace) or child.text is None:
            continue
        try:
            value = float(child.text.strip())
        except ValueError:
            return None
        return value if math.isfinite(value) else None
    return None


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    d_lat = lat2 - lat1
    d_lon = math.radians(b[0] - a[0])
    haversine = (
        math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, haversine)))


def parse_gpx(
    payload: bytes, *, max_bytes: int | None = None, max_points: int | None = None
) -> ParsedGpx:
    """Parse a GPX payload with XXE and resource limits enabled."""

    if not isinstance(payload, bytes) or not payload:
        raise GpxValidationError("GPX payload is empty")
    max_bytes = int(
        max_bytes if max_bytes is not None else _setting("GPX_MAX_BYTES", 10 * 1024 * 1024)
    )
    max_points = int(max_points if max_points is not None else _setting("GPX_MAX_POINTS", 200_000))
    if len(payload) > max_bytes:
        raise GpxValidationError(f"GPX payload exceeds the {max_bytes}-byte limit")
    try:
        root = ElementTree.fromstring(payload)
    except Exception as exc:  # defusedxml raises several parser-specific exceptions
        raise GpxValidationError(f"GPX XML parsing failed: {exc}") from exc
    if _local_name(root) != "gpx":
        raise GpxValidationError("The XML document root is not GPX")
    namespace = _namespace(root)
    if namespace not in _GPX_NAMESPACES:
        raise GpxValidationError("The GPX namespace is unsupported")
    version = root.attrib.get("version")
    if version not in {"1.0", "1.1"}:
        raise GpxValidationError("GPX version must be 1.0 or 1.1")
    expected_namespace = f"http://www.topografix.com/GPX/1/{version[-1]}"
    if namespace != expected_namespace:
        raise GpxValidationError("GPX namespace and version do not match")

    containers = [element for element in root if _local_name(element) in {"trk", "rte", "wpt"}]
    if any(_namespace(element) != namespace for element in containers):
        raise GpxValidationError("A GPX route container is outside the GPX namespace")
    tracks_containers = [element for element in containers if _local_name(element) == "trk"]
    routes_containers = [element for element in containers if _local_name(element) == "rte"]
    if len(tracks_containers) > 1 or len(routes_containers) > 1:
        raise GpxValidationError("Multiple GPX route containers are unsupported")
    if tracks_containers and routes_containers:
        raise GpxValidationError("GPX cannot contain both track and route containers")

    segments = [element for element in root.iter() if _local_name(element) == "trkseg"]
    if any(_namespace(element) != namespace for element in segments):
        raise GpxValidationError("A track segment is outside the GPX namespace")
    if len(segments) > 1:
        raise GpxValidationError(
            "Multiple GPX track segments are unsupported by the LineString geometry field"
        )
    tracks = (
        [element for element in segments[0] if _gpx_element(element, "trkpt", namespace)]
        if segments
        else []
    )
    if segments and any(
        _local_name(element) == "trkpt" and not _gpx_element(element, "trkpt", namespace)
        for element in segments[0]
    ):
        raise GpxValidationError("A track point is outside the GPX namespace")
    if tracks_containers and not segments:
        raise GpxValidationError("A GPX track must contain one track segment")
    if not tracks and routes_containers:
        route = routes_containers[0]
        tracks = [element for element in route if _gpx_element(element, "rtept", namespace)]
        if any(
            _local_name(element) == "rtept" and not _gpx_element(element, "rtept", namespace)
            for element in route
        ):
            raise GpxValidationError("A route point is outside the GPX namespace")
    if not tracks and not tracks_containers and not routes_containers:
        # Standalone waypoints are accepted in document order, which is a
        # deterministic LineString source for point-to-point place exports.
        tracks = [element for element in containers if _gpx_element(element, "wpt", namespace)]
        if any(
            _local_name(element) == "wpt" and not _gpx_element(element, "wpt", namespace)
            for element in containers
        ):
            raise GpxValidationError("A waypoint is outside the GPX namespace")
    if not tracks:
        raise GpxValidationError("GPX contains no direct route points")
    if len(tracks) > max_points:
        raise GpxValidationError(f"GPX contains more than the {max_points}-point limit")
    if len(tracks) < 2:
        raise GpxValidationError("GPX must contain at least two route points")

    coordinates: list[list[float]] = []
    elevations: list[float | None] = []
    for point in tracks:
        if "lat" not in point.attrib or "lon" not in point.attrib:
            raise GpxValidationError("A GPX point is missing latitude or longitude")
        latitude = _coordinate(point.attrib["lat"], "latitude")
        longitude = _coordinate(point.attrib["lon"], "longitude")
        coordinates.append([longitude, latitude])
        elevations.append(_optional_elevation(point, namespace))

    distance_m = sum(
        _distance((left[0], left[1]), (right[0], right[1]))
        for left, right in zip(coordinates, coordinates[1:], strict=False)
    )
    elevation_deltas: list[float] = []
    for previous, current in zip(elevations, elevations[1:], strict=False):
        if previous is not None and current is not None:
            elevation_deltas.append(current - previous)
    ascent_m = sum(delta for delta in elevation_deltas if delta > 0) if elevation_deltas else None
    descent_m = -sum(delta for delta in elevation_deltas if delta < 0) if elevation_deltas else None
    loop_status = (
        LoopStatus.LOOP
        if _distance(
            (coordinates[0][0], coordinates[0][1]), (coordinates[-1][0], coordinates[-1][1])
        )
        <= 50
        else LoopStatus.POINT_TO_POINT
    )
    title = next(
        (
            element.text.strip()
            for element in root.iter()
            if _gpx_element(element, "name", namespace) and element.text and element.text.strip()
        ),
        "",
    )
    return ParsedGpx(
        geometry={"type": "LineString", "coordinates": coordinates},
        elevations=tuple(elevations),
        distance_m=round(distance_m, 2),
        ascent_m=round(ascent_m, 2) if ascent_m is not None else None,
        descent_m=round(descent_m, 2) if descent_m is not None else None,
        loop_status=loop_status,
        title=title,
    )


def _elevation_profile(parsed: ParsedGpx, *, max_points: int = 500) -> list[dict[str, float]]:
    """Return a bounded distance/elevation series for the public detail view."""

    coordinates = parsed.geometry["coordinates"]
    points: list[dict[str, float]] = []
    distance_m = 0.0
    for index, elevation in enumerate(parsed.elevations):
        if index:
            distance_m += _distance(tuple(coordinates[index - 1]), tuple(coordinates[index]))
        if elevation is not None and math.isfinite(elevation):
            points.append({"distance_m": round(distance_m, 2), "elevation_m": round(elevation, 2)})
    if len(points) <= max_points:
        return points
    stride = (len(points) - 1) / (max_points - 1)
    return [points[round(index * stride)] for index in range(max_points)]


def _export(adapter: object, source_url: str) -> ExportedGpx:
    fetch = getattr(adapter, "fetch_gpx", None) or getattr(adapter, "export", None)
    if fetch is None and callable(adapter):
        fetch = adapter
    if fetch is None:
        raise GpxExtractionError("The GPX adapter must provide fetch_gpx() or export()")
    result = fetch(source_url)
    if isinstance(result, ExportedGpx):
        validate_gpx_content_type(result.content_type)
        if result.final_url:
            validate_gpx_url(result.final_url)
        return result
    if isinstance(result, bytes):
        return ExportedGpx(content=result)
    raise GpxExtractionError("The GPX adapter returned an unsupported response")


def _attempt_number(source_id: int) -> int:
    return (
        ExtractionAttempt.objects.filter(source_id=source_id)
        .order_by("-attempt_number")
        .values_list("attempt_number", flat=True)
        .first()
        or 0
    ) + 1


def _failure(
    source_id: int, attempt_id: int, error: Exception, *, diagnostics: dict[str, Any]
) -> dict[str, Any]:
    message = str(error)[:4000]
    now = timezone.now()
    from apps.catalogue.models import Route, RouteSource

    with transaction.atomic():
        route_id = RouteSource.objects.values_list("route_id", flat=True).get(pk=source_id)
        Route.objects.select_for_update().get(pk=route_id)
        source = RouteSource.objects.select_for_update().get(pk=source_id)
        attempt = ExtractionAttempt.objects.select_for_update().get(pk=attempt_id)
        attempt.status = ExtractionStatus.FAILED
        attempt.error = message
        attempt.diagnostics = diagnostics
        attempt.finished_at = now
        attempt.save(update_fields=["status", "error", "diagnostics", "finished_at"])
        latest_id = (
            ExtractionAttempt.objects.filter(source_id=source_id)
            .order_by("-pk")
            .values_list("pk", flat=True)
            .first()
        )
        if latest_id == attempt_id and source.processing_status == ProcessingStatus.PROCESSING:
            source.processing_status = ProcessingStatus.FAILED
            source.processed_at = now
            source.last_checked_at = now
            source.last_error = message
            source.save(
                update_fields=[
                    "processing_status",
                    "processed_at",
                    "last_checked_at",
                    "last_error",
                ]
            )
    return {
        "status": ExtractionStatus.FAILED,
        "source_id": source_id,
        "attempt_id": attempt_id,
        "error": message,
        "diagnostics": diagnostics,
    }


def extract_gpx(source_id: int, *, adapter: object | None = None) -> dict[str, Any]:
    """Extract and import one source, isolating failures to that source."""

    from apps.catalogue.models import Route, RouteSource, SourceDenylistEntry

    with transaction.atomic():
        source = RouteSource.objects.get(pk=source_id)
        route = Route.objects.select_for_update().get(pk=source.route_id)
        source = RouteSource.objects.select_for_update().get(pk=source_id)
        attempt = ExtractionAttempt.objects.create(
            source=source,
            source_url=source.mapy_url,
            attempt_number=_attempt_number(source.pk),
            status=ExtractionStatus.PROCESSING,
            started_at=timezone.now(),
        )
        source.processing_status = ProcessingStatus.PROCESSING
        source.last_error = ""
        source.save(update_fields=["processing_status", "last_error"])
        blocked_reason = (
            "Source is denylisted."
            if SourceDenylistEntry.objects.filter(source_url=source.mapy_url, active=True).exists()
            else "Route is removed."
            if route.lifecycle == RouteLifecycle.SOFT_DELETED
            else ""
        )
    if blocked_reason:
        return _failure(
            source_id,
            attempt.pk,
            GpxExtractionError(blocked_reason),
            diagnostics={"blocked_before_download": True},
        )
    saved_storage_key: str | None = None
    try:
        exported = _export(adapter or MapyGpxExporterAdapter(), source.mapy_url)
        parsed = parse_gpx(exported.content)
        checksum = hashlib.sha256(exported.content).hexdigest()
        attempt.checksum = checksum
        diagnostics: dict[str, Any] = {
            "point_count": len(parsed.geometry["coordinates"]),
            "distance_m": parsed.distance_m,
            "loop_status": parsed.loop_status,
        }
        with transaction.atomic():
            route = Route.objects.select_for_update().get(pk=source.route_id)
            source = RouteSource.objects.select_for_update().get(pk=source.pk)
            latest_attempt_id = (
                ExtractionAttempt.objects.filter(source_id=source.pk)
                .order_by("-pk")
                .values_list("pk", flat=True)
                .first()
            )
            if latest_attempt_id != attempt.pk:
                raise GpxExtractionError("Extraction attempt was superseded by a newer attempt")
            existing = RouteVersion.objects.filter(source=source, checksum=checksum).first()
            storage_key = ""
            if existing is None and route.lifecycle == RouteLifecycle.PUBLISHED:
                filename = PurePosixPath(
                    "gpx", "routes", str(source.route_id), f"{checksum}.gpx"
                ).as_posix()
                storage_key = default_storage.save(filename, ContentFile(exported.content))
                saved_storage_key = storage_key
            version, created = record_route_version(
                source=source,
                checksum=checksum,
                storage_key=storage_key,
                normalized_geometry=normalize_geometry(parsed.geometry),
                simplified_geometry=normalize_geometry(parsed.geometry),
                distance_m=parsed.distance_m,
                ascent_m=parsed.ascent_m,
                descent_m=parsed.descent_m,
                elevation_profile=_elevation_profile(parsed),
                loop_status=parsed.loop_status,
            )
            route = source.route
            published = False
            duplicate = False
            explicit_decision = False
            similarity_id: int | None = None
            similarity_evidence: dict[str, Any] = {}
            # Only a new route identity is automatically classified.  A
            # re-import of an existing source must never undo a prior human
            # decision or quarantine a route merely because its history now
            # has another similar version.
            if created and route.current_approved_version_id is None:
                relationship, similarity = classify_version(version)
                if relationship is not None and similarity is not None:
                    similarity_id = relationship.pk
                    similarity_evidence = similarity.evidence
                    duplicate = similarity.duplicate
                    explicit_decision = route.moderation_decisions.filter(
                        action__in=[
                            "keep_both",
                            "merge_sources",
                            "quarantine",
                            "restore",
                            "remove",
                        ]
                    ).exists()
                    if duplicate and not explicit_decision:
                        from apps.catalogue.services import quarantine_route

                        quarantine_route(
                            route,
                            reason="High-confidence spatial duplicate; awaiting moderation.",
                            metadata={
                                "relationship_id": relationship.pk,
                                "similarity_evidence": similarity.evidence,
                                "similarity_score": round(similarity.score, 4),
                            },
                        )
            if route.lifecycle == RouteLifecycle.PUBLISHED and (not duplicate or explicit_decision):
                approve_version(version)
                published = True
            now = timezone.now()
            diagnostics.update(
                {
                    "version_id": version.pk,
                    "created": created,
                    "published": published,
                    "duplicate": duplicate,
                    "similarity_relationship_id": similarity_id,
                    "similarity_evidence": similarity_evidence,
                }
            )
            attempt.status = ExtractionStatus.SUCCEEDED
            attempt.finished_at = now
            attempt.diagnostics = diagnostics
            attempt.checksum = checksum
            attempt.version = version
            attempt.save(
                update_fields=["status", "finished_at", "diagnostics", "checksum", "version"]
            )
        return {
            "status": ExtractionStatus.SUCCEEDED,
            "source_id": source_id,
            "attempt_id": attempt.pk,
            "version_id": version.pk,
            "created": created,
            "published": published,
            "duplicate": diagnostics["duplicate"],
            "similarity_relationship_id": diagnostics["similarity_relationship_id"],
            "checksum": checksum,
            "diagnostics": diagnostics,
        }
    except Exception as exc:  # preserve every source failure for retry/review
        diagnostics = {"exception": type(exc).__name__}
        if saved_storage_key:
            try:
                default_storage.delete(saved_storage_key)
            except Exception as cleanup_error:  # persist cleanup for a later retry
                cleanup_message = str(cleanup_error)[:4000]
                OrphanPayloadCleanup.objects.create(
                    source_id=source_id,
                    attempt_id=attempt.pk,
                    storage_key=saved_storage_key,
                    status=OrphanPayloadStatus.PENDING,
                    last_error=cleanup_message,
                )
                diagnostics.update(
                    {
                        "orphan_storage_key": saved_storage_key,
                        "cleanup_error": cleanup_message,
                    }
                )
        return _failure(source_id, attempt.pk, exc, diagnostics=diagnostics)


def cleanup_orphan_payload(orphan_id: int) -> dict[str, Any]:
    """Retry deletion of a payload left by a failed import transaction."""

    with transaction.atomic():
        orphan = OrphanPayloadCleanup.objects.select_for_update().get(pk=orphan_id)
        if orphan.status == OrphanPayloadStatus.COMPLETED:
            return {"status": orphan.status, "orphan_id": orphan.pk}
        orphan.attempts += 1
        orphan.last_attempt_at = timezone.now()
        orphan.save(update_fields=["attempts", "last_attempt_at"])
    try:
        default_storage.delete(orphan.storage_key)
    except Exception as exc:
        orphan.status = OrphanPayloadStatus.FAILED
        orphan.last_error = str(exc)[:4000]
        orphan.save(update_fields=["status", "last_error"])
        return {
            "status": orphan.status,
            "orphan_id": orphan.pk,
            "error": orphan.last_error,
        }
    orphan.status = OrphanPayloadStatus.COMPLETED
    orphan.completed_at = timezone.now()
    orphan.last_error = ""
    orphan.save(update_fields=["status", "completed_at", "last_error"])
    return {"status": orphan.status, "orphan_id": orphan.pk}


# Descriptive aliases keep the service easy to discover for callers.
extract_route = extract_gpx
parse_route_gpx = parse_gpx
