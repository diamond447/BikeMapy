from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from apps.catalogue.models import (
    Route,
    RouteBrowseGeometry,
    RouteHeatmapCell,
    RouteHeatmapMembership,
)
from apps.catalogue.spatial import generate_browse_geometries, refresh_route_heatmap

# Analysed after a rebuild: stale statistics made viewport queries ~95x slower.
ANALYZED_MODELS = (Route, RouteBrowseGeometry, RouteHeatmapCell, RouteHeatmapMembership)


def analyze_spatial_tables() -> None:
    if connection.vendor != "postgresql":
        return
    tables = ", ".join(connection.ops.quote_name(model._meta.db_table) for model in ANALYZED_MODELS)
    with connection.cursor() as cursor:
        cursor.execute(f"ANALYZE {tables}")


class Command(BaseCommand):
    help = "Rebuild derived browse geometries and heatmap memberships after deployment."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--route-id", help="Rebuild one route instead of the full catalogue.")
        parser.add_argument("--batch-size", type=int, default=100)

    def handle(self, *args: Any, **options: Any) -> None:
        batch_size = options["batch_size"]
        if batch_size < 1:
            raise CommandError("--batch-size must be positive")
        route_id = options.get("route_id")
        routes = Route.objects.all().order_by("pk")
        if route_id:
            routes = routes.filter(pk=route_id)
        if not routes.exists():
            raise CommandError("No matching route found")
        rebuilt_versions = 0
        rebuilt_routes = 0
        for offset in range(0, routes.count(), batch_size):
            for route in routes[offset : offset + batch_size]:
                version = route.current_approved_version
                if version is not None:
                    generate_browse_geometries(version)
                    rebuilt_versions += 1
                refresh_route_heatmap(route)
                rebuilt_routes += 1
        analyze_spatial_tables()
        self.stdout.write(
            self.style.SUCCESS(
                f"Rebuilt spatial products for {rebuilt_routes} routes and "
                f"{rebuilt_versions} approved versions."
            )
        )
