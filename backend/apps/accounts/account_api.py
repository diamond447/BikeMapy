"""Session-backed local account and direct upload endpoints."""

# djangorestframework does not currently ship type stubs.
# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth import login as auth_login
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_protect
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.api.throttling import PlayerSessionThrottle, TrustedClientThrottleMixin
from config.client_identity import rate_limit_identifier

from .account_services import (
    AccountError,
    authenticate_player,
    confirm_password_reset,
    create_invited_account,
    protect_reset_email,
    set_password,
    validate_invite_code,
)
from .account_tasks import send_password_reset_email_task
from .activity_services import remove_activity
from .game_api import _logout_player_session, _private
from .models import ActivityUploadBatch, Player
from .services import player_accounts_is_available
from .upload_services import (
    UploadError,
    create_batch,
)
from .upload_tasks import process_activity_upload_batch_task

logger = logging.getLogger(__name__)


class AccountThrottle(TrustedClientThrottleMixin, SimpleRateThrottle):
    scope = "player_accounts"

    def get_cache_key(self, request: Any, view: Any) -> str | None:
        del view
        player_id = request.session.get("player_id")
        epoch = request.session.get("player_session_epoch")
        if player_id and epoch is not None:
            ident = rate_limit_identifier(f"player-session:{player_id}:{epoch}")
        else:
            user = getattr(request, "user", None)
            if user is not None and getattr(user, "is_authenticated", False):
                ident = rate_limit_identifier(f"django-user:{user.pk}")
            else:
                ident = self.get_ident(request)
        return str(self.cache_format % {"scope": self.scope, "ident": ident}) if ident else None


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


class ActivityUploadResultSerializer(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField()
    status = serializers.CharField()
    error_code = serializers.CharField(allow_blank=True)
    error_detail = serializers.CharField(allow_blank=True)
    activity_id = serializers.CharField(allow_null=True)


class ActivityUploadBatchSerializer(serializers.Serializer[dict[str, Any]]):
    batch_id = serializers.UUIDField()
    status = serializers.CharField()
    total_files = serializers.IntegerField()
    processed_files = serializers.IntegerField()
    accepted_files = serializers.IntegerField()
    duplicate_files = serializers.IntegerField()
    failed_files = serializers.IntegerField()
    files = ActivityUploadResultSerializer(many=True)


class AccountEndpoint(APIView):
    throttle_classes = (AccountThrottle,)

    @classmethod
    def as_view(cls, *args: Any, **kwargs: Any) -> Any:
        protected = csrf_protect(super().as_view(*args, **kwargs))
        protected.csrf_exempt = False
        return protected

    def dispatch(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        get_token(request)
        if not player_accounts_is_available():
            return _private(Response({"detail": "Player accounts are unavailable."}, status=404))
        response = super().dispatch(request, *args, **kwargs)
        response["X-CSRFToken"] = get_token(request)
        return response


def _session_player(request: Any, *, allow_password_change: bool = False) -> Player | None:
    player_id = request.session.get("player_id")
    if not player_id:
        return None
    epoch = request.session.get("player_session_epoch")
    if epoch is None:
        return None
    player = Player.objects.filter(pk=player_id, lifecycle=Player.Lifecycle.CONNECTED).first()
    if player is None or player.session_epoch != epoch:
        return None
    if player.must_change_password and not allow_password_change:
        return None
    return player


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
        except Exception as exc:
            # The service transaction has already rolled back the account and
            # redemption when delivery fails; keep transport details private.
            logger.error(
                "temporary password email delivery failed (error_type=%s)",
                type(exc).__name__,
                extra={"event": "account_email_delivery_failed", "error_type": type(exc).__name__},
            )
            return _private(Response({"detail": "Unable to create this account."}, status=503))
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
        return _private(Response({"url": request.build_absolute_uri("/accounts/github/login/")}))


class AccountLogoutView(AccountEndpoint):
    @extend_schema(
        request=None,
        responses={204: OpenApiResponse(description="Signed out.")},
        tags=["account-auth"],
    )
    def post(self, request: Any) -> Response:
        _logout_player_session(request)
        return _private(Response(status=204))


class PasswordChangeView(AccountEndpoint):
    throttle_classes = (AccountThrottle,)

    @extend_schema(
        request=PasswordInput,
        responses={200: OpenApiResponse(description="Password changed.")},
        tags=["account-auth"],
    )
    def post(self, request: Any) -> Response:
        player = _session_player(request, allow_password_change=True)
        if player is None:
            return _private(Response({"detail": "Authentication is required."}, status=401))
        data = PasswordInput(data=request.data)
        if not data.is_valid():
            return _private(Response({"detail": "A new password is required."}, status=400))
        try:
            set_password(player, data.validated_data["password"])
        except AccountError as exc:
            return _private(Response({"detail": exc.detail, "code": exc.code}, status=400))
        request.session["player_session_epoch"] = player.session_epoch
        request.session.save()
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
            encrypted_email = protect_reset_email(data.validated_data["email"])
            try:
                send_password_reset_email_task.apply_async(args=(encrypted_email,))
            except Exception as exc:
                # Every valid address follows the same broker path and public response.
                logger.error(
                    "password reset email enqueue failed (error_type=%s)",
                    type(exc).__name__,
                    extra={
                        "event": "account_email_enqueue_failed",
                        "error_type": type(exc).__name__,
                    },
                )
        # Both paths return the same generic response; mail delivery runs in Celery.
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
        request=None,
        responses={200: OpenApiResponse(description="Explicit linking URL.")},
        tags=["account-auth"],
    )
    def post(self, request: Any) -> Response:
        player = _session_player(request)
        if player is None:
            return _private(Response({"detail": "Authentication is required."}, status=401))
        # allauth's connect flow is bound to this exact local session/player;
        # a random intent prevents a copied callback URL from being reused.
        import secrets

        request.session["github_link_intent"] = secrets.token_urlsafe(32)
        request.session["github_link_player_id"] = str(player.pk)
        request.session["github_link_epoch"] = player.session_epoch
        auth_login(
            request,
            player.user,
            backend="allauth.account.auth_backends.AuthenticationBackend",
        )
        request.session.save()
        return _private(
            Response({"url": request.build_absolute_uri("/accounts/github/login/?process=connect")})
        )


class UploadInput(serializers.Serializer[dict[str, Any]]):
    attested = serializers.BooleanField(required=True)
    files = serializers.ListField(child=serializers.FileField(), required=True, allow_empty=False)


class ActivityUploadView(AccountEndpoint):
    throttle_classes = (PlayerSessionThrottle,)
    parser_classes = (MultiPartParser,)

    @extend_schema(
        request=UploadInput,
        responses={202: ActivityUploadBatchSerializer},
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
        files: list[tuple[str, Any]] = []
        for item in request.FILES.getlist("files"):
            files.append((item.name, item))
        try:
            batch = create_batch(player, files, attested=data.validated_data["attested"])
        except UploadError as exc:
            return _private(Response({"detail": exc.detail, "code": exc.code}, status=400))
        process_activity_upload_batch_task.apply_async(args=(str(batch.pk),))
        return _private(Response(_batch_payload(batch), status=202))


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
            {
                "name": item.original_name,
                "status": item.status,
                "error_code": item.error_code,
                "error_detail": item.error_detail,
                "activity_id": str(item.activity_id) if item.activity_id else None,
            }
            for item in batch.files.order_by("pk")
        ],
    }


class ActivityUploadBatchView(AccountEndpoint):
    throttle_classes = (PlayerSessionThrottle,)

    @extend_schema(responses={200: ActivityUploadBatchSerializer}, tags=["game-activities"])
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
