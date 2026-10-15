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
