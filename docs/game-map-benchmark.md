# Private competition map benchmark

The private map endpoint is deliberately bounded so a large competition cannot
turn one request into a complete activity export. Production uses the
`ImportedActivity.geometry` spatial index and PostGIS `ST_Intersects`; the
SQLite quality suite uses the same viewport contract with a bounded candidate
window.

## Fixture

The runnable fixture creates a competition with 80 members, 1,600 eligible
activities (20 per member), and 250 vertices per activity. Fifteen activities
per member are placed in the central-European viewport, giving exactly 1,200
intersections; the other 400 traces are outside it. The map request is made at
zoom 12 with all members selected.

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
separately for the PostGIS query and browser render. At the 1,200-trace
response ceiling, the API budget is 1,500 ms p95: it covers the bounded
intersection, simplification, serialization, and response transfer for the
maximum response, rather than a smaller typical map. A run is acceptable when
the API p95 is below that ceiling and the browser adds no more than 100 ms to
the GeoJSON source render after the response arrives. The API probe performs
one unmeasured warm-up request before collecting its 30 samples. The browser
probe waits
for the initial map and then toggles a member filter 30 times on that same
page. Each sample waits for MapLibre's `private-traces` source to finish
loading and one animation frame, so unrelated style/tile loading and later
viewport requests are not included. These budgets are deployment targets tied
to the response limits, not thresholds changed to fit one run.

The reproducible command and the latest local result are recorded in
[game-map-benchmark-results.json](game-map-benchmark-results.json). The
checked-in result was collected against local PostGIS with 30 API and browser
samples.
