from django.contrib import admin

from .models import (
    ReferenceCollection,
    ReferenceImport,
    ReferenceRecomputation,
    ReferenceRoute,
    ReferenceRouteVersion,
)


@admin.register(ReferenceCollection)
class ReferenceCollectionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    readonly_fields = ("created_at", "updated_at")


@admin.register(ReferenceImport)
class ReferenceImportAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    readonly_fields = tuple(field.name for field in ReferenceImport._meta.fields)
    actions = None

    def has_delete_permission(self, request: object, obj: object = None) -> bool:
        return False


@admin.register(ReferenceRoute)
class ReferenceRouteAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    readonly_fields = ("created_at", "updated_at")


@admin.register(ReferenceRouteVersion)
class ReferenceRouteVersionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    readonly_fields = tuple(field.name for field in ReferenceRouteVersion._meta.fields)
    actions = None

    def has_delete_permission(self, request: object, obj: object = None) -> bool:
        return False


@admin.register(ReferenceRecomputation)
class ReferenceRecomputationAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    readonly_fields = tuple(field.name for field in ReferenceRecomputation._meta.fields)
