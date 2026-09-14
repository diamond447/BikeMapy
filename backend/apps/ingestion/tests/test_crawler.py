from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.robotparser import RobotFileParser

import httpx
import pytest
from django.conf import settings
from django.db import models
from django.test import override_settings
from django.utils import timezone

from apps.catalogue.models import (
    ForumAuthor,
    ForumPost,
    ForumThread,
    RouteSource,
    SourceDenylistEntry,
)
from apps.ingestion.cache import cleanup_expired_crawl_response_bodies
from apps.ingestion.cache_policy import configured_body_retention_seconds
from apps.ingestion.crawler import (
    BeautifulSoupBikeForumParser,
    CrawlBusy,
    CrawlError,
    FetchedPage,
    HttpxPageFetcher,
    HttpxSourceChecker,
    LxmlBikeForumParser,
    PageFetcher,
    PageKind,
    ParsedThread,
    RobotsDenied,
    SourceChecker,
    SourceCheckResult,
    _check_sources,
    import_thread,
    run_crawl,
)
from apps.ingestion.models import (
    CrawlCheckpoint,
    CrawlPageStatus,
    CrawlPageWork,
    CrawlResponseCache,
    CrawlTask,
    CrawlTaskStatus,
)

pytestmark = pytest.mark.django_db
FIXTURE = Path(__file__).parent / "fixtures" / "thread.html"
THREAD_URL = "https://bikeforum.example/t/42"
REAL_INDEX_FIXTURE = Path(__file__).parent / "fixtures" / "bike_forum_index.html"
REAL_THREAD_FIXTURE = Path(__file__).parent / "fixtures" / "bike_forum_thread.html"


def test_fixture_parser_contract_uses_stable_post_and_source_fields() -> None:
    parsed = BeautifulSoupBikeForumParser().parse(FIXTURE.read_text(), THREAD_URL)
    assert parsed.page_kind == PageKind.THREAD
    assert parsed.title == "Spring ride around Brno"
    assert parsed.external_id == "thread-42"
    assert parsed.next_url == "https://bikeforum.example/t/42?page=2"
    assert parsed.posts[0].url.endswith("#post-7")
    assert parsed.posts[0].author == "rider"
    assert parsed.posts[0].source_urls == ("https://mapy.com/s/demo",)
    assert parsed.posts[0].posted_at == datetime(2025, 4, 12, 10, tzinfo=UTC)
    assert LxmlBikeForumParser().parse_thread(FIXTURE.read_text(), THREAD_URL).title == parsed.title


def test_index_fixture_discovers_thread_frontier_links() -> None:
    parsed = BeautifulSoupBikeForumParser().parse(
        (FIXTURE.parent / "index.html").read_text(), "https://bikeforum.example/"
    )
    assert parsed.thread_urls == (
        "https://bikeforum.example/t/42",
        "https://bikeforum.example/thread/99",
    )
    assert parsed.page_kind == PageKind.LISTING
    assert parsed.next_url == "https://bikeforum.example/archive?page=2"


def test_empty_final_listing_is_not_imported_as_a_thread() -> None:
    parsed = BeautifulSoupBikeForumParser().parse(
        "<html><head><title>Archive</title></head><body></body></html>",
        "https://bikeforum.example/archive?page=99",
    )
    assert parsed.page_kind == PageKind.LISTING
    assert parsed.thread_urls == ()


def test_sanitized_bike_forum_structure_parses_forum_threads_and_comments() -> None:
    index = BeautifulSoupBikeForumParser().parse(
        REAL_INDEX_FIXTURE.read_text(), "https://www.bike-forum.cz/forum/"
    )
    assert index.page_kind == PageKind.LISTING
    assert index.thread_urls == (
        "https://www.bike-forum.cz/forum/jarni-vylet",
        "https://www.bike-forum.cz/forum/podzimni-okruh",
    )
    assert index.next_url == "https://www.bike-forum.cz/forum/?page=2"

    thread = BeautifulSoupBikeForumParser().parse(
        REAL_THREAD_FIXTURE.read_text(),
        "https://www.bike-forum.cz/forum/jarni-vylet",
    )
    assert thread.page_kind == PageKind.THREAD
    assert thread.external_id == "jarni-vylet"
    assert thread.title == "Jarní výlet kolem Brna"
    assert thread.posts[0].external_id == "123"
    assert thread.posts[0].url.endswith("#comment-123")
    assert thread.posts[0].author == "cyklista"
    assert thread.posts[0].source_urls == ("https://mapy.com/s/sanitized-route",)


