"""Session-backed local account and direct upload endpoints."""

from __future__ import annotations

from typing import Any

from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_protect
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from apps.api.throttling import PlayerSessionThrottle

from .account_services import (
    AccountError,
    authenticate_player,
    create_invited_account,
    confirm_password_reset,
    request_password_reset,
    set_password,
    validate_invite_code,
)
from .activity_services import remove_activity
from .game_api import _private
from .models import ActivityUploadBatch, Player
from .upload_services import UploadError, create_batch
from .upload_tasks import process_activity_upload_batch_task


class AccountThrottle(AnonRateThrottle):
    scope = "player_accounts"


class AccountInput(serializers.Serializer[dict[str, Any]]):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField(max_length=254)
    invite_code = serializers.CharField(max_length=64)


class LoginInput(serializers.Serializer[dict[str, Any]]):
    identifier = serializers.CharField(max_length=254)
    password = serializers.CharField(max_length=256, trim_whitespace=False)


class PasswordInput(serializers.Serializer[dict[str, Any]]):
    password = serializers.CharField(max_length=256, trim_whitespace=False)


class ResetInput(serializers.Serializer[dict[str, Any]]):
    email = serializers.EmailField(max_length=254)


class InviteInput(serializers.Serializer[dict[str, Any]]):
    invite_code = serializers.CharField(max_length=64)


