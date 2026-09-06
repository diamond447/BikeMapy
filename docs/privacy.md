# BikeMapy Privacy Notice

**Last reviewed:** 2026-09-06  
**Status:** launch draft — operator identity, legal basis, and jurisdiction
require legal review

This notice describes the current repository behavior. It is not legal advice
and is not a substitute for the final notice naming the operator and lawful
bases for processing.

## What the public site does

The public catalogue has no user account, profile, advertising, marketing
cookie, browser fingerprint, or non-essential analytics service. The frontend
stores the selected language, filters, selected route, and viewport preference
in the browser's `localStorage`; this stays on the device and is not sent as a
personal profile. Search and map requests send the selected filters, map view,
and route identifier to the BikeMapy API so the requested feature can work.

MapLibre loads the configured OpenFreeMap style and tiles. Those requests go
to the map provider and may expose the visitor's network address to that
provider under its terms. The app also loads the configured font stylesheet
from Google Fonts. The operator must confirm the production font and tile
arrangements before launch.

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

The crawler also persists each fetched page's raw HTML in the
`CrawlResponseCache.body` database field, together with its source URL, final
URL, status, validators, checksum, and fetch time. The cache has no implemented
expiry or cleanup task, so raw HTML (which can contain forum post content and
usernames) remains in the database indefinitely unless an operator deletes it
or removes the database. It is not rendered as public page text, but it is
still collected and retained personal/content data.

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
unknown. Optional Sentry events are
scrubbed of request headers, bodies, query strings, IP addresses, email, and
secret-like values and must be configured for exactly 30 days. Hosted Sentry
retention and the actual elapsed proxy/log retention still require operational
evidence.

Closed reports retain their optional email for 90 days after closure. Report
message and decision details are anonymized after 365 days; the daily Celery
task performs these transitions and records an audit event. Reports that have
not been closed are not automatically aged out by this task, so the owner
must close or delete them when there is no ongoing need. Administrative audit
records currently have no implemented automatic expiry; this is a launch
blocker, not an omitted promise.

Database and GPX backups also copy the data they contain, including raw crawl
cache HTML, report records, catalogue records, and private GPX payloads. The
host cleanup task keeps the newest 30 complete snapshots and the laptop keeps
the newest 90 encrypted snapshots. These are counts rather than elapsed-day
limits; actual backup retention depends on scheduling and operator cleanup.
Backup copies therefore remain relevant to any deletion request until their
retention point passes or the operator securely removes them.

## Rights and contact

You may request access, correction, or removal of personal information or
source attribution by submitting the [Removal Policy](removal-policy.md) path.
Use the public [issue tracker](https://github.com/diamond447/BikeMapy/issues)
for a first contact, and do not post identity documents or other sensitive
data publicly. A private operator contact, legal basis, controller identity,
and response process must be confirmed before launch.

The raw HTML cache's indefinite retention and the absence of an implemented
audit/cache expiry policy must also be resolved before launch. No retention
period is assumed for either store.
