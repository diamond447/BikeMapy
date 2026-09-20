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

To reconstruct the alteration, start with the manifest-selected raw response,
run `refresh_reference_routes --payload-file ... --manifest ...`, and apply
the recorded candidate diagnostics and publication review. The command is
fail-closed when the manifest hash, selected IDs, HTTP evidence, or source
timestamp does not match the raw response. This repository is the alteration
method and the fixture bundle is the machine-readable source evidence.

The source notice is © OpenStreetMap contributors. OpenStreetMap data is
available under the Open Database License (ODbL) 1.0:
<https://opendatacommons.org/licenses/odbl/1-0/>. Source notice:
<https://www.openstreetmap.org/copyright>.
