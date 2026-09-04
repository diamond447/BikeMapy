from django.db import connection
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET


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
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ready"})


@require_GET
def api_root(request: HttpRequest) -> JsonResponse:
    del request
    return JsonResponse({"service": "bikemapy-api", "version": "v1"})
