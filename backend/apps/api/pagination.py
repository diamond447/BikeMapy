"""Pagination defaults for public collection endpoints."""

# mypy: disable-error-code="import-untyped,misc"

from rest_framework.pagination import PageNumberPagination


class RoutePagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100
