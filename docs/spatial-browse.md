# Spatial browse products

BikeMapy stores two derived products alongside the immutable route version:

* `RouteBrowseGeometry` stores one lossy LineString per configured zoom
  (`SPATIAL_BROWSE_ZOOMS`, default `6,8,10,12,14`).  The tolerance is half an
  equatorial Web-Mercator pixel; normalized geometry is still returned for a
  selected route.  A viewport reads only the closest stored zoom at or below
  its own zoom (`DISTINCT ON`), so above z14 it serves the z14 line: at z16
  and z18 the simplification kept nearly every vertex of the normalized
  geometry.  Browse lines are returned with six coordinate decimals (about
  0.1 m).  After changing the zoom list, run `rebuild_spatial_products`; it
  also deletes rows for zooms that are no longer configured.
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

The benchmark seeds 2,000 synthetic routes of 300-1,500 vertices spread over
Czechia, generates browse lines for every configured zoom and runs `ANALYZE`
before measuring.  It prints the PostGIS version and the `EXPLAIN (ANALYZE,
BUFFERS)` plan of the slowest route-viewport query next to the timings.

The local PostGIS 3.5 / PostgreSQL 17 run of 2026-11-05 (twenty cold-cache
samples) reported route viewport min/median/p95 of 103.7/116.8/215.5 ms with
500 lines returned at the hard cap, heatmap viewport 1.79/2.09/2.43 ms, and
selected route 4.16/4.78/5.86 ms.  All three are within budget.  The slowest
query was the browse-line fetch (23 ms, an index scan with `DISTINCT ON`).

The earlier result of about 1.5 s per route viewport (2026-09-04) measured
missing planner statistics rather than the query: the rows were created inside
the test transaction, where autovacuum never analyses them, and PostgreSQL
chose a per-route nested loop.  The same dataset and code measured 15 ms
(median) once `ANALYZE` had run.  `rebuild_spatial_products` therefore
analyses the route, browse and heatmap tables when it finishes; run `ANALYZE`
on those tables after any other bulk import as well.

Vector-tile work should be reconsidered when two consecutive representative
PostGIS runs exceed either viewport budget after query plans, indexes, cache
TTL, and result limits have been checked.  This measured trigger—not route
count alone—is the threshold for a future vector-tile issue.
