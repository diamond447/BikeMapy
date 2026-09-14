# Legal and attribution review

**Review date:** 2026-09-14
**Status:** launch gate; not legal advice

This is a product and engineering record of the sources checked for BikeMapy.
It is not a legal opinion and does not replace permission from a rights holder.
The maintainer must repeat this review before enabling a new data source,
changing the map provider, or publishing GPX files.

## Outcomes

| Material or provider      | Source checked                                                                                                                                                     | Dated finding and implementation outcome                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| BikeForum (bike-forum.cz) | [Rules and terms](https://www.bike-forum.cz/podminky-uziti), [robots.txt](https://www.bike-forum.cz/robots.txt), retrieved 2026-09-14 | The accessible terms identify MTBIKER community s.r.o. as provider, state that the version applies from 2020-11-02, require author and Bike-forum.cz attribution when sharing forum material, prohibit infringement, and prohibit automated requests that unreasonably burden the service. Sections 5.8 and 6.1–6.3 restrict copying/communication of protected works and grant a licence to the BikeForum provider, not automatically to third parties. `robots.txt` disallows selected account/report actions but does not grant a content licence. BikeMapy therefore uses an identifiable crawler, follows the configured origin and robots policy, rate-limits requests, stores source URLs/post attribution, and does not treat public visibility as permission to republish post text or images. Raw HTML is eligible for replay only for 24 hours from acquisition; physical live-DB clearing is attempted in bounded hourly batches and can be delayed by backlog or outage, while backups can retain copies. Written permission or another authoritative rights basis for crawling, deriving routes, and enabling GPX remains unresolved. |
| Mapy.com                  | [Mapy.com terms](https://mapy.com/en/terms), [licensing page](https://licence.mapy.cz/?doc=mapy_pu&lang=en), retrieved 2026-09-14 | The current `mapy_pu` licence document is reachable through `pro.mapy.cz`. Part III, section 7 expressly prohibits redistribution or creation of similar/derived services unless determined otherwise by the Operator or agreed in writing; storage, archiving, or access provision (caching) unless explicitly approved; extracting data from the Service (scraping), including indexing, sharing, pre-caching, saving, exporting, and mass downloading; use to create or improve other datasets; and reverse engineering aimed at obtaining code or data. The unofficial GPX exporter is BikeMapy's own code, but that does not override these service terms. Provider approval or a separately applicable official developer licence is required before server-side fetching, retention, or public GPX redistribution. |
| OpenFreeMap               | [OpenFreeMap project](https://openfreemap.org/), [Terms of Service](https://openfreemap.org/tos/), [privacy policy](https://openfreemap.org/privacy/), [repository](https://github.com/hyperknot/openfreemap), retrieved 2026-09-14 | The public site says its hosted instance is free, has no request limits or API keys, and permits commercial use; it requires the attribution `OpenFreeMap © OpenMapTiles Data from OpenStreetMap` and provides no SLA guarantee. Its Terms of Service, updated September 9, 2026, identify Hyperknot Software Kft. in Hungary, prohibit automated collection without permission, and apply Hungarian law. BikeMapy now keeps the linked OpenFreeMap/OpenMapTiles/OpenStreetMap attribution visible but must record the production style URL and accept the provider's no-SLA/availability risk before launch. |
| OpenStreetMap and ODbL    | [OSM copyright and licence notice](https://www.openstreetmap.org/copyright), [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/), retrieved 2026-09-14      | OpenStreetMap data is available under the Open Database License (ODbL), with attribution and notice obligations. BikeMapy does not claim that its route catalogue is an OSM-derived database; the current map style uses OSM-backed tiles. The app displays the provider wording “Data from OpenStreetMap” with a link to the OSM notice, alongside OpenFreeMap and OpenMapTiles attribution. Any future extraction, combination, or publication of OSM data requires a separate ODbL database/produced-work review. |
| GPX and route rights      | [BikeForum terms, section 6](https://www.bike-forum.cz/podminky-uziti#6), [Mapy.com licensing](https://licence.mapy.cz/?doc=mapy_pu&lang=en), retrieved 2026-09-14 | A shared Mapy link is not proof that the person who posted it owns or may redistribute the underlying track. A GPX file can contain copyrightable route description, personal data, or other protected material. The owner has stated an intention to launch with every product feature enabled, including GPX, but no permission or rights basis has been supplied. `GPX_REDISTRIBUTION_APPROVED=false` therefore remains the safe technical default and the API returns 404 until affirmative provider/rights evidence and owner approval are recorded. |
| Name and affiliation      | [Mapy.com terms](https://mapy.com/en/terms), retrieved 2026-09-14                                                                                                  | BikeMapy is an independent project and is not Mapy.com, Seznam.cz, MTBIKER, Bike-forum.cz, OpenFreeMap, or OpenStreetMap. The product uses “BikeMapy” only as its own project name, labels Mapy.com as a source, links to the provider, and does not use the Mapy.com logo or imply sponsorship, endorsement, or affiliation. No Czech Industrial Property Office or EUIPO search was possible from this environment, so “BikeMapy” name/trademark clearance remains an owner/counsel action. |
| Google Fonts             | [Google Fonts FAQ](https://developers.google.com/fonts/faq), retrieved 2026-09-14 | The configured stylesheet is a third-party request boundary. The Google Developers page returned an upstream 403 in this environment, so no current vendor statement is asserted here. The owner must either record the current Google Fonts terms/privacy basis for the chosen font or self-host an appropriately licensed font before relying on this provider at launch. |
| Cloudflare Web Analytics and Turnstile | [Web Analytics about](https://developers.cloudflare.com/web-analytics/about/), [FAQ](https://developers.cloudflare.com/web-analytics/faq/), [Turnstile Privacy Policy](https://www.cloudflare.com/turnstile-privacy-policy/), retrieved 2026-09-14 | Cloudflare's accessible Web Analytics FAQ states that dashboard data covers the previous six months and describes the product's privacy/measurement behavior; the exact production account settings and controller/processor arrangement were not inspected. Cloudflare's Turnstile documentation links to the cited privacy policy, but that policy returned a sandbox 403 and was not reviewed here. Enabling both requires production account evidence and final privacy wording. |
| Sentry                    | [Sentry legal documentation](https://sentry.io/legal/dpa/), retrieved 2026-09-14 | Sentry's legal host returned an upstream 403 in this environment. The application requires `SENTRY_RETENTION_DAYS=30` and scrubs event fields before submission, but hosted retention, data-region, DPA, and account settings remain unverified. Do not treat the application setting as proof of hosted deletion. |

## Required attribution

The map must keep a readable, clickable notice using the provider wording
`OpenFreeMap © OpenMapTiles Data from OpenStreetMap`. The default
implementation provides HTML links to all three notices through MapLibre's
sanitized attribution control and repeats the same links in the application
footer. A deployment override of
`VITE_MAP_ATTRIBUTION` must preserve equivalent links.
Source entries link to the canonical Mapy.com URL and BikeForum post URL, and
show the source status and check date when available. Attribution must remain
with any future screenshots, exports, or map-derived products.

## Crawler response-cache decision

For the narrow operational purpose of replaying a recently fetched page after
a worker interruption and completing a conditional HTTP request, the approved
engineering retention boundary for raw `CrawlResponseCache.body` is 24 hours
from body acquisition. After that deadline the body is ineligible for replay;
Celery Beat physically clears eligible bodies in the next successful hourly
bounded batch (a backlog or outage can delay that live-DB operation) while
retaining URL, redirect, status, validators, checksum,
and fetch time as crawler metadata. Database backups can retain a pre-cleanup
body until their documented 30-snapshot host or 90-snapshot encrypted-laptop
expiry. This engineering decision does not itself grant a content licence or
resolve the provider's terms; `BIKEFORUM_CRAWL_ENABLED=false`,
`BIKEFORUM_PROVIDER_AUTHORIZED=false`, and
`BIKEFORUM_OPERATOR_APPROVED=false` remain required until provider permission
and operator approval are recorded.

## Retention decision for administrative audits

The proposed launch policy is to retain non-content administrative and
moderation audit events for 24 months from creation. The policy covers the
actor, action, target identifier, timestamp, reason, and before/after metadata
needed to explain a moderation or rights decision; it excludes report message
text, contact email, raw HTML, and GPX payloads. A legal hold, unresolved
security incident, active rights dispute, or required accounting record may
temporarily suspend expiry and must be recorded with an owner and review date.
Otherwise, a monthly maintenance job should delete expired audit rows and
record only an aggregate maintenance result. This is a documented target, not
an implemented expiry: the current `ModerationDecision` and `ReportAudit`
records have no automatic deletion task, so implementation and a migration
safe against protected relationships remain part of the launch work.

## Evidence and access notes

The 2026-09-14 review distinguishes vendor-published statements from owner
assertions and production evidence. BikeForum, the current Mapy.com terms and
licensing document, OpenFreeMap, OSM, and Cloudflare Web Analytics pages were
reachable. The Google Fonts FAQ, Sentry legal pages, Cloudflare's Turnstile
Privacy Policy, Czech Industrial Property Office search, and EUIPO search were
blocked or returned an upstream 403/404 in this sandbox. A reachable page is
not permission to crawl, export, or republish its content. The Mapy.com
document is stronger than a mere absence of permission: it expressly prohibits
the relevant scraping, storage, export, and derived-service uses unless the
Operator approves them. The owner must attach provider communications or an
applicable official developer licence, the trademark search result, and the
production settings to #141 before enabling the corresponding gates.

## Launch blockers

The following questions are intentionally unresolved and must not be silently
assumed away:

1. Obtain a written or otherwise authoritative basis for crawling and
   retaining BikeForum-linked route/GPX material, including how an author can
   object to derived geometry.
2. Obtain Mapy/Seznam approval or an applicable official developer licence for
   the selected exporter, server-side fetching, private retention, and any
   future redistribution. The current Mapy.com terms expressly prohibit these
   scraping/export/storage/derived-service uses without the required approval.
3. Identify the operator, legal contact, and lawful basis for the Terms and
   Privacy notice. The owner has confirmed the Czech Republic as governing law
   and publication jurisdiction. The operator identity and
   `legal@bikemapy.cz` mailbox ownership remain unconfirmed.
4. Complete a trademark/name clearance for “BikeMapy”; confirm the Google
   Fonts arrangement; and record the OpenFreeMap production style, attribution,
   limits, and no-SLA acceptance.
5. Confirm that the documented 24-hour replay-eligibility deadline for raw
   `CrawlResponseCache.body` HTML is lawful; verify eventual bounded cleanup
   from the live database and confirm backup expiry or documented removal of
   affected copies.
6. Implement and verify the proposed 24-month administrative-audit expiry,
   confirm the hosted Sentry retention setting (the app requires 30 days), and
   confirm the deployment's proxy/log retention before collecting production
   reports.

Until these questions are answered and recorded, do not call the service a
public launch and do not set `GPX_REDISTRIBUTION_APPROVED=true`.
