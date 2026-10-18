---
what: "(example) db-prod-1 replica lag"
signal: "aws.describe_* or your metrics verb: replication lag on db-prod-1"
threshold: "above 30s for 10 minutes"
why: "a lagging replica serves stale reads and slows failover; replace this example with your own"
---
What your proactive sweeps look at beyond the charter's checklist: one document
per thing to watch. Add one when an incident shows a blind spot; remove it when
it stops mattering.
