# Reference-route source decision

**Decision date:** 2026-09-19
**Scope:** Via Czechia routes and stages, and numbered Czech cycling routes
**Status:** OSM numbered-route import is conditionally approved; Via Czechia
and other proprietary mirrors are blocked pending permission. This is an
engineering and provenance record, not legal advice.

This decision is required before issue #53 imports a reference route. A route
being visible on a website or in a map is not evidence that BikeMapy may copy,
transform, store, or redistribute it.

## Dated source decisions

| Source and coverage | Primary evidence checked (retrieved 2026-09-19) | Ownership, licence, and obligations | Decision |
| --- | --- | --- | --- |
| OpenStreetMap (OSM) `type=route`, `route=bicycle` relations in Czechia, with a route reference (`ref`) and a supported network tag | [OSM copyright and licence notice](https://www.openstreetmap.org/copyright); [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/); [cycle-route tagging](https://wiki.openstreetmap.org/wiki/Tag:route%3Dbicycle); [OSM API v0.6 relation documentation](https://wiki.openstreetmap.org/wiki/API_v0.6#Relations); [Overpass API](https://overpass-api.de/api/interpreter); and the [Overpass public-instance guidance](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html). | OSMF licenses the OSM database under ODbL on behalf of its contributors. The ODbL requires OSM attribution and a notice that the data is available under ODbL. A database made by extracting or adapting a substantial part is a derivative database. Its public use must be considered under ODbL sections 4.4(c) and 4.6 when a Produced Work is generated from it: retain notices, share alike, and offer the machine-readable derivative database or alteration file/method. Individual contents can carry other rights, so route names, trademarks, and operator claims still need review. | **Conditionally approved for numbered-route candidates.** It is a licensed, technically reproducible source, but it is community-maintained and is not proof that a route is currently signed or an authoritative complete inventory. Require `operator`, `network`, `ref`, Czech geographic coverage, and a human review before publishing. Respect the Overpass/API policies below; do not treat a tile or rendered map as the import source. |
| Club of Czech Tourists (KČT) route planner and route-marking information, used as an authority cross-check | [KČT route planner](https://trasy.kct.cz/); [KČT](https://www.kct.cz/). | The planner is a useful official-looking map and search interface, but the checked pages do not state a reusable data licence or provide an approved machine-readable export. A route planner view is not permission to scrape or copy its geometry. | **Not approved as an ingestion source.** Use it to check a candidate's operator/reference and to contact KČT for a written licence or export agreement. Until that is obtained, OSM is the only importable numbered-route source in this decision. |
| Via Czechia official site: six Czech route families, cycling variants, parent routes, and stages | [Via Czechia home](https://viaczechia.cz/); [route pages](https://viaczechia.cz/severni-stezka/); [online maps and GPX page](https://viaczechia.cz/online-mapy/); [contact/licence footer](https://viaczechia.cz/kontakt/). | The site identifies its content as authored by Jan Hocek and displays **CC BY-NC-SA 4.0**; it also says “Via Czechia” is a registered trademark. The online-maps page says GPX links are updated approximately once a year, links to Mapy.cz navigation, and warns that navigation can differ from the actual Via Czechia route. It reports that walking routes are in OSM with codes such as `VIA-CZE-101`, which does not establish a licence for the cycling GPX or for a BikeMapy database. | **Blocked.** The footer licence is not an affirmative, route-data-specific permission for BikeMapy to extract, normalize, retain, publish, or use the geometry in a public completion game. CC BY-NC-SA's non-commercial and share-alike conditions, the separate trademark, and the Mapy.cz links require a rights-holder decision. Ask Via Czechia/Jan Hocek for written permission covering route/stage GPX, derivative geometry, database/API display, attribution wording, updates, and redistribution. Do not scrape or import while this is unresolved. |
| Mapy.cz links and saved maps linked by Via Czechia | [Via Czechia online-maps page](https://viaczechia.cz/online-mapy/); [Mapy.com terms](https://mapy.com/en/terms); [Mapy licence page](https://licence.mapy.cz/?doc=mapy_pu&lang=en). | Mapy.cz is a separate provider. A link, public map, or route visible in its UI does not grant a server-side export or redistribution right to BikeMapy. The Via Czechia page itself warns that Mapy.cz may re-plan around closures. | **Not an import source.** Keep a canonical link only as provenance after Via Czechia permission is obtained; do not use Mapy.cz as a substitute for the original route data. |

### What the OSM approval does and does not mean

The approved OSM candidate query is deliberately narrow: a Czechia-covered
relation with `type=route`, `route=bicycle`, a non-empty `ref`, and one of the
documented `network` values (`lcn`, `rcn`, or `ncn`). `network=icn` is excluded,
as are `EuroVelo`/international-network relations, because international
catalogues are outside this issue's scope. The importer must retain the
relation ID, every member way/node ID, tags, member roles, source URL, and the
retrieval timestamp. A `ref` such as “1” is not a global identity:
the relation ID, operator/network, and collection context are required.

OSM relation membership can contain alternatives, superroutes, unordered ways,
or incomplete data. “Conditionally approved” therefore means that a candidate
is eligible for validation, not that its geometry is automatically correct or
that KČT endorses the import. Candidates with an unknown operator, missing
members, an unresolved parent relation, or a route that fails the validation
sample remain inactive.

### OSM discovery, download, and snapshot

Discovery uses one bounded request to the public
[Overpass interpreter](https://overpass-api.de/api/interpreter), not a map tile
or a rendered route. The importer records this exact query (including the
retrieval date) and rejects any result whose `network` is `icn`, whose `ref`
matches an international/EuroVelo catalogue, or whose relation is not a Czech
numbered candidate:

```text
[out:json][timeout:90];
area["ISO3166-1"="CZ"]["admin_level"="2"]->.cz;
relation["type"="route"]["route"="bicycle"]["ref"]
  ["network"~"^(lcn|rcn|ncn)$"](area.cz);
out body;
>>;
out geom;
```

The response includes each selected relation and recursively fetched member
relations, ways, and nodes, so it contains the full member geometry rather
than only a rendered line. Store the unmodified JSON response, query text,
endpoint, HTTP metadata, Overpass `osm3s.timestamp_osm_base` when present, and
SHA-256. Also store the native OSM `id`, `version`, `changeset`, and
`timestamp` for every relation, way, and node. A response with missing members
or a truncated/failed request is not a source version.

The Overpass public-instance guidance says not to scrape the world or request
elements one by one, gives approximate safety limits of 10,000 requests and
1 GB per day, and recommends an extract for heavier use. BikeMapy therefore
runs at most one cached Czechia discovery/snapshot request per monthly refresh
and a bounded operator-triggered request after a reported change. If the
collection grows beyond those limits, switch to a dated Czech regional
extract after recording its provider and policy; do not increase request rate.
The response's native versions/changesets/timestamps and payload hash are the
update cursor. A changed relation version, changeset, timestamp, member set,
or payload hash creates a new immutable BikeMapy version; unchanged payloads
are idempotent. This is the concrete source and mechanism issue #53 must use.

## Identity, versions, and provenance

The importer must keep immutable source and version records. At minimum each
record contains:

* `source_kind` (`osm`, `via_czechia`, or another explicitly approved kind),
  canonical source URL, rightsholder/contact, licence URI and the exact
  attribution text to display;
* the upstream identifier (`osm relation/<id>` or the Via Czechia canonical
  route slug plus stage/variant number), parent identifier when applicable,
  and the original download/navigation URL;
* for OSM, the native `id`, `version`, `changeset`, and `timestamp` for the
  relation and every member element, plus the exact Overpass query and
  `osm3s.timestamp_osm_base` used to obtain them;
* UTC retrieval time, upstream `ETag`/`Last-Modified` or stated update date,
  importer version, payload SHA-256, source geometry hash, and the diagnostic
  result; and
* active/inactive status, valid-from/valid-to timestamps, and the reason for
  every administrative or upstream change.

For OSM, a relation ID is stable enough to link versions but not immutable:
deleted or split relations create a new version or an explicit alias. For Via
Czechia, use a composite identity such as
`via-czechia:severni-stezka:cycle:101`; never use a Mapy.cz short URL as the
identity. Keep parent and stage rows separate, with the canonical Via page and
GPX URL recorded as provenance. A source payload is versioned by content hash,
not by a guessed date. Unchanged imports are idempotent; a changed payload
creates an immutable version and schedules completion recomputation. Do not
overwrite historical geometry.

The initial OSM refresh may be scheduled monthly, with an operator-triggered
refresh after a reported route change. Discovery and download must use a
provider-approved API or extract and obey its rate and attribution policies.
The importer stores the exact request metadata and query/extract timestamp so
that a later refresh can be compared. Via Czechia's stated approximately
annual GPX update cadence is an observation, not permission or a guarantee.

## Repeatable geometry validation sample

Before enabling an importer, create a fixture bundle from at least one
reviewed candidate in each approved collection. Select the live OSM candidate
deterministically by sorting `(network, ref, relation_id)` and recording the
selected relation IDs and snapshot hashes. For Via Czechia, this bundle is
prepared only after written permission; until then it contains no Via payload.
Run the same bundle on every importer change and on every source-version
change. Each fixture is the original snapshot plus one labelled mutation, so
the expected failure is reproducible without modifying upstream data.

| Fixture | Mutation or case | Required result |
| --- | --- | --- |
| `valid-main` | A reviewed relation with resolved ways, valid coordinates, and a contiguous main member chain | Import as one version; preserve source order and IDs. |
| `missing-member` | Remove one referenced way/node from the snapshot | Reject the version, keep the prior active version, and report the missing IDs. |
| `duplicate-member` | Repeat a way or duplicate a segment in the relation | Reject or quarantine with the duplicate IDs; never silently count the segment twice. |
| `disconnected` | Split the main chain into components beyond the configured endpoint tolerance | Reject as incomplete. A declared `alternative`/stage is a separate child, not an excuse to join components. |
| `wrong-member-order` | For a relation marked `signed_direction=yes`, permute two adjacent members so their endpoints no longer follow the signed chain | Reject and mark inactive; preserve the source member order and report both member IDs. |
| `unordered-member-list` | For a relation with no directional roles, permute connected members while preserving the endpoint graph | Pass after deterministic endpoint-graph assembly; retain the original member order and record the normalization. Member order alone is not invalid for an undirected route. |
| `undirected-way-reversal` | Reverse the node sequence of one member way with a blank relation role and no signed direction | Pass if endpoints still connect and geometry is valid; reversing an undirected way is not inherently invalid. |
| `directional-way-reversal` | Reverse a member way carrying `forward`/`backward` semantics or a signed-direction constraint | Reject and mark inactive; do not normalize away the directional violation. |
| `empty-invalid` | Empty members, fewer than two usable points, non-finite coordinates, self-invalid line, or zero valid length | Reject with an actionable diagnostic and leave the last valid version available. |

The order fixtures are made from the same `valid-main` chain, with the exact
relation tags and member roles recorded in the fixture manifest. `Pass` means a
new active version is created; `reject` means no new version is created; and
`inactive` means the candidate is not eligible while the previous valid
version, if any, remains active. The undirected reversal case must therefore
pass, while the signed-order and directional-reversal cases must reject and
become inactive. This keeps “source order differs” distinct from “geometry is
disconnected or directionally invalid.”

Validation must also check route identity, parent/stage links, coordinate
bounds, line validity, duplicate/zero-length segments, and a bounded maximum
size. Normalization for matching may create a derived geometry hash, but the
original member sequence, payload hash, and diagnostics remain available for
audit. A human reviewer records the source URL, snapshot date, and expected
route/stage count alongside the fixture bundle.

## Attribution and redistribution gate

For an approved OSM record, every public map or route detail must show a
readable link to [OpenStreetMap contributors](https://www.openstreetmap.org/copyright)
and identify the data as available under [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
Treat ODbL section 4.4(c) as a release trigger: a derivative database is
publicly used when a Produced Work made from that derivative database is
publicly used, even when BikeMapy does not hand the database itself to the
viewer. Thus a public completion map, route detail, screenshot, or export
generated from a BikeMapy derivative OSM database must satisfy the section 4.4
licensing and notice conditions, subject to a legal review of the exact
product boundary. Under section 4.6, public use of the derivative database or
of such a Produced Work must also offer recipients a machine-readable copy of
the entire derivative database, or a file/method containing every alteration
needed to reconstruct it. The offer, ODbL URI, notices, and access location
must be recorded in the product documentation and attached to the relevant
map/export where practical. The OSM attribution must travel with screenshots,
exports, and other produced works where the source is used.

Via Czechia attribution, if permission is later granted, must follow the
rights-holder's written wording and include the canonical route/stage link,
author credit, CC BY-NC-SA notice where applicable, and trademark disclaimer.
Its data must remain in a separately licensed source collection; never mix it
into an ODbL derivative database without a documented compatibility review.
Public GPX downloads, bulk database exports, or commercial reuse are disabled
unless the written permission explicitly covers them. Public visibility of a
GPX or map is not permission.

## Conservative fallback and release gate

Until the Via Czechia permission and geometry decision are recorded, the game
must show Via Czechia as `source pending`/unavailable and omit it from
completion totals. It may continue with validated numbered OSM candidates,
with OSM attribution and the ODbL obligations above. It must not scrape,
rehost, or derive a substitute Via Czechia catalogue from Mapy.cz, OSM walking
relations, screenshots, or published maps. A rights-holder response that is
ambiguous, incomplete, or withdrawn returns that source to blocked status and
deactivates its future versions without deleting provenance.

This decision is revisited before enabling issue #53, after a material source
licence/terms change, and at least annually. A future importer PR must link
this record, include the exact source snapshot and validation evidence, and
fail closed when the source gate is not approved.
