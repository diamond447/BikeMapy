"""Cache invalidation for public viewport filter membership."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django.db.models.signals import post_delete, post_save

from .models import (
    Category,
    ForumAuthor,
    ForumPost,
    ForumThread,
    Route,
    RouteCategory,
    RouteSource,
    RouteSourceMerge,
    RouteSourcePost,
    RouteVersion,
)

_FILTER_MEMBERSHIP_FIELDS: Mapping[type[Any], frozenset[str]] = {
    Category: frozenset({"slug"}),
    ForumAuthor: frozenset({"username"}),
    ForumPost: frozenset({"thread", "author"}),
    ForumThread: frozenset({"title", "locality"}),
    Route: frozenset(
        {
            "slug",
            "lifecycle",
            "current_approved_version",
            "display_title",
            "generated_title",
            "admin_title_override",
            "thread_title",
        }
    ),
    RouteCategory: frozenset({"route", "category"}),
    RouteSource: frozenset({"route", "source_status"}),
    RouteSourceMerge: frozenset({"canonical_route", "source", "active"}),
    RouteSourcePost: frozenset({"source", "post"}),
    RouteVersion: frozenset({"source", "distance_m", "ascent_m"}),
}


def _invalidate_filter_cache(
    *, sender: type[Any], created: bool = False, update_fields: Any = None, **_: object
) -> None:
    changed_fields = None if update_fields is None else {str(field) for field in update_fields}
    if not created and changed_fields is not None:
        if not changed_fields.intersection(_FILTER_MEMBERSHIP_FIELDS[sender]):
            return
    from .spatial import schedule_viewport_filter_cache_invalidation

    schedule_viewport_filter_cache_invalidation()


for _model in _FILTER_MEMBERSHIP_FIELDS:
    post_save.connect(
        _invalidate_filter_cache,
        sender=_model,
        dispatch_uid=f"viewport-save-{_model}",
    )
    post_delete.connect(
        _invalidate_filter_cache,
        sender=_model,
        dispatch_uid=f"viewport-delete-{_model}",
    )
