# sre-bot

A first-line SRE as a conductor **bot**: a persistent teammate you talk to in
paseo, that receives pages and chat requests, keeps its own memory, delegates to
background workers, and **proposes** every change for you to approve.

This pack ships the parts that are the same for every SRE bot:

| file | what |
|---|---|
| `charter.md` | the bot's standing instructions — what it owns, how it acts, what it never does |
| `runbooks/` | typed **runbook** documents (frontmatter: title, when_to_use, prerequisites, steps, verification, rollback, owner): page triage → investigate → escalate; Terraform change → propose → conductor applies; env var change requests; the weekly spend & commitment review; scaling proposals |
| `watch/` | typed **watch** documents (what, signal, threshold, why): what proactive sweeps watch beyond the checklist — one example to replace |
| `collections/postmortems.yaml` | a **`postmortems` collection** (incident reviews: title, incident, severity, date, impact, root_cause, action_items) a bot or team uses with `collections: { postmortems: { from: … } }` |
| `conductor-pack.yaml` | three disarmed triggers that route work into the bot: `page`, `chat_request`, `weekly` (spend review, then memory consolidation) |
| `bot.example.yaml` | a starting `bots:` entry: verbs, autonomy tiers, spend cap, Terraform |

**Seeding.** `seed_dir` copies the pack's typed documents (`runbooks/`, `watch/`)
into a new memory repo; conductor copies only valid documents of a kind and
reports anything else. Add your own `knowledge/` documents (frontmatter: title,
applies_to, evidence, confidence) for what a new SRE needs on day one — an
infra map, per-service notes, access (no secrets), escalation — or let the bot
write them as it learns (`conductor bot kinds` shows every schema).

It names no company or system. Run one pack instance per bot, and define as
many bots as you want, cut any way that suits you (a team, an environment,
a service). Each bot has its own memory repo, ledger, home session, verbs
and operators, and nothing crosses between bots unless you link them as
peers — a link is a channel for data to cross between them, so only link
bots whose data may mix.

## Requirements

| what | version | status |
|---|---|---|
| conductor | the first release with bots (NodeSpy/conductor#172) | **not tagged yet** — `v0.65.0` does not have them; the manifest's floor is `>=0.66.0`, to re-check when #172 ships |
| `pagerduty` plugin — incident **source** (the `page` trigger) | `>=1.0.0` | released |
| `pagerduty` plugin — incident **verbs** (acknowledge / add_note / resolve / escalate, in the bot's verbs) | the release after `v1.1.0` | **not tagged yet** |
| `aws-cost` plugin (spend reads and purchases, in the bot's verbs) | its first release | **not tagged yet** |
| `slack` plugin — `app_mention` (the `chat_request` trigger) | `>=1.0.0` | released |
| `terraform` plugin (two connectors: conductor plans with one role and applies with the other) | any | released |
| `git.push` (how the bot pushes its Terraform branch) | — | **not released yet** — until then your operator pushes the branch |
| a cron connector with a schedule named by `settings.review_schedule`, used by no other trigger | built in | — |

The pack's own triggers need only the released parts. The unreleased verbs are
used by the bot's **verbs**, which live in your `bots:` entry — leave
those verbs out of `skill.verbs` until their releases exist.

## Install

```yaml
connectors:
  pd:    { use: pagerduty, listen: ":9098", signing_secret: ${PD_SIGNING_SECRET}, api_token: ${PD_TOKEN}, from: oncall-bot@example.com }
  aws:   { use: aws-cost, region: us-east-1 }
  tf-plan:  { use: terraform, env: { AWS_PROFILE: infra-read } }     # conductor plans with this role
  tf-apply: { use: terraform, env: { AWS_PROFILE: infra-deploy } }   # and applies with this one
  slack: { use: slack, app_token: ${SLACK_APP_TOKEN}, bot_token: ${SLACK_BOT_TOKEN} }
  timer: { use: cron, schedules: { sre-weekly: { cron: "0 9 * * 1" } } }

runtimes:
  paseo:      { use: paseo, default: true }
  claude-cli: { use: cli, tool: claude-code }

packs:
  platform-sre:
    source: github.com/NodeSpy/conductor-packs//sre-bot
    version: "~> 1.0"
    connectors: { pagerduty: pd, chat: slack, schedule: timer }
    settings: { bot: platform-sre, review_schedule: sre-weekly }
    triggers:
      "*": { enabled: true }

# operators: + bots: — start from bot.example.yaml
```

Then `conductor init`, `conductor pack plan`, `conductor validate`, and talk to
the bot: it appears in paseo as `bot platform-sre (home)`.

## Calibrate, then promote

A new bot starts in **shadow mode**: it triages and writes up what it would
do, but nothing above observe executes. Review its shadow proposals
(`conductor bot proposals platform-sre --all`); when its judgment on an action
class is consistently right, promote that class (`autonomy.promote`) and turn
shadow off. Promotion of reversible actions takes effect only once bot
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
- **Env var changes** (`chat_request` → `runbooks/env-var-change.md`): the
  requester's trust, never more; investigate, then propose the exact change.
- **Spend & commitments** (`weekly` → `runbooks/spend-review.md`):
  spend, utilization, coverage, recommendations; a purchase proposal with its
  verified total under the hard spend cap.
- **Scaling** (`runbooks/scaling-proposal.md`): a week of data, a sized change,
  a proposal.
- **Consolidation** (`weekly`, second step): a worker distils the week's
  log into typed `knowledge/` documents as reviewable commits.
- **Proactive checks** (opt in with `proactive:` on the bot — commented out
  in `bot.example.yaml`): every few hours a read-only sweep works through the
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
