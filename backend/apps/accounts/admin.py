"""A dedicated Django Admin site that admits only the configured owner."""

from django.contrib.admin import AdminSite
from django.http import HttpRequest, HttpResponseForbidden

from .authorization import is_owner


class OwnerAdminSite(AdminSite):
    site_header = "BikeMapy owner administration"
    site_title = "BikeMapy admin"
    index_title = "Catalogue and moderation"

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
