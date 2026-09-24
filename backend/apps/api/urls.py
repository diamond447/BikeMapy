from django.urls import path

from apps.accounts.activity_api import (
    PlayerActivitySettingsView,
    PlayerFullHistoryView,
    StravaWebhookView,
)
from apps.accounts.competition_api import (
    CompetitionDetailView,
    CompetitionJoinView,
    CompetitionLeaveView,
    CompetitionListView,
    CompetitionMemberColorView,
    CompetitionRemoveMemberView,
    CompetitionRotateInviteView,
    CompetitionSwitchView,
    CompetitionTransferView,
)
from apps.accounts.competition_capture_api import CompetitionCaptureView
from apps.accounts.competition_map_api import CompetitionMapView
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
from .views_reference_completion import ReferenceRouteCompletionView
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
    path(
        "game/account/activities/",
        PlayerActivitySettingsView.as_view(),
        name="game-player-activities",
    ),
    path(
        "game/account/activities/full-history/",
        PlayerFullHistoryView.as_view(),
        name="game-player-full-history",
    ),
    path("game/webhooks/strava/", StravaWebhookView.as_view(), name="game-strava-webhook"),
    path("game/competitions/", CompetitionListView.as_view(), name="game-competition-list"),
    path("game/competitions/join/", CompetitionJoinView.as_view(), name="game-competition-join"),
    path(
        "game/competitions/<uuid:competition_id>/",
        CompetitionDetailView.as_view(),
        name="game-competition-detail",
    ),
    path(
        "game/competitions/<uuid:competition_id>/switch/",
        CompetitionSwitchView.as_view(),
        name="game-competition-switch",
    ),
    path(
        "game/competitions/<uuid:competition_id>/map/",
        CompetitionMapView.as_view(),
        name="game-competition-map",
    ),
    path(
        "game/competitions/<uuid:competition_id>/capture/",
        CompetitionCaptureView.as_view(),
        name="game-competition-capture",
    ),
    path(
        "game/competitions/<uuid:competition_id>/rotate-invite/",
        CompetitionRotateInviteView.as_view(),
        name="game-competition-rotate-invite",
    ),
    path(
        "game/competitions/<uuid:competition_id>/leave/",
        CompetitionLeaveView.as_view(),
        name="game-competition-leave",
    ),
    path(
        "game/competitions/<uuid:competition_id>/members/me/",
        CompetitionMemberColorView.as_view(),
        name="game-competition-member-color",
    ),
    path(
        "game/competitions/<uuid:competition_id>/members/<int:player_id>/",
        CompetitionRemoveMemberView.as_view(),
        name="game-competition-remove-member",
    ),
    path(
        "game/competitions/<uuid:competition_id>/transfer/",
        CompetitionTransferView.as_view(),
        name="game-competition-transfer",
    ),
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
    path(
        "game/reference-routes/<uuid:route_id>/completion/",
        ReferenceRouteCompletionView.as_view(),
        name="game-reference-route-completion",
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
