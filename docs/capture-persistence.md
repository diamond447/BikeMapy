# Persistent chronological capture projections

Issue #57 promotes the validated topology harness into a durable competition
projection. `apps.accounts.capture_services` rebuilds one bounded generation
from the current eligible activity rows and competition memberships. The
rebuild is deterministic: activities become owner/date traces, the PostGIS
algorithm from issue #55 produces atomic faces, and the resulting WGS84
polygons are stored in an immutable `CaptureCalculation` generation.

Each calculation records the algorithm version, input digest, trace and
coordinate counts, face count, status, timestamps, and error. Exactly one
successful generation is marked current for a competition. A failed or stale
generation remains inspectable while the previous current generation remains
available, so a timeout or invalid geometry cannot publish partial ownership.

`CaptureFace` stores bounded polygon geometry and its effective Prague-local
claim date. `CaptureFaceOwner` stores every current owner; shared faces have
one row per owner and `shared_area_m2` is the equal fraction of the face.
`CapturePlayerArea` stores each player's total currently owned area for the
generation. These rows are replaced only inside the successful projection
transaction.

Activity imports, privacy removals, membership removals, and account deletion
schedule the existing durable recomputation generation. Competition creation
and joining queue a capture-only generation; the capture dispatcher claims it
with the same bounded lease/retry policy. The worker runs the spatial rebuild
outside its lease transaction, then atomically applies the ordinary competition
projection. PostGIS failures are recorded as failed work for bounded
dispatcher retry; SQLite host checks skip spatial execution and retain the
GIS-optional quality suite behavior.

The service intentionally uses a full bounded rebuild for every generation.
This is the production-safe baseline: webhook order cannot alter ownership,
removals cannot leave stale owners, and replay is equivalent to rebuilding from
the current activity set. An optimized incremental cache can be introduced only
after it preserves the same generation digest and current-snapshot semantics.
