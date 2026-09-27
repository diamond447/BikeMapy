# Capture topology validation

Issue #55 adds an executable reference harness in
`apps.accounts.capture_validation`. It is intentionally a pure validation
service: it has no capture tables, persistent territory model, or UI.

## Selected algorithm

The harness accepts bounded WGS84 `LineString` traces and orders them by owner,
trace ID, Prague-local timestamp, and coordinates. Endpoint distance is measured
with WGS84 `geography` (`ST_DWithin`), so the 50-metre rule is accurate at the
equator and at 80°N. PostGIS transforms ordinary lines to EPSG:6933 (a global
metre-based equal-area CRS) for noding and `ST_Polygonize`. If a batch crosses
the antimeridian, it finds the largest empty longitude gap and places the seam
there in an equivalent cylindrical equal-area projection. This prevents a
179°E → 179°W line, or an unrelated Greenwich line, from being cut by a fixed
seam; the output is normalized back to WGS84. Endpoints that already match
another endpoint are anchors and cannot be pulled into nearby nested loops;
unanchored endpoint clusters use a stable canonical point.
Unbounded exterior geometry is not emitted; faces smaller than 1 m² are
discarded. Face area is measured after transforming the face back to WGS84 with
`ST_Area(geography)`, so the reported value is geodesic rather than degree-based.

Each owner/date candidate is polygonized independently from that owner's
cumulative (that date and newer) network. Those claim polygons are then
overlaid and noded into atomic faces. A claim covers a face only when the whole
face is within that owner's claim polygon; the greatest complete candidate date
is its effective date. The latest date wins between owners, and owners on the
same date are retained in sorted order. This produces truthful A-only,
overlap, and B-only areas, preserves an older owner's annulus around a newer
inner claim, and requires a fully newer boundary for a retake.

The harness rejects invalid coordinates, invalid lines, duplicate IDs, and
inputs over 300 traces or 30,000 coordinates before issuing SQL. It bounds
generated faces at 200, caps the serialized face response at 8 MB, and applies
a 5,000 ms PostgreSQL statement timeout. The `safe_validate` wrapper retries
only transient database/timeout failures (at most twice); permanent validation
errors return immediately. Both preserve the last valid result on failure.
Sorting and stable face ordering make delivery order irrelevant.

The incremental fixture intentionally models the production-safe fallback: each
batch queues a bounded full rebuild from the accumulated trace set. Removals
are authoritative tombstones keyed by immutable trace ID, so a remove-before-
add delivery and an add-before-remove delivery converge to the same result.
It does not claim an independent mutable topology cache; additions, removals,
duplicates, and reordered batches compare directly with one-shot rebuild output.

## Alternatives rejected

- Shapely/GEOS-only polygonization was rejected because the project has no
  Shapely dependency and it does not provide the required geography-area and
  global projection behavior in the production database.
- A Web-Mercator (EPSG:3857) buffer was rejected because its metre scale and
  area are latitude-dependent, which breaks the worldwide rule. EPSG:6933 (or
  its antimeridian-centered equivalent for crossing batches) is used for
  topology/area, but never to decide the 50 m distance.
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
records repeated measured results and database versions. The benchmark uses 240
traces (80% of the 300-trace bound) forming 60 disconnected faces and completed
in 3.379, 2.887, and 3.462 seconds in the recorded runs, each under the 5
second harness budget. The fixture suite also checks
50 m noise joining, Prague chronology, intersections, nested loops,
disconnected traces, overlapping claims, same-day ties, bitten-apple and
newer-retake chronology, the bitten-apple late-closing boundary,
self-intersections, 0.1 m² sliver filtering, geodesic
area and threshold accuracy at the equator/80°N, delivery-order determinism,
full/incremental equivalence, bounded input, transient retry, and safe failure.
