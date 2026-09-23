# Capture topology validation

Issue #55 adds an executable reference harness in
`apps.accounts.capture_validation`. It is intentionally a pure validation
service: it has no capture tables, persistent territory model, or UI.

## Selected algorithm

The harness accepts bounded WGS84 `LineString` traces and orders them by owner,
trace ID, Prague-local timestamp, and coordinates. PostGIS transforms the lines
to EPSG:6933 (a global metre-based equal-area CRS), canonicalizes only
endpoints that are within 50 metres, then runs `ST_UnaryUnion`, noding, and
`ST_Polygonize`. Unbounded exterior geometry is not emitted; faces smaller than
1 m² are discarded. Face area is measured after transforming the face back to
WGS84 with `ST_Area(geography)`, so the reported value is geodesic rather than
degree-based.

An owner/date candidate is complete only when its cumulative (that date and
newer) network covers the entire face boundary. The greatest date that remains
complete is the effective claim date. The latest date wins between owners, and
owners on the same date are retained in sorted order. This models partial old
boundaries, newer complete retakes, bitten-apple chronology, same-day shared
ownership, nested loops, intersections, and disconnected traces without
introducing a persistent territory model.

The harness rejects invalid coordinates, invalid lines, duplicate IDs, and
inputs over 2,000 traces or 200,000 coordinates before issuing SQL. The
`safe_validate` wrapper retries at most twice and returns the last valid result
on failure. Sorting and stable face ordering make delivery order irrelevant.

## Alternatives rejected

- Shapely/GEOS-only polygonization was rejected because the project has no
  Shapely dependency and it does not provide the required geography-area and
  global projection behavior in the production database.
- A Web-Mercator (EPSG:3857) buffer was rejected because its metre scale and
  area are latitude-dependent, which breaks the worldwide rule.
- Snapping every vertex was rejected because it can incorrectly join nearby
  nested loops. Only endpoints participate in the 50 m joining tolerance.
- A persistent territory model was rejected for this issue because capture
  validation must remain deterministic and replayable from the activity trace
  set; persistence belongs to a later product issue.

## Reproducible evidence

The deterministic fixture and PostGIS benchmark are in
`backend/apps/accounts/tests/test_capture_validation.py`. Run the ordinary
tests with:

```text
POSTGRES_HOST=127.0.0.1 RUN_CAPTURE_VALIDATION_BENCHMARK=1 \
  uv run pytest -q backend/apps/accounts/tests/test_capture_validation.py -s
```

The checked-in [benchmark evidence](evidence/issue-55-capture-validation-20260923.json)
records the measured result and database versions. The benchmark uses 400
traces forming 100 disconnected faces and completed in 2.085 seconds in the
recorded run, under the 5 second harness budget. The fixture suite also checks
50 m noise joining, Prague chronology, intersections, nested loops,
disconnected traces, overlapping claims, same-day ties, bitten-apple and
newer-retake chronology, self-intersections, sliver filtering, geodesic area,
delivery-order determinism, bounded input, and safe failure.
