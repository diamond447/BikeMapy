# Private capture API

The private competition capture projection is exposed at
`GET /api/v1/game/competitions/<competition-id>/capture/`. It is available
only to an authenticated member of the active competition. A non-member and a
missing competition intentionally return the same `404 Competition not found`
response, so competition membership cannot be enumerated.

Every request must include a bounded `west`, `south`, `east`, `north`, and
`zoom` viewport. The server applies the viewport only to the returned faces;
the member leaderboard and monthly history are calculated from the complete
competition snapshot. `member` query values are visibility filters, not
authorization or ranking filters. `member=0` is the explicit empty selection.

The response is private and sent with `Cache-Control: private, no-store`.
Faces are capped at 1,200 and the serialized response at four million bytes.
The `truncated` flag is true whenever either cap removed faces and
`returned_face_count` reports the number actually sent. When fixed metadata
alone cannot fit inside the byte limit, the endpoint returns
`413 response_limit`; it never silently serves unbounded geometry. The UI
warns when a 200 response is a partial map.

Capture calculations are immutable fresh generations. `area_m2` is the current
equal-share area, so a face with multiple owners contributes the same face area
divided equally to each owner. Leaderboard ranks are global and stable for the
response, including when a member visibility filter hides faces. Each
`monthly_net_change_m2` row is the signed difference between consecutive fresh
player-area snapshots, bucketed by the Prague-local publication month. The first
fresh snapshot is compared with zero; removals and reassignments therefore
appear as negative values. This history is independent of the requested map
viewport.

The browser acceptance journey measures `capture-response-to-source` completion
and requires it to stay at or below 1,000 ms for the bounded fixture. This is a
non-trivial UI performance guard, not an API latency service-level objective.

Pending and running generations return `pending` while retaining the last
fresh snapshot when one exists. A failed generation returns `failed` with that
last valid snapshot. Only a fresh generation matching the competition revision
sets `is_final` to true. The UI explains the 50-metre connection rule, older
boundary precedence, and equal same-day sharing beside the map.