def test_import_is_idempotent_and_keeps_source_provenance() -> None:
    parsed = BeautifulSoupBikeForumParser().parse(FIXTURE.read_text(), THREAD_URL)
    first = import_thread(parsed)
    second = import_thread(parsed)
    assert (first.threads, first.posts, first.authors, first.sources) == (1, 1, 1, 1)
    assert (second.threads, second.posts, second.authors, second.sources) == (0, 0, 0, 0)
    assert ForumThread.objects.count() == 1
    assert ForumPost.objects.count() == ForumAuthor.objects.count() == 1
    assert RouteSource.objects.count() == 1


def test_import_skips_active_denylist_without_creating_source() -> None:
    SourceDenylistEntry.objects.create(source_url="https://mapy.com/s/demo", reason="takedown")
    result = import_thread(BeautifulSoupBikeForumParser().parse(FIXTURE.read_text(), THREAD_URL))
    assert result.denied == 1
    assert RouteSource.objects.count() == 0


def test_source_check_marks_unavailable_without_erasing_success_timestamp() -> None:
    import_thread(BeautifulSoupBikeForumParser().parse(FIXTURE.read_text(), THREAD_URL))
    source = RouteSource.objects.get()
    from apps.catalogue.services import mark_source_available

    mark_source_available(source)
    source.refresh_from_db()
    successful = source.last_successful_check_at
    assert successful is not None

    class UnavailableChecker(SourceChecker):
        def check(self, url: str) -> SourceCheckResult:
            return SourceCheckResult(available=False, status_code=404, error="gone")

    errors = _check_sources({source.pk}, UnavailableChecker())
    source.refresh_from_db()
    assert errors == ["https://mapy.com/s/demo: gone"]
    assert source.source_status == "unavailable"
    assert source.last_successful_check_at == successful


class FakeFetcher(PageFetcher):
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.urls: list[str] = []

    def fetch(self, url: str) -> FetchedPage:
        self.urls.append(url)
        return FetchedPage(url, self.pages[url])

    def close(self) -> None:
        return None


class NoopSourceChecker(SourceChecker):
    def check(self, url: str) -> SourceCheckResult:
        return SourceCheckResult(available=True, status_code=200)


class FailingFetcher(FakeFetcher):
    def __init__(self, pages: dict[str, str], failed: set[str]) -> None:
        super().__init__(pages)
        self.failed = failed

    def fetch(self, url: str) -> FetchedPage:
        if url in self.failed:
            self.urls.append(url)
            raise CrawlError(f"failed {url}")
        return super().fetch(url)


