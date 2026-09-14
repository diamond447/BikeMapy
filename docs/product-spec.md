# BikeMapy Product Specification

## Document status

This document records the product direction agreed for the first public
release. The repository contains a working implementation of the core
catalogue, map browsing, API, ingestion, moderation, and quality automation;
this specification remains the source of truth for product requirements and
for capabilities that are gated, incomplete, or intentionally future-facing.
Measured results and later decisions may refine it through normal project
documentation. See the [README](../README.md) for the current implementation
overview and local entry point.

## Product intent

BikeMapy helps cyclists discover routes shared in BikeForum discussions. The
forum contains a valuable historical archive, but route links are scattered
through long-lived threads and are difficult to search, compare, and revisit.
BikeMapy makes that material easier to explore without losing the original
author, post, and route-source context.

The project has two complementary goals:

- Approximately 60% portfolio engineering value: demonstrate professional
  delivery, a reliable ingestion pipeline, geospatial data handling, testing,
  security, operations, and thoughtful trade-offs.
- Approximately 40% community utility: provide a useful, respectful route
  discovery tool for cyclists.

The initial success hypothesis is 500 unique visitors during the first 30 days
after public launch. This is a target for a measured launch experiment, not a
guarantee. When configured, Cloudflare Web Analytics provides the primary
visit measurement, supplemented by anonymous aggregate counters for
route-detail views, GPX download clicks, and original-source clicks. Visits remain the primary metric;
the product does not use browser fingerprinting.

The implementation keeps those product counters in day-bucketed, allow-listed
records with no route, session, user, network address, user-agent, or arbitrary
metadata. Cloudflare Web Analytics is configured only with its public site
token; an empty token leaves the beacon unloaded in local development and
previews.

## Product requirements

### Route discovery and ingestion

BikeMapy will build its initial catalogue from public BikeForum route
references. A resumable backfill will attempt to cover all reachable history,
in rate-limited batches. The application may launch with a validated partial
dataset while the remaining backfill continues in the background.

After the initial backfill, an incremental crawl will run approximately once a
day. Crawling must be conservative and permission-aware: follow applicable
`robots.txt` rules, identify the crawler, use a low request rate, cache
responses, retry safely with backoff, and provide an author or rights-holder
removal path. Crawl progress and checkpoints must survive interruption.

The pipeline must be idempotent. Re-running a page or task must not create
unbounded duplicate records or silently replace moderation decisions. Source
URLs, post identity, processing status, checksums, timestamps, and errors must
remain traceable.

GPX extraction uses the `mapy-gpx-exporter[frpc]` library behind a BikeMapy
adapter. This dependency uses unofficial Mapy.com interfaces, so it is an
explicit operational risk. Extraction failures must be isolated and fail
safely: the source and failure state remain available for retry or review, and
a failed extraction must not take down crawling or public route browsing.

### Validation and publication

The minimum technical validity requirement is a parseable GPX file containing
valid coordinates. Missing elevation, timestamps, distance, or other optional
metadata must not by themselves prevent publication.

A technically valid route that is not identified as a high-confidence duplicate
is published automatically. A route may therefore appear without a category,
and no empty or placeholder category label is shown.

### Categories

Categories are optional, multi-valued metadata assigned manually by the
owner-admin. They are displayed only when present. Category assignment is a
moderation convenience and does not gate technically valid route publication.

### Deduplication and variants

Deduplication is conservative and optimized for high precision. Incorrectly
quarantining a legitimate route is considered more harmful than allowing a
duplicate to remain visible. Similarity scoring must be configurable,
explicitly defined, and benchmarked against representative route examples;
an arbitrary fixed percentage is not a product invariant.

The comparison must account for GPS noise, geometry normalization, direction,
and loop start points. Reversed direction and a shifted start point on an
equivalent loop are treated as equivalent. A meaningful change in route length
or geometry—roughly more than 10% of the route in the initial policy—is
treated as a separate route variant rather than hidden as a duplicate. The
threshold and scoring details remain tunable after benchmarking.

Suspected duplicates are recoverably quarantined for an administrator. The
duplicate GPX payload is removed from storage, but source URL, checksum,
similarity evidence, decision reason, timestamps, and audit metadata are
retained so that the record can be understood and restored or reprocessed.
The system must never imply that this metadata-only retention preserves the
original payload.

Variants remain separate public routes and are linked as similar variants.
One canonical route can retain multiple public BikeForum sources, authors, and
post links. The administrator can merge source relationships, keep both
routes, quarantine a suspected duplicate, or restore a quarantined record.

