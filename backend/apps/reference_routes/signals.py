"""Privacy hooks for derived activity completion evidence."""

from __future__ import annotations

from typing import Any, cast

from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver

from apps.accounts.models import Competition, ImportedActivity

from .models import ReferenceCollection, ReferenceRoute, ReferenceRouteVersion


@receiver(pre_save, sender=ReferenceCollection)
def remember_collection_completion_gates(
    sender: type[ReferenceCollection], instance: ReferenceCollection, **kwargs: Any
) -> None:
    del sender, kwargs
    cast(Any, instance)._completion_gate_snapshot = (
        ReferenceCollection.objects.filter(pk=instance.pk)
        .values_list("active", "permission_granted", "source_kind")
        .first()
        if instance.pk
        else None
    )


@receiver(post_save, sender=ReferenceCollection)
def invalidate_collection_completions_on_gate_change(
    sender: type[ReferenceCollection], instance: ReferenceCollection, **kwargs: Any
) -> None:
    del sender, kwargs
    old = getattr(instance, "_completion_gate_snapshot", None)
    current = (instance.active, instance.permission_granted, instance.source_kind)
    if old is not None and old != current:
        from .completion_services import invalidate_collection_completion_data

        invalidate_collection_completion_data(instance.pk)


@receiver(pre_save, sender=Competition)
def remember_competition_completion_gate(
    sender: type[Competition], instance: Competition, **kwargs: Any
) -> None:
    del sender, kwargs
    cast(Any, instance)._completion_gate_was_active = (
        Competition.objects.filter(pk=instance.pk).values_list("is_active", flat=True).first()
        if instance.pk
        else None
    )


@receiver(post_save, sender=Competition)
def invalidate_competition_completions_on_deactivation(
    sender: type[Competition], instance: Competition, **kwargs: Any
) -> None:
    del sender, kwargs
    if getattr(instance, "_completion_gate_was_active", None) is True and not instance.is_active:
        from .completion_services import invalidate_competition_completion_data

        invalidate_competition_completion_data(instance.pk)


@receiver(pre_save, sender=ReferenceRoute)
def remember_route_completion_gates(
    sender: type[ReferenceRoute], instance: ReferenceRoute, **kwargs: Any
) -> None:
    del sender, kwargs
    cast(Any, instance)._completion_gate_snapshot = (
        ReferenceRoute.objects.filter(pk=instance.pk)
        .values_list("active", "publication_status", "current_version_id")
        .first()
        if instance.pk
        else None
    )


@receiver(post_save, sender=ReferenceRoute)
def invalidate_route_completions_on_gate_change(
    sender: type[ReferenceRoute], instance: ReferenceRoute, **kwargs: Any
) -> None:
    del sender, kwargs
    old = getattr(instance, "_completion_gate_snapshot", None)
    current = (instance.active, instance.publication_status, instance.current_version_id)
    if old is not None and old != current:
        from .completion_services import invalidate_route_completion_data

        invalidate_route_completion_data(instance.pk)


@receiver(pre_save, sender=ReferenceRouteVersion)
def remember_version_completion_gates(
    sender: type[ReferenceRouteVersion], instance: ReferenceRouteVersion, **kwargs: Any
) -> None:
    del sender, kwargs
    cast(Any, instance)._completion_gate_snapshot = (
        ReferenceRouteVersion.objects.filter(pk=instance.pk)
        .values_list("active", "validation_status")
        .first()
        if instance.pk
        else None
    )


@receiver(post_save, sender=ReferenceRouteVersion)
def invalidate_version_completions_on_gate_change(
    sender: type[ReferenceRouteVersion], instance: ReferenceRouteVersion, **kwargs: Any
) -> None:
    del sender, kwargs
    old = getattr(instance, "_completion_gate_snapshot", None)
    current = (instance.active, instance.validation_status)
    if old is not None and old != current:
        from .completion_services import invalidate_version_completion_data

        invalidate_version_completion_data(instance.pk)


@receiver(pre_delete, sender=ImportedActivity)
def erase_activity_completion_evidence(
    sender: type[ImportedActivity], instance: ImportedActivity, using: str, **kwargs: Any
) -> None:
    del sender, using, kwargs
    from .completion_services import erase_activity_completion_data

    erase_activity_completion_data(instance.pk, player_id=instance.player_id)
