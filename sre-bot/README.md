# sre-bot

A first-line SRE as a conductor **bot**: a persistent teammate you talk to in
paseo, that receives pages and chat requests, keeps its own memory, delegates to
background workers, and **proposes** every change for you to approve.

This pack is a **bot template**: the parts that are the same for every SRE
bot. You instantiate it into as many bots as you want; each starts in
**practice mode with only its read-only verbs**, and everything above observe
is a widening you grant yourself.

| file | what |
|---|---|
| `conductor-pack.yaml` | the template `sre`: its inputs (pagerduty, aws-cost, slack, cron), the verbs it requests with their maximum tiers, suggested triggers (`page`, `chat-request`, `weekly-review`, `weekly-consolidate`) and proactive checks, a suggested model and runtime |
| `charter.md` | the bot's standing instructions — what it owns, how it acts, what it never does. It lands in the bot's memory repo, where only conductor writes |
| `seed/runbooks/` | typed **runbook** documents: page triage → investigate → escalate; Terraform change → propose → conductor applies; env var change requests; the weekly spend & commitment review; scaling proposals |
| `seed/watch/` | typed **watch** documents: what proactive sweeps watch beyond the checklist — one example to replace |
| `collections/postmortems.yaml` | a **`postmortems` collection** (incident reviews) the template's bots use |
| `bot.example.yaml` | a commented config: the pack instance, a bot from the template, its grants and the parts that stay yours |

**A template only requests.** It cannot set your operators, team,
connector bindings, secrets, autonomy, sandbox or egress. A newer version of
this pack never overwrites a bot's memory: charter and runbook changes arrive
as one proposal — a diff against the memory as the bot evolved it — that you
approve or deny; requested-verb changes show up in `conductor validate` for you
to grant.

Add your own `knowledge/` documents for what a new SRE needs on day one — an
infra map, per-service notes, access (no secrets), escalation — or let the bot
write them as it learns (`conductor bot kinds` shows every schema).

It names no company or system. Each bot has its own memory repo, ledger, home
session, grants and operators, and nothing crosses between bots unless you
link them as peers — a link is a channel for data to cross between them, so
only link bots whose data may mix.

## Requirements

