from django.urls import path

from .views import api_root
from .views_routes import (
    RouteDetailView,
    RouteGpxDownloadView,
    RouteListView,
    SelectedRouteView,
    ViewportRouteView,
)

urlpatterns = [
    path("", api_root, name="api-root"),
    path("routes/", RouteListView.as_view(), name="public-route-list"),
    path("routes/viewport/", ViewportRouteView.as_view(), name="public-route-viewport"),
    path("routes/<uuid:route_id>/", RouteDetailView.as_view(), name="public-route-detail"),
    path("routes/<uuid:route_id>/gpx/", RouteGpxDownloadView.as_view(), name="public-route-gpx"),
    path("routes/by-slug/<slug:slug>/", RouteDetailView.as_view(), name="public-route-by-slug"),
    # This explicit name documents that geometry is only returned for a
    # selected route, never as a bulk payload.
    path(
        "routes/<uuid:route_id>/geometry/",
        SelectedRouteView.as_view(),
        name="public-route-geometry",
    ),
]
