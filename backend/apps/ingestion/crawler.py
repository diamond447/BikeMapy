"""Polite, resumable ingestion of server-rendered BikeForum pages.

The crawler deliberately has two replaceable seams: ``PageFetcher`` handles
network policy and ``BikeForumParser`` handles forum markup.  Persistence is
performed one page at a time, which makes a stopped Celery worker safe to
resume from the last committed checkpoint.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from lxml import etree  # type: ignore[import-untyped]
from lxml import html as lxml_html

from apps.catalogue.models import (
    ForumAuthor,
    ForumPost,
    ForumThread,
    Route,
    RouteLifecycle,
    RouteSource,
    SourceDenylistEntry,
)
from apps.catalogue.services import (
    SourceDeniedError,
    TitleContext,
    apply_generated_title,
    mark_source_available,
    mark_source_unavailable,
    register_source,
)

from .dispatch import dispatch_sources
from .models import (
    CrawlCheckpoint,
    CrawlPageStatus,
    CrawlPageWork,
    CrawlResponseCache,
    CrawlTask,
    CrawlTaskStatus,
)


class CrawlError(RuntimeError):
    """A recoverable fetch or crawl error."""


class RobotsDenied(CrawlError):
    """The site's robots policy does not permit this URL."""


@dataclass(frozen=True)
class FetchedPage:
    url: str
    body: str
    status_code: int = 200
    from_cache: bool = False


@dataclass(frozen=True)
class ParsedPost:
    url: str
    external_id: str = ""
    author: str = ""
    author_external_id: str = ""
    author_url: str = ""
    posted_at: datetime | None = None
    content: str = ""
    source_urls: tuple[str, ...] = ()


class PageKind(StrEnum):
    THREAD = "thread"
    LISTING = "listing"


@dataclass(frozen=True)
class ParsedThread:
    url: str
    title: str
    external_id: str = ""
    locality: str = ""
    posts: tuple[ParsedPost, ...] = ()
    next_url: str = ""
    thread_urls: tuple[str, ...] = ()
    page_kind: PageKind = PageKind.THREAD


class BikeForumParser(ABC):
    """Explicit parser contract, independent of HTTP and persistence."""

    @abstractmethod
    def parse(self, body: str, url: str) -> ParsedThread:
        raise NotImplementedError

    # Named alias keeps the contract convenient for adapters that use the
    # forum-specific term "thread".
    def parse_thread(self, body: str, url: str) -> ParsedThread:
        return self.parse(body, url)


def _text(node: Any) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _absolute(base: str, href: str) -> str:
    return urljoin(base, href) if href else ""


def _source_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and (parsed.hostname.lower() == "mapy.com" or parsed.hostname.lower() == "mapy.cz")
    )


def _canonical_source_url(url: str) -> str:
    """Fragments identify a forum post, not a distinct Mapy source."""

    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment=""))


def _effective_port(parsed: Any) -> int:
    return parsed.port or (443 if parsed.scheme.lower() == "https" else 80)


def _same_origin(base: str, target: str) -> bool:
    try:
        left, right = urlparse(base), urlparse(target)
        return (
            left.scheme.lower() == right.scheme.lower()
            and (left.hostname or "").lower() == (right.hostname or "").lower()
            and _effective_port(left) == _effective_port(right)
        )
    except ValueError:
        return False


def _is_thread_url(url: str) -> bool:
    """Recognize known thread paths without treating ``/forum/`` as one."""

    try:
        path = urlparse(url).path.rstrip("/")
    except ValueError:
        return False
    return bool(re.search(r"/(?:thread|t)/[^/?#]+$", path) or re.fullmatch(r"/forum/[^/]+", path))


def _configured_origin(url: str) -> tuple[str, str, int | None]:
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise CrawlError(f"Unsafe crawl URL: {url}") from exc
    # Reject userinfo even when it is empty (``https://@host``).  Empty
    # parsed.username is otherwise indistinguishable from no userinfo.
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or "@" in parsed.netloc:
        raise CrawlError(f"Unsafe crawl URL: {url}")
    try:
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise CrawlError(f"Unsafe crawl URL: {url}") from exc
    if not host:
        raise CrawlError(f"Unsafe crawl URL: {url}")
    configured = getattr(settings, "BIKEFORUM_ALLOWED_ORIGINS", ["https://www.bike-forum.cz"])

    def key(value: str) -> tuple[str, str, int]:
        item = urlparse(value)
        return (
            item.scheme.lower(),
            (item.hostname or "").lower(),
            item.port or (443 if item.scheme == "https" else 80),
        )

    requested = (
        parsed.scheme.lower(),
        host.lower(),
        port or (443 if parsed.scheme == "https" else 80),
    )
    if requested not in {key(origin) for origin in configured}:
        raise CrawlError(f"Crawl URL is outside configured BikeForum origins: {url}")
    effective_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    return f"{parsed.scheme.lower()}://{host.lower()}:{effective_port}", host, port


def _posted_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


