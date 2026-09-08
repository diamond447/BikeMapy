from __future__ import annotations

# django-allauth does not currently ship type stubs.
# mypy: disable-error-code="import-untyped"
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from allauth.account.internal.flows.login import record_authentication
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, override_settings

from apps.accounts.adapters import OwnerSocialAccountAdapter
from apps.accounts.admin import owner_admin_site
from apps.accounts.authorization import is_owner
from apps.catalogue.models import (
    Category,
    ModerationDecision,
    Route,
    RouteCategory,
    RouteSource,
    RouteVersion,
    SimilarityRelationship,
    SourceDenylistEntry,
    TitleProvenance,
)
from apps.catalogue.services import (
    assign_route_category,
    create_category,
    delete_category,
    keep_both_routes,
    mark_source_unavailable,
    remove_route_category,
    restore_denylist_entry,
    update_category,
    update_route_metadata,
)

pytestmark = pytest.mark.django_db


def owner(github_id: str = "12345") -> Any:
    user = get_user_model().objects.create_user(username=f"user-{github_id}", password="secret")
    SocialAccount.objects.create(user=user, provider="github", uid=github_id)
    return user


def oauth_session(client: Client, uid: str = "12345") -> None:
    session = client.session
    record_authentication(
        type("Request", (), {"session": session})(),
        None,
        "socialaccount",
        provider="github",
        uid=uid,
    )
    session.save()


def test_allauth_adapter_staff_alignment_uses_provider_uid() -> None:
    user = owner()
    request = Client().get("/").wsgi_request
    login = SimpleNamespace(user=user, account=SimpleNamespace(provider="github", uid="12345"))
    with override_settings(GITHUB_OWNER_IDS=frozenset({"12345"})):
        adapter = OwnerSocialAccountAdapter()
        user.is_staff = False
        adapter.pre_social_login(request, login)
        assert user.is_staff
        user.is_staff = False
        with patch(
            "allauth.socialaccount.adapter.DefaultSocialAccountAdapter.save_user",
            return_value=user,
        ):
            adapter.save_user(request, login)
        assert user.is_staff
        login.account.uid = "not-a-number"
        adapter.pre_social_login(request, login)
        assert not user.is_staff


def test_allowlist_requires_current_github_record_and_rejects_flags() -> None:
    user = owner()
    request = Client().get("/").wsgi_request
    request.session["account_authentication_methods"] = [
        {"method": "socialaccount", "provider": "github", "uid": "12345"}
    ]
    with override_settings(GITHUB_OWNER_IDS=frozenset()):
        assert not is_owner(user, request)
    with override_settings(GITHUB_OWNER_IDS=frozenset({"username", "-1", "0"})):
        assert not is_owner(user, request)
    user.is_active = False
    user.is_superuser = True
    user.save(update_fields=["is_active", "is_superuser"])
    with override_settings(GITHUB_OWNER_IDS=frozenset({"12345"})):
        assert not is_owner(user, request)


def test_actual_allauth_record_authentication_allows_owner_only() -> None:
    user = owner()
    request = Client().get("/").wsgi_request
    with override_settings(GITHUB_OWNER_IDS=frozenset({"12345"})):
        record_authentication(request, user, "password", username=user.username)
        assert not is_owner(user, request)
        record_authentication(request, user, "socialaccount", provider="github", uid="12345")
        assert is_owner(user, request)


def test_non_owner_cannot_reach_any_custom_admin_endpoint() -> None:
    user = owner("99999")
    route, other = Route.objects.create(), Route.objects.create()
    category = Category.objects.create(name="Gravel", slug="gravel")
    source = RouteSource.objects.create(route=route, mapy_url="https://mapy.com/s/admin-test")
    version = RouteVersion.objects.create(source=source, version_number=1, checksum="admin-test")
    relationship = SimilarityRelationship.objects.create(
        route_a=route,
        route_b=other,
        relationship_type="suspected_duplicate",
    )
    entry = SourceDenylistEntry.objects.create(
        source_url=source.mapy_url, route_source=source, reason="test"
    )
    client = Client()
    client.force_login(user)
    urls = [
        "/admin/",
        f"/admin/catalogue/route/{route.pk}/review/",
        f"/admin/catalogue/route/{route.pk}/metadata/",
        f"/admin/catalogue/route/{route.pk}/soft-remove/",
        f"/admin/catalogue/routesource/{source.pk}/mark-unavailable/",
        f"/admin/catalogue/routeversion/{version.pk}/select/",
        f"/admin/catalogue/sourcedenylistentry/{entry.pk}/restore/",
        f"/admin/catalogue/similarityrelationship/{relationship.pk}/review/",
    ]
    assert all(client.get(url).status_code == 403 for url in urls)
    assert category.pk and owner_admin_site._registry