If the same Mapy.com URL later produces changed GPX data, BikeMapy versions the
route data instead of overwriting history. The current approved version is
public; previous versions and change evidence remain available to the admin
for audit and rollback decisions.

If a source link becomes unavailable, the route remains visible with a source
status such as “source unavailable” and the date of the last successful check.
An unavailable source does not by itself remove a route.

An intentional admin removal creates a soft-deleted record and a permanent
until-restored denylist entry for the source. The denylist prevents automatic
re-import until the admin explicitly restores the removal. Source payloads
removed during quarantine or takedown are not treated as recoverable file
backups.

### Route titles and detail

Titles are derived from the BikeForum thread where the route was found, not
from a Mapy.com title. The title-generation order is:

1. Thread title plus a geographic start/end or loop suffix.
2. Thread title plus the author and post date when geographic context is not
   available.
3. Thread title plus a stable short route ID as a final disambiguator.

Examples include “Gravel Routes in South Moravia — Brno → Mikulov” and “MTB
Tips — Loop from Tišnov”. If one thread contains multiple routes, stable
ordering or the route ID distinguishes them. The system stores title
provenance, original source title, generated title, display title, and any
admin override separately.

The route detail view includes, when available:

- The thread-derived title.
- BikeForum author and original post date.
- A link to the specific forum post.
- A link to the corresponding Mapy.com route.
- Optional manually assigned categories.
- Distance, ascent, descent, and other elevation fields when available.
- An elevation profile when available.
- The route shape and current source status.
- Whether the route is a loop or point-to-point, when determinable.
- A narrow `Reviewed` badge only when the admin has checked technical validity,
  source context, and obvious content suitability. It does not promise safety,
  current passability, or legal access.
- A conditional GPX download.
- A report action and a shareable permanent link.
- Multiple BikeForum sources associated with the canonical route.

Public GPX downloads are enabled only after the required legal and terms review
approves redistribution. If redistribution is not permitted, the fallback is
route display and links to the original sources.

### Map and browsing experience

The map is the primary interface. A hybrid presentation prevents a national
view from becoming a dense mass of overlapping lines:

- At low zoom, a grid-based line-density heatmap counts distinct routes that
  cross each cell. It must not count only route start points.
- The heatmap grid is precomputed in PostGIS, and route changes incrementally
  update only the affected cells.
- At closer zoom, the current viewport shows simplified route lines.
- Full geometry loads for the selected route.
- Selecting a route automatically frames the map around its geometry while
  preserving enough surrounding context to keep the selection understandable.
- The selected route has a strong, high-contrast highlight and other routes
  are visually subdued as appropriate.
- When several routes overlap at a clicked location, the user can move through
  them with arrows and an indicator such as `2 / 7`. Each route remains
  individually selectable.

The route list is secondary and deliberately low-key. On desktop it is a very
narrow, collapsible panel; on mobile it is a bottom sheet. Selection is
synchronized between the map, list, route detail, and URL. The map remains
dominant in both layouts.

The following filters are supported: text, author, category, distance,
elevation, and current viewport. Text search covers route titles, thread
titles, authors, and localities. PostgreSQL full-text search and trigram
indexes are preferred over a separate search service at the initial dataset
scale.

Selected route, map viewport, and active filters are addressable in the URL.
Permanent route links use stable identifiers and readable slugs. Browser back,
refresh, and shared links should preserve relevant map state.

### Language, accessibility, and SEO

The user interface supports Czech and English. The language follows the
browser initially and can be changed persistently. Source titles, usernames,
and original forum content remain in their original language rather than being
translated.

The MVP targets WCAG 2.2 AA, including keyboard operation, visible focus,
adequate contrast, reduced-motion support, meaningful labels, and a textual
route-list alternative to map-only interaction.

The MVP is responsive and online-only. It does not require installable PWA or
offline map behavior.

Basic SEO is part of the MVP: semantic HTML, localized metadata, canonical
URLs, Open Graph metadata, `robots.txt`, a sitemap, and stable route
permalinks. Cloudflare Pages Functions provide edge-generated metadata for
published shared routes and generate the route sitemap from the public API;
unpublished, removed, and unknown routes remain absent from crawler output.

## Administration and trust

The first release has one owner-admin. Access uses GitHub OAuth 2.0 through
`django-allauth` and an allowlist based on the immutable numeric GitHub user
ID, not a mutable username. Non-allowlisted identities cannot administer the
application.

Django Admin is the unified administration interface. It covers routes,
sources, versions, categories, reports, denylist entries, and audit history.
A custom MapLibre duplicate-review screen is integrated into that interface
and supports side-by-side evidence and the actions `merge sources`, `keep
both`, `quarantine`, and `restore`.