class BeautifulSoupBikeForumParser(BikeForumParser):
    """Parse Bike-Forum's server-rendered listings and comment-based threads.

    The ``span.title``/``li.clearfix`` listing and ``div.comment`` post
    selectors mirror the current public HTML while the generic selectors keep
    the adapter useful for compatible forum snapshots.
    """

    def parse(self, body: str, url: str) -> ParsedThread:
        soup = BeautifulSoup(body, "lxml")
        title_node = soup.select_one("h1, span.title, .thread-title, [data-thread-title]")
        title = _text(title_node) or _text(soup.title)
        if not title:
            raise CrawlError(f"No thread title found in {url}")
        thread_node = soup.select_one("[data-thread-id]")
        external_id = str(thread_node.get("data-thread-id", "")) if thread_node else ""
        if not external_id:
            match = re.search(r"(?:/thread/|/t/|/forum/)([^/?#]+)", url)
            external_id = match.group(1) if match else ""
        locality = _text(soup.select_one("[data-locality], .locality"))
        post_nodes = soup.select("article, .post, div.comment, [data-post-id]")
        posts: list[ParsedPost] = []
        for index, node in enumerate(post_nodes):
            post_id = str(node.get("data-post-id", ""))
            raw_node_id = str(node.get("id", ""))
            if not post_id and raw_node_id:
                post_id = re.sub(r"^(?:post|p|comment)[-_]", "", raw_node_id)
            anchor = node.select_one("a.permalink, a[href*='#'], a[data-post-url]")
            post_url = (
                _absolute(url, str(anchor.get("href", "")))
                if anchor
                else f"{url}#{raw_node_id or f'post-{post_id or index + 1}'}"
            )
            author_node = node.select_one("[data-author], .author, .username, .user")
            author = _text(author_node)
            author_id = str(author_node.get("data-author-id", "")) if author_node else ""
            author_url_node = (
                author_node
                if author_node and author_node.name == "a" and author_node.get("href")
                else author_node.select_one("a[href]")
                if author_node
                else None
            )
            author_url = (
                _absolute(url, str(author_url_node.get("href", ""))) if author_url_node else ""
            )
            time_node = node.select_one("time[datetime], time, [data-posted-at]")
            posted_value = str(time_node.get("datetime", "")) if time_node else ""
            posted_value = posted_value or (
                str(time_node.get("data-posted-at", "")) if time_node else ""
            )
            content_node = node.select_one(
                "[data-post-content], .post-content, .content, .message, .comment-text"
            )
            content = _text(content_node or node)
            links = tuple(
                link
                for link in (_absolute(url, str(a.get("href", ""))) for a in node.select("a[href]"))
                if _source_url(link)
            )
            links = tuple(dict.fromkeys(_canonical_source_url(link) for link in links))
            posts.append(
                ParsedPost(
                    url=post_url,
                    external_id=post_id,
                    author=author,
                    author_external_id=author_id,
                    author_url=author_url,
                    posted_at=_posted_at(posted_value),
                    content=content,
                    source_urls=tuple(dict.fromkeys(links)),
                )
            )
        next_node = soup.select_one("a[rel='next'], .pagination a.next, a.next")
        next_url = _absolute(url, str(next_node.get("href", ""))) if next_node else ""
        thread_links = tuple(
            _absolute(url, str(anchor.get("href", ""))).split("#", 1)[0]
            for anchor in soup.select(
                "a.thread-link, a[data-thread-link], a[href*='/thread/'], "
                "a[href*='/t/'], li.clearfix span.title a[href*='/forum/'], "
                "li.clearfix a[href*='/forum/']"
            )
            if _absolute(url, str(anchor.get("href", "")))
        )
        current_path = urlparse(url).path.rstrip("/")
        thread_links = tuple(
            link
            for link in thread_links
            if _same_origin(url, link)
            and urlparse(link).path.rstrip("/") != current_path
            and _is_thread_url(link)
        )
        return ParsedThread(
            url=url,
            title=title,
            external_id=external_id,
            locality=locality,
            posts=tuple(posts),
            next_url=next_url,
            thread_urls=tuple(dict.fromkeys(thread_links)),
            page_kind=(
                PageKind.THREAD if posts or thread_node or _is_thread_url(url) else PageKind.LISTING
            ),
        )


class LxmlBikeForumParser(BeautifulSoupBikeForumParser):
    """Parser variant that validates malformed input with lxml first."""

    def parse(self, body: str, url: str) -> ParsedThread:
        try:
            lxml_html.fromstring(body)
        except (ValueError, TypeError, etree.ParserError) as exc:
            raise CrawlError(f"Malformed HTML at {url}: {exc}") from exc
        return super().parse(body, url)


def _bounded_http_response(
    client: httpx.Client, url: str, *, headers: dict[str, str], max_bytes: int
) -> httpx.Response:
    """Read an origin response without buffering beyond the parser budget."""

    with client.stream("GET", url, headers=headers) as streamed:
        content_length = streamed.headers.get("content-length")
        try:
            declared_length = int(content_length) if content_length else None
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            raise CrawlError(f"BikeForum page exceeds the {max_bytes}-byte limit")
        chunks: list[bytes] = []
        size = 0
        for chunk in streamed.iter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise CrawlError(f"BikeForum page exceeds the {max_bytes}-byte limit")
            chunks.append(chunk)
        return httpx.Response(
            streamed.status_code,
            headers=streamed.headers,
            content=b"".join(chunks),
            request=streamed.request,
        )


