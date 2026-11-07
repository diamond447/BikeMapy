# Architecture

BikeMapy is a Django modular monolith with a separate React frontend. Django
apps live in `backend/apps/`:

| App | Responsibility |
| --- | --- |
| `catalogue` | Routes, sources, immutable versions, deduplication, spatial products, moderation |
| `ingestion` | BikeForum crawler and GPX import |
| `api` | Public read-only REST API |
| `reports` | Anonymous route reports |
| `analytics` | Three aggregate product counters |
| `accounts` | Owner login (GitHub OAuth) |

## Catalogue model

A `Route` is the stable public identity (UUID plus a readable slug). Each
`RouteSource` is one Mapy.com link found in a forum post and belongs to one
route. Every import of a source creates an immutable `RouteVersion` with the
normalized geometry, a simplified geometry, distance, elevation and a
500-point elevation profile. A route is public only when it is `published`
and has a `current_approved_version`.

Moderation actions run in transactions and append a `ModerationDecision`
with the actor, reason and before/after state. Source URLs and versions are
never rewritten: a merge records a provenance alias, and a soft removal adds
a denylist entry so the source is not re-imported.

## Ingestion

The crawler fetches public BikeForum pages through Celery, pacing requests,
honouring `robots.txt`, and resuming from leased work items and checkpoints
stored in PostgreSQL. BikeForum has moved part of its threads to mtbiker.cz and
redirects to them; the crawler follows those redirects (mtbiker.cz is a
configured origin with its own `robots.txt` and a 5-second interval,
`BIKEFORUM_HOST_RATE_LIMITS`), walks every page of a moved thread, and does not
crawl the rest of mtbiker.cz. Mapy links are read from links on any Mapy host
the GPX import accepts (`mapy.cz`, `mapy.com`, `www.` and language subdomains),
from mtbiker.cz's `/external-click/` wrapper without fetching it, and from
links written as plain text. A `429` waits for `Retry-After`. Only the configured origins are fetched, every redirect
and DNS answer is checked for private addresses, and responses are size
capped. Raw HTML is cached for 24 hours for conditional requests.

GPX files are fetched from Mapy.com through `mapy-gpx-exporter` behind an
adapter, parsed with `defusedxml`, and bounded in size and point count.
Sources found by the crawler are queued for import right away, and imported
GPX files can be downloaded from the route detail.

## Duplicates and variants

Valid versions are compared on a distance-resampled polyline in both
directions, and loops across every start offset, so point density, direction
and loop start do not matter. A bounded candidate scan (PostGIS envelope and
length pre-filters, keyset paging) keeps the comparison cheap without
dropping late matches. Only a route's current approved version takes part; an
unreviewed route uses its valid historical versions.

Default thresholds (`ROUTE_*` settings):

- **Duplicate:** at most 10 % length difference, 25 m mean and 100 m maximum
  alignment distance. The duplicate route is quarantined and its payload is
  removed; its source, evidence and audit trail remain.
- **Variant:** any other pair with a score of at least 0.55. Variants stay
  separate public routes and are linked to each other.

The score combines `exp(-mean_distance / (4 × 30 m GPS tolerance))` with the
length similarity.

## Map browsing

Two derived products are rebuilt from the approved versions:

- `RouteBrowseGeometry`: one simplified line per zoom in
  `SPATIAL_BROWSE_ZOOMS` (default `6,8,10,12,14`), simplified to half a
  Web Mercator pixel. A viewport reads the closest stored zoom at or below
  its own.
- `RouteHeatmapCell`: a tile grid with the number of distinct routes per cell,
  used up to the highest `SPATIAL_HEATMAP_ZOOMS` value (default 8).

Viewport responses are bounded (500 routes, 10,000 cells, 90° span), cached
for 60 seconds with epoch-based invalidation, and sent with
`Cache-Control: public, max-age=60` and an `ETag`. The frontend snaps
viewports to the tile grid so nearby positions share a URL. After a bulk
import, run `manage.py rebuild_spatial_products`; it also refreshes the
planner statistics.

## Administration and reports

`/admin/` is limited to the GitHub accounts whose numeric IDs are listed in
`GITHUB_OWNER_IDS`. The owner reviews routes, metadata, duplicate decisions,
source removals and reports there. The `Reviewed` badge means only that the
latest review confirmed technical validity, source context and content
suitability.

Anonymous reports (`POST /api/v1/routes/<id>/reports/`) need a Turnstile
token and are rate limited per HMAC-derived client identifier (3 per hour, 10
per day); raw IP addresses are not stored. Reports never change a route on
their own. Contact e-mail is removed 90 days after a report is closed and
report details are anonymized after a year.
