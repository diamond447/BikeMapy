"""Owner-only catalogue and moderation administration."""

from __future__ import annotations

import json
from typing import Any, cast

from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from apps.accounts.admin import owner_admin_site

from .models import (
    Category,
    ForumAuthor,
    ForumPost,
    ForumThread,
    ModerationDecision,
    PayloadDeletionRequest,
    Route,
    RouteBrowseGeometry,
    RouteCategory,
    RouteHeatmapCell,
    RouteHeatmapMembership,
    RouteSource,
    RouteSourceMerge,
    RouteSourcePost,
    RouteVersion,
    SimilarityRelationship,
    SourceDenylistEntry,
)
from .services import (
    approve_version,
    assign_route_category,
    create_category,
    delete_category,
    keep_both_routes,
    mark_source_unavailable,
    merge_route_sources,
    quarantine_suspected_duplicate,
    remove_route_category,
    restore_denylist_entry,
    restore_route,
    review_route,
    soft_delete_route,
    update_category,
    update_route_metadata,
)


class OwnerModelAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin model policy shared by every registered catalogue model."""

    def has_module_permission(self, request: HttpRequest) -> bool:
        return bool(owner_admin_site.has_permission(request))

    def has_view_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return bool(owner_admin_site.has_permission(request))

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(Route, site=owner_admin_site)
class RouteAdmin(OwnerModelAdmin):
    list_display = (
        "effective_title",
        "lifecycle",
        "current_approved_version",
        "reviewed_at",
        "workflow_links",
    )
    list_filter = ("lifecycle", "title_provenance")
    search_fields = ("slug", "display_title", "generated_title", "original_source_title")
    readonly_fields = (
        "id",
        "lifecycle",
        "current_approved_version",
        "created_at",
        "updated_at",
        "deleted_at",
        "quarantine_reason",
        "reviewed_at",
    )

    @admin.display(description="Workflows")
    def workflow_links(self, obj: Route) -> str:
        links = [
            format_html(
                '<a href="{}">Metadata</a>',
                reverse("owner_admin:catalogue_route_metadata", args=[obj.pk]),
            ),
            format_html(
                '<a href="{}">Review</a>',
                reverse("owner_admin:catalogue_route_review", args=[obj.pk]),
            ),
        ]
        if obj.lifecycle == "published":
            links.append(
                format_html(
                    '<a href="{}">Remove</a>',
                    reverse(
                        "owner_admin:catalogue_route_lifecycle_action", args=[obj.pk, "soft-remove"]
                    ),
                )
            )
        else:
            links.append(
                format_html(
                    '<a href="{}">Restore</a>',
                    reverse(
                        "owner_admin:catalogue_route_lifecycle_action", args=[obj.pk, "restore"]
                    ),
                )
            )
        return format_html(" | ".join(str(link) for link in links))

    def get_urls(self):  # type: ignore[no-untyped-def]
        return [
            path(
                "<path:object_id>/review/",
                self.admin_site.admin_view(self.review_view),
                name="catalogue_route_review",
            ),
            path(
                "<path:object_id>/metadata/",
                self.admin_site.admin_view(self.metadata_view),
                name="catalogue_route_metadata",
            ),
            path(
                "<path:object_id>/<str:action>/",
                self.admin_site.admin_view(self.lifecycle_view),
                name="catalogue_route_lifecycle_action",
            ),
        ] + super().get_urls()

    def metadata_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        route = self.get_queryset(request).get(pk=object_id)
        if request.method == "POST":
            try:
                update_route_metadata(
                    route,
                    admin_title_override=request.POST.get("admin_title_override", ""),
                    reason=request.POST.get("reason", ""),
                    actor=request.user,
                )
            except ValidationError as exc:
                self.message_user(request, str(exc), messages.ERROR)
            else:
                self.message_user(request, "Route metadata updated.", messages.SUCCESS)
                return HttpResponseRedirect(
                    reverse("owner_admin:catalogue_route_change", args=[route.pk])
                )
        return TemplateResponse(
            request,
            "admin/catalogue/action_form.html",
            {
                **self.admin_site.each_context(request),
                "title": "Update metadata",
                "object": route,
                "action": "metadata",
            },
        )

    def review_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        route = self.get_queryset(request).get(pk=object_id)
        if request.method == "POST":
            reason = request.POST.get("reason", "").strip()
            checks = {
                "technical_validity": request.POST.get("technical_validity") == "on",
                "source_context": request.POST.get("source_context") == "on",
                "content_suitability": request.POST.get("content_suitability") == "on",
            }
            try:
                if not all(checks.values()):
                    raise ValidationError("All three Reviewed criteria must be checked.")
                review_route(route, reason=reason, actor=request.user, **checks)
            except ValidationError as exc:
                self.message_user(request, str(exc), messages.ERROR)
            else:
                self.message_user(request, "Reviewed criteria recorded.", messages.SUCCESS)
                return HttpResponseRedirect(
                    reverse("owner_admin:catalogue_route_change", args=[route.pk])
                )
        return TemplateResponse(
            request,
            "admin/catalogue/review_form.html",
            {**self.admin_site.each_context(request), "title": "Review route", "object": route},
        )

    def lifecycle_view(self, request: HttpRequest, object_id: str, action: str) -> HttpResponse:
        route = self.get_queryset(request).get(pk=object_id)
        if request.method != "POST":
            return TemplateResponse(
                request,
                "admin/catalogue/action_form.html",
                {
                    **self.admin_site.each_context(request),
                    "title": action,
                    "object": route,
                    "action": action,
                },
            )
        reason = request.POST.get("reason", "")
        try:
            if action == "soft-remove":
                soft_delete_route(route, reason=reason, actor=request.user)
            elif action == "restore":
                restore_route(route, reason=reason, actor=request.user)
            else:
                raise ValidationError("Unknown route lifecycle action.")
        except ValidationError as exc:
            self.message_user(request, str(exc), messages.ERROR)
        else:
            self.message_user(request, "Route lifecycle action recorded.", messages.SUCCESS)
        return HttpResponseRedirect(reverse("owner_admin:catalogue_route_change", args=[route.pk]))


@admin.register(RouteSource, site=owner_admin_site)
class RouteSourceAdmin(OwnerModelAdmin):
    list_display = (
        "mapy_url",
        "route",
        "source_status",
        "processing_status",
        "last_checked_at",
        "workflow_link",
    )
    list_filter = ("source_status", "processing_status")
    search_fields = ("mapy_url", "source_title", "route__slug")
    readonly_fields = (
        "route",
        "mapy_url",
        "processing_status",
        "source_status",
        "discovered_at",
        "processed_at",
        "last_checked_at",
        "last_successful_check_at",
        "last_error",
    )

    @admin.display(description="Workflow")
    def workflow_link(self, obj: RouteSource) -> str:
        return format_html(
            '<a href="{}">Mark unavailable</a>',
            reverse("owner_admin:catalogue_routesource_mark_unavailable", args=[obj.pk]),
        )

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def get_urls(self):  # type: ignore[no-untyped-def]
        urls = super().get_urls()
        custom = [
            path(
                "<path:object_id>/mark-unavailable/",
                self.admin_site.admin_view(self.mark_unavailable_view),
                name="catalogue_routesource_mark_unavailable",
            )
        ]
        return custom + urls

    def mark_unavailable_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        source = self.get_queryset(request).get(pk=object_id)
        if request.method != "POST":
            return TemplateResponse(
                request,
                "admin/catalogue/action_form.html",
                {
                    **self.admin_site.each_context(request),
                    "title": "Mark source unavailable",
                    "object": source,
                    "action": "mark-unavailable",
                },
            )
        try:
            mark_source_unavailable(
                source,
                error=request.POST.get("reason", ""),
                reason=request.POST.get("reason", ""),
                actor=request.user,
            )
        except ValidationError as exc:
            self.message_user(request, str(exc), messages.ERROR)
        else:
            self.message_user(request, "Source marked unavailable.", messages.SUCCESS)
        return HttpResponseRedirect(
            reverse("owner_admin:catalogue_routesource_change", args=[source.pk])
        )


@admin.register(RouteVersion, site=owner_admin_site)
class RouteVersionAdmin(OwnerModelAdmin):
    list_display = (
        "route",
        "source",
        "version_number",
        "technical_status",
        "approved_at",
        "workflow_link",
    )
    list_filter = ("technical_status", "loop_status", "payload_removed_at")
    search_fields = ("checksum", "source__mapy_url", "source__route__slug")
    readonly_fields = (
        "source",
        "version_number",
        "checksum",
        "original_gpx_storage_key",
        "payload_removed_at",
        "payload_removal_reason",
        "normalized_geometry",
        "simplified_geometry",
        "distance_m",
        "ascent_m",
        "descent_m",
        "elevation_profile",
        "loop_status",
        "technical_status",
        "validation_error",
        "created_at",
        "approved_at",
    )

    @admin.display(description="Route")
    def route(self, obj: RouteVersion) -> Route:
        return obj.route

    @admin.display(description="Workflow")
    def workflow_link(self, obj: RouteVersion) -> str:
        return format_html(
            '<a href="{}">Select approved</a>',
            reverse("owner_admin:catalogue_routeversion_select", args=[obj.pk]),
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def get_urls(self):  # type: ignore[no-untyped-def]
        return [
            path(
                "<path:object_id>/select/",
                self.admin_site.admin_view(self.select_view),
                name="catalogue_routeversion_select",
            )
        ] + super().get_urls()

    def select_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        version = self.get_queryset(request).get(pk=object_id)
        if request.method != "POST":
            return TemplateResponse(
                request,
                "admin/catalogue/action_form.html",
                {
                    **self.admin_site.each_context(request),
                    "title": "Select approved version",
                    "object": version,
                    "action": "select",
                },
            )
        try:
            approve_version(
                version,
                actor=request.user,
                reason=request.POST.get("reason", ""),
            )
        except ValidationError as exc:
            self.message_user(request, str(exc), messages.ERROR)
        else:
            self.message_user(request, "Approved route version selected.", messages.SUCCESS)
        return HttpResponseRedirect(
            reverse("owner_admin:catalogue_routeversion_change", args=[version.pk])
        )


@admin.register(Category, site=owner_admin_site)
class CategoryAdmin(OwnerModelAdmin):
    list_display = ("name", "slug", "created_at", "workflow_links")
    change_list_template = "admin/catalogue/category_changelist.html"
    search_fields = ("name", "slug", "description")

    @admin.display(description="Workflows")
    def workflow_links(self, obj: Category) -> str:
        return format_html(
            '<a href="{}">Edit</a> | <a href="{}">Delete</a>',
            reverse("owner_admin:catalogue_category_edit", args=[obj.pk]),
            reverse("owner_admin:catalogue_category_delete_workflow", args=[obj.pk]),
        )

    def get_urls(self):  # type: ignore[no-untyped-def]
        return [
            path(
                "new/",
                self.admin_site.admin_view(self.category_workflow),
                {"workflow": "new"},
                name="catalogue_category_new",
            ),
            path(
                "<path:object_id>/edit/",
                self.admin_site.admin_view(self.category_workflow),
                {"workflow": "edit"},
                name="catalogue_category_edit",
            ),
            path(
                "<path:object_id>/delete-workflow/",
                self.admin_site.admin_view(self.category_workflow),
                {"workflow": "delete"},
                name="catalogue_category_delete_workflow",
            ),
        ] + super().get_urls()

    def category_workflow(
        self, request: HttpRequest, object_id: str | None = None, workflow: str = "new"
    ) -> HttpResponse:
        category = self.get_queryset(request).filter(pk=object_id).first() if object_id else None
        if request.method == "POST":
            try:
                reason = request.POST.get("reason", "")
                if workflow == "new":
                    create_category(
                        name=request.POST.get("name", ""),
                        slug=request.POST.get("slug", ""),
                        description=request.POST.get("description", ""),
                        reason=reason,
                        actor=request.user,
                    )
                elif workflow == "edit" and category:
                    update_category(
                        category,
                        name=request.POST.get("name", ""),
                        slug=request.POST.get("slug", ""),
                        description=request.POST.get("description", ""),
                        reason=reason,
                        actor=request.user,
                    )
                elif workflow == "delete" and category:
                    delete_category(category, reason=reason, actor=request.user)
                else:
                    raise ValidationError("Unknown category workflow.")
            except ValidationError as exc:
                self.message_user(request, str(exc), messages.ERROR)
            else:
                self.message_user(request, "Category workflow recorded.", messages.SUCCESS)
                return HttpResponseRedirect(reverse("owner_admin:catalogue_category_changelist"))
        return TemplateResponse(
            request,
            "admin/catalogue/category_form.html",
            {
                **self.admin_site.each_context(request),
                "title": workflow,
                "object": category,
                "workflow": workflow,
            },
        )


@admin.register(RouteCategory, site=owner_admin_site)
class RouteCategoryAdmin(OwnerModelAdmin):
    list_display = ("route", "category", "assigned_at", "assigned_by", "workflow_link")
    list_filter = ("category",)
    change_list_template = "admin/catalogue/route_category_changelist.html"

    @admin.display(description="Workflow")
    def workflow_link(self, obj: RouteCategory) -> str:
        return format_html(
            '<a href="{}">Remove</a>',
            reverse("owner_admin:catalogue_routecategory_remove", args=[obj.pk]),
        )

    def get_urls(self):  # type: ignore[no-untyped-def]
        return [
            path(
                "assign/",
                self.admin_site.admin_view(self.assign_view),
                name="catalogue_routecategory_assign",
            ),
            path(
                "<path:object_id>/remove/",
                self.admin_site.admin_view(self.remove_view),
                name="catalogue_routecategory_remove",
            ),
        ] + super().get_urls()

    def assign_view(self, request: HttpRequest) -> HttpResponse:
        if request.method == "POST":
            try:
                route_id = request.POST.get("route_id")
                category_id = request.POST.get("category_id")
                if not route_id or not category_id:
                    raise ValidationError("Route and category IDs are required.")
                assign_route_category(
                    Route.objects.get(pk=route_id),
                    Category.objects.get(pk=category_id),
                    reason=request.POST.get("reason", ""),
                    actor=request.user,
                )
            except (ValidationError, Route.DoesNotExist, Category.DoesNotExist) as exc:
                self.message_user(request, str(exc), messages.ERROR)
            else:
                self.message_user(request, "Category assigned.", messages.SUCCESS)
                return HttpResponseRedirect(
                    reverse("owner_admin:catalogue_routecategory_changelist")
                )
        return TemplateResponse(
            request,
            "admin/catalogue/route_category_form.html",
            {**self.admin_site.each_context(request), "title": "Assign category"},
        )

    def remove_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        link = self.get_queryset(request).select_related("route", "category").get(pk=object_id)
        if request.method == "POST":
            try:
                remove_route_category(
                    link.route,
                    link.category,
                    reason=request.POST.get("reason", ""),
                    actor=request.user,
                )
            except ValidationError as exc:
                self.message_user(request, str(exc), messages.ERROR)
            else:
                self.message_user(request, "Category removed.", messages.SUCCESS)
                return HttpResponseRedirect(
                    reverse("owner_admin:catalogue_routecategory_changelist")
                )
        return TemplateResponse(
            request,
            "admin/catalogue/action_form.html",
            {
                **self.admin_site.each_context(request),
                "title": "Remove category",
                "object": link,
                "action": "remove",
            },
        )


@admin.register(SourceDenylistEntry, site=owner_admin_site)
class SourceDenylistEntryAdmin(OwnerModelAdmin):
    list_display = ("source_url", "active", "reason", "denied_at", "restored_at", "restore_link")
    list_filter = ("active",)
    search_fields = ("source_url", "reason")
    readonly_fields = ("source_url", "route_source", "denied_at", "restored_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    @admin.display(description="Restore")
    def restore_link(self, obj: SourceDenylistEntry) -> str:
        return format_html(
            '<a href="{}">Restore</a>',
            reverse("owner_admin:catalogue_sourcedenylistentry_restore", args=[obj.pk]),
        )

    def get_urls(self):  # type: ignore[no-untyped-def]
        return [
            path(
                "<path:object_id>/restore/",
                self.admin_site.admin_view(self.restore_view),
                name="catalogue_sourcedenylistentry_restore",
            )
        ] + super().get_urls()

    def restore_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        entry = self.get_queryset(request).get(pk=object_id)
        if request.method != "POST":
            return TemplateResponse(
                request,
                "admin/catalogue/action_form.html",
                {
                    **self.admin_site.each_context(request),
                    "title": "Restore denylist entry",
                    "object": entry,
                    "action": "restore",
                },
            )
        try:
            restore_denylist_entry(entry, reason=request.POST.get("reason", ""), actor=request.user)
        except ValidationError as exc:
            self.message_user(request, str(exc), messages.ERROR)
        else:
            self.message_user(request, "Denylist entry restored.", messages.SUCCESS)
        return HttpResponseRedirect(
            reverse("owner_admin:catalogue_sourcedenylistentry_change", args=[entry.pk])
        )


def _geometry(version: RouteVersion | None) -> dict[str, Any] | None:
    if version is None:
        return None
    value: Any = version.normalized_geometry or version.simplified_geometry
    if value is None:
        return None
    if hasattr(value, "geojson"):
        return cast(dict[str, Any], json.loads(value.geojson))
    if isinstance(value, str):
        try:
            return cast(dict[str, Any], json.loads(value))
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


@admin.register(SimilarityRelationship, site=owner_admin_site)
class SimilarityRelationshipAdmin(OwnerModelAdmin):
    list_display = (
        "route_a",
        "route_b",
        "relationship_type",
        "similarity_score",
        "decided_at",
        "review_link",
    )
    list_filter = ("relationship_type",)
    search_fields = ("route_a__slug", "route_b__slug", "decision_reason")
    readonly_fields = (
        "route_a",
        "route_b",
        "similarity_score",
        "evidence",
        "created_at",
        "decided_at",
    )

    @admin.display(description="Review")
    def review_link(self, obj: SimilarityRelationship) -> str:
        return format_html(
            '<a href="{}">Review duplicate</a>',
            reverse("owner_admin:catalogue_similarityrelationship_review", args=[obj.pk]),
        )

    def get_urls(self):  # type: ignore[no-untyped-def]
        return [
            path(
                "<path:object_id>/review/",
                self.admin_site.admin_view(self.review_view),
                name="catalogue_similarityrelationship_review",
            )
        ] + super().get_urls()

    def review_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        relationship = (
            self.get_queryset(request).select_related("route_a", "route_b").get(pk=object_id)
        )
        route_a, route_b = relationship.route_a, relationship.route_b
        versions = {}
        for route in (route_a, route_b):
            version = (
                route.current_approved_version
                or route.versions.filter().order_by("-created_at").first()
            )
            versions[str(route.pk)] = version
        if request.method == "POST":
            reason = request.POST.get("reason", "").strip()
            action = request.POST.get("action", "")
            target_id = request.POST.get("target_route", "")
            try:
                if action in {"merge_sources", "quarantine", "restore"} and target_id not in {
                    str(route_a.pk),
                    str(route_b.pk),
                }:
                    raise ValidationError("Select one of the two routes as the target.")
                if action == "keep_both":
                    keep_both_routes(
                        route_a,
                        route_b,
                        reason=reason,
                        actor=request.user,
                        evidence=relationship.evidence,
                    )
                elif action == "merge_sources":
                    canonical = route_a if target_id == str(route_a.pk) else route_b
                    duplicate = route_b if canonical.pk == route_a.pk else route_a
                    merge_route_sources(canonical, duplicate, reason=reason, actor=request.user)
                elif action == "quarantine":
                    target = route_a if target_id == str(route_a.pk) else route_b
                    quarantine_suspected_duplicate(
                        target, reason=reason, relationship=relationship, actor=request.user
                    )
                elif action == "restore":
                    target = route_a if target_id == str(route_a.pk) else route_b
                    restore_route(target, reason=reason, actor=request.user)
                else:
                    raise ValidationError("Unknown moderation action.")
            except ValidationError as exc:
                self.message_user(request, str(exc), messages.ERROR)
            else:
                self.message_user(request, "Moderation action recorded.", messages.SUCCESS)
                return HttpResponseRedirect(
                    reverse(
                        "owner_admin:catalogue_similarityrelationship_review",
                        args=[relationship.pk],
                    )
                )
        context = {
            **self.admin_site.each_context(request),
            "title": "Review suspected duplicate",
            "relationship": relationship,
            "route_a": route_a,
            "route_b": route_b,
            "geometry_a": _geometry(versions[str(route_a.pk)]),
            "geometry_b": _geometry(versions[str(route_b.pk)]),
            "evidence_json": json.dumps(relationship.evidence, sort_keys=True),
        }
        return TemplateResponse(request, "admin/catalogue/similarity_review.html", context)


@admin.register(ModerationDecision, site=owner_admin_site)
class ModerationDecisionAdmin(OwnerModelAdmin):
    list_display = ("created_at", "action", "route", "actor", "reason")
    list_filter = ("action", "created_at")
    search_fields = ("route__slug", "reason", "actor__username")
    readonly_fields = tuple(field.name for field in ModerationDecision._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


for model in (
    ForumAuthor,
    ForumThread,
    ForumPost,
    RouteSourcePost,
    RouteSourceMerge,
    RouteBrowseGeometry,
    RouteHeatmapCell,
    RouteHeatmapMembership,
    PayloadDeletionRequest,
):
    owner_admin_site.register(model, OwnerModelAdmin)
