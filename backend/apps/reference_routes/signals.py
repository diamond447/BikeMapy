"""Privacy hooks for derived activity completion evidence."""

from __future__ import annotations

from typing import Any

from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.accounts.models import ImportedActivity


@receiver(pre_delete, sender=ImportedActivity)
def erase_activity_completion_evidence(
    sender: type[ImportedActivity], instance: ImportedActivity, using: str, **kwargs: Any
) -> None:
    del sender, using, kwargs
    from .completion_services import erase_activity_completion_data

    erase_activity_completion_data(instance.pk)