class InterruptedFetcher(FakeFetcher):
    """Simulate a worker process stopping while a frontier item is leased."""

    def fetch(self, url: str) -> FetchedPage:
        self.urls.append(url)
        raise KeyboardInterrupt


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"], BIKEFORUM_DNS_CHECK=False
)
def test_crawl_advances_checkpoint_and_second_run_resumes() -> None:
    second_html = FIXTURE.read_text().replace('href="/t/42?page=2"', 'href=""')
    fetcher = FakeFetcher(
        {THREAD_URL: FIXTURE.read_text(), "https://bikeforum.example/t/42?page=2": second_html}
    )
    first = run_crawl(
        start_url=THREAD_URL, max_pages=1, fetcher=fetcher, source_checker=NoopSourceChecker()
    )
    assert first["pages"] == 1
    assert CrawlCheckpoint.objects.get(stream="incremental").next_url.endswith("page=2")
    second = run_crawl(
        start_url=THREAD_URL, max_pages=2, fetcher=fetcher, source_checker=NoopSourceChecker()
    )
    assert second["pages"] == 2, second
    assert fetcher.urls == [THREAD_URL, "https://bikeforum.example/t/42?page=2"]
    assert CrawlTask.objects.filter(status=CrawlTaskStatus.COMPLETED).count() == 1


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"],
    BIKEFORUM_DNS_CHECK=False,
    BIKEFORUM_LEASE_SECONDS=60,
)
def test_interrupted_backfill_resumes_expired_frontier_without_duplicate_import() -> None:
    single_page = FIXTURE.read_text().replace('href="/t/42?page=2"', 'href=""')
    interrupted = InterruptedFetcher({THREAD_URL: single_page})
    with pytest.raises(KeyboardInterrupt):
        run_crawl(
            start_url=THREAD_URL,
            max_pages=1,
            kind=CrawlTask.Kind.BACKFILL,
            stream="backfill-interruption",
            fetcher=interrupted,
            source_checker=NoopSourceChecker(),
        )

    checkpoint = CrawlCheckpoint.objects.get(stream="backfill-interruption")
    work = CrawlPageWork.objects.get(url=THREAD_URL)
    task = CrawlTask.objects.get(stream="backfill-interruption")
    assert checkpoint.next_url == THREAD_URL
    assert work.status == CrawlPageStatus.PROCESSING
    assert task.status == CrawlTaskStatus.RUNNING

    # A restarted worker can reclaim the durable lease after its bounded lease
    # expires, exactly as it would after a process or host interruption.
    expired = timezone.now() - timedelta(seconds=1)
    CrawlCheckpoint.objects.filter(pk=checkpoint.pk).update(lease_until=expired)
    CrawlPageWork.objects.filter(pk=work.pk).update(lease_until=expired)
    CrawlTask.objects.filter(pk=task.pk).update(lease_until=expired)
    resumed = run_crawl(
        start_url=THREAD_URL,
        max_pages=1,
        kind=CrawlTask.Kind.BACKFILL,
        stream="backfill-interruption",
        fetcher=FakeFetcher({THREAD_URL: single_page}),
        source_checker=NoopSourceChecker(),
    )

    assert resumed["pages"] == 1
    assert resumed["frontier_remaining"] is False
    assert ForumThread.objects.count() == 1
    assert ForumPost.objects.count() == 1
    assert RouteSource.objects.count() == 1


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"], BIKEFORUM_DNS_CHECK=False
)
def test_index_page_enqueues_thread_frontier_without_importing_index_as_thread() -> None:
    index_url = "https://bikeforum.example/"
    fetcher = FakeFetcher({index_url: (FIXTURE.parent / "index.html").read_text()})
    result = run_crawl(
        start_url=index_url,
        max_pages=1,
        fetcher=fetcher,
        source_checker=NoopSourceChecker(),
        stream="index-frontier",
    )
    assert result["threads"] == 0
    assert set(
        CrawlPageWork.objects.filter(status=CrawlPageStatus.QUEUED).values_list("url", flat=True)
    ) == {
        "https://bikeforum.example/t/42",
        "https://bikeforum.example/thread/99",
        "https://bikeforum.example/archive?page=2",
    }


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"], BIKEFORUM_DNS_CHECK=False
)
def test_failed_frontier_item_isolated_while_other_pages_continue() -> None:
    index_url = "https://bikeforum.example/"
    pages = {
        index_url: (FIXTURE.parent / "index.html").read_text(),
        "https://bikeforum.example/t/42": FIXTURE.read_text(),
        "https://bikeforum.example/thread/99": FIXTURE.read_text().replace(
            'data-thread-id="thread-42"', 'data-thread-id="thread-99"'
        ),
    }
    fetcher = FailingFetcher(pages, {"https://bikeforum.example/t/42"})
    result = run_crawl(
        start_url=index_url,
        max_pages=3,
        fetcher=fetcher,
        source_checker=NoopSourceChecker(),
        stream="failure-isolation",
    )
    statuses = set(CrawlPageWork.objects.values_list("status", flat=True))
    assert CrawlPageStatus.FAILED in statuses
    assert CrawlPageStatus.SUCCEEDED in statuses
    assert result["errors"]
    assert CrawlTask.objects.get(pk=result["task_id"]).status == CrawlTaskStatus.FAILED


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"],
    BIKEFORUM_DNS_CHECK=False,
    BIKEFORUM_PAGE_ATTEMPTS=1,
)
def test_exhausted_frontier_is_not_resumed_or_reset() -> None:
    fetcher = FailingFetcher({THREAD_URL: FIXTURE.read_text()}, {THREAD_URL})
    first = run_crawl(
        start_url=THREAD_URL,
        max_pages=1,
        fetcher=fetcher,
        source_checker=NoopSourceChecker(),
        stream="exhausted",
    )
    first_work = CrawlPageWork.objects.get(task_id=first["task_id"])
    assert first_work.status == CrawlPageStatus.EXHAUSTED
    assert first_work.attempts == 1

    second = run_crawl(
        start_url=THREAD_URL,
        max_pages=1,
        fetcher=fetcher,
        source_checker=NoopSourceChecker(),
        stream="exhausted",
    )
    second_work = CrawlPageWork.objects.get(task_id=second["task_id"])
    assert second["task_id"] != first["task_id"]
    assert second_work.attempts == 1
    first_work.refresh_from_db()
    assert first_work.status == CrawlPageStatus.EXHAUSTED


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"], BIKEFORUM_DNS_CHECK=False
)
def test_stale_worker_cannot_commit_thread_after_lease_takeover() -> None:
    class TakeoverParser(BeautifulSoupBikeForumParser):
        def parse(self, body: str, url: str) -> ParsedThread:
            parsed = super().parse(body, url)
            CrawlCheckpoint.objects.filter(stream="stale-worker").update(lease_token="takeover")
            return parsed

    with pytest.raises(CrawlBusy):
        run_crawl(
            start_url=THREAD_URL,
            max_pages=1,
            fetcher=FakeFetcher({THREAD_URL: FIXTURE.read_text()}),
            parser=TakeoverParser(),
            source_checker=NoopSourceChecker(),
            stream="stale-worker",
        )
    assert ForumThread.objects.count() == 0


