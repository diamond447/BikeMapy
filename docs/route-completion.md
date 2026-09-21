# Route completion calculation

Route completion is pinned to an immutable `ReferenceRouteVersion`. Each
eligible activity is transformed to EPSG:5514 and buffered by the configured
50-metre corridor (`ROUTE_COMPLETION_TOLERANCE_METERS`). The reference line is
intersected with the union of all activity corridors, so direction, repeated
rides, and overlapping member contributions cannot inflate covered distance.
Player and competition projections use the same operation; a competition is a
spatial union of its members, never a sum of percentages. Via Czechia imports
remain blocked until the feature gate, source availability, active collection,
and explicit permission are all present. A complete route and each imported
stage are separate route identities and therefore separate projections.

Lengths are rounded half-up to 0.001 metre and percentages to 0.001 percent
only when persisted. Values are clamped to `[0, total_length]` and `[0, 100]`.
Empty, malformed, non-line, or invalid geometries are excluded as activity
evidence; an invalid reference geometry fails the calculation and leaves the
projection `failed`. Small GEOS slivers are retained only when they have a
positive length; zero-length intersections contribute zero. The algorithm
version (`corridor-v1`), tolerance, route checksum, activity geometry hashes,
membership revision, and evidence digest are stored with each result.

Monthly rows contain newly covered reference geometry after subtracting all
earlier activities for that subject/version. Re-running a job deletes and
rebuilds evidence and monthly rows, making retries, activity removal, member
removal, and route-version changes reversible and idempotent.

`fresh`, `pending`, and `failed` are intentionally exposed by the private
completion endpoint. Calculation jobs are durable and leased so provider
webhook/list workers can request recalculation without doing spatial work in a
request transaction.