class PageFetcher(ABC):
    def close(self) -> None:  # noqa: B027
        """Release network resources; injectable test fetchers need no-op cleanup."""
        return None

    @abstractmethod
    def fetch(self, url: str) -> FetchedPage:
        raise NotImplementedError


class HttpxPageFetcher(PageFetcher):
    """httpx fetcher with robots, cache validators, safe redirects and backoff."""

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        timeout: float | None = None,
        rate_limit: float | None = None,
        retries: int | None = None,
        backoff: float | None = None,
        cache_ttl: float | None = None,
        max_bytes: int | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.user_agent = user_agent or getattr(
            settings,
            "BIKEFORUM_USER_AGENT",
            "BikeMapyBot/1.0 (+https://github.com/diamond447/BikeMapy)",
        )
        self.timeout = (
            timeout if timeout is not None else float(getattr(settings, "BIKEFORUM_TIMEOUT", 15))
        )
        self.rate_limit = (
            rate_limit
            if rate_limit is not None
            else float(getattr(settings, "BIKEFORUM_RATE_LIMIT", 2))
        )
        self.retries = (
            retries if retries is not None else int(getattr(settings, "BIKEFORUM_RETRIES", 2))
        )
        self.backoff = (
            backoff if backoff is not None else float(getattr(settings, "BIKEFORUM_BACKOFF", 1))
        )
        self.cache_ttl = (
            cache_ttl
            if cache_ttl is not None
            else float(getattr(settings, "BIKEFORUM_CACHE_TTL", 3600))
        )
        self.max_bytes = max(
            1,
            int(
                max_bytes
                if max_bytes is not None
                else getattr(settings, "BIKEFORUM_MAX_BYTES", 5 * 1024 * 1024)
            ),
        )
        self._last_request = 0.0
        self._robots: dict[str, RobotFileParser | None] = {}
        self._client = client or httpx.Client(follow_redirects=False, timeout=self.timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HttpxPageFetcher:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _wait(self) -> None:
        delay = self.rate_limit - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        self._last_request = time.monotonic()

    def _robots_for(self, url: str) -> RobotFileParser | None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        robots_url = f"{origin}/robots.txt"
        try:
            self._wait()
            response = _bounded_http_response(
                self._client,
                robots_url,
                headers={"User-Agent": str(self.user_agent)},
                max_bytes=self.max_bytes,
            )
            if response.status_code == 404:
                parser: RobotFileParser | None = None
            elif 300 <= response.status_code < 400:
                parser = RobotFileParser()
                parser.parse(["User-agent: *", "Disallow: /"])
            elif response.is_error:
                parser = RobotFileParser()
                parser.parse(["User-agent: *", "Disallow: /"])
            else:
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
        except httpx.HTTPError:
            parser = RobotFileParser()
            parser.parse(["User-agent: *", "Disallow: /"])
        self._robots[origin] = parser
        return parser

    def _allowed(self, url: str) -> None:
        self._validate_url(url)
        policy = self._robots_for(url)
        if policy is not None and not policy.can_fetch(str(self.user_agent), url):
            raise RobotsDenied(f"robots.txt disallows {url}")

    def _validate_url(self, url: str) -> None:
        _origin, host, port = _configured_origin(url)
        scheme = urlparse(url).scheme
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None:
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
            ):
                raise CrawlError(f"Private or reserved crawl address: {url}")
            return
        if not getattr(settings, "BIKEFORUM_DNS_CHECK", True):
            return
        try:
            addresses = socket.getaddrinfo(
                host,
                port or (443 if scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            if not addresses:
                raise CrawlError(f"BikeForum origin did not resolve: {url}")
            for address_info in addresses:
                resolved = ipaddress.ip_address(address_info[4][0])
                if (
                    resolved.is_private
                    or resolved.is_loopback
                    or resolved.is_link_local
                    or resolved.is_reserved
                ):
                    raise CrawlError(f"BikeForum origin resolves to a private address: {url}")
        except socket.gaierror as exc:
            raise CrawlError(f"BikeForum origin DNS failed: {url}") from exc

    def fetch(self, url: str) -> FetchedPage:
        self._validate_url(url)
        cached = CrawlResponseCache.objects.filter(url=url).first()
        self._allowed(url)  # robots is always checked, including cache hits
        if (
            cached
            and cached.body
            and (timezone.now() - cached.fetched_at).total_seconds() < self.cache_ttl
        ):
            if len(cached.body.encode("utf-8")) > self.max_bytes:
                raise CrawlError(f"Cached BikeForum page exceeds the {self.max_bytes}-byte limit")
            return FetchedPage(
                url=cached.final_url or url,
                body=cached.body,
                status_code=cached.status_code,
                from_cache=True,
            )
        headers: dict[str, str] = {"User-Agent": str(self.user_agent), "Accept": "text/html"}
        if cached and cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached and cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified
        current = url
        origin = _configured_origin(url)[0]
        for _redirect in range(4):
            for attempt in range(self.retries + 1):
                try:
                    # DNS is resolved and pacing is enforced immediately
                    # before every network attempt, including retries.  A
                    # DNS answer can change while a retry is pending.
                    self._validate_url(current)
                    self._wait()
                    response = _bounded_http_response(
                        self._client,
                        current,
                        headers=headers,
                        max_bytes=self.max_bytes,
                    )
                    if response.status_code == 304:
                        if cached and cached.body:
                            if len(cached.body.encode("utf-8")) > self.max_bytes:
                                raise CrawlError(
                                    f"Cached BikeForum page exceeds the {self.max_bytes}-byte limit"
                                )
                            cached.fetched_at = timezone.now()
                            cached.save(update_fields=["fetched_at"])
                            return FetchedPage(
                                url=cached.final_url or current,
                                body=cached.body,
                                status_code=200,
                                from_cache=True,
                            )
                        # Retention deliberately leaves validators behind. A
                        # metadata-only 304 cannot be parsed, so retry without
                        # validators to obtain a fresh body.
                        if "If-None-Match" in headers or "If-Modified-Since" in headers:
                            headers.pop("If-None-Match", None)
                            headers.pop("If-Modified-Since", None)
                            continue
                        raise CrawlError("BikeForum returned 304 without a retained response body")
                    if response.status_code in {408, 425, 429} or response.status_code >= 500:
                        if attempt < self.retries:
                            time.sleep(min(self.backoff * (2**attempt), 30))
                            continue
                    if response.is_error:
                        raise CrawlError(f"HTTP {response.status_code} for {current}")
                    location = response.headers.get("location")
                    if location and response.status_code in {301, 302, 303, 307, 308}:
                        target = urljoin(current, location)
                        try:
                            target_origin = _configured_origin(target)
                        except CrawlError as exc:
                            raise CrawlError(f"Unsafe redirect from {current} to {target}") from exc
                        if target_origin[0] != origin:
                            raise CrawlError(f"Unsafe redirect from {current} to {target}")
                        self._allowed(target)
                        current = target
                        break
                    body = response.content.decode(response.encoding or "utf-8", errors="replace")
                    CrawlResponseCache.objects.update_or_create(
                        url=url,
                        defaults={
                            "status_code": response.status_code,
                            "final_url": current,
                            "body": body,
                            "etag": response.headers.get("etag", ""),
                            "last_modified": response.headers.get("last-modified", ""),
                            "checksum": hashlib.sha256(response.content).hexdigest(),
                            "fetched_at": timezone.now(),
                        },
                    )
                    return FetchedPage(url=current, body=body, status_code=response.status_code)
                except (httpx.HTTPError, CrawlError) as exc:
                    if isinstance(exc, CrawlError) and not str(exc).startswith("HTTP 5"):
                        raise
                    if attempt >= self.retries:
                        raise CrawlError(str(exc)) from exc
                    time.sleep(min(self.backoff * (2**attempt), 30))
            else:
                continue
            # A redirect ended this retry loop; fetch its target.
            if current != url:
                continue
        raise CrawlError(f"Too many redirects for {url}")


@dataclass
class ImportResult:
    threads: int = 0
    posts: int = 0
    authors: int = 0
    sources: int = 0
    denied: int = 0
    source_ids: set[int] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SourceCheckResult:
    available: bool
    status_code: int | None = None
    error: str = ""


class SourceChecker(ABC):
    def close(self) -> None:  # noqa: B027
        return None

    @abstractmethod
    def check(self, url: str) -> SourceCheckResult:
        raise NotImplementedError


class HttpxSourceChecker(SourceChecker):
    """Conservative, bounded availability check for canonical Mapy URLs."""

    def __init__(
        self,
        *,
        timeout: float | None = None,
        retries: int | None = None,
        backoff: float | None = None,
        rate_limit: float = 1.0,
        max_bytes: int = 2_000_000,
        client: httpx.Client | None = None,
    ) -> None:
        self.timeout = timeout or float(getattr(settings, "BIKEFORUM_TIMEOUT", 15))
        self.retries = (
            retries if retries is not None else int(getattr(settings, "BIKEFORUM_RETRIES", 2))
        )
        self.backoff = (
            backoff if backoff is not None else float(getattr(settings, "BIKEFORUM_BACKOFF", 1))
        )
        self.rate_limit = rate_limit
        self.max_bytes = max_bytes
        self._last_request = 0.0
        self._client = client or httpx.Client(follow_redirects=False, timeout=self.timeout)
        self._owns_client = client is None

    def check(self, url: str) -> SourceCheckResult:
        try:
            parsed = urlparse(url)
        except ValueError:
            return SourceCheckResult(False, error="unsafe or non-canonical Mapy URL")
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"mapy.com", "mapy.cz"}
            or "@" in parsed.netloc
            or parsed.port is not None
        ):
            return SourceCheckResult(False, error="unsafe or non-canonical Mapy URL")
        last_status: int | None = None
        for attempt in range(self.retries + 1):
            try:
                infos = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
                if not infos:
                    return SourceCheckResult(False, error="source DNS did not resolve")
                for info in infos:
                    address = ipaddress.ip_address(info[4][0])
                    if (
                        address.is_private
                        or address.is_loopback
                        or address.is_link_local
                        or address.is_reserved
                    ):
                        return SourceCheckResult(
                            False, error="source resolves to a private address"
                        )
            except socket.gaierror as exc:
                return SourceCheckResult(False, error=f"source DNS failed: {exc}")
            delay = self.rate_limit - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()
            try:
                with self._client.stream(
                    "GET",
                    url,
                    headers={
                        "User-Agent": getattr(settings, "BIKEFORUM_USER_AGENT", "BikeMapyBot/1.0")
                    },
                ) as response:
                    last_status = response.status_code
                    if response.status_code >= 500 or response.status_code in {408, 425, 429}:
                        if attempt < self.retries:
                            time.sleep(min(self.backoff * 2**attempt, 30))
                            continue
                    if not 200 <= response.status_code < 300:
                        return SourceCheckResult(
                            False, response.status_code, f"HTTP {response.status_code}"
                        )
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type and content_type not in {
                        "text/html",
                        "application/octet-stream",
                    }:
                        return SourceCheckResult(
                            False, response.status_code, "unexpected content type"
                        )
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            return SourceCheckResult(
                                False, response.status_code, "source response too large"
                            )
                    return SourceCheckResult(True, response.status_code)
            except httpx.HTTPError as exc:
                if attempt < self.retries:
                    time.sleep(min(self.backoff * 2**attempt, 30))
                    continue
                return SourceCheckResult(available=False, status_code=last_status, error=str(exc))
        return SourceCheckResult(False, last_status, "source check retries exhausted")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


