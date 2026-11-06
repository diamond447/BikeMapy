# BikeMapy Privacy Notice

**Status:** draft; it takes effect when the service launches and the operator
is named here.

## Visiting the site

BikeMapy has no user accounts, advertising or tracking cookies. The browser
stores your language, filters and selected route in `localStorage`; this stays
on your device. Searches and map moves send the filters, map area and route
ID to the BikeMapy API so the page can show results.

The site counts three events per day in total, without any route, URL,
session, address or browser details: route-detail views, GPX download clicks
and original-source clicks. If enabled, Cloudflare Web Analytics measures
visits without cookies or fingerprinting; Cloudflare states that it keeps raw
beacon data for seven days.

Map tiles and styles are loaded from OpenFreeMap and fonts from Google Fonts,
so those providers receive your IP address under their own terms.

## Route and source data

The catalogue stores route geometry and metrics, source and forum post URLs,
public author names, post dates and the moderation history. Removing a route
deletes its stored GPX file but keeps the source URL, reason and audit record,
so it is not imported again.

The crawler identifies itself, follows `robots.txt` and fetches only public
pages. Raw page HTML is kept for at most 24 hours (it can contain forum text
and usernames) and is then cleared by an hourly job; the URL, status and
caching headers remain.

## Reports

A route report contains the reason, an optional message of up to 5,000
characters, an optional contact e-mail, the route and timestamps. Turnstile
checks the submission; its token is not stored. Raw IP addresses are not
stored: rate limiting uses an HMAC-derived identifier kept in Redis for at
most 24 hours. The contact e-mail is deleted 90 days after a report is closed
and the report details are anonymized after 365 days.

## Logs, errors and backups

Access logs do not contain IP addresses. Application logs are rotated by size
(at most 14 files of 10 MiB per service). Optional Sentry error reports
contain no request, user or local-variable data and are kept for 30 days.

Backups copy the database and GPX files, including data that was deleted from
the live service afterwards. The host keeps the newest 30 daily snapshots and
an encrypted offline copy the newest 90.

## Your requests

To ask for access, correction or removal of information about you, follow
the [Removal Policy](removal-policy.md). Do not post identity documents or
other sensitive data in a public issue.
