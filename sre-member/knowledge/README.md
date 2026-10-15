# Knowledge

Seed this directory of your member's memory repo with what a new SRE would need
on day one; the member keeps it current.

- `infra-map.md` — services, where they run, what they depend on, who owns them.
- `services/<name>.md` — per service: dashboards, deploy process, known failure
  modes, scaling notes, the env vars that matter.
- `access.md` — which connectors/verbs reach which systems (no secrets).
- `escalation.md` — who to escalate to for what, and how.

Copy `runbooks/` from this pack into the memory repo's `runbooks/` the same way;
adapt them to how your organization actually works.
