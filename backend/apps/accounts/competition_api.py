"""Private competition endpoints behind the authenticated game session."""

# djangorestframework currently does not ship type stubs.
# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from typing import Any
from uuid import UUID

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response

from .competition_services import (
    CompetitionError,
    create_competition,
    delete_competition,
    join_competition,
    leave_competition,
    remove_member,
    rename_competition,
    rotate_invite_code,
    set_member_color,
    switch_competition,
    transfer_ownership,
)
from .game_api import GameEndpoint, _private
from .models import Competition, CompetitionMembership, Player
from .services import game_is_available


class CompetitionMemberSerializer(serializers.Serializer[dict[str, Any]]):
    player_id = serializers.IntegerField()
    display_name = serializers.CharField()
    nickname = serializers.CharField(allow_null=True)
    color = serializers.CharField()
    is_owner = serializers.BooleanField()


class CompetitionSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField()
    name = serializers.CharField()
    invite_code = serializers.CharField()
    owner_player_id = serializers.IntegerField()
    is_owner = serializers.BooleanField()
    is_active = serializers.BooleanField()
    is_selected = serializers.BooleanField()
    color = serializers.CharField()
    created_at = serializers.DateTimeField()
    members = CompetitionMemberSerializer(many=True)


class CompetitionsResponseSerializer(serializers.Serializer[dict[str, Any]]):
    competitions = CompetitionSerializer(many=True)
    active_competition_id = serializers.UUIDField(allow_null=True)


class CompetitionResponseSerializer(serializers.Serializer[dict[str, Any]]):
    competition = CompetitionSerializer()


class CompetitionCreateSerializer(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(max_length=120)
    color = serializers.CharField(max_length=7, required=False)


class CompetitionJoinSerializer(serializers.Serializer[dict[str, Any]]):
    invite_code = serializers.CharField(max_length=32)
    color = serializers.CharField(max_length=7, required=False)


class CompetitionRenameSerializer(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(max_length=120)


class CompetitionColorSerializer(serializers.Serializer[dict[str, Any]]):
    color = serializers.CharField(max_length=7)


class CompetitionTransferSerializer(serializers.Serializer[dict[str, Any]]):
    player_id = serializers.IntegerField(min_value=1)


class CompetitionErrorResponseSerializer(serializers.Serializer[dict[str, Any]]):
    detail = serializers.CharField()
    code = serializers.CharField(required=False)
    fields = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()), required=False
    )


COMPETITION_ERROR_RESPONSES = {
    400: CompetitionErrorResponseSerializer,
    401: CompetitionErrorResponseSerializer,
    403: CompetitionErrorResponseSerializer,
    404: CompetitionErrorResponseSerializer,
    409: CompetitionErrorResponseSerializer,
}


