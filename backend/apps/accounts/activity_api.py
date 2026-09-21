"""Strava activity settings and verified webhook endpoints."""

# djangorestframework currently does not ship type stubs.
# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

import hmac
import json
from typing import Any

from django.conf import settings
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from .activity_services import (
    geometry_payload,
    queue_sync,
    queue_webhook_event,
    verify_webhook_signature,
    webhook_event_key,
)
from .game_api import GameEndpoint, _private
from .models import ImportedActivity, StravaSyncState, StravaWebhookEvent
from .services import game_is_available


class ActivitySyncSerializer(serializers.Serializer[dict[str, Any]]):
    status = serializers.CharField()
    mode = serializers.CharField()
    imported_count = serializers.IntegerField()
    rejected_count = serializers.IntegerField()
    processed_count = serializers.IntegerField()
    cursor_page = serializers.IntegerField()
    last_error = serializers.CharField()
    completed_at = serializers.DateTimeField(allow_null=True)


class ActivitySyncResponseSerializer(serializers.Serializer[dict[str, Any]]):
    sync = ActivitySyncSerializer()


class ActivitySerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField()
    player_id = serializers.IntegerField()
    calendar_date = serializers.DateField()
    geometry = serializers.JSONField()


class StravaWebhookPayloadSerializer(serializers.Serializer[dict[str, Any]]):
    object_type = serializers.CharField()
    object_id = serializers.IntegerField()
    owner_id = serializers.IntegerField()
    aspect_type = serializers.CharField()
    event_time = serializers.IntegerField(required=False)
    subscription_id = serializers.IntegerField(required=False)


def sync_payload(state: StravaSyncState | None) -> dict[str, Any]:
    if state is None:
        return {
            "status": StravaSyncState.Status.IDLE,
            "mode": "incremental",
            "imported_count": 0,
            "rejected_count": 0,
            "processed_count": 0,
            "cursor_page": 1,
            "last_error": "",
            "completed_at": None,
        }
    return {
        "status": state.status,
        "mode": state.mode,
        "imported_count": state.imported_count,
        "rejected_count": state.rejected_count,
        "processed_count": state.processed_count,
        "cursor_page": state.cursor_page,
        "last_error": state.last_error,
        "completed_at": state.completed_at,
    }


class PlayerActivitySettingsView(GameEndpoint):
    @extend_schema(
        responses={
            200: ActivitySyncResponseSerializer,
            401: OpenApiResponse(description="Authentication required."),
        },
        tags=["game-activities"],
    )
    def get(self, request: Any) -> Response:
        if not game_is_available():
            return self.unavailable()
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        state = StravaSyncState.objects.filter(player=player).first()
        return _private(Response({"sync": sync_payload(state)}))


class PlayerFullHistoryView(GameEndpoint):
    @extend_schema(
        request=None,
        responses={
            200: ActivitySyncResponseSerializer,
            401: OpenApiResponse(description="Authentication required."),
        },
        tags=["game-activities"],
    )
    @extend_schema(
        request=StravaWebhookPayloadSerializer,
        responses={
            200: OpenApiResponse(description="Webhook accepted."),
            403: OpenApiResponse(description="Invalid signature."),
        },
        tags=["game-webhooks"],
    )
    def post(self, request: Any) -> Response:
        if not game_is_available():
            return self.unavailable()
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        queue_sync(player, kind="full-history", full_history=True)
        state = StravaSyncState.objects.get(player=player)
        return _private(Response({"sync": sync_payload(state)}))


@method_decorator(csrf_exempt, name="dispatch")
class StravaWebhookView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []
    serializer_class = StravaWebhookPayloadSerializer

    @extend_schema(
        responses={
            200: OpenApiResponse(description="Verified Strava subscription challenge."),
            403: OpenApiResponse(description="Invalid challenge."),
        },
        tags=["game-webhooks"],
    )
    def get(self, request: Any) -> Response:
        if request.query_params.get("hub.mode") != "subscribe":
            return Response({"detail": "Invalid subscription challenge."}, status=400)
        verify_token = str(getattr(settings, "STRAVA_WEBHOOK_VERIFY_TOKEN", ""))
        supplied = str(request.query_params.get("hub.verify_token") or "")
        challenge = str(request.query_params.get("hub.challenge") or "")
        if not verify_token or not hmac.compare_digest(verify_token, supplied) or not challenge:
            return Response({"detail": "Invalid subscription challenge."}, status=403)
        return Response({"hub.challenge": challenge})

    def post(self, request: Any) -> Response:
        raw = request.body
        if not verify_webhook_signature(raw, request.headers.get("X-Strava-Signature", "")):
            return Response({"detail": "Invalid webhook signature."}, status=403)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return Response({"detail": "Invalid webhook payload."}, status=400)
        if not isinstance(payload, dict) or payload.get("object_type") != "activity":
            return Response({"detail": "Unsupported webhook payload."}, status=400)
        try:
            if int(payload.get("object_id") or 0) <= 0 or int(payload.get("owner_id") or 0) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return Response({"detail": "Invalid webhook identity."}, status=400)
        event_key = webhook_event_key(payload, raw)
        duplicate = StravaWebhookEvent.objects.filter(event_key=event_key).exists()
        queue_webhook_event(payload, event_key)
        return Response({"accepted": True, "duplicate": duplicate}, status=200)


def activity_payload(activity: ImportedActivity) -> dict[str, Any]:
    return {
        "id": activity.pk,
        "player_id": activity.player_id,
        "calendar_date": activity.calendar_date,
        "geometry": geometry_payload(activity.geometry),
    }
