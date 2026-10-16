# Knowledge

Seed this directory of your bot's memory repo with what a new SRE would need
on day one; the bot keeps it current.

- `infra-map.md` — services, where they run, what they depend on, who owns them.
- `services/<name>.md` — per service: dashboards, deploy process, known failure
  modes, scaling notes, the env vars that matter.
- `access.md` — which connectors/verbs reach which systems (no secrets).
- `escalation.md` — who to escalate to for what, and how.
- `watch.md` — what proactive sweeps should look at beyond the charter's
  checklist (a starting file ships with this pack).

Copy `runbooks/` from this pack into the memory repo's `runbooks/` the same way;
adapt them to how your systems and team actually work.