@transaction.atomic
def import_thread(
    thread_data: ParsedThread, *, lease: tuple[str, str] | None = None
) -> ImportResult:
    """Idempotently persist one parsed page and all permitted source links."""

    if lease:
        _assert_lease_locked(*lease)
    result = ImportResult()
    thread = ForumThread.objects.filter(url=thread_data.url).first()
    if thread is None and thread_data.external_id:
        thread = ForumThread.objects.filter(external_id=thread_data.external_id).first()
    thread_created = thread is None
    if thread is None:
        thread = ForumThread.objects.create(
            url=thread_data.url,
            title=thread_data.title,
            external_id=thread_data.external_id,
            locality=thread_data.locality,
        )
    if (
        thread.title != thread_data.title
        or thread.external_id != thread_data.external_id
        or thread.locality != thread_data.locality
    ):
        thread.title, thread.external_id, thread.locality = (
            thread_data.title,
            thread_data.external_id,
            thread_data.locality,
        )
        thread.save(update_fields=["title", "external_id", "locality"])
    result.threads = int(thread_created)
    for post_data in thread_data.posts:
        author = None
        if post_data.author:
            lookup = (
                {"external_id": post_data.author_external_id}
                if post_data.author_external_id
                else {"username": post_data.author}
            )
            author, author_created = ForumAuthor.objects.get_or_create(
                **lookup,
                defaults={"username": post_data.author, "profile_url": post_data.author_url},
            )
            author.last_seen_at = timezone.now()
            if post_data.author_url and author.profile_url != post_data.author_url:
                author.profile_url = post_data.author_url
            author.save(update_fields=["last_seen_at", "profile_url"])
            result.authors += int(author_created)
        post, post_created = ForumPost.objects.get_or_create(
            url=post_data.url,
            defaults={
                "thread": thread,
                "author": author,
                "external_id": post_data.external_id,
                "posted_at": post_data.posted_at,
                "content_hash": hashlib.sha256(post_data.content.encode()).hexdigest()
                if post_data.content
                else "",
            },
        )
        if post.thread_id != thread.pk:
            result.errors.append(f"Post URL already belongs to another thread: {post.url}")
            continue
        changed: list[str] = []
        for name, value in (
            ("author", author),
            ("external_id", post_data.external_id),
            ("posted_at", post_data.posted_at),
        ):
            if value and getattr(post, name) != value:
                setattr(post, name, value)
                changed.append(name)
        if post_data.content:
            post.content_hash = hashlib.sha256(post_data.content.encode()).hexdigest()
            changed.append("content_hash")
        if changed:
            post.save(update_fields=changed)
        result.posts += int(post_created)
        for source_url in post_data.source_urls:
            if SourceDenylistEntry.objects.filter(source_url=source_url, active=True).exists():
                result.denied += 1
                continue
            source = RouteSource.objects.select_related("route").filter(mapy_url=source_url).first()
            if source is None:
                route = Route.objects.create(lifecycle=RouteLifecycle.PUBLISHED)
                route_created = True
            else:
                route = source.route
                route_created = False
            if route_created or not route.thread_title:
                apply_generated_title(
                    route,
                    TitleContext(
                        thread_title=thread.title,
                        author=post_data.author,
                        post_date=post_data.posted_at,
                        route_id=route.id,
                    ),
                )
            try:
                source, source_created = register_source(
                    route=route, post=post, mapy_url=source_url
                )
                result.source_ids.add(source.pk)
                result.sources += int(source_created)
            except SourceDeniedError:
                result.denied += 1
                if route_created and not route.sources.exists():
                    route.delete()
            except ValidationError as exc:
                result.errors.append(f"Could not attach source {source_url}: {exc}")
                if route_created and not route.sources.exists():
                    route.delete()
    thread.last_crawled_at = timezone.now()
    thread.save(update_fields=["last_crawled_at"])
    return result


