---
title: Runbook - database failover
domains: ops, database
visibility: internal
---
Use this runbook when the primary database stops answering health checks.

1. Confirm the alert on the database dashboard; a single failed check is not enough.
2. Announce the failover in the incident channel and name an incident lead.
3. Trigger the failover from the admin console (Database > Replicas > Promote). Promotion takes
   about two minutes.
4. Watch error rates for fifteen minutes, then write a short incident summary.

Failovers are recorded automatically; there is no need to file an issue by hand.