def test_stream_lease_prevents_concurrent_cursor_overwrite() -> None:
    checkpoint = CrawlCheckpoint.objects.create(
        stream="leased",
        next_url="https://bikeforum.example/",
        lease_token="other",
        lease_until=timezone.now() + timedelta(minutes=5),
    )
    with pytest.raises(CrawlBusy):
        run_crawl(start_url=checkpoint.next_url, stream="leased", max_pages=1)


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"], BIKEFORUM_DNS_CHECK=False
)
@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@bikeforum.example/",
        "https://@bikeforum.example/",
        "https://bikeforum.example:8443/",
        "http://bikeforum.example/",
    ],
)
def test_crawl_url_rejects_userinfo_http_and_unconfigured_ports(url: str) -> None:
    with pytest.raises(CrawlError):
        HttpxPageFetcher(client=httpx.Client(), rate_limit=0).fetch(url)


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"], BIKEFORUM_DNS_CHECK=False
)
def test_malformed_page_is_recorded_and_does_not_raise() -> None:
    fetcher = FakeFetcher({THREAD_URL: "<html><body>broken</body></html>"})
    result = run_crawl(
        start_url=THREAD_URL,
        max_pages=1,
        fetcher=fetcher,
        source_checker=NoopSourceChecker(),
        stream="bad-page",
    )
    assert result["errors"]
    assert CrawlCheckpoint.objects.get(stream="bad-page").last_error


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"], BIKEFORUM_DNS_CHECK=False
)
def test_http_fetcher_obeys_robots_and_reuses_cache() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /t/\nDisallow: /private\n")
        return httpx.Response(200, text=FIXTURE.read_text(), headers={"ETag": '"v1"'})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = HttpxPageFetcher(client=client, rate_limit=0, cache_ttl=3600)
    page = fetcher.fetch(THREAD_URL)
    repeated = fetcher.fetch(THREAD_URL)
    assert page.body == repeated.body
    assert requests == ["https://bikeforum.example/robots.txt", THREAD_URL]
    fetcher._robots.clear()
    with patch.object(fetcher, "_robots_for") as robots:
        robots.return_value = RobotFileParser()
        robots.return_value.parse(["User-agent: *", "Disallow: /t/"])
        with pytest.raises(RobotsDenied):
            fetcher.fetch(THREAD_URL)
    with pytest.raises(RobotsDenied):
        fetcher.fetch("https://bikeforum.example/private")
    client.close()


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"], BIKEFORUM_DNS_CHECK=False
)
def test_http_fetcher_rejects_cross_host_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"Location": "https://evil.example/"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    with pytest.raises(CrawlError, match="Unsafe redirect"):
        HttpxPageFetcher(client=client, rate_limit=0).fetch(THREAD_URL)
    with pytest.raises(CrawlError, match="configured BikeForum origins"):
        HttpxPageFetcher(client=client, rate_limit=0).fetch("https://evil.example/")
    client.close()


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"], BIKEFORUM_DNS_CHECK=False
)
def test_redirect_returns_and_caches_final_url_for_parser() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(200, text=FIXTURE.read_text())

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = HttpxPageFetcher(client=client, rate_limit=0)
    first = fetcher.fetch("https://bikeforum.example/start")
    second = fetcher.fetch("https://bikeforum.example/start")
    assert first.url == second.url == "https://bikeforum.example/final"
    client.close()


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"], BIKEFORUM_DNS_CHECK=False
)
def test_http_fetcher_retries_transient_response_with_backoff_and_ua() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(503 if len(calls) == 2 else 200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = HttpxPageFetcher(
        client=client, timeout=7, rate_limit=0, retries=1, backoff=0.01, cache_ttl=0
    )
    with patch("apps.ingestion.crawler.time.sleep") as sleep:
        assert fetcher.fetch(THREAD_URL).body == "ok"
    assert calls[-1].headers["user-agent"].startswith("BikeMapyBot")
    assert fetcher.timeout == 7
    assert sleep.call_count >= 1
    client.close()


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"], BIKEFORUM_DNS_CHECK=False
)
def test_http_fetcher_stops_consuming_an_oversized_page() -> None:
    consumed: list[bytes] = []

    class Chunks(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for chunk in (b"12", b"34", b"567"):
                consumed.append(chunk)
                yield chunk

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=Chunks())

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = HttpxPageFetcher(client=client, rate_limit=0, max_bytes=3)
    with pytest.raises(CrawlError, match="byte limit"):
        fetcher.fetch(THREAD_URL)
    assert consumed == [b"12", b"34"]
    client.close()


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"], BIKEFORUM_DNS_CHECK=False
)
def test_http_fetcher_caps_robots_response_before_parsing() -> None:
    consumed: list[bytes] = []

    class Chunks(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for chunk in (b"12", b"34", b"567"):
                consumed.append(chunk)
                yield chunk

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=Chunks())

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = HttpxPageFetcher(client=client, rate_limit=0, max_bytes=3)
    with pytest.raises(CrawlError, match="byte limit"):
        fetcher.fetch(THREAD_URL)
    assert consumed == [b"12", b"34"]
    client.close()


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"], BIKEFORUM_DNS_CHECK=False
)
def test_http_fetcher_does_not_return_an_oversized_cached_304() -> None:
    CrawlResponseCache.objects.create(
        url=THREAD_URL,
        final_url=THREAD_URL,
        status_code=200,
        body="oversized",
        fetched_at=timezone.now() - timedelta(hours=2),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(304)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = HttpxPageFetcher(client=client, rate_limit=0, cache_ttl=1, max_bytes=3)
    with pytest.raises(CrawlError, match="byte limit"):
        fetcher.fetch(THREAD_URL)
    client.close()


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"], BIKEFORUM_DNS_CHECK=True
)
def test_http_fetcher_rechecks_dns_and_pacing_for_each_attempt() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        attempts += 1
        return httpx.Response(503 if attempts == 1 else 200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = HttpxPageFetcher(client=client, rate_limit=0, retries=1, backoff=0)
    with (
        patch.object(fetcher, "_validate_url") as validate,
        patch.object(fetcher, "_wait") as wait,
    ):
        assert fetcher.fetch(THREAD_URL).body == "ok"
    # Initial URL validation, robots validation, then one validation and one
    # pacing event immediately before each of the two page attempts.
    assert validate.call_count == 4
    assert wait.call_count == 3
    client.close()


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"], BIKEFORUM_DNS_CHECK=False
)
def test_http_fetcher_uses_conditional_cache_validators() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(304)

    CrawlResponseCache.objects.create(
        url=THREAD_URL,
        final_url=THREAD_URL,
        status_code=200,
        body="cached",
        etag='"v1"',
        last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
        fetched_at=timezone.now() - timedelta(hours=2),
    )
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = HttpxPageFetcher(client=client, rate_limit=0, cache_ttl=1)
    assert fetcher.fetch(THREAD_URL).body == "cached"
    assert seen[-1].headers["if-none-match"] == '"v1"'
    assert seen[-1].headers["if-modified-since"] == "Wed, 01 Jan 2025 00:00:00 GMT"
    client.close()