Anonymous reports enter the admin dashboard and never automatically hide a
route. A report may identify a suspected rights-holder takedown request, but
the admin reviews it. Abuse controls include Cloudflare Turnstile, a honeypot,
duplicate-report protection, and Redis limits around three reports per hour
and ten per day. The exact limit is configurable; rejected or repeated reports
must not cause an automatic content action.

The footer links to Terms, Privacy, and a Removal Policy without interrupting
normal use. The removal policy provides a contact email and explains what
information a rights holder should provide. The report form also provides an
author-removal reason.

## Implementation direction

### Application architecture

BikeMapy is a modular monolith. Django, crawling, extraction, import,
deduplication, and background task code live in one backend project with
explicit domain boundaries. The frontend is a separate React application.
This leaves room for later extraction of a component if measurements justify
it without paying the operational cost of microservices in the MVP.

The current implementation uses the following stack:

- Python 3.13.
- Django 5.2 LTS, Django REST Framework, GeoDjango, and PostgreSQL/PostGIS.
- Celery with Redis for crawl, extraction, import, versioning, and maintenance
  jobs. PostgreSQL remains the source of truth for task state and checkpoints.
- `httpx`, Beautiful Soup 4, and `lxml` for conservative server-rendered
  BikeForum crawling, with stored HTML fixtures and parser contract tests.
- The `mapy-gpx-exporter[frpc]` dependency behind a BikeMapy adapter.
- React, TypeScript, Vite, MapLibre GL JS, and OpenFreeMap.
- `pnpm` for frontend dependencies.
- `drf-spectacular` for OpenAPI, with `openapi-typescript` and `openapi-fetch`
  for generated frontend types and requests, and TanStack Query for server
  state.

OpenFreeMap is the initial basemap provider. Map provider configuration and
attribution are kept replaceable so a regional PMTiles archive or another
permitted provider can be evaluated later. Low-thousands route volume is
initially addressed with PostGIS spatial indexes, bounded queries, simplified
geometries, caching, and progressive detail. Benchmarks define whether
PostGIS-generated vector tiles become worthwhile; scalability claims must be
based on measured budgets rather than assumed traffic.

### API

The frontend consumes a public, versioned read API under `/api/v1`. It exposes
route metadata and bounded map queries with pagination, filters, and geometry
appropriate to the requested viewport or zoom. The API and OpenAPI schema are
documented for reviewers and potential clients.

The report submission endpoint is public but protected by Turnstile and
rate-limiting. Administrative and crawler internals are protected. The public
contract does not offer a bulk GPX database dump.

### Data and storage

PostgreSQL/PostGIS stores route metadata, source relationships, versions,
moderation decisions, checksums, normalized geometries, simplified
geometries, and spatial indexes. Original GPX payloads live in a Docker volume
behind Django’s storage abstraction so the domain code is not tied to a
particular disk. A reverse proxy serves downloads efficiently.

The same Docker Compose service model is intended for local development and a
future homeserver. Environment configuration, migrations, health checks,
backups, and restore procedures are documented and portable. A deployment
must not assume the homeserver already exists.

### Deployment

Cloudflare Pages is the documented target for branch and pull-request
frontend previews. Previews use production read-only API data; report
mutations and administration are disabled, and no production secrets are
embedded in preview builds. Production promotion remains human-controlled as
described in the deployment runbook.

GitHub Actions builds tested, immutable backend images tagged by commit and
publishes them to GitHub Container Registry. The initial homeserver deployment
is manual and will use an explicitly selected image, a backup, migrations,
readiness verification, and a rollback procedure. A later gated workflow may
provide a human-confirmed deployment. Cloudflare Tunnel exposes the backend
without requiring inbound router port forwarding.

### Backups and operations

The homeserver creates daily local snapshots of the database and GPX volume. A
laptop pulls encrypted copies when it is available, using a restricted backup
account and a recoverable repository. The system alerts when the laptop copy
becomes too old, and restore tests run regularly. The laptop is treated as a
separate physical copy, without claiming cloud or geographic disaster
protection.

Operations begin with structured JSON logs, Docker log rotation, Sentry with
PII scrubbing and a 30-day retention period, live and readiness health
endpoints, crawler-freshness monitoring, and external uptime checks. A
Prometheus/Grafana/Loki-style stack is deferred until measured needs justify
its resource and maintenance cost.

### Privacy and retention

Privacy follows data minimization:

- Rate limiting uses an HMAC-derived identifier in Redis for 24 hours; raw IP
  addresses are not stored in the application database.
- Production Docker logging keeps at most fourteen rotated files of 10 MiB per
  service. This is a size/count cap rather than a 14-day period; elapsed
  retention depends on traffic and is not currently known.
