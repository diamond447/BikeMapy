# Spatial browse products

BikeMapy stores two derived products alongside the immutable route version:

* `RouteBrowseGeometry` stores one lossy LineString per configured zoom.  The
  tolerance is half an equatorial Web-Mercator pixel; normalized geometry is
  still returned for a selected route.
* `RouteHeatmapCell` and `RouteHeatmapMembership` store a Web-Mercator tile
  grid and the distinct canonical routes crossing each cell.  A publication,
  quarantine, restoration, or removal deletes and rebuilds memberships for
  that route only, then recalculates the old and new cells only.

PostGIS deployments receive GiST indexes for normalized/simplified route
geometry, browse geometry, and heatmap boundaries.  Viewport services reject
oversized bounds and cap route results at 500 and cells at 10,000 by default.
The response is cached for 60 seconds; lifecycle changes advance a cache epoch
so stale map results are naturally bypassed.  These are service boundaries,
not an HTTP bulk-export contract.

The derived tables are intentionally not rebuilt inside the schema migration:
large catalogues must not hold a deployment transaction while doing GEOS work.
After applying migrations, run the resumable, route-by-route rebuild command
on the PostGIS worker (it is safe to rerun):

```sh
docker compose exec backend uv run --locked --no-dev \
  python backend/manage.py rebuild_spatial_products --batch-size 100
```

## Measurement budget

The opt-in `benchmark` test is intended to run against the Compose PostGIS
database with 2,000 representative route rows:

```sh
DJANGO_DATABASE_ENGINE=django.contrib.gis.db.backends.postgis \
  POSTGRES_HOST=127.0.0.1 POSTGRES_PASSWORD=bikemapy-local-only \
  RUN_SPATIAL_BENCHMARK=1 uv run --extra dev pytest backend/apps/catalogue/tests/test_spatial.py \
  -m benchmark -s
```

The initial operating budgets are p95 <= 250 ms for a bounded route viewport,
p95 <= 150 ms for a heatmap viewport, and <= 150 ms for a selected full route
(excluding network transfer).  The benchmark output, database `EXPLAIN
(ANALYZE, BUFFERS)`, and the PostGIS version must be recorded before making a
scalability claim; the lightweight SQLite quality suite is not a PostGIS
performance measurement.

The local PostGIS 17 benchmark (2026-09-04, twenty cold-cache samples, 2,000
synthetic routes with varied positions, vertex counts, lengths, and hit ratios)
reported route viewport min/median/p95 of 1,517.85/1,539.11/1,564.22 ms,
heatmap viewport 1.64/1.71/2.20 ms, and selected route 2.80/3.25/3.81 ms.
The route viewport returned 500 lines at its hard cap, exercising the
high-hit workload.  The route result exceeds the initial 250 ms budget in
this local setup; this is an optimization signal, not a production claim. These measurements include ORM and
GeoJSON service time as well as the database query; they are a local baseline,
not a production scalability claim.  Record query plans and the PostGIS
version with future runs.

Vector-tile work should be reconsidered when two consecutive representative
PostGIS runs exceed either viewport budget after query plans, indexes, cache
TTL, and result limits have been checked.  This measured trigger—not route
count alone—is the threshold for a future vector-tile issue.
