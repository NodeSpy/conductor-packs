# Page triage → investigate → escalate

A page arrives as an URGENT case and conductor has already started a worker on
it. The worker follows this; the home session tracks it.

1. **Acknowledge if allowed.** If acknowledging pages is in your autonomy
   (`pagerduty.acknowledge` — usually the first action class an operator
   promotes), acknowledge so the page stops escalating while you look. If it
   comes back as a proposal, say so and keep going — do not wait on it.
2. **Scope it (≤5 minutes).** What service, what symptom, since when, how many
   users. Read the incident (`pagerduty.get_incident`), the service's
   dashboards and recent deploys. Check `knowledge/` for this service's map and
   known failure modes, and `episodes/` for the last time this fired.
3. **Find what changed.** Deploys, config changes, traffic, dependencies,
   certificates, quotas. Most pages have a change behind them.
4. **Mitigate within autonomy.** A rollback, a restart, a scale-up — only if
   your tiers allow it; otherwise propose the exact action with the evidence.
5. **Note as you go.** `pagerduty.add_note` (if allowed) and
   `conductor member case note <case>` — short, timestamped facts.
6. **Escalate** per your charter's escalation policy: severity, time without
   mitigation, or low confidence. Escalation carries the investigation so far:
   what you know, what you ruled out, what you would try next.
7. **Close out.** When resolved, report via `step.done` with the cause, what
   was done, and follow-ups. If you learned something about the system, write
   it to `knowledge/` with `conductor member remember`.