class CrawlBusy(CrawlError):
    """Another worker currently owns the requested crawl stream lease."""


def _enqueue(task: CrawlTask, urls: list[str]) -> None:
    for url in urls:
        if not url:
            continue
        # URL validation happens before network access; this also prevents an
        # untrusted page from growing a frontier outside the configured host.
        _configured_origin(url)
        host = urlparse(url).hostname or ""
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
        ):
            raise CrawlError(f"Private or reserved frontier address: {url}")
        CrawlPageWork.objects.get_or_create(task=task, url=url)


def _claim_page(
    task: CrawlTask, *, lease_until: datetime, max_attempts: int
) -> CrawlPageWork | None:
    now = timezone.now()
    # A worker can disappear after claiming its final allowed attempt.  Make
    # that durable state terminal before selecting more work so a takeover
    # never grants the same page an implicit extra retry.
    over_limit = list(
        CrawlPageWork.objects.select_for_update().filter(
            task=task,
            status__in=[CrawlPageStatus.QUEUED, CrawlPageStatus.PROCESSING],
            attempts__gte=max_attempts,
        )
    )
    for item in over_limit:
        item.status = CrawlPageStatus.EXHAUSTED
        item.finished_at = item.finished_at or now
        item.lease_until = None
        item.last_error = item.last_error or "Page retry limit exhausted"
        item.save(update_fields=["status", "finished_at", "lease_until", "last_error"])
    work = None
    # Freshly discovered siblings always run before retries of an older
    # failure; a poison page therefore cannot starve the frontier.
    for query in (
        models.Q(status=CrawlPageStatus.QUEUED, attempts__lt=max_attempts),
        models.Q(
            status=CrawlPageStatus.PROCESSING,
            lease_until__lt=now,
            attempts__lt=max_attempts,
        ),
        models.Q(status=CrawlPageStatus.FAILED, attempts__lt=max_attempts),
    ):
        work = (
            CrawlPageWork.objects.select_for_update()
            .filter(task=task)
            .filter(query)
            .order_by("discovered_at", "pk")
            .first()
        )
        if work is not None:
            break
    if work is None:
        return None
    work.status = CrawlPageStatus.PROCESSING
    work.attempts += 1
    work.started_at = now
    work.lease_until = lease_until
    work.save(update_fields=["status", "attempts", "started_at", "lease_until"])
    return work


