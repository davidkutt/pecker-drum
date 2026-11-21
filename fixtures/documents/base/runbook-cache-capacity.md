---
title: Runbook - cache capacity alerts
domains: ops, cache
visibility: internal
---
Use this runbook when the cache capacity alert fires (memory above 85 percent for ten minutes).

1. Check the cache dashboard for the node with the highest memory use and the eviction rate.
2. If evictions are rising, raise the tier in the capacity planner and ask the on-call lead for
   approval. Scaling up takes about fifteen minutes.
3. If one tenant causes the growth, open an issue for the owning team with the dashboard snapshot.

Do not restart cache nodes for a capacity alert; a restart drops the warm data and makes the load
on the database worse. Restarts are covered by the cache restart runbook.
