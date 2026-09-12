"""Cache invalidation for public viewport filter membership."""

from __future__ import annotations

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

_FILTER_MEMBERSHIP_MODELS = (
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


def _invalidate_filter_cache(**_: object) -> None:
    # This epoch is intentionally advanced immediately. A rollback can only
    # discard a cache entry early; retaining an entry after a committed write
    # would serve stale filter results.
    from .spatial import bump_viewport_filter_cache_epoch

    bump_viewport_filter_cache_epoch()


for _model in _FILTER_MEMBERSHIP_MODELS:
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