def _renew_lease(task_id: int, stream: str, token: str, lease_seconds: int) -> CrawlTask:
    """Renew stream and task leases under the checkpoint lock."""

    with transaction.atomic():
        checkpoint = CrawlCheckpoint.objects.select_for_update().get(stream=stream)
        if checkpoint.lease_token != token:
            raise CrawlBusy(f"Crawl stream {stream} lease was lost")
        task = CrawlTask.objects.select_for_update().get(pk=task_id)
        if task.lease_token != token:
            raise CrawlBusy(f"Crawl stream {stream} lease was lost")
        expiry = timezone.now() + timedelta(seconds=lease_seconds)
        task.lease_until = expiry
        checkpoint.lease_until = expiry
        task.save(update_fields=["lease_until"])
        checkpoint.save(update_fields=["lease_until", "updated_at"])
        return task


def _assert_lease_locked(stream: str, token: str) -> None:
    """Verify a lease while the caller's transaction holds its checkpoint lock."""

    checkpoint = CrawlCheckpoint.objects.select_for_update().get(stream=stream)
    if checkpoint.lease_token != token:
        raise CrawlBusy(f"Crawl stream {stream} lease was lost")


def _assert_lease(stream: str, token: str) -> None:
    with transaction.atomic():
        _assert_lease_locked(stream, token)


def _check_sources(
    source_ids: set[int], checker: SourceChecker, *, lease: tuple[str, str] | None = None
) -> list[str]:
    errors: list[str] = []
    for source in RouteSource.objects.filter(pk__in=source_ids):
        if SourceDenylistEntry.objects.filter(source_url=source.mapy_url, active=True).exists():
            continue
        try:
            result = checker.check(source.mapy_url)
            with transaction.atomic():
                if lease:
                    _assert_lease_locked(*lease)
                source = RouteSource.objects.get(pk=source.pk)
                if result.available:
                    mark_source_available(source, error=result.error)
                else:
                    mark_source_unavailable(source, error=result.error)
                    errors.append(f"{source.mapy_url}: {result.error or 'source unavailable'}")
        except CrawlBusy:
            raise
        except Exception as exc:  # checker failures are isolated per source
            with transaction.atomic():
                if lease:
                    _assert_lease_locked(*lease)
                source = RouteSource.objects.get(pk=source.pk)
                mark_source_unavailable(source, error=str(exc))
                errors.append(f"{source.mapy_url}: {exc}")
    return errors