@override_settings(BIKEFORUM_CACHE_BODY_RETENTION_SECONDS=24 * 3600)
def test_cache_cleanup_is_bounded_and_preserves_conditional_metadata() -> None:
    now = timezone.now()
    old = CrawlResponseCache.objects.create(
        url="https://bikeforum.example/old",
        final_url="https://bikeforum.example/final",
        status_code=200,
        body="old body",
        etag='"old"',
        last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
        checksum="old-checksum",
        fetched_at=now - timedelta(days=2),
    )
    newer = CrawlResponseCache.objects.create(
        url="https://bikeforum.example/newer",
        status_code=200,
        body="new body",
        etag='"new"',
        fetched_at=now - timedelta(days=2),
    )

    result = cleanup_expired_crawl_response_bodies(now=now, limit=1)

    assert result["cleared"] == 1
    assert result["remaining"] == 1
    old.refresh_from_db()
    assert old.body == ""
    assert (old.final_url, old.etag, old.last_modified, old.checksum) == (
        "https://bikeforum.example/final",
        '"old"',
        "Wed, 01 Jan 2025 00:00:00 GMT",
        "old-checksum",
    )
    newer.refresh_from_db()
    assert newer.body == "new body"


def test_cache_body_expiry_uses_a_partial_index() -> None:
    expiry_index = next(
        index
        for index in CrawlResponseCache._meta.indexes
        if index.name == "ingestion_c_body_expiry_idx"
    )

    assert expiry_index.fields == ["body_expires_at", "id"]
    assert expiry_index.condition == models.Q(body_expires_at__isnull=False)