class AccountEndpoint(APIView):
    throttle_classes = (AccountThrottle,)

    @classmethod
    def as_view(cls, *args: Any, **kwargs: Any) -> Any:
        protected = csrf_protect(super().as_view(*args, **kwargs))
        protected.csrf_exempt = False
        return protected

    def dispatch(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        get_token(request)
        response = super().dispatch(request, *args, **kwargs)
        response["X-CSRFToken"] = get_token(request)
        return response


def _session_player(request: Any) -> Player | None:
    player_id = request.session.get("player_id")
    if not player_id:
        return None
    return Player.objects.filter(pk=player_id, lifecycle=Player.Lifecycle.CONNECTED).first()


def _account_payload(player: Player, *, authenticated: bool = True) -> dict[str, Any]:
    return {
        "player_id": str(player.pk),
        "username": player.user.username,
        "email": player.user.email,
        "must_change_password": player.must_change_password,
        "authenticated": authenticated,
    }


class AccountOnboardingView(AccountEndpoint):
    throttle_classes = (AccountThrottle,)

    @extend_schema(
        request=AccountInput,
        responses={
            201: OpenApiResponse(description="Account created."),
            400: OpenApiResponse(description="Invalid request."),
        },
        tags=["account-auth"],
    )
    def post(self, request: Any) -> Response:
        data = AccountInput(data=request.data)
        if not data.is_valid():
            return _private(Response({"detail": "Unable to create this account."}, status=400))
        try:
            player, _ = create_invited_account(**data.validated_data)
        except AccountError:
            return _private(Response({"detail": "Unable to create this account."}, status=400))
        return _private(Response(_account_payload(player, authenticated=False), status=201))


class AccountLoginView(AccountEndpoint):
    @extend_schema(
        request=LoginInput,
        responses={
            200: OpenApiResponse(description="Signed in."),
            401: OpenApiResponse(description="Invalid credentials."),
        },
        tags=["account-auth"],
    )
    def post(self, request: Any) -> Response:
        data = LoginInput(data=request.data)
        if not data.is_valid():
            return _private(
                Response({"detail": "Invalid username, email, or password."}, status=401)
            )
        player = authenticate_player(**data.validated_data)
        if player is None:
            return _private(
                Response({"detail": "Invalid username, email, or password."}, status=401)
            )
        request.session.cycle_key()
        request.session["player_id"] = player.pk
        request.session["player_session_epoch"] = player.session_epoch
        request.session.save()
        return _private(Response(_account_payload(player)))


class GitHubOnboardingInviteView(AccountEndpoint):
    """Store a validated invite in the session before first-time OAuth."""

    @extend_schema(
        request=InviteInput,
        responses={200: OpenApiResponse(description="OAuth URL stored.")},
        tags=["account-auth"],
    )
    def post(self, request: Any) -> Response:
        code = str(request.data.get("invite_code") or "").strip().upper()
        try:
            validate_invite_code(code)
        except AccountError:
            return _private(Response({"detail": "The invite code is not valid."}, status=400))
        request.session["account_invite_code"] = code
        request.session.save()
        return _private(Response({"url": "/accounts/github/login/"}))


class AccountLogoutView(AccountEndpoint):
    @extend_schema(
        request=None,
        responses={204: OpenApiResponse(description="Signed out.")},
        tags=["account-auth"],
    )
    def post(self, request: Any) -> Response:
        request.session.pop("player_id", None)
        request.session.pop("player_session_epoch", None)
        request.session.save()
        return _private(Response(status=204))


class PasswordChangeView(AccountEndpoint):
    throttle_classes = (PlayerSessionThrottle,)

    @extend_schema(
        request=PasswordInput,
        responses={200: OpenApiResponse(description="Password changed.")},
        tags=["account-auth"],
    )
    def post(self, request: Any) -> Response:
        player = _session_player(request)
        if player is None:
            return _private(Response({"detail": "Authentication is required."}, status=401))
        data = PasswordInput(data=request.data)
        if not data.is_valid():
            return _private(Response({"detail": "A new password is required."}, status=400))
        try:
            set_password(player, data.validated_data["password"])
        except AccountError as exc:
            return _private(Response({"detail": exc.detail, "code": exc.code}, status=400))
        return _private(Response({"must_change_password": False}))


class PasswordResetRequestView(AccountEndpoint):
    @extend_schema(
        request=ResetInput,
        responses={200: OpenApiResponse(description="Reset request accepted.")},
        tags=["account-auth"],
    )
    def post(self, request: Any) -> Response:
        data = ResetInput(data=request.data)
        if data.is_valid():
            request_password_reset(data.validated_data["email"])
        # Always return the same response and timing envelope.
        return _private(
            Response({"detail": "If the account exists, reset instructions were sent."})
        )


class PasswordResetConfirmView(AccountEndpoint):
    @extend_schema(
        request=PasswordInput,
        responses={200: OpenApiResponse(description="Password reset.")},
        tags=["account-auth"],
        operation_id="game_local_reset_confirm",
        parameters=[
            OpenApiParameter("uidb64", str, OpenApiParameter.PATH),
            OpenApiParameter("token", str, OpenApiParameter.PATH),
        ],
    )
    def post(self, request: Any, uidb64: str, token: str) -> Response:
        data = PasswordInput(data=request.data)
        if not data.is_valid():
            return _private(Response({"detail": "A new password is required."}, status=400))
        try:
            confirm_password_reset(
                uidb64=uidb64, token=token, password=data.validated_data["password"]
            )
        except AccountError:
            return _private(
                Response({"detail": "The reset link is invalid or expired."}, status=400)
            )
        return _private(Response({"detail": "Password reset."}))


class GitHubLinkView(AccountEndpoint):
    @extend_schema(
        responses={200: OpenApiResponse(description="Explicit linking URL.")}, tags=["account-auth"]
    )
    def get(self, request: Any) -> Response:
        player = _session_player(request)
        if player is None:
            return _private(Response({"detail": "Authentication is required."}, status=401))
        return _private(Response({"url": "/accounts/github/login/?process=connect"}))


class UploadInput(serializers.Serializer[dict[str, Any]]):
    attested = serializers.BooleanField(required=True)


class ActivityUploadView(AccountEndpoint):
    throttle_classes = (PlayerSessionThrottle,)

    @extend_schema(
        request=UploadInput,
        responses={202: OpenApiResponse(description="Batch queued.")},
        tags=["game-activities"],
    )
    def post(self, request: Any) -> Response:
        player = _session_player(request)
        if player is None:
            return _private(Response({"detail": "Authentication is required."}, status=401))
        data = UploadInput(data=request.data)
        if not data.is_valid():
            return _private(
                Response({"detail": "A data ownership attestation is required."}, status=400)
            )
        files = [(item.name, item.read()) for item in request.FILES.getlist("files")]
        try:
            batch = create_batch(player, files, attested=data.validated_data["attested"])
        except UploadError as exc:
            return _private(Response({"detail": exc.detail, "code": exc.code}, status=400))
        process_activity_upload_batch_task.apply_async(args=(str(batch.pk),))
        return _private(Response({"batch_id": str(batch.pk), "status": batch.status}, status=202))


def _batch_payload(batch: ActivityUploadBatch) -> dict[str, Any]:
    return {
        "batch_id": str(batch.pk),
        "status": batch.status,
        "total_files": batch.total_files,
        "processed_files": batch.processed_files,
        "accepted_files": batch.accepted_files,
        "duplicate_files": batch.duplicate_files,
        "failed_files": batch.failed_files,
        "files": [
            {"name": item.original_name, "status": item.status, "error_code": item.error_code}
            for item in batch.files.order_by("pk")
        ],
    }


class ActivityUploadBatchView(AccountEndpoint):
    throttle_classes = (PlayerSessionThrottle,)

    @extend_schema(
        responses={200: OpenApiResponse(description="Batch progress.")}, tags=["game-activities"]
    )
    def get(self, request: Any, batch_id: Any) -> Response:
        player = _session_player(request)
        if player is None:
            return _private(Response({"detail": "Authentication is required."}, status=401))
        batch = ActivityUploadBatch.objects.filter(pk=batch_id, player=player).first()
        if batch is None:
            return _private(Response({"detail": "Upload batch not found."}, status=404))
        return _private(Response(_batch_payload(batch)))


class UploadedActivityDeleteView(AccountEndpoint):
    throttle_classes = (PlayerSessionThrottle,)

    @extend_schema(
        responses={204: OpenApiResponse(description="Activity deleted.")}, tags=["game-activities"]
    )
    def delete(self, request: Any, activity_id: Any) -> Response:
        player = _session_player(request)
        if player is None:
            return _private(Response({"detail": "Authentication is required."}, status=401))
        activity = player.imported_activities.filter(pk=activity_id).first()
        if activity is None:
            return _private(Response({"detail": "Activity not found."}, status=404))
        remove_activity(player, activity.provider_activity_id, reason="player-request")
        return _private(Response(status=204))