def test_workflow_models_are_not_directly_mutable_or_deletable() -> None:
    user = owner()
    request = Client().get("/").wsgi_request
    request.user = user
    oauth_session(Client(), "12345")
    for model in (
        RouteSource,
        RouteVersion,
        RouteCategory,
        SimilarityRelationship,
        ModerationDecision,
    ):
        model_admin = owner_admin_site._registry[model]
        assert not model_admin.has_add_permission(request)
        assert not model_admin.has_change_permission(request)
        assert not model_admin.has_delete_permission(request)


def test_category_validation_audit_and_rollback() -> None:
    user = owner()
    with pytest.raises(ValidationError):
        create_category(name="", slug="", reason="bad", actor=user)
    assert not ModerationDecision.objects.exists()
    category = create_category(name=" Gravel ", slug="gravel", reason="curate", actor=user)
    created = ModerationDecision.objects.get(action="category_create")
    assert created.actor_id == user.pk
    assert created.metadata["before"]["exists"] is False
    with pytest.raises(ValidationError):
        create_category(name="Other", slug="gravel", reason="duplicate", actor=user)
    assert Category.objects.count() == 1
    update_category(category, name="Road", slug="road", reason="rename", actor=user)
    updated = ModerationDecision.objects.get(action="category_update")
    assert updated.metadata["before"]["slug"] == "gravel"
    assert updated.metadata["after"]["slug"] == "road"
    delete_category(category, reason="retire", actor=user)
    assert not Category.objects.exists()
    assert ModerationDecision.objects.filter(action="category_delete").exists()


def test_category_assignment_and_removal_audit_link_snapshot() -> None:
    user, route, category = (
        owner(),
        Route.objects.create(),
        Category.objects.create(name="X", slug="x"),
    )
    link = assign_route_category(route, category, reason="classify", actor=user)
    remove_route_category(route, category, reason="correct", actor=user)
    decision = ModerationDecision.objects.get(action="category_unassign")
    assert decision.metadata["before"]["link_id"] == link.pk
    assert decision.metadata["after"]["assigned"] is False
    assert not RouteCategory.objects.exists()


def test_route_metadata_clear_restores_original_provenance() -> None:
    user = owner()
    route = Route.objects.create()
    route.thread_title = "Loops"
    route.generated_title = "Loops — Loop from Brno"
    route.display_title = route.generated_title
    route.title_provenance = TitleProvenance.GEOGRAPHIC
    route.save(
        update_fields=["thread_title", "generated_title", "display_title", "title_provenance"]
    )
    update_route_metadata(route, admin_title_override="Curated", reason="edit", actor=user)
    update_route_metadata(route, admin_title_override="", reason="clear", actor=user)
    route.refresh_from_db()
    assert route.title_provenance == TitleProvenance.GEOGRAPHIC
    assert route.display_title == route.generated_title


def test_source_reason_and_relationship_before_after_audit() -> None:
    user = owner()
    route, other = Route.objects.create(), Route.objects.create()
    source = RouteSource.objects.create(route=other, mapy_url="https://mapy.com/s/relationship")
    mark_source_unavailable(source, reason="manual check", actor=user)
    source_decision = ModerationDecision.objects.get(action="source_unavailable")
    assert source_decision.metadata["before"]["source_status"] != "unavailable"
    with pytest.raises(ValidationError):
        mark_source_unavailable(source, actor=user)
    relationship = SimilarityRelationship.objects.create(
        route_a=min(route, other, key=lambda item: str(item.pk)),
        route_b=max(route, other, key=lambda item: str(item.pk)),
        relationship_type="suspected_duplicate",
        evidence={"score": 0.8},
    )
    keep_both_routes(route, other, reason="distinct", actor=user, evidence={"score": 0.8})
    decision = ModerationDecision.objects.filter(action="keep_both").first()
    assert decision is not None
    assert decision.metadata["relationship_before"]["id"] == relationship.pk
    assert decision.metadata["relationship_after"]["relationship_type"] == "variant"