@override_settings(BIKEFORUM_CACHE_BODY_RETENTION_SECONDS=86401)
def test_cache_body_retention_setting_cannot_exceed_24_hours() -> None:
    assert settings.CRAWLER_CACHE_BODY_RETENTION_MAX_SECONDS == 24 * 3600
    with pytest.raises(ValueError, match="between 1 and 86400 seconds"):
        configured_body_retention_seconds()


@override_settings(BIKEFORUM_CACHE_BODY_RETENTION_SECONDS=0)
def test_cache_body_retention_setting_must_be_positive() -> None:
    with pytest.raises(ValueError, match="between 1 and 86400 seconds"):
        configured_body_retention_seconds()


def test_cache_cleanup_is_scheduled_and_real_crawl_is_disabled_by_default() -> None:
    from apps.ingestion.tasks import cleanup_crawl_response_cache, crawl_bikeforum

    assert settings.CELERY_BEAT_SCHEDULE["cleanup-crawler-response-cache"] == {
        "task": "bikemapy.ingestion.cleanup_crawl_response_cache",
        "schedule": 3600,
    }
    assert crawl_bikeforum()["status"] == "disabled"

    old = CrawlResponseCache.objects.create(
        url="https://bikeforum.example/scheduled-cleanup",
        status_code=200,
        body="old body",
        fetched_at=timezone.now() - timedelta(days=2),
    )
    assert cleanup_crawl_response_cache(limit=1)["cleared"] == 1
    old.refresh_from_db()
    assert old.body == ""


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"],
    BIKEFORUM_DNS_CHECK=False,
)
def test_metadata_only_cache_retries_without_validators_after_304() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, text="fresh body", headers={"ETag": '"v2"'})

    CrawlResponseCache.objects.create(
        url=THREAD_URL,
        final_url=THREAD_URL,
        status_code=200,
        body="",
        etag='"v1"',
        fetched_at=timezone.now() - timedelta(days=2),
    )
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = HttpxPageFetcher(client=client, rate_limit=0, cache_ttl=1)
    assert fetcher.fetch(THREAD_URL).body == "fresh body"
    assert seen[-2].headers["if-none-match"] == '"v1"'
    assert "if-none-match" not in seen[-1].headers
    client.close()


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"],
    BIKEFORUM_DNS_CHECK=False,
)
def test_empty_response_has_no_body_expiry_or_cleanup_index_entry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = HttpxPageFetcher(client=client, rate_limit=0)

    assert fetcher.fetch(THREAD_URL).body == ""

    cache_entry = CrawlResponseCache.objects.get(url=THREAD_URL)
    assert cache_entry.body_expires_at is None
    assert not CrawlResponseCache.objects.filter(
        pk=cache_entry.pk, body_expires_at__isnull=False
    ).exists()
    cache_entry.body = "restored temporarily"
    cache_entry.save(update_fields=["body"])
    cache_entry.body = ""
    cache_entry.save(update_fields=["body"])
    cache_entry.refresh_from_db()
    assert cache_entry.body_expires_at is None
    client.close()