@extend_schema(auth=[{"cookieAuth": []}])  # type: ignore[list-item]
class CompetitionApi(GameEndpoint):
    def dispatch(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        return _private(super().dispatch(request, *args, **kwargs))

    def handle_exception(self, exc: Exception) -> Response:
        response = super().handle_exception(exc)
        if response.status_code == 400:
            response.data = {
                "detail": "Request validation failed.",
                "code": "validation_error",
                "fields": response.data,
            }
        return _private(response)

    def _enabled(self) -> Response | None:
        return None if game_is_available() else self.unavailable()

    @staticmethod
    def _error(error: CompetitionError) -> Response:
        if error.code == "not_found":
            return Response({"detail": "Competition not found."}, status=404)
        code = (
            403
            if error.code == "owner_required"
            else 409
            if error.code in {"already_member", "owner_cannot_leave", "color_not_distinguishable"}
            else 400
        )
        return Response({"detail": error.detail, "code": error.code}, status=code)

    @staticmethod
    def _competition_or_none(identifier: UUID) -> Competition | None:
        try:
            return Competition.objects.get(pk=identifier)
        except Competition.DoesNotExist:
            return None

    @staticmethod
    def _payload(competition: Competition, player: Player) -> dict[str, Any]:
        membership = CompetitionMembership.objects.get(competition=competition, player=player)
        members = [
            {
                "player_id": member.player_id,
                "display_name": member.player.strava_display_name,
                "nickname": member.player.nickname or None,
                "color": member.color,
                "is_owner": member.player_id == competition.owner_id,
            }
            for member in competition.memberships.select_related("player").order_by(
                "joined_at", "pk"
            )
        ]
        return {
            "id": competition.pk,
            "name": competition.name,
            "invite_code": competition.invite_code,
            "owner_player_id": competition.owner_id,
            "is_owner": competition.owner_id == player.pk,
            "is_active": competition.is_active,
            "is_selected": player.active_competition_id == competition.pk,
            "color": membership.color,
            "created_at": competition.created_at,
            "members": members,
        }

    def _response(self, competition: Competition, player: Player, *, code: int = 200) -> Response:
        return Response({"competition": self._payload(competition, player)}, status=code)


class CompetitionListView(CompetitionApi):
    @extend_schema(
        operation_id="game_competition_list",
        responses={**COMPETITION_ERROR_RESPONSES, 200: CompetitionsResponseSerializer},
        tags=["game-competitions"],
    )
    def get(self, request: Any) -> Response:
        if (response := self._enabled()) is not None:
            return response
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        competitions = [
            self._payload(membership.competition, player)
            for membership in player.competition_memberships.select_related("competition").all()
        ]
        return Response(
            {"competitions": competitions, "active_competition_id": player.active_competition_id},
        )

    @extend_schema(
        request=CompetitionCreateSerializer,
        responses={**COMPETITION_ERROR_RESPONSES, 201: CompetitionResponseSerializer},
        tags=["game-competitions"],
    )
    def post(self, request: Any) -> Response:
        if (response := self._enabled()) is not None:
            return response
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        serializer = CompetitionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            competition, _ = create_competition(player, **serializer.validated_data)
        except CompetitionError as error:
            return self._error(error)
        return self._response(competition, player, code=status.HTTP_201_CREATED)


class CompetitionJoinView(CompetitionApi):
    @extend_schema(
        request=CompetitionJoinSerializer,
        responses={**COMPETITION_ERROR_RESPONSES, 200: CompetitionResponseSerializer},
        tags=["game-competitions"],
    )
    def post(self, request: Any) -> Response:
        if (response := self._enabled()) is not None:
            return response
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        serializer = CompetitionJoinSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            competition, _ = join_competition(player, **serializer.validated_data)
        except CompetitionError as error:
            return self._error(error)
        return self._response(competition, player)


class CompetitionDetailView(CompetitionApi):
    def _get(self, request: Any, identifier: UUID) -> tuple[Player | Response, Competition | None]:
        if (response := self._enabled()) is not None:
            return response, None
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player, None
        return player, self._competition_or_none(identifier)

    @extend_schema(
        request=CompetitionRenameSerializer,
        responses={**COMPETITION_ERROR_RESPONSES, 200: CompetitionResponseSerializer},
        tags=["game-competitions"],
    )
    def patch(self, request: Any, competition_id: UUID) -> Response:
        player, competition = self._get(request, competition_id)
        if isinstance(player, Response):
            return player
        if competition is None:
            return Response({"detail": "Competition not found."}, status=404)
        serializer = CompetitionRenameSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            competition = rename_competition(player, competition, **serializer.validated_data)
        except CompetitionError as error:
            return self._error(error)
        return self._response(competition, player)

    @extend_schema(
        operation_id="game_competition_detail",
        responses={**COMPETITION_ERROR_RESPONSES, 200: CompetitionResponseSerializer},
        tags=["game-competitions"],
    )
    def get(self, request: Any, competition_id: UUID) -> Response:
        player, competition = self._get(request, competition_id)
        if isinstance(player, Response):
            return player
        if competition is None:
            return Response({"detail": "Competition not found."}, status=404)
        try:
            # Resolving membership through the domain helper makes a guessed
            # UUID indistinguishable from an object the caller cannot see.
            from .competition_services import _membership

            _membership(player, competition)
        except CompetitionError as error:
            return self._error(error)
        return self._response(competition, player)

    @extend_schema(
        responses={
            **COMPETITION_ERROR_RESPONSES,
            204: OpenApiResponse(description="Competition deleted."),
        },
        tags=["game-competitions"],
    )
    def delete(self, request: Any, competition_id: UUID) -> Response:
        player, competition = self._get(request, competition_id)
        if isinstance(player, Response):
            return player
        if competition is None:
            return Response({"detail": "Competition not found."}, status=404)
        try:
            delete_competition(player, competition)
        except CompetitionError as error:
            return self._error(error)
        return Response(status=204)


class CompetitionSwitchView(CompetitionApi):
    @extend_schema(
        request=None,
        responses={**COMPETITION_ERROR_RESPONSES, 200: CompetitionResponseSerializer},
        tags=["game-competitions"],
    )
    def post(self, request: Any, competition_id: UUID) -> Response:
        if (response := self._enabled()) is not None:
            return response
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        competition = self._competition_or_none(competition_id)
        if competition is None:
            return Response({"detail": "Competition not found."}, status=404)
        try:
            switch_competition(player, competition)
        except CompetitionError as error:
            return self._error(error)
        return self._response(competition, player)


class CompetitionRotateInviteView(CompetitionApi):
    @extend_schema(
        request=None,
        responses={**COMPETITION_ERROR_RESPONSES, 200: CompetitionResponseSerializer},
        tags=["game-competitions"],
    )
    def post(self, request: Any, competition_id: UUID) -> Response:
        if (response := self._enabled()) is not None:
            return response
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        competition = self._competition_or_none(competition_id)
        if competition is None:
            return Response({"detail": "Competition not found."}, status=404)
        try:
            competition = rotate_invite_code(player, competition)
        except CompetitionError as error:
            return self._error(error)
        return self._response(competition, player)


class CompetitionLeaveView(CompetitionApi):
    @extend_schema(
        request=None,
        responses={
            **COMPETITION_ERROR_RESPONSES,
            204: OpenApiResponse(description="Membership removed."),
        },
        tags=["game-competitions"],
    )
    def post(self, request: Any, competition_id: UUID) -> Response:
        if (response := self._enabled()) is not None:
            return response
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        competition = self._competition_or_none(competition_id)
        if competition is None:
            return Response({"detail": "Competition not found."}, status=404)
        try:
            leave_competition(player, competition)
        except CompetitionError as error:
            return self._error(error)
        return Response(status=204)


class CompetitionMemberColorView(CompetitionApi):
    @extend_schema(
        request=CompetitionColorSerializer,
        responses={**COMPETITION_ERROR_RESPONSES, 200: CompetitionResponseSerializer},
        tags=["game-competitions"],
    )
    def patch(self, request: Any, competition_id: UUID) -> Response:
        if (response := self._enabled()) is not None:
            return response
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        competition = self._competition_or_none(competition_id)
        if competition is None:
            return Response({"detail": "Competition not found."}, status=404)
        serializer = CompetitionColorSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            set_member_color(player, competition, **serializer.validated_data)
        except CompetitionError as error:
            return self._error(error)
        return self._response(competition, player)


class CompetitionRemoveMemberView(CompetitionApi):
    @extend_schema(
        responses={
            **COMPETITION_ERROR_RESPONSES,
            204: OpenApiResponse(description="Member removed."),
        },
        tags=["game-competitions"],
    )
    def delete(self, request: Any, competition_id: UUID, player_id: int) -> Response:
        if (response := self._enabled()) is not None:
            return response
        owner = self.player_or_401(request)
        if isinstance(owner, Response):
            return owner
        competition = self._competition_or_none(competition_id)
        try:
            member = Player.objects.get(pk=player_id)
        except Player.DoesNotExist:
            member = None
        if competition is None or member is None:
            return Response({"detail": "Competition not found."}, status=404)
        try:
            remove_member(owner, competition, member)
        except CompetitionError as error:
            return self._error(error)
        return Response(status=204)


class CompetitionTransferView(CompetitionApi):
    @extend_schema(
        request=CompetitionTransferSerializer,
        responses={**COMPETITION_ERROR_RESPONSES, 200: CompetitionResponseSerializer},
        tags=["game-competitions"],
    )
    def post(self, request: Any, competition_id: UUID) -> Response:
        if (response := self._enabled()) is not None:
            return response
        owner = self.player_or_401(request)
        if isinstance(owner, Response):
            return owner
        competition = self._competition_or_none(competition_id)
        if competition is None:
            return Response({"detail": "Competition not found."}, status=404)
        serializer = CompetitionTransferSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            new_owner = Player.objects.get(pk=serializer.validated_data["player_id"])
            competition = transfer_ownership(owner, competition, new_owner)
        except Player.DoesNotExist:
            return Response({"detail": "Competition not found."}, status=404)
        except CompetitionError as error:
            return self._error(error)
        return self._response(competition, owner)
