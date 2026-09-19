# Legal and attribution review

**Review date:** 2026-09-06  
**Status:** launch gate; not legal advice

This is a product and engineering record of the sources checked for BikeMapy.
It is not a legal opinion and does not replace permission from a rights holder.
The maintainer must repeat this review before enabling a new data source,
changing the map provider, or publishing GPX files.

The separate [Strava API and cross-member display review](strava-api-review.md)
records the 2026-09-19 primary-source review for the future private game. Its
current decision is to keep cross-member Strava data and derived results
disabled; this document must not be read as permission to enable that module.

## Outcomes

| Material or provider      | Source checked                                                                                                                                                     | Dated finding and implementation outcome                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| BikeForum (bike-forum.cz) | [Rules and terms](https://www.bike-forum.cz/podminky-uziti), [robots.txt](https://www.bike-forum.cz/robots.txt), retrieved 2026-09-06                              | The terms page says user submissions remain subject to authors' rights, asks for author and Bike-forum.cz attribution, prohibits infringement, and prohibits automated requests that unreasonably burden the service. The page identifies its operator and states that its terms version applies from 2020-11-02. `robots.txt` disallows selected account/report actions but does not itself grant a content licence. BikeMapy therefore uses an identifiable crawler, follows the configured origin and robots policy, rate-limits requests, stores source URLs/post attribution, and does not treat public visibility as permission to republish post text or images. Each fetched page is also persisted as raw HTML in `CrawlResponseCache.body` with no implemented expiry, including in database backups. The legality and retention period for this cache, the planned crawl, and derived route data remain unresolved. |
| Mapy.com                  | [Mapy.com terms](https://mapy.com/en/terms), [licensing page](https://licence.mapy.cz/?doc=mapy_pu&lang=en), retrieved 2026-09-06                                  | Mapy.com publishes its own service terms and licensing conditions. BikeMapy currently fetches GPX through an unofficial, replaceable adapter constrained to HTTPS Mapy hosts; this is not a claim that Mapy.com authorizes automated access, scraping, or redistribution. BikeMapy links to the canonical Mapy URL and names Mapy.com as an independent source. API, exporter, and commercial-use permission must be confirmed with the provider before launch.                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| OpenFreeMap               | [OpenFreeMap project](https://openfreemap.org/), [OpenFreeMap repository](https://github.com/openfreemap/openfreemap), retrieved 2026-09-06                        | The configured Liberty style is served from `tiles.openfreemap.org`. OpenFreeMap describes its service as based on OpenStreetMap data and requires the applicable attribution; it does not provide BikeMapy with a general licence to copy or rehost the tiles. BikeMapy uses the hosted style, keeps attribution visible in the map control and app footer, and must re-check provider availability, usage limits, and terms before a production deployment.                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| OpenStreetMap and ODbL    | [OSM copyright and licence notice](https://www.openstreetmap.org/copyright), [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/), retrieved 2026-09-06      | OpenStreetMap data is available under the Open Database License (ODbL), with attribution and notice obligations. BikeMapy does not claim that its route catalogue is an OSM-derived database; the current map style uses OSM-backed tiles. The app displays “© OpenStreetMap contributors” with a link to the OSM notice. Any future extraction, combination, or publication of OSM data requires a separate ODbL database/produced-work review.                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| GPX and route rights      | [BikeForum terms, section 6](https://www.bike-forum.cz/podminky-uziti#6), [Mapy.com licensing](https://licence.mapy.cz/?doc=mapy_pu&lang=en), retrieved 2026-09-06 | A shared Mapy link is not proof that the person who posted it owns or may redistribute the underlying track. A GPX file can contain copyrightable route description, personal data, or other protected material. BikeMapy makes the conservative decision **not to offer public GPX downloads at launch**. Raw payloads may be fetched and held in private storage for bounded processing and moderation, but this internal capability is not permission to publish. `GPX_REDISTRIBUTION_APPROVED=false` remains the default and the API returns 404 until a documented affirmative approval changes the deployment gate.                                                                                                                                                                                                                                                                                                      |
| Name and affiliation      | [Mapy.com terms](https://mapy.com/en/terms), retrieved 2026-09-06                                                                                                  | BikeMapy is an independent project and is not Mapy.com, Seznam.cz, MTBIKER, Bike-forum.cz, OpenFreeMap, or OpenStreetMap. The product uses “BikeMapy” only as its own project name, labels Mapy.com as a source, links to the provider, and does not use the Mapy.com logo or imply sponsorship, endorsement, or affiliation. A trademark/name clearance for the project name is still a launch blocker.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |

## Required attribution

The map must keep a readable, clickable notice that includes OpenFreeMap and
OpenStreetMap contributors. The default implementation provides HTML links to
both notices through MapLibre's sanitized attribution control and repeats the
same links in the application footer. A deployment override of
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

## Launch blockers

The following questions are intentionally unresolved and must not be silently
assumed away:

1. Obtain a written or otherwise authoritative basis for crawling and
   retaining BikeForum-linked route/GPX material, including how an author can
   object to derived geometry.
2. Confirm that the Mapy.com terms and licensing permit the selected exporter,
   server-side fetching, private retention, and any future redistribution.
3. Identify the operator, legal contact, governing law, and publication
   jurisdiction for the Terms and Privacy notice.
4. Complete a trademark/name clearance for “BikeMapy” and confirm the
   OpenFreeMap production arrangement, limits, and attribution wording.
5. Confirm that the documented 24-hour retention/deletion policy for raw
   `CrawlResponseCache.body` HTML is lawful and verify its removal from live
   database and backup copies.
6. Confirm the hosted Sentry retention setting (the app requires 30 days) and
   the deployment's proxy/log retention before collecting production reports.

Until these questions are answered and recorded, do not call the service a
public launch and do not set `GPX_REDISTRIBUTION_APPROVED=true`.