@override_settings(
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"],
    BIKEFORUM_DNS_CHECK=False,
    BIKEFORUM_CACHE_BODY_RETENTION_SECONDS=24 * 3600,
)
def test_body_backed_304_does_not_extend_body_retention_deadline() -> None:
    expiry = timezone.now() + timedelta(hours=1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(304)

    cache_entry = CrawlResponseCache.objects.create(
        url=THREAD_URL,
        final_url=THREAD_URL,
        status_code=200,
        body="cached",
        etag='"v1"',
        fetched_at=timezone.now() - timedelta(hours=2),
        body_expires_at=expiry,
    )
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    fetcher = HttpxPageFetcher(client=client, rate_limit=0, cache_ttl=1)

    assert fetcher.fetch(THREAD_URL).body == "cached"

    cache_entry.refresh_from_db()
    assert cache_entry.fetched_at > expiry - timedelta(hours=2)
    assert cache_entry.body_expires_at == expiry
    client.close()


def test_stale_metadata_save_cannot_restore_a_concurrent_cache_deadline() -> None:
    now = timezone.now()
    initial_expiry = now + timedelta(hours=1)
    cache_entry = CrawlResponseCache.objects.create(
        url="https://bikeforum.example/race",
        status_code=200,
        body="original",
        fetched_at=now,
        body_expires_at=initial_expiry,
    )
    stale = CrawlResponseCache.objects.get(pk=cache_entry.pk)

    fresh_expiry = now + timedelta(days=1)
    CrawlResponseCache.objects.filter(pk=cache_entry.pk).update(
        body="fresh", body_expires_at=fresh_expiry
    )
    stale.fetched_at = now + timedelta(minutes=1)
    stale.save(update_fields=["fetched_at"])

    cache_entry.refresh_from_db()
    assert (cache_entry.body, cache_entry.body_expires_at) == ("fresh", fresh_expiry)


def test_stale_metadata_save_cannot_resurrect_body_after_cleanup() -> None:
    now = timezone.now()
    cache_entry = CrawlResponseCache.objects.create(
        url="https://bikeforum.example/cleanup-race",
        status_code=200,
        body="original",
        fetched_at=now - timedelta(days=1),
        body_expires_at=now - timedelta(minutes=1),
    )
    stale = CrawlResponseCache.objects.get(pk=cache_entry.pk)

    assert cleanup_expired_crawl_response_bodies(now=now)["cleared"] == 1
    stale.fetched_at = now + timedelta(minutes=1)
    stale.save(update_fields=["fetched_at"])

    cache_entry.refresh_from_db()
    assert cache_entry.body == ""
    assert cache_entry.body_expires_at is None


def test_http_fetcher_paces_requests() -> None:
    fetcher = HttpxPageFetcher(client=httpx.Client(), rate_limit=2)
    with patch("apps.ingestion.crawler.time.monotonic", side_effect=[0.0, 0.0, 2.0, 2.0]) as clock:
        with patch("apps.ingestion.crawler.time.sleep") as sleep:
            fetcher._wait()
            fetcher._wait()
    assert sleep.call_args_list[0].args[0] == 2
    assert clock.call_count == 4
    fetcher.close()


def test_source_checker_rejects_noncanonical_urls_and_response_policy() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, text="ok", headers={"content-type": "application/json"})
    )
    checker = HttpxSourceChecker(client=httpx.Client(transport=transport), rate_limit=0)
    assert checker.check("http://mapy.com/s/demo").available is False
    assert checker.check("https://www.mapy.com/s/demo").available is False
    with patch(
        "apps.ingestion.crawler.socket.getaddrinfo",
        return_value=[(0, 0, 0, "", ("93.184.216.34", 443))],
    ):
        assert checker.check("https://mapy.com/s/demo").error == "unexpected content type"
    checker.close()


