from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.ingestion.models import CrawlCheckpoint, CrawlTask, CrawlTaskStatus


@require_GET
def health_live(request: HttpRequest) -> JsonResponse:
    del request
    return JsonResponse({"status": "ok"})


@require_GET
def health_ready(request: HttpRequest) -> JsonResponse:
    del request
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        cache_key = "health:ready"
        cache.set(cache_key, "ok", timeout=10)
        if cache.get(cache_key) != "ok":
            raise RuntimeError("cache round trip failed")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ready"})


@require_GET
def health_crawler(request: HttpRequest) -> JsonResponse:
    """Report whether the scheduled incremental crawl has fresh progress."""

    del request
    try:
        checkpoint = CrawlCheckpoint.objects.get(stream="incremental")
    except CrawlCheckpoint.DoesNotExist:
        return JsonResponse({"status": "stale", "stream": "incremental"}, status=503)
    except Exception:
        return JsonResponse({"status": "unavailable", "stream": "incremental"}, status=503)
    if checkpoint.last_successful_at is None:
        return JsonResponse({"status": "stale", "stream": "incremental"}, status=503)
    age = (timezone.now() - checkpoint.last_successful_at).total_seconds()
    latest_task = (
        CrawlTask.objects.filter(kind=CrawlTask.Kind.INCREMENTAL)
        .order_by("-created_at", "-pk")
        .first()
    )
    latest_status = latest_task.status if latest_task else None
    task_is_unhealthy = latest_status == CrawlTaskStatus.FAILED
    response = {
        "status": "ok"
        if age <= settings.CRAWLER_FRESHNESS_MAX_AGE and not task_is_unhealthy
        else "stale",
        "stream": "incremental",
        "last_successful_at": checkpoint.last_successful_at.isoformat(),
        "age_seconds": int(max(0, age)),
        "latest_task_status": latest_status,
    }
    return JsonResponse(response, status=200 if response["status"] == "ok" else 503)


@require_GET
def api_root(request: HttpRequest) -> JsonResponse:
    del request
    return JsonResponse({"service": "bikemapy-api", "version": "v1"})
