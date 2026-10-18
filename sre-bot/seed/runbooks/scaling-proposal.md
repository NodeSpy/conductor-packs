---
title: Scaling proposals
when_to_use: A service is persistently over- or under-provisioned.
prerequisites:
- at least a week of utilization data
steps:
- Use a week of data at peak and trough, with errors and latency alongside.
- 'Size the change to the service''s headroom target (default: peak at ≤70% of capacity).'
- Propose the exact change with the numbers, the cost delta and how you will verify it.
- After it is applied, check the next peak and record the result.
verification: The next peak stays within the headroom target with no new errors.
rollback: Propose the previous capacity or autoscaling bounds.
owner: the bot's operator
---
# Scaling proposals

You notice a service is persistently over- or under-provisioned.

1. **Use data, not a moment.** At least a week of utilization (CPU, memory,
   request rate, queue depth) at peak and trough, and the error/latency
   picture alongside it.
2. **Size the change.** Target headroom per the service's notes in
   `knowledge/` (default: peak at ≤70% of capacity). Prefer changing the
   autoscaling bounds over fixed counts.
3. **Propose** the exact change (the Terraform plan, or the scaling verb) with
   the numbers, the expected cost delta, and how you will verify it.
4. After it is applied, check the next peak and record the result.
