from django.urls import path

from apps.accounts.game_api import (
    PlayerAccountView,
    PlayerDisconnectView,
    PlayerLogoutView,
    PlayerRefreshView,
    PlayerSessionView,
    StravaAuthorizeView,
    StravaCallbackView,
)
from apps.analytics.api import AnalyticsEventView
from apps.reports.api import RouteReportView

from .views import api_root
from .views_reference_routes import ReferenceRouteDetailView, ReferenceRouteListView
from .views_routes import (
    RouteDetailView,
    RouteGpxDownloadView,
    RouteListView,
    SelectedRouteView,
    ViewportRouteView,
)

urlpatterns = [
    path("", api_root, name="api-root"),
    path(
        "game/auth/strava/authorize/",
        StravaAuthorizeView.as_view(),
        name="game-strava-authorize",
    ),
    path(
        "game/auth/strava/callback/",
        StravaCallbackView.as_view(),
        name="game-strava-callback",
    ),
    path("game/auth/session/", PlayerSessionView.as_view(), name="game-player-session"),
    path("game/auth/logout/", PlayerLogoutView.as_view(), name="game-player-logout"),
    path("game/account/refresh/", PlayerRefreshView.as_view(), name="game-player-refresh"),
    path("game/account/disconnect/", PlayerDisconnectView.as_view(), name="game-player-disconnect"),
    path("game/account/", PlayerAccountView.as_view(), name="game-player-account"),
    path("analytics/events/", AnalyticsEventView.as_view(), name="analytics-events"),
    path("routes/", RouteListView.as_view(), name="public-route-list"),
    path("routes/viewport/", ViewportRouteView.as_view(), name="public-route-viewport"),
    path("routes/<uuid:route_id>/", RouteDetailView.as_view(), name="public-route-detail"),
    path("routes/<uuid:route_id>/gpx/", RouteGpxDownloadView.as_view(), name="public-route-gpx"),
    path("routes/<uuid:route_id>/reports/", RouteReportView.as_view(), name="public-route-report"),
    path(
        "game/reference-routes/", ReferenceRouteListView.as_view(), name="game-reference-route-list"
    ),
    path(
        "game/reference-routes/<uuid:route_id>/",
        ReferenceRouteDetailView.as_view(),
        name="game-reference-route-detail",
    ),
    path("routes/by-slug/<slug:slug>/", RouteDetailView.as_view(), name="public-route-by-slug"),
    # This explicit name documents that geometry is only returned for a
    # selected route, never as a bulk payload.
    path(
        "routes/<uuid:route_id>/geometry/",
        SelectedRouteView.as_view(),
        name="public-route-geometry",
    ),
]
