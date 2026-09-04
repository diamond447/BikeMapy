from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.catalogue.models import Route
from apps.catalogue.spatial import generate_browse_geometries, refresh_route_heatmap


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
        self.stdout.write(
            self.style.SUCCESS(
                f"Rebuilt spatial products for {rebuilt_routes} routes and "
                f"{rebuilt_versions} approved versions."
            )
        )