def run_crawl(
    *,
    start_url: str,
    max_pages: int = 10,
    kind: str = CrawlTask.Kind.INCREMENTAL,
    stream: str | None = None,
    fetcher: PageFetcher | None = None,
    parser: BikeForumParser | None = None,
    source_checker: SourceChecker | None = None,
    task_id: int | None = None,
) -> dict[str, Any]:
    """Run a bounded leased frontier crawl, retaining failed work for retry."""

    max_pages = max(1, min(max_pages, int(getattr(settings, "BIKEFORUM_MAX_PAGES", 100))))
    stream = stream or ("backfill" if kind == CrawlTask.Kind.BACKFILL else "incremental")
    lease_seconds = int(getattr(settings, "BIKEFORUM_LEASE_SECONDS", 600))
    max_attempts = max(1, int(getattr(settings, "BIKEFORUM_PAGE_ATTEMPTS", 3)))
    now = timezone.now()
    token = hashlib.sha256(f"{time.monotonic_ns()}:{start_url}".encode()).hexdigest()[:48]
    with transaction.atomic():
        checkpoint, _ = CrawlCheckpoint.objects.select_for_update().get_or_create(
            stream=stream, defaults={"next_url": start_url}
        )
        if checkpoint.lease_token and checkpoint.lease_until and checkpoint.lease_until > now:
            raise CrawlBusy(f"Crawl stream {stream} is already leased")
        if task_id:
            task = CrawlTask.objects.select_for_update().get(pk=task_id)
        else:
            resumed = (
                CrawlTask.objects.select_for_update()
                .filter(
                    stream=stream,
                    status__in=[
                        CrawlTaskStatus.PAUSED,
                        CrawlTaskStatus.FAILED,
                        CrawlTaskStatus.RUNNING,
                    ],
                )
                .filter(
                    models.Q(frontier__status=CrawlPageStatus.QUEUED)
                    | models.Q(
                        frontier__status=CrawlPageStatus.PROCESSING,
                        frontier__lease_until__lt=now,
                    )
                    | models.Q(
                        frontier__status=CrawlPageStatus.FAILED,
                        frontier__attempts__lt=max_attempts,
                    )
                )
                .order_by("-created_at", "-pk")
                .first()
            )
            if resumed is None:
                task = CrawlTask.objects.create(
                    kind=kind,
                    stream=stream,
                    start_url=start_url,
                    next_url=checkpoint.next_url or start_url,
                    max_pages=max_pages,
                )
            else:
                task = resumed
        if task.status == CrawlTaskStatus.RUNNING and task.lease_until and task.lease_until > now:
            raise CrawlBusy(f"Crawl stream {stream} is already leased")
        task.status, task.started_at, task.attempts = (
            CrawlTaskStatus.RUNNING,
            task.started_at or now,
            task.attempts + 1,
        )
        task.lease_token, task.lease_until, task.last_error = (
            token,
            now + timedelta(seconds=lease_seconds),
            "",
        )
        task.save(
            update_fields=[
                "status",
                "started_at",
                "attempts",
                "lease_token",
                "lease_until",
                "last_error",
            ]
        )
        checkpoint.lease_token = token
        checkpoint.lease_until = now + timedelta(seconds=lease_seconds)
        checkpoint.save(update_fields=["lease_token", "lease_until", "updated_at"])
        _enqueue(task, [checkpoint.next_url or start_url])
    own_fetcher = fetcher is None
    own_checker = source_checker is None
    fetcher = fetcher or HttpxPageFetcher()
    parser = parser or BeautifulSoupBikeForumParser()
    source_checker = source_checker or HttpxSourceChecker()
    total = ImportResult()
    total_extractions_queued = 0
    page_errors: list[str] = []
    try:
        for _ in range(max_pages):
            task = _renew_lease(task.pk, stream, token, lease_seconds)
            with transaction.atomic():
                checkpoint = CrawlCheckpoint.objects.select_for_update().get(stream=stream)
                if checkpoint.lease_token != token:
                    raise CrawlBusy(f"Crawl stream {stream} lease was lost")
                work = _claim_page(
                    task,
                    lease_until=timezone.now() + timedelta(seconds=lease_seconds),
                    max_attempts=max_attempts,
                )
            if work is None:
                break
            try:
                page = fetcher.fetch(work.url)
                parsed = parser.parse(page.body, page.url)
                task = _renew_lease(task.pk, stream, token, lease_seconds)
                page_result = ImportResult()
                if parsed.page_kind == PageKind.THREAD:
                    page_result = import_thread(parsed, lease=(stream, token))
                _renew_lease(task.pk, stream, token, lease_seconds)
                source_errors = (
                    _check_sources(page_result.source_ids, source_checker, lease=(stream, token))
                    if getattr(settings, "BIKEFORUM_CHECK_SOURCES", True)
                    else []
                )
                extraction_dispatch = dispatch_sources(page_result.source_ids)
                total_extractions_queued += extraction_dispatch.get("queued", 0)
                page_errors.extend(page_result.errors + source_errors)
                total.threads += page_result.threads
                total.posts += page_result.posts
                total.authors += page_result.authors
                total.sources += page_result.sources
                total.denied += page_result.denied
                total.source_ids.update(page_result.source_ids)
                with transaction.atomic():
                    checkpoint = CrawlCheckpoint.objects.select_for_update().get(stream=stream)
                    if checkpoint.lease_token != token:
                        raise CrawlBusy(f"Crawl stream {stream} lease was lost") from None
                    work = CrawlPageWork.objects.select_for_update().get(pk=work.pk)
                    (
                        work.status,
                        work.finished_at,
                        work.lease_until,
                        work.status_code,
                        work.last_error,
                    ) = CrawlPageStatus.SUCCEEDED, timezone.now(), None, page.status_code, ""
                    work.save(
                        update_fields=[
                            "status",
                            "finished_at",
                            "lease_until",
                            "status_code",
                            "last_error",
                        ]
                    )
                    task = CrawlTask.objects.select_for_update().get(pk=task.pk)
                    discovered = list(parsed.thread_urls) + (
                        [parsed.next_url] if parsed.next_url else []
                    )
                    _enqueue(task, discovered)
                    next_work = (
                        CrawlPageWork.objects.filter(task=task, status=CrawlPageStatus.QUEUED)
                        .order_by("discovered_at", "pk")
                        .first()
                    )
                    checkpoint.next_url = next_work.url if next_work else ""
                    checkpoint.page_number += 1
                    checkpoint.last_successful_at = timezone.now()
                    checkpoint.last_error = ""
                    checkpoint.save(
                        update_fields=[
                            "next_url",
                            "page_number",
                            "last_successful_at",
                            "last_error",
                            "updated_at",
                        ]
                    )
                    task.pages_processed += 1
                    task.items_imported += page_result.posts
                    task.next_url = checkpoint.next_url
                    task.save(update_fields=["pages_processed", "items_imported", "next_url"])
            except Exception as exc:  # mark this item, then continue the frontier
                message = str(exc)
                page_errors.append(message)
                with transaction.atomic():
                    checkpoint = CrawlCheckpoint.objects.select_for_update().get(stream=stream)
                    if checkpoint.lease_token != token:
                        raise CrawlBusy(f"Crawl stream {stream} lease was lost") from None
                    work = CrawlPageWork.objects.select_for_update().get(pk=work.pk)
                    work.status, work.finished_at, work.lease_until, work.last_error = (
                        CrawlPageStatus.EXHAUSTED
                        if work.attempts >= max_attempts
                        else CrawlPageStatus.FAILED,
                        timezone.now(),
                        None,
                        message,
                    )
                    work.save(update_fields=["status", "finished_at", "lease_until", "last_error"])
                    checkpoint.last_error = message
                    checkpoint.save(update_fields=["last_error", "updated_at"])
                    task = CrawlTask.objects.select_for_update().get(pk=task.pk)
                    task.pages_processed += 1
                    task.last_error = message
                    task.save(update_fields=["pages_processed", "last_error"])
    finally:
        if own_fetcher:
            fetcher.close()
        if own_checker:
            source_checker.close()
    with transaction.atomic():
        checkpoint = CrawlCheckpoint.objects.select_for_update().get(stream=stream)
        if checkpoint.lease_token != token:
            raise CrawlBusy(f"Crawl stream {stream} lease was lost")
        task = CrawlTask.objects.select_for_update().get(pk=task.pk)
        remaining = CrawlPageWork.objects.filter(
            task=task, status__in=[CrawlPageStatus.QUEUED, CrawlPageStatus.PROCESSING]
        ).exists()
        persisted_errors = list(
            CrawlPageWork.objects.filter(
                task=task,
                status__in=[CrawlPageStatus.FAILED, CrawlPageStatus.EXHAUSTED],
            )
            .exclude(last_error="")
            .values_list("last_error", flat=True)
        )
        all_errors = page_errors + persisted_errors
        task.status = (
            CrawlTaskStatus.FAILED
            if all_errors
            else CrawlTaskStatus.PAUSED
            if remaining
            else CrawlTaskStatus.COMPLETED
        )
        task.last_error = all_errors[-1] if all_errors else ""
        task.finished_at = timezone.now()
        task.lease_until, task.lease_token = None, ""
        task.save(
            update_fields=["status", "last_error", "finished_at", "lease_until", "lease_token"]
        )
        checkpoint.lease_until, checkpoint.lease_token = None, ""
        checkpoint.save(update_fields=["lease_until", "lease_token", "updated_at"])
        if remaining and not checkpoint.next_url:
            pending = (
                CrawlPageWork.objects.filter(task=task, status=CrawlPageStatus.QUEUED)
                .order_by("discovered_at", "pk")
                .first()
            )
            checkpoint.next_url = pending.url if pending else ""
            checkpoint.save(update_fields=["next_url", "updated_at"])
    return {
        "task_id": task.pk,
        "pages": task.pages_processed,
        "threads": total.threads,
        "posts": total.posts,
        "authors": total.authors,
        "sources": total.sources,
        "denied": total.denied,
        "errors": all_errors,
        "next_url": checkpoint.next_url,
        "frontier_remaining": remaining,
        "extractions_queued": total_extractions_queued,
    }


# Stable adapter names for callers that do not need the implementation detail.
HttpxFetcher = HttpxPageFetcher
BeautifulSoupParser = BeautifulSoupBikeForumParser
