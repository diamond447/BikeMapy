"""A dedicated Django Admin site that admits only the configured owner."""

from typing import Any

from django.conf import settings
from django.contrib.admin import AdminSite
from django.core.cache import cache
from django.http import HttpRequest, HttpResponseForbidden
from django.template.response import TemplateResponse

from .authorization import is_owner

PERIODIC_TASKS_LAST_RUN_KEY = "deployment:periodic-tasks:last-run"


def deployment_mode() -> dict[str, Any]:
    """Describe how this deployment runs background work, for the admin index."""

    lean = not settings.BACKGROUND_JOBS_ENABLED
    return {
        "lean": lean,
        "tasks": "inline, from host cron" if lean else "Celery worker and beat",
        "cache": "PostgreSQL" if lean else "Redis",
        "periodic_tasks_last_run": cache.get(PERIODIC_TASKS_LAST_RUN_KEY) if lean else None,
    }


class OwnerAdminSite(AdminSite):
    site_header = "BikeMapy owner administration"
    site_title = "BikeMapy admin"
    index_title = "Catalogue and moderation"

    def index(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> TemplateResponse:
        return super().index(request, {**(extra_context or {}), "deployment": deployment_mode()})

    def has_permission(self, request: HttpRequest) -> bool:
        return bool(is_owner(request.user, request))

    def admin_view(self, view, cacheable=False):  # type: ignore[no-untyped-def]
        guarded_view = super().admin_view(view, cacheable=cacheable)

        def owner_guard(request, *args, **kwargs):  # type: ignore[no-untyped-def]
            if getattr(request.user, "is_authenticated", False) and not self.has_permission(
                request
            ):
                return HttpResponseForbidden("Administrative access is restricted to the owner.")
            return guarded_view(request, *args, **kwargs)

        return owner_guard


owner_admin_site = OwnerAdminSite(name="owner_admin")
