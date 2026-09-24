"""Authenticated player account endpoints for the private game boundary."""

# djangorestframework currently does not ship type stubs.
# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import OAuthState, Player
from .services import (
    StravaOAuthError,
    authorization_url,
    delete_player,
    disconnect_player,
    exchange_code,
    game_is_available,
    game_return_url,
    refresh_connection,
    revoke_or_schedule,
    save_connection,
    validate_granted_scopes,
)


def _private(response: Response) -> Response:
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _current_player(request: Any) -> Player | None:
    player_id = request.session.get("player_id")
    epoch = request.session.get("player_session_epoch")
    if not player_id or epoch is None:
        return None
    try:
        player = Player.objects.get(pk=player_id)
    except Player.DoesNotExist:
        return None
    if player.session_epoch != epoch or player.lifecycle != Player.Lifecycle.CONNECTED:
        return None
    return player


def _clear_player_session(request: Any) -> None:
    request.session.pop("player_id", None)
    request.session.pop("player_session_epoch", None)
    methods = request.session.get("account_authentication_methods", [])
    request.session["account_authentication_methods"] = [
        method
        for method in methods
        if not isinstance(method, dict) or method.get("provider") != "strava"
    ]
    request.session.save()


def _player_payload(player: Player) -> dict[str, Any]:
    return {
        "id": str(player.pk),
        "athlete_id": str(player.strava_athlete_id),
        "display_name": player.strava_display_name,
        "profile_image_url": player.strava_profile_image_url or None,
        "nickname": player.nickname or None,
        "lifecycle": player.lifecycle,
        "connected_at": player.connected_at,
    }


class PlayerSerializer(serializers.Serializer[Player]):
    id = serializers.CharField()
    athlete_id = serializers.CharField()
    display_name = serializers.CharField()
    profile_image_url = serializers.URLField(allow_null=True)
    nickname = serializers.CharField(allow_null=True)
    lifecycle = serializers.CharField()
    connected_at = serializers.DateTimeField()


class PlayerResponseSerializer(serializers.Serializer[dict[str, Any]]):
    player = PlayerSerializer()


class ErrorResponseSerializer(serializers.Serializer[dict[str, str]]):
    detail = serializers.CharField()


class LifecycleResponseSerializer(serializers.Serializer[dict[str, str]]):
    lifecycle = serializers.CharField()


class NicknameRequestSerializer(serializers.Serializer[dict[str, str]]):
    nickname = serializers.CharField(max_length=80)


