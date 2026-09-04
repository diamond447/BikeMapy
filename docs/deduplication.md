# Route similarity and duplicate policy

BikeMapy compares valid route versions using a distance-resampled polyline.
Resampling makes point density irrelevant and the comparison checks both
directions. Closed loops are additionally tested across every cyclic start
offset, so a route recorded from a different start point remains equivalent.
The evidence records the selected direction, offset, mean and maximum spatial
distance, route lengths, and the policy values used for the decision.

The checked-in benchmark fixture contains representative pairs and is run as
part of the catalogue tests. With the default policy, it measured:

| Case | Score | Length delta | Mean distance | Outcome |
| --- | ---: | ---: | ---: | --- |
| GPS-noise duplicate | 0.9220 | 0.0138 | 8.1 m | duplicate |
| Different point-density duplicate | 0.9524 | 0.0052 | 5.2 m | duplicate |
| Reversed point-to-point recording | 1.0000 | 0.0000 | 0.0 m | duplicate |
| Shifted loop start | 1.0000 | 0.0000 | 0.0 m | duplicate |
| Near-threshold non-duplicate | 0.7374 | 0.0718 | 27.6 m | variant |
| Meaningful detour | 0.7024 | 0.0733 | 33.3 m | variant |
| Meaningful extension | 0.6703 | 0.0665 | 39.8 m | variant |
| Nearby parallel distinct route | 0.0512 | 0.0000 | 356.8 m | unrelated |
| Unrelated route | 0.0000 | 0.0080 | 185,561.7 m | unrelated |

These measured results support the deliberately conservative defaults:

* a suspected duplicate may differ by at most 10% in length, 25 m mean
  alignment distance, and 100 m maximum alignment distance;
* a non-duplicate with a score of at least 0.55 is linked as a public variant;
* the score combines `exp(-mean_alignment_distance / (4 × GPS_tolerance))`
  and the length similarity. GPS tolerance defaults to 30 m.

They are configured through `ROUTE_*` environment variables and should be
re-measured against local routes before changing the automatic quarantine
policy. A meaningful extension or shortcut therefore remains a separately
published variant. Only the high-confidence duplicate class is automatically
quarantined; its payload is removed through the durable deletion queue, which
is dispatched after commit and retried every 15 minutes by Celery Beat, while
its source URL, checksum, similarity evidence, reason, and moderation audit
remain.

Moderation actions are transactional and append audit decisions. A source
merge creates an explicit provenance alias exposed through
`Route.provenance_sources`, retains the original immutable source ownership,
and quarantines the consumed duplicate route (including payload cleanup), so
reprocessing cannot rewrite historical source identity. Keep-both changes the
pair to a variant and restores a quarantined valid route before recording the
decision for both public routes.
