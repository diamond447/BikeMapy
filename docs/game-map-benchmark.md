# Private competition map benchmark

The private map endpoint is deliberately bounded so a large competition cannot
turn one request into a complete activity export. Production uses the
`ImportedActivity.geometry` spatial index and PostGIS `ST_Intersects`; the
SQLite quality suite uses the same viewport contract with a bounded candidate
window.

## Fixture

The representative fixture is a competition with 80 members, 15,000 eligible
activities, and 250 vertices per activity. Activities are distributed across
Europe, with 1,200 traces intersecting the central-European viewport. The map
request is made at zoom 12 with all members selected.

## Limits and acceptance budget

| Resource | Limit |
| --- | ---: |
| returned traces | 1,200 |
| returned coordinates | 120,000 |
| zoom | 0–22 |
| longitude viewport | at most 120° |

The client renders one GeoJSON source and one line layer. Member colors are
copied into feature properties; trace inspection exposes only the calendar
date. Titles, exact timestamps, speed, provider IDs, and payload metadata are
not part of the response contract.

The benchmark should report median and p95 over at least 30 warm requests,
separately for the PostGIS query and browser render. A run is acceptable when
the API p95 is below 250 ms and the browser adds no more than 100 ms to the
map update after the response arrives. These budgets are targets for the
deployment benchmark; they are not inferred from a single local timing.

The current lightweight sandbox has no reachable PostGIS service (`db`), so a
deployment benchmark result must be collected in the CI or staging database
before release. The reproducible command and the blocked local result are
recorded in [game-map-benchmark-results.json](game-map-benchmark-results.json).
