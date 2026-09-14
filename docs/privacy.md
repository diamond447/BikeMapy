# BikeMapy Privacy Notice

**Last reviewed:** 2026-09-14
**Status:** launch draft — operator/controller identity, private contact,
lawful basis, effective date, and final legal review remain pending

This notice describes the current repository behavior. It is not legal advice
and is not a substitute for the final notice naming the operator and lawful
bases for processing.

## What the public site does

The public catalogue has no user account, profile, advertising, marketing
cookie, or browser fingerprint. When the operator configures the public
`VITE_CF_WEB_ANALYTICS_TOKEN`, the site loads Cloudflare Web Analytics as a
cookie-free, aggregate visit measurement service. It is the primary launch
metric and is not combined with the product counters to identify a visitor.
The frontend
stores the selected language, filters, selected route, and viewport preference
in the browser's `localStorage`; this stays on the device and is not sent as a
personal profile. Search and map requests send the selected filters, map view,
and route identifier to the BikeMapy API so the requested feature can work.

The product also sends only three allow-listed aggregate events to the API:
route-detail views, GPX download clicks, and original-source clicks. The API
stores one counter per event and local calendar day. Events contain no route
identifier, URL, session, user, network address, user-agent, or arbitrary
metadata, and the response does not expose counter values publicly. The owner
intends to enable all product features at launch, including public GPX
downloads,
but the legal deployment gate remains false until provider and rights evidence
is recorded. While that gate is false, the GPX click counter remains zero and
the API returns no download URL.

### Cloudflare Web Analytics vendor boundary

Cloudflare's [Web Analytics about page](https://developers.cloudflare.com/web-analytics/about/)
and [FAQ](https://developers.cloudflare.com/web-analytics/faq/) (reviewed
2026-09-14) describe Web Analytics as not collecting or using visitors'
personal data and as operating without cookies or fingerprinting. The
operator's review is still required: Cloudflare receives the browser beacon
request and its network/protocol data at Cloudflare's analytics service, and
the exact controller/processor role, applicable terms, and account settings
must be confirmed for the production property. The application sends no
custom event metadata to Cloudflare; the three product counters are sent only
to the BikeMapy API.

Cloudflare's FAQ says raw/unsampled beacon data is retained for seven days,
then aggregated to approximately ten percent sampling, while dashboard data
is available for the previous six months. It also says query strings are not
logged, beacons can be blocked, and custom events are not supported by Web
Analytics. These are vendor-stated limits, not guarantees made by BikeMapy;
the owner must verify the production account's current retention and privacy
settings before launch. Removing `VITE_CF_WEB_ANALYTICS_TOKEN` disables the
beacon if that review fails.

MapLibre loads the configured OpenFreeMap style and tiles. Those requests go
to the map provider and may expose the visitor's network address to that
provider under its terms. The map attribution uses the provider wording
`OpenFreeMap © OpenMapTiles Data from OpenStreetMap` with links. The app loads the configured font stylesheet from
Google Fonts, which is another third-party request boundary. The operator
must confirm the production font, tile, and provider privacy arrangements
before launch; the Google Fonts policy page was not accessible from the
2026-09-14 sandbox review.

## Catalogue and source data

The backend stores route geometry and derived metrics, source URLs, BikeForum
thread/post URLs, public author names, post dates, source titles, processing
status, and moderation/provenance history. These records are retained as
catalogue and audit data, including when a source later becomes unavailable.
An administrator can soft-delete a route and remove the stored GPX payload;
the source URL, removal reason, timestamps, and audit evidence may remain so a
removal cannot be accidentally re-imported. This metadata retention does not
preserve the removed file.

The crawler uses an identifiable user agent, follows configured origins and
robots policy, and applies request/size/time limits. It does not intentionally
collect private account pages, passwords, or forum message bodies for public
display.

The crawler temporarily persists each fetched page's raw HTML in the
`CrawlResponseCache.body` database field so a recent page can be replayed after
a worker interruption. The approved engineering replay/eligibility deadline
is 24 hours from body acquisition
(`BIKEFORUM_CACHE_BODY_RETENTION_SECONDS=86400`); a validator-only refresh
never extends the body deadline. An hourly bounded Celery cleanup attempts to
clear eligible expired bodies in its next successful batch; backlog or an
outage can delay physical clearing from the live database. It retains only the
source URL, final URL, status, HTTP validators, checksum, and fetch time needed
for conditional requests and crawl diagnostics. The body is not rendered as
public page text, but it can contain forum content and usernames while retained.

## Reports and security controls

An anonymous route report may contain a reason, up to 5,000 characters of
message text, an optional contact email, route ID, and timestamps. Reports are
queued for owner review. Turnstile receives the challenge data when reporting
is enabled; the short-lived token is used for verification and is not stored
in a report. A honeypot field is discarded.

Raw client IP addresses are not stored in the report database. Rate limiting
uses an HMAC-derived identifier held in Redis for at most 24 hours. Access
logs are intentionally privacy-safe. Production Docker logging keeps at most
14 rotated files of 10 MiB per service; this is a size/count cap, not a
14-day guarantee, so elapsed retention depends on traffic and is currently
unknown. Optional Sentry events are scrubbed of request and user payloads,
exception values and frame locals/source context, breadcrumb messages/data,
custom contexts, arbitrary extras, transaction names, and spans. Only
typed event metadata and explicitly allow-listed exception fields, finite
allow-listed breadcrumb metadata, and structurally validated trace/span IDs
are retained;
local-variable capture is disabled at the SDK. Sentry must be configured for
exactly 30 days. Hosted Sentry retention and the actual elapsed proxy/log
retention still require operational evidence.

Closed reports retain their optional email for 90 days after closure. Report
message and decision details are anonymized after 365 days; the daily Celery
task performs these transitions and records an audit event. Reports that have
not been closed are not automatically aged out by this task, so the owner
must close or delete them when there is no ongoing need. The proposed
administrative-audit policy is 24 months from event creation, with a recorded
legal hold for an unresolved incident, rights dispute, or required accounting
record. This policy is not yet implemented: current moderation and report
audit rows have no automatic expiry and must not be represented as compliant
until the maintenance job and migration are verified.

Database and GPX backups also copy the data they contain, including raw crawl
cache HTML created before cleanup, report records, catalogue records, and
private GPX payloads. A body cleared from the live database can therefore remain
in a backup until that backup expires: the host keeps the newest 30 complete
snapshots and the laptop the newest 90 encrypted snapshots. These are counts,
not exact elapsed-day guarantees, and apply after live cleanup. The fixed body
expiry is an application boundary, not a promise of immediate physical
erasure. The operator must securely remove affected
backup artifacts for an urgent request, or record the request's expiry at the
normal backup-retention point.

## Rights and contact

You may request access, correction, or removal of personal information or
source attribution by submitting the [Removal Policy](removal-policy.md) path.
Use the public [issue tracker](https://github.com/diamond447/BikeMapy/issues)
for a first contact, and do not post identity documents or other sensitive
data publicly. A private operator contact, legal basis, controller identity,
and response process must be confirmed before launch. The owner has confirmed
the Czech Republic as governing-law and publication jurisdiction. The
`legal@bikemapy.cz` mailbox and operator identity have not been verified.

The raw HTML cache has a documented 24-hour replay/eligibility deadline. Its
physical live-database clearing is eventual, performed in bounded hourly
batches, and may be delayed by backlog or outage; backups can retain copies.
Provider/operator approval for crawling and the backup propagation process
remain launch gates.