class GameEndpoint(APIView):
    permission_classes = (AllowAny,)

    @classmethod
    def as_view(cls, *args: Any, **kwargs: Any) -> Any:
        protected = csrf_protect(super().as_view(*args, **kwargs))
        # APIView.as_view marks its result csrf_exempt; remove that marker
        # after wrapping so session-backed mutations are checked by Django.
        protected.csrf_exempt = False
        return protected

    def dispatch(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        get_token(request)
        response = super().dispatch(request, *args, **kwargs)
        response["X-CSRFToken"] = get_token(request)
        return response

    def unavailable(self) -> Response:
        return _private(
            Response(
                {"detail": "The private game is unavailable."},
                status=status.HTTP_404_NOT_FOUND,
            )
        )

    def player_or_401(self, request: Any) -> Player | Response:
        player = _current_player(request)
        if player is None:
            return _private(Response({"detail": "Player authentication is required."}, status=401))
        return player


class StravaAuthorizeView(GameEndpoint):
    @extend_schema(
        responses={
            302: OpenApiResponse(description="Redirect to Strava authorization."),
            404: ErrorResponseSerializer,
        },
        tags=["game-auth"],
    )
    def get(self, request: Any) -> Response:
        if not game_is_available():
            return self.unavailable()
        if not request.session.session_key:
            request.session.create()
        session_key = request.session.session_key
        if not session_key:
            return self.unavailable()
        _, raw_state = OAuthState.issue(session_key, player=_current_player(request))
        request.session["strava_oauth_started_at"] = timezone.now().isoformat()
        request.session.save()
        return redirect(authorization_url(state=raw_state))


class StravaCallbackView(GameEndpoint):
    @extend_schema(
        responses={
            302: OpenApiResponse(description="Redirect to the game after OAuth completion."),
            404: ErrorResponseSerializer,
        },
        tags=["game-auth"],
    )
    def get(self, request: Any) -> Response:
        if not game_is_available():
            return self.unavailable()
        raw_state = str(request.GET.get("state") or "")
        if not raw_state or not request.session.session_key:
            return redirect(game_return_url("error"))
        with transaction.atomic():
            try:
                state = OAuthState.objects.select_for_update().get(
                    state_digest=OAuthState.digest(raw_state),
                    session_key=request.session.session_key,
                    used_at__isnull=True,
                    expires_at__gt=timezone.now(),
                )
            except OAuthState.DoesNotExist:
                return redirect(game_return_url("error"))
            if state.player_id is not None:
                state_player = state.player
                if (
                    state_player is None
                    or state_player.lifecycle != Player.Lifecycle.CONNECTED
                    or state.player_session_epoch != state_player.session_epoch
                ):
                    state.used_at = timezone.now()
                    state.save(update_fields=("used_at",))
                    return redirect(game_return_url("error"))
            state.used_at = timezone.now()
            state.save(update_fields=("used_at",))
        if request.GET.get("error"):
            return redirect(game_return_url("denied"))
        code = str(request.GET.get("code") or "")
        if not code:
            return redirect(game_return_url("error"))
        exchanged_payload: dict[str, Any] | None = None
        try:
            exchanged_payload = exchange_code(code)
            validate_granted_scopes(exchanged_payload)
            player = save_connection(exchanged_payload, oauth_state_id=state.pk)
        except StravaOAuthError:
            if exchanged_payload is not None:
                revoke_or_schedule(str(exchanged_payload.get("access_token") or ""))
            return redirect(game_return_url("error"))
        request.session.cycle_key()
        request.session["player_id"] = player.pk
        request.session["player_session_epoch"] = player.session_epoch
        methods = [
            method
            for method in request.session.get("account_authentication_methods", [])
            if isinstance(method, dict) and method.get("provider") != "strava"
        ]
        methods.append(
            {"method": "player", "provider": "strava", "athlete_id": str(player.strava_athlete_id)}
        )
        request.session["account_authentication_methods"] = methods
        request.session.save()
        return redirect(game_return_url("success"))


class PlayerSessionView(GameEndpoint):
    @extend_schema(
        responses={
            200: PlayerResponseSerializer,
            401: ErrorResponseSerializer,
            404: ErrorResponseSerializer,
        },
        tags=["game-auth"],
    )
    def get(self, request: Any) -> Response:
        if not game_is_available():
            return self.unavailable()
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        return _private(Response({"player": PlayerSerializer(_player_payload(player)).data}))


class PlayerLogoutView(GameEndpoint):
    @extend_schema(
        request=None,
        responses={204: OpenApiResponse(description="Player session ended.")},
        tags=["game-auth"],
    )
    def post(self, request: Any) -> Response:
        if not game_is_available():
            return self.unavailable()
        _clear_player_session(request)
        return _private(Response(status=status.HTTP_204_NO_CONTENT))


class PlayerDisconnectView(GameEndpoint):
    @extend_schema(
        request=None,
        responses={
            200: LifecycleResponseSerializer,
            401: ErrorResponseSerializer,
            404: ErrorResponseSerializer,
        },
        tags=["game-account"],
    )
    def post(self, request: Any) -> Response:
        if not game_is_available():
            return self.unavailable()
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        player = disconnect_player(player, session_key=request.session.session_key)
        _clear_player_session(request)
        return _private(Response({"lifecycle": player.lifecycle}, status=200))


class PlayerRefreshView(GameEndpoint):
    @extend_schema(
        request=None,
        responses={
            200: PlayerResponseSerializer,
            401: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
            404: ErrorResponseSerializer,
        },
        tags=["game-account"],
    )
    def post(self, request: Any) -> Response:
        if not game_is_available():
            return self.unavailable()
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        refresh_outcome = refresh_connection(player)
        if refresh_outcome == "retryable":
            return _private(
                Response(
                    {"detail": "Strava refresh is temporarily unavailable."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            )
        if refresh_outcome == "revoked":
            _clear_player_session(request)
            return _private(
                Response({"detail": "Strava connection is no longer valid."}, status=401)
            )
        return _private(Response({"player": PlayerSerializer(_player_payload(player)).data}))


class PlayerAccountView(GameEndpoint):
    @extend_schema(
        responses={
            200: PlayerResponseSerializer,
            401: ErrorResponseSerializer,
            404: ErrorResponseSerializer,
        },
        tags=["game-account"],
    )
    def get(self, request: Any) -> Response:
        if not game_is_available():
            return self.unavailable()
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        return _private(Response({"player": PlayerSerializer(_player_payload(player)).data}))

    @extend_schema(
        request=NicknameRequestSerializer,
        responses={
            200: PlayerResponseSerializer,
            400: ErrorResponseSerializer,
            401: ErrorResponseSerializer,
            404: ErrorResponseSerializer,
        },
        tags=["game-account"],
    )
    def patch(self, request: Any) -> Response:
        if not game_is_available():
            return self.unavailable()
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        nickname = request.data.get("nickname")
        if nickname is None or not isinstance(nickname, str):
            return _private(Response({"detail": "nickname must be a string."}, status=400))
        nickname = nickname.strip()
        if len(nickname) > 80:
            return _private(Response({"detail": "nickname is too long."}, status=400))
        player.nickname = nickname
        player.save(update_fields=("nickname", "updated_at"))
        return _private(Response({"player": PlayerSerializer(_player_payload(player)).data}))

    @extend_schema(
        responses={
            204: OpenApiResponse(description="Player account deleted."),
            401: ErrorResponseSerializer,
            404: ErrorResponseSerializer,
        },
        tags=["game-account"],
    )
    def delete(self, request: Any) -> Response:
        if not game_is_available():
            return self.unavailable()
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        delete_player(player, session_key=request.session.session_key)
        request.session.flush()
        return _private(Response(status=status.HTTP_204_NO_CONTENT))
