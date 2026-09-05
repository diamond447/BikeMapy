from __future__ import annotations

# django-allauth does not currently ship type stubs.
# mypy: disable-error-code="import-untyped"
from typing import Any

import pytest
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from apps.accounts.authorization import is_owner, numeric_github_id
from apps.catalogue.models import ModerationDecision, Route, SimilarityRelationship

pytestmark = pytest.mark.django_db


def _user(*, github_id: str, username: str = "owner") -> Any:
    user = get_user_model().objects.create_user(username=username, password="test-password")
    SocialAccount.objects.create(user=user, provider="github", uid=github_id)
    return user


def _mark_github_session(client: Client, github_id: str = "12345") -> None:
    session = client.session
    session["account_authentication_methods"] = [
        {"method": "socialaccount", "provider": "github", "uid": github_id}
    ]
    session.save()


def test_owner_identity_is_numeric_and_not_a_username() -> None:
    assert numeric_github_id("12345") == "12345"
    assert numeric_github_id("owner-name") is None
    assert numeric_github_id("+12345") is None

    with override_settings(GITHUB_OWNER_IDS=frozenset({"12345"})):
        user = _user(github_id="12345", username="renamed-owner")
        request = Client().get("/").wsgi_request
        request.session["account_authentication_methods"] = [
            {"method": "socialaccount", "provider": "github", "uid": "12345"}
        ]
        assert is_owner(user, request)
        user.username = "different-name"
        user.save(update_fields=["username"])
        assert is_owner(user, request)


def test_authenticated_non_owner_receives_forbidden_admin_response() -> None:
    with override_settings(GITHUB_OWNER_IDS=frozenset({"12345"})):
        user = _user(github_id="99999", username="not-owner")
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        client = Client()
        client.force_login(user)
        response = client.get("/admin/")
    assert response.status_code == 403


def test_linked_owner_cannot_use_local_password_session_for_admin() -> None:
    with override_settings(GITHUB_OWNER_IDS=frozenset({"12345"})):
        user = _user(github_id="12345")
        client = Client()
        assert client.login(username=user.username, password="test-password")
        response = client.get("/admin/")
    assert response.status_code == 403


def test_owner_can_open_admin_and_duplicate_review_screen() -> None:
    with override_settings(GITHUB_OWNER_IDS=frozenset({"12345"})):
        user = _user(github_id="12345")
        left, right = Route.objects.create(), Route.objects.create()
        relationship = SimilarityRelationship.objects.create(
            route_a=left,
            route_b=right,
            relationship_type=SimilarityRelationship.RelationshipType.SUSPECTED_DUPLICATE,
            similarity_score="0.9000",
            evidence={"algorithm": "distance-resampled", "mean_distance_m": 4},
        )
        client = Client()
        client.force_login(user)
        _mark_github_session(client)
        assert client.get("/admin/").status_code == 200
        response = client.get(f"/admin/catalogue/similarityrelationship/{relationship.pk}/review/")
    assert response.status_code == 200
    assert b"maplibre" in response.content.lower()
    assert not ModerationDecision.objects.exists()


def test_admin_review_requires_all_exact_criteria_and_a_reason() -> None:
    with override_settings(GITHUB_OWNER_IDS=frozenset({"12345"})):
        user = _user(github_id="12345")
        route = Route.objects.create()
        client = Client()
        client.force_login(user)
        _mark_github_session(client)
        url = f"/admin/catalogue/route/{route.pk}/review/"
        rejected = client.post(url, {"reason": "partial", "technical_validity": "on"})
        assert rejected.status_code == 200
        assert not ModerationDecision.objects.exists()
        accepted = client.post(
            url,
            {
                "reason": "Checked all three criteria",
                "technical_validity": "on",
                "source_context": "on",
                "content_suitability": "on",
            },
        )
    assert accepted.status_code == 302
    decision = ModerationDecision.objects.get(action=ModerationDecision.Action.REVIEW)
    assert decision.actor_id == user.pk
    assert decision.metadata["before"]["reviewed_at"] is None
    assert decision.metadata["technical_validity"] is True


def test_admin_duplicate_action_validates_target_and_audits() -> None:
    with override_settings(GITHUB_OWNER_IDS=frozenset({"12345"})):
        user = _user(github_id="12345")
        left, right = Route.objects.create(), Route.objects.create()
        relationship = SimilarityRelationship.objects.create(
            route_a=left,
            route_b=right,
            relationship_type=SimilarityRelationship.RelationshipType.SUSPECTED_DUPLICATE,
            evidence={"score": 0.9},
        )
        client = Client()
        client.force_login(user)
        _mark_github_session(client)
        url = f"/admin/catalogue/similarityrelationship/{relationship.pk}/review/"
        invalid = client.post(
            url, {"action": "quarantine", "reason": "bad", "target_route": "other"}
        )
        assert invalid.status_code == 200
        assert left.lifecycle == "published"
        accepted = client.post(
            url,
            {
                "action": "quarantine",
                "reason": "Duplicate confirmed",
                "target_route": str(right.pk),
            },
        )
    assert accepted.status_code == 302
    decision = ModerationDecision.objects.get(action=ModerationDecision.Action.QUARANTINE)
    assert decision.actor_id == user.pk
    assert decision.metadata["before"]["lifecycle"] == "published"
    assert decision.metadata["after"]["lifecycle"] == "quarantined"
