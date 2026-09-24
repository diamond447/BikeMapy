# OpenStreetMap alteration offer

BikeMapy stores a bounded, reviewed extract of OpenStreetMap bicycle-route
relations for the private completion catalogue. The source snapshot is kept
byte-for-byte with its SHA-256, endpoint, retrieval time, HTTP metadata, OSM
timestamp, relation/member metadata, and query evidence.

The import is an alteration of the source database: invalid or international
relations are excluded, member ways are validated and assembled into a
LineString, and the accepted relation/version/provenance records are stored
in the `reference_routes` tables. The exact source snapshot and manifest used
for the reviewed fixture are tracked under
`backend/apps/reference_routes/fixtures/`.
The companion `osm-cz-representative-mutations.manifest.json` records the
deterministic labelled rejection/acceptance cases derived from that original
response.

To reconstruct the alteration, start with the manifest-selected raw response,
run `refresh_reference_routes --payload-file ... --manifest ...`, and apply
the recorded candidate diagnostics and publication review. The command is
fail-closed when the manifest hash, selected IDs, relation metadata, complete
way/node counts, HTTP evidence, or payload-derived source timestamp does not
match the raw response. This repository is the complete alteration method and
the fixture bundle is the machine-readable source evidence for the reviewed
candidate.

For every production snapshot used to publish a route, BikeMapy retains the
immutable raw response, its per-snapshot manifest, the exact bounded query or
API request, HTTP evidence, selected relation metadata, complete element
counts, and payload hash. The corresponding normalized output and diagnostic
result are retained in the immutable `reference_routes` import/version
records. The deployable alteration offer must expose the complete snapshot
manifest and the complete method/output needed to reproduce that published
derivative; a representative fixture alone is not a production offer.

Activation and publication are fail-closed unless
`REFERENCE_ROUTE_DERIVATIVE_OFFER_URL` is configured to a non-empty allowed
publication base. The default is empty, so a deployment with no explicit
offer location cannot activate or publish routes. For each valid import, an
operator must export the immutable raw snapshot and manifest, publish the
normalized output and complete alteration method, then run the
`record_alteration_offer` service with the manifest URL, output/artifact URL,
method URL, UTC publication time, exact raw snapshot SHA-256, and operator
evidence. The resulting immutable offer record is tied to that import hash;
stale, incomplete, or mismatched records do not satisfy activation or API
visibility. A new source version requires a new publication record before it
can be approved.

The source notice is © OpenStreetMap contributors. OpenStreetMap data is
available under the Open Database License (ODbL) 1.0:
<https://opendatacommons.org/licenses/odbl/1-0/>. Source notice:
<https://www.openstreetmap.org/copyright>.