| what | version | status |
|---|---|---|
| conductor | the first release with bots and bot templates (NodeSpy/conductor#172) | **not tagged yet** — `v0.65.0` has neither; the manifest's floor is `>=0.66.0`, to re-check when it ships |
| `pagerduty` plugin — incident **source** (the `page` trigger) | `>=1.0.0` | released |
| `pagerduty` plugin — incident **verbs** (acknowledge / add_note / resolve / escalate) | the release after `v1.1.0` | **not tagged yet** |
| `aws-cost` plugin (spend reads and purchases) | its first release | **not tagged yet** — the `aws` input is optional |
| `slack` plugin — `app_mention` (the `chat-request` trigger) | `>=1.0.0` | released |
| `terraform` plugin (two connectors: conductor plans with one role and applies with the other) | any | released |
| `git.push` (how the bot pushes its Terraform branch) | — | **not released yet** — until then your operator pushes the branch |
| a cron connector with schedules `weekly-review` and `weekly-consolidate`, used by no other trigger | built in | — |

Grant only verbs whose releases exist.

## Install

```yaml
connectors:
  pd:    { use: pagerduty, listen: ":9098", signing_secret: ${PD_SIGNING_SECRET}, api_token: ${PD_TOKEN}, from: oncall-bot@example.com }
  aws:   { use: aws-cost, region: us-east-1 }
  tf-plan:  { use: terraform, env: { AWS_PROFILE: infra-read } }     # conductor plans with this role
  tf-apply: { use: terraform, env: { AWS_PROFILE: infra-deploy } }   # and applies with this one
  slack: { use: slack, app_token: ${SLACK_APP_TOKEN}, bot_token: ${SLACK_BOT_TOKEN} }
  timer: { use: cron, schedules: { weekly-review: { cron: "0 9 * * 1" }, weekly-consolidate: { cron: "0 10 * * 1" } } }

runtimes:
  paseo:      { use: paseo, default: true }
  claude-cli: { use: cli, tool: claude-code }

packs:
  sre: { use: conductor-packs/sre-bot, version: "~> 2.0" }

# operators: — your SSH key, then:
```

```
conductor init
conductor bot templates --search sre          # what it requests, and the packs: block if not installed
conductor bot new platform-sre --from sre/sre # planned on a copy of your config: review the diff
conductor bot new platform-sre --from sre/sre --write
```

`bot new` writes the bot (with its read-only grants) and the template's
suggested triggers, **disarmed** — set `enabled: true` on the ones you want.
Then talk to the bot: it appears in paseo as `bot platform-sre (home)`.

## Calibrate, then promote

A new bot starts in **practice (shadow) mode** with only its read-only verbs:
it triages and writes up what it would do, but nothing above observe executes.
Grant a verb when you want it (`conductor bot grant platform-sre
pagerduty.acknowledge=reversible`, planned on a copy and shown as a diff), review
its practice proposals (`conductor bot proposals platform-sre --all`), and when
its judgment on an action class is consistently right, promote that class
(`autonomy.promote`) and turn practice mode off. Promotion of reversible actions takes effect only once bot
sessions run in conductor's agent sandbox.

Every proposal is approved by you, signed with your SSH key:

```
conductor bot approve platform-sre p12
```

## The workflows

- **Page triage** (`page` → `runbooks/page-triage.md`): an urgent case and a
  worker immediately — acknowledge (if promoted), scope, find what changed,
  mitigate within autonomy, note, escalate per the charter, close out.
- **Terraform** (`runbooks/terraform-change.md`): the bot commits and
  pushes the change, then `conductor bot propose-terraform --repo infra
  --commit <sha> --dir envs/prod`. Conductor plans it with the read role and
  shows you the plan; your signed approval covers that plan's hash, and
  conductor applies exactly that plan with the write role. Plans that destroy
  stateful resources are refused unless you allow the address.
- **Env var changes** (`chat-request` → `runbooks/env-var-change.md`): the
  requester's trust, never more; investigate, then propose the exact change.
- **Spend & commitments** (`weekly-review` → `runbooks/spend-review.md`):
  spend, utilization, coverage, recommendations; a purchase proposal with its
  verified total under the hard spend cap.
- **Scaling** (`runbooks/scaling-proposal.md`): a week of data, a sized change,
  a proposal.
- **Consolidation** (`weekly-consolidate`): a worker summarizes the week's
  case logs into a digest (and `knowledge/`) as reviewable commits; the raw
  logs then leave the working copy, still found by recall.
- **Proactive checks** (opt in with `proactive: {}` on the bot — the
  template's suggested schedule; add a `budget:`): every few hours a read-only sweep works through the
  charter's "Proactive checks" (alerts, unacked incidents, error-rate shifts,
  spend jumps, expiring commitments, stuck deploys, capacity) and
  the `watch/` documents. The gate holds a sweep to observe-tier verbs — a write
  becomes a proposal — and conductor tells you only about findings at
  `notify_at` or above (deduplicated against open cases); a quiet sweep is one
  ledger line. Budget and quiet hours are on the bot. To get findings in
  chat too, add a trigger on conductor's lifecycle event to YOUR config (a
  pack cannot subscribe to `conductor.*` events):

  ```yaml
  triggers:
    - on: conductor.bot_notify
      steps:
        - uses: slack.post
          if: "{{.bot}} == platform-sre"
          options: { channel: C0ALERTS, text: "{{.message}}" }
  ```
- **A team** (commented out at the end of `bot.example.yaml`): a lead and two
  bots that share ceilings (operators, autonomy, budgets with one proactive
  pool, isolation), peer with each other, and share a knowledge repo bots
  propose promotions into. Route work with `bot: { team: platform, … }` in
  your own trigger: a rule picks the bot, else the lead triages read-only
  and runs `conductor bot assign`, else the fallback takes it. The
  assignee works the original request on the original requester's trust.
- **Helpers.** The bot splits work across workers (`spawn --group`, then
  `join`) and sends them follow-ups (`tell`); workers act on the same request's
  trust as the bot, never more.
