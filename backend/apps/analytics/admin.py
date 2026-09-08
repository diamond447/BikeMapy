"""Owner-only read view for the aggregate launch counters."""

from django.contrib import admin

from apps.accounts.admin import owner_admin_site
from apps.catalogue.admin import OwnerModelAdmin

from .models import AnalyticsCounter


@admin.register(AnalyticsCounter, site=owner_admin_site)
class AnalyticsCounterAdmin(OwnerModelAdmin):
    list_display = ("day", "event", "count")
    list_filter = ("event", "day")
    ordering = ("-day", "event")
    readonly_fields = ("day", "event", "count")

    def has_add_permission(self, request):  # type: ignore[no-untyped-def]
        return False

    def has_change_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False

    def has_delete_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False