def test_source_checker_rejects_empty_userinfo() -> None:
    checker = HttpxSourceChecker(client=httpx.Client(), rate_limit=0)
    assert checker.check("https://@mapy.com/s/demo").error == "unsafe or non-canonical Mapy URL"
    checker.close()


def test_source_checker_rejects_redirects_and_oversized_payloads() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("redirect"):
            return httpx.Response(302, headers={"Location": "https://mapy.com/s/other"})
        return httpx.Response(200, content=b"too long", headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    checker = HttpxSourceChecker(client=client, rate_limit=0, max_bytes=3)
    public = [(0, 0, 0, "", ("93.184.216.34", 443))]
    with patch("apps.ingestion.crawler.socket.getaddrinfo", return_value=public):
        assert checker.check("https://mapy.com/s/demo").error == "source response too large"
        assert checker.check("https://mapy.com/s/redirect").available is False
    checker.close()


def test_source_checker_rechecks_dns_and_paces_each_retry() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            503 if requests == 1 else 200,
            text="ok",
            headers={"content-type": "text/html"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    checker = HttpxSourceChecker(client=client, rate_limit=1, retries=1, backoff=0)
    public = [(0, 0, 0, "", ("93.184.216.34", 443))]
    with (
        patch("apps.ingestion.crawler.socket.getaddrinfo", return_value=public) as dns,
        patch("apps.ingestion.crawler.time.sleep") as sleep,
        patch("apps.ingestion.crawler.time.monotonic", return_value=0.0),
    ):
        assert checker.check("https://mapy.com/s/demo").available
    assert dns.call_count == 2
    # The two one-second sleeps are pacing immediately before each request;
    # the zero-second sleep between them is the retry backoff.
    assert [call.args[0] for call in sleep.call_args_list] == [1, 0, 1]
    checker.close()


def test_source_checker_stops_consuming_an_oversized_stream() -> None:
    consumed: list[bytes] = []

    class Chunks(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for chunk in (b"12", b"34", b"567"):
                consumed.append(chunk)
                yield chunk

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "text/html"}, stream=Chunks())
        ),
        follow_redirects=False,
    )
    checker = HttpxSourceChecker(client=client, rate_limit=0, max_bytes=3)
    public = [(0, 0, 0, "", ("93.184.216.34", 443))]
    with patch("apps.ingestion.crawler.socket.getaddrinfo", return_value=public):
        result = checker.check("https://mapy.com/s/demo")
    assert result.error == "source response too large"
    assert consumed == [b"12", b"34"]
    checker.close()
