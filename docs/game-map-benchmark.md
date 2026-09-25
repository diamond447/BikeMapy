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
intersections; the other 400 traces are outside it. The fixture records an
explicit recent-history sharing consent for every membership so the benchmark
measures the authorized sharing path. The map request is made at zoom 12 with
all members selected.

## Limits and acceptance budget

| Resource             |        Limit |
| -------------------- | -----------: |
| returned traces      |        1,200 |
| returned coordinates |      120,000 |
| zoom                 |         0–22 |
| longitude viewport   | at most 120° |

Competition list responses expose at most 100 consenting members and set
`members_truncated` for legacy competitions that exceed that page. The client
uses only this bounded page for its initial map request; the map endpoint still
authorizes an explicitly requested member beyond the page without materializing
the rest of the competition.

The client renders one grouped canvas overlay while MapLibre retains navigation
and attribution. Activity geometry remains in the already-authorized response,
but is not copied into a second MapLibre worker source. Before the overlay is
drawn, each visible line is
deterministically tolerance-simplified for the current zoom and bounded by a
2,400-coordinate client render budget; line endpoints and separate line parts
are retained. A click on a visible member line resolves the
nearest activity in that member's authorized geometry. A one-degree,
antimeridian-safe activity index bounds candidate lookup, and pointer bursts
are coalesced to one exact lookup per animation frame, so precise trace
selection is lazy and does not delay the initial render. Member colors are
copied into feature properties; trace inspection exposes only the calendar
date. Titles, exact timestamps, speed, provider IDs, and payload metadata are
not part of the response contract.

The benchmark should report median and p95 over at least 30 warm requests,
separately for the PostGIS query and browser render. At the 1,200-trace
response ceiling, the API budget is 1,500 ms p95: it covers the bounded
intersection, simplification, serialization, and response transfer for the
maximum response, rather than a smaller typical map. A run is acceptable when
the API p95 is below that ceiling and the browser adds no more than 100 ms to
the map update after the response arrives. The API probe performs one
unmeasured warm-up request before collecting its 30 samples. The browser probe
waits for the initial map and then toggles a member filter 30 times on that
same page in an opt-in `benchmark=full-update` mode. Every sample forces a new
authorized map response and measures grouped overlay drawing and one animation
frame. It also records the response-to-source processing stage
(simplification and grouping), the synchronous overlay update, and the final
visible frame in `browser_stages_ms`, so an over-budget run can identify
whether client processing or browser rendering dominates.
The browser probe also performs
30 authorized lazy selections at a known trace coordinate through the same
nearest-activity path used by map clicks, and records the visible trace-detail
render as `browser_lazy_interaction_ms`. This avoids making the lazy timing
depend on canvas hit-raster variability. The normal cached filter path is not
the acceptance measurement; it may be reported separately as a diagnostic.
These budgets are deployment targets tied to the response limits, not
thresholds changed to fit one run.

The reproducible command and the latest local result are recorded in
[game-map-benchmark-results.json](game-map-benchmark-results.json). The
command explicitly targets the local PostGIS service at `127.0.0.1`; its
password and Strava values are benchmark-only local placeholders, not
credentials. Before running it, start PostGIS and Redis with `docker compose
up -d db redis`, apply migrations with the same environment, and start the
backend at `127.0.0.1:8000` using the command's explicit environment
assignments (including `DJANGO_CACHE_URL=redis://127.0.0.1:6379/1`). Start the
frontend at `127.0.0.1:4173` with `VITE_API_URL=http://127.0.0.1:8000`.
The checked-in result was collected against local PostGIS with 30 API samples;
the browser field is added when the optional frontend/backend `--browser-url`
probe is run.

The checked-in result must be replaced after the full-response browser probe is
run at the final implementation head. The browser acceptance value is the
`browser_response_to_render_ms` p95, with a 100 ms budget. Lazy activity
selection is reported separately in `browser_lazy_interaction_ms`; it is not
part of the initial-render budget because it runs only after an explicit map
click. `browser_stages_ms` provides the processing, synchronous overlay update,
and complete render-to-visible timings for the same 30 samples.