- An optional report email is removed 90 days after the report closes.
- Report text and its decision are retained for 12 months, then personal
  details are anonymized.
- Administrative and moderation audit events have a proposed 24-month
  retention from creation, with a recorded legal hold for an unresolved
  incident, rights dispute, or required accounting record. Automatic expiry is
  not implemented yet; implementation and verification remain a launch gate.
- Fetched BikeForum HTML is stored verbatim in `CrawlResponseCache.body` and is
  eligible for replay for at most 24 hours from acquisition. Physical clearing
  is attempted in bounded hourly cleanup batches and may be delayed by
  backlog or outage. Database backups can retain a pre-cleanup body until
  their snapshot expiry, so backup deletion handling remains a launch gate.
- Database and GPX backups retain their newest 30 complete host snapshots and
  newest 90 encrypted laptop snapshots. These are counts, not elapsed-day
  limits, so actual retention depends on scheduling and operator cleanup.
- Denylist URLs are retained until an admin restores the entry.
- No browser fingerprinting, marketing cookies, or non-essential analytics
  cookies are used in the MVP.

The final privacy notice must accurately reflect implementation and applicable
law; these periods are the product direction, not legal advice.

### Security and legal gates

The application treats fetched HTML, GPX files, and submitted URLs as hostile
input. It requires strict size limits, safe XML parsing, coordinate and URL
validation, SSRF defenses, XXE defenses, timeouts, safe redirects, content
type checks, and careful handling of archive or parser failures. A
`SECURITY.md` and a small threat model document supported versions, reporting,
trust boundaries, and important mitigations.

Quality and security automation runs through GitHub Actions, including CodeQL,
Gitleaks, Python and frontend dependency audits, and the project’s normal test
checks. These checks are part of the repository’s current delivery workflow;
they do not constitute a production launch or a legal approval.

The code is intended to use the MIT License. MIT applies to BikeMapy source
code only. BikeForum content, Mapy.com data or services, OpenFreeMap,
OpenStreetMap data, GPX files, and other external material may have separate
terms, licenses, attribution requirements, or restrictions.

Before public launch, the project must review BikeForum terms, Mapy.com terms
and unofficial interface risks, OpenFreeMap terms, OpenStreetMap/ODbL
requirements, GPX redistribution rights, and possible BikeMapy/Mapy.com brand
or trademark confusion. BikeMapy must not imply affiliation with Mapy.com.
The owner intends to enable all product features, including public GPX
downloads, at launch. They remain conditional on that review; if
redistribution is not allowed, the product falls back to displaying the route
with attribution and links to the original source.

## Quality direction

The project uses Ruff, mypy with `django-stubs`, pytest and pytest-django for
the backend, ESLint and Prettier for the frontend, Vitest and Testing Library
for unit and component tests, and Playwright for end-to-end tests. `pre-commit`
provides fast local checks for formatting, linting, secrets, file hygiene, and
commit-message conventions; CI remains authoritative.

Common `make` targets such as `make lint`, `make typecheck`, `make test`, and
`make check` provide a consistent local and CI interface. The backend coverage
denominator is production modules under `backend/apps` and `backend/config`,
excluding test modules. The target coverage floor is 80% for backend and
frontend, coverage must not decrease in a pull
request, and high-risk logic receives focused tests beyond a global number.
Critical examples include parser behavior, idempotent imports, spatial
deduplication, versioning, moderation, permissions, report protection, and
source denylisting.

## Visual direction

BikeMapy should feel like a precise, quiet cartographic tool. It may take
usability cues from Mapy.com, especially its map-first interaction model, but
must not copy its branding or visual design.

The map dominates the layout. Controls are restrained, readable, and
consistent. The selected route, overlap count, and route detail provide the
visual hierarchy; the surrounding UI stays quiet. The design should avoid a
generic SaaS dashboard aesthetic, oversized marketing sections, decorative
gradients, and ornamental cards that compete with the map.

The project owner reviews and refines screenshots on desktop and mobile before
accepting the result. The narrow desktop list, mobile bottom sheet, overlap
picker, focus states, and real route content are reviewed as an interaction
system rather than as isolated components.

## Future direction

One possible later addition is a private, invitation-only completion map for
one friend group. Members could connect Strava accounts, import historical and
new rides, display each person’s travelled geometry in a personal color, and
measure progress on official cycling routes, Via Czechia stages, and other
reference routes with PostGIS. Potential group statistics and achievements
would be considered only after the core product is stable.

This future direction is recorded to preserve product context, not to define
the current data model or promise delivery in the MVP.