def test_repeated_metadata_overrides_restore_last_underlying_provenance() -> None:
    user = owner()
    for provenance, generated in (
        (TitleProvenance.GEOGRAPHIC, "Loops — Loop from Brno"),
        (TitleProvenance.AUTHOR_DATE, "Rides — rider · 2025-01-01"),
    ):
        route = Route.objects.create(
            thread_title="Rides",
            generated_title=generated,
            display_title=generated,
            title_provenance=provenance,
        )
        update_route_metadata(route, admin_title_override="Override A", reason="first", actor=user)
        update_route_metadata(route, admin_title_override="Override B", reason="second", actor=user)
        update_route_metadata(route, admin_title_override="", reason="clear", actor=user)
        route.refresh_from_db()
        assert route.title_provenance == provenance
        assert route.display_title == generated


def test_route_metadata_rejects_overlong_title_without_audit() -> None:
    user = owner()
    route = Route.objects.create(display_title="Original")
    with pytest.raises(ValidationError):
        update_route_metadata(route, admin_title_override="x" * 501, reason="too long", actor=user)
    route.refresh_from_db()
    assert route.display_title == "Original"
    assert not ModerationDecision.objects.filter(route=route).exists()


def test_denylist_restore_audits_success_and_rejects_invalid_states() -> None:
    user, route = owner(), Route.objects.create()
    source = RouteSource.objects.create(route=route, mapy_url="https://mapy.com/s/deny-restore")
    entry = SourceDenylistEntry.objects.create(
        source_url=source.mapy_url, route_source=source, reason="takedown"
    )
    with pytest.raises(ValidationError):
        restore_denylist_entry(entry, reason="", actor=user)
    assert entry.active
    restored = restore_denylist_entry(entry, reason="owner release", actor=user)
    assert not restored.active
    decision = ModerationDecision.objects.get(action="denylist_restore")
    assert decision.route_id == route.pk
    assert decision.actor_id == user.pk
    assert decision.metadata["before"]["active"] is True
    assert decision.metadata["after"]["active"] is False
    with pytest.raises(ValidationError):
        restore_denylist_entry(entry, reason="again", actor=user)


def test_orphan_denylist_restore_and_audit_failure_are_atomic() -> None:
    user, route = owner(), Route.objects.create()
    orphan = SourceDenylistEntry.objects.create(
        source_url="https://mapy.com/s/orphan-restore", route_source=None, reason="manual"
    )
    with pytest.raises(ValidationError):
        restore_denylist_entry(orphan, reason="cannot associate", actor=user)
    orphan.refresh_from_db()
    assert orphan.active
    assert not ModerationDecision.objects.filter(action="denylist_restore").exists()

    source = RouteSource.objects.create(route=route, mapy_url="https://mapy.com/s/rollback")
    entry = SourceDenylistEntry.objects.create(
        source_url=source.mapy_url, route_source=source, reason="rollback"
    )
    with (
        patch(
            "apps.catalogue.services.ModerationDecision.objects.create",
            side_effect=RuntimeError("audit unavailable"),
        ),
        pytest.raises(RuntimeError),
    ):
        restore_denylist_entry(entry, reason="must rollback", actor=user)
    entry.refresh_from_db()
    assert entry.active


def test_admin_denylist_restore_post_uses_actor_and_reason() -> None:
    with override_settings(GITHUB_OWNER_IDS=frozenset({"12345"})):
        user, route = owner(), Route.objects.create()
        source = RouteSource.objects.create(
            route=route, mapy_url="https://mapy.com/s/admin-restore"
        )
        entry = SourceDenylistEntry.objects.create(
            source_url=source.mapy_url, route_source=source, reason="admin"
        )
        client = Client()
        client.force_login(user)
        oauth_session(client)
        response = client.post(
            f"/admin/catalogue/sourcedenylistentry/{entry.pk}/restore/",
            {"reason": "reviewed takedown"},
        )
    assert response.status_code == 302
    entry.refresh_from_db()
    assert not entry.active
    assert ModerationDecision.objects.filter(
        action="denylist_restore", actor=user, reason="reviewed takedown"
    ).exists()
