# pr-review-team

A distributable [conductor](https://github.com/NodeSpy/conductor) **pack**:
multi-lens pull-request review with an optional human hand-off, and a
calibration loop that teaches the reviewer from what happened after its
reviews.

On a review request it runs a panel of lens reviewers in parallel
(Architecture, Complexity, Performance, Refactoring, Security, Test quality),
refute-verifies every finding to drop the ones that are positively wrong,
reconciles the survivors deterministically into a single review (one comment per
file:line, blocking-first), then **either auto-posts** the review or **hands the
draft to a human** to approve, edit, or discard. With `auto: true` it always
posts.

```
review_requested ─▶ assess ─▶ review-team (always) ─▶ post      (auto)
                                                    └▶ hand-off  (manual; never with auto: true)
```

The multi-lens review always runs, so the manual branch has a real drafted
review to hand off — the human edits/approves it instead of reviewing from
scratch.

## Install

Bind your github connector and arm the trigger. The pack brings its own
review-team/assess/hand-off steps, so you don't define any agents:

```yaml
packs:
  review:
    source: github.com/NodeSpy/conductor-packs//pr-review-team   # or a local path
    version: "~> 3.0"
    # settings: { auto: true }                   # never hand off; always post
    triggers:
      on_review_request:
        enabled: true                              # arm it
        repos:   [your-org/app]                    # scope it — REQUIRED for a github trigger
```

Then:

```
conductor init            # fetch + lock + vendor the pack
conductor pack plan       # preview what it adds (agents, code steps, armed triggers)
conductor validate
```

The github connector auto-binds when it is your only one; with several, bind
it: `connectors: { github: <your-connector> }`. The pack carries no
connectors and no secrets — posting uses your connector's own write identity.

## What it needs (`requires:`)

- **`conductor: >=0.69.0`** — memory scope keys with `${repo}` inside them
  (`standard:${repo}`) and a step's `memory: { extra_scopes: [...] }`, which
  the lens panel's per-repo layering uses. It covers the older floors too:
  0.57.0 for whole-PR review (the agent workspace jail and
  `github.pr_files[].patch`), 0.54.0 for `decide:` steps, 0.33.0 for the
  reactive hand-off.
- **A `github` connector.** The only bind.
- **`memory`** — conductor's built-in memory (declared, binds to itself). The
  review works without a top-level `memory:` section — it then reads no
  memory — and the calibration triggers stop with a message asking for one.

## Settings

Every setting is optional. Set them under `packs.<instance>.settings`.

| Setting | Default | What it does |
|---|---|---|
| `auto` | `false` | `true`: never hand off. Every review posts with the team's verdict (`APPROVE` or `REQUEST_CHANGES`); `assess` doesn't run; emphasis entries only sharpen the review. `false`: `assess` may route a PR to a human hand-off, and a PR matching an emphasis entry leans to that hand-off. |
| `state_dir` | `""` | conductor's state directory, for the calibration triggers. Empty: `$XDG_STATE_HOME/conductor`, else `~/.local/state/conductor`. Set it if your daemon runs with another state directory — code steps don't inherit `XDG_STATE_HOME`. |
| `audit_log` | `""` | The audit log calibration reads reviews from. Empty: `<state_dir>/audit.jsonl` (its rotated `.1` is read too). Set it if your config moves `store.audit_log`. |
| `git_base` | `https://github.com` | Where calibration clones reviewed repos from (`<git_base>/<owner>/<repo>.git`). https clones authenticate through the `gh` CLI's login. |
| `calibration_window_days` | `14` | How long after a reviewed PR merges a later commit that changes its lines counts as a follow-up. |
| `calibration_bootstrap_days` | `30` | How far back the first calibration run looks. |
| `calibration_unmerged_days` | `30` | Stop waiting for a reviewed PR to merge after this many days. |
| `calibration_discard_days` | `7` | A hand-off draft nobody posted within this many days counts as discarded. |
| `calibration_min_lines` | `3` | A later commit is a follow-up only if it changes or removes at least this many lines the reviewed PR added. |
| `calibration_team_pages` | `15` | Pages (100 comments each) of teammates' review comments read per repo per calibration run. |
| `backfill_max_pages` | `300` | Pages of review comments read per repo by `review-backfill`. |
| `backfill_min_chars` | `40` | `review-backfill` ignores comments shorter than this. |
| `backfill_reviewer_since` | `""` | `review-backfill`: your own comments from this date (`YYYY-MM-DD`) on may have been posted by this pack under your account, so they aren't counted as yours. Empty: the date of the first review this pack posted, from the audit log. |

Settings are substituted as text. `auto` is read once, by the review flow's
first step (`automode`), case-insensitively and with surrounding space
trimmed: `true`/`yes`/`1` and `false`/`no`/`0` are accepted, so `auto: true`,
`auto: "True"` and `auto: " yes "` all mean on. Anything else **fails the run**
with an error naming the setting — a misspelled `auto` is never quietly read
as "gated".

## Auto mode

`auto: false` (the default) is the 2.x behavior: `assess` decides whether a
PR may post itself or needs a human, and a PR with real comments that needs a
human gets the hand-off. A clean review (no comments) posts either way.

`auto: true` skips `assess` and the hand-off entirely. The gate always posts,
and the verdict the team reconciled stands — an `APPROVE` included. Use it
where nobody is watching for hand-offs, or once calibration has earned the
reviewer your trust.

## Memory: how the reviewer learns

The lens reviewers read conductor's shared memory, layered per repo. Each
review recalls, newest first and up to 100 entries across all of them:

| Scope | What lives there |
|---|---|
| `global` (the shared set) | The reviewer-wide standard — rules that apply in every repo. |
| `<owner/repo>` | Facts about this repo the diff can't show: how it is built, deployed, operated, used. |
| `standard:<owner/repo>` | This repo's additions to the review standard. |
| `emphasis:<owner/repo>` | "Look hard at X" rules: kinds of change in this repo that past reviews got wrong. |

Entries written by calibration are tagged with their layer (`standard`,
`emphasis`, `fact`), and the lens prompt explains the tags: `standard` notes
weigh like the repo's own CLAUDE.md/AGENTS.md/CONTRIBUTING, `emphasis` notes
mark areas to look at hard, everything else is context.

`emphasis:<repo>` also feeds `assess`: with `auto: false`, a PR that matches
an emphasis rule leans to the hand-off. With `auto: true` the rules only add
review emphasis.

You can write entries by hand too — with the `dir:` backend each one is a
Markdown file in your memory directory, and the `scope:` line picks its
layer. To change how many entries a review reads, override the step:
`steps: { review-team/review: { memory: { limit: 40, extra_scopes: ["standard:${repo}", "emphasis:${repo}"] } } }`.

## Calibration

Two triggers turn what happened after past reviews into memory entries. Both
ship **disarmed**, and their script steps ship **confined**: a pack's code
steps run in a jail with no network and no view of conductor's state, and
these need both. Arming them is your call.

### review-calibration

Reads what is new since its last run:

- **reviews this pack posted** — from the audit log, so the repos it covers
  are exactly the repos it reviewed; nothing is configured per repo;
- **follow-up commits** — commits that changed or removed lines a reviewed PR
  added, within `calibration_window_days` of its merge, read from a partial
  clone it keeps under `<state_dir>/pr-review-team/clones/`;
- **hand-off outcomes** — each draft it handed to a human compared with what
  was actually posted: as-is, edited (dropped / added / reworded comments, a
  changed verdict), or discarded;
- **teammates' review comments** on those repos (bots, replies, and the
  reviewer's own account — from `gh api user` — excluded).

An agent reads all of that inline — no network, no files, no memory access —
and proposes entries, each with a `kind` (`fact`, `standard`, `emphasis`,
`global`, or `none` for a candidate it dropped), a repo, and the PRs it rests
on. A deterministic step then checks every proposal before anything is
written:

- the scope comes from the kind and repo — `fact` → `<repo>`, `standard` →
  `standard:<repo>`, `emphasis` → `emphasis:<repo>`, `global` → the shared
  set — never from the agent;
- what one repo teaches stays in that repo: a `global` entry is accepted only
  when its evidence cites PRs from **at least two different repos** in the
  run. One resting on a single repo (however many of its PRs) is downgraded
  to `standard:<that repo>`, and the downgrade is logged;
- the repo must be one this run collected evidence for, and the evidence must
  cite PRs in this run's repos (a per-repo entry must cite its own repo);
- a proposal may replace an existing entry only in the scope it writes.

Anything else is rejected and logged. Repo names read from the audit log or
the run's state must be plain `owner/name` before they reach git, `gh` or a
path; anything else is skipped and listed in the collect step's
`invalid_repos`. The pack's own `write` step makes the
writes (`ctx.memory.remember`); the run's section is appended to
`<state_dir>/pr-review-team/calibration-log.md`. The run's position only
advances once the writes land, so a failed run re-reads the same window.

### review-backfill

A one-time seed from a window of past human review comments on repos you
name: one agent per month extracts what reviewers look out for, one agent
merges the patterns into entries, and the same checked write path writes
them.

```
conductor run review/review-backfill --input repos=your-org/app,your-org/api --input days=365
```

### Arming calibration

```yaml
packs:
  review:
    # …
    steps:
      # The script steps need the network (gh, git) and the state directory.
      review-calibration/collect: { trust: full }
      review-calibration/record:  { trust: full }
      review-backfill/collect:    { trust: full }   # only if you use backfill
      review-backfill/record:     { trust: full }
    triggers:
      review-calibration: { enabled: true }
      review-backfill:    { enabled: true }
```

The script steps call the `gh` CLI (logged in as the account the reviews post
as) and `git`. Without the grant, `collect` stops with a message naming it.
Then `conductor run review/review-calibration` runs it now (use your instance
name in place of `review`).

To run it on a schedule, declare a schedule on your cron connector and fire
the trigger from your own config (a pack can't own a schedule):

```yaml
connectors:
  timer:
    use: cron
    schedules:
      review_calibration: { cron: "23 8 * * 1" }   # Mondays 08:23

triggers:
  - name: weekly-review-calibration
    on: timer.review_calibration
    steps:
      - uses: conductor.run
        options: { name: review/review-calibration }
```

Everything calibration keeps is under `<state_dir>/pr-review-team/`: its
position (`calibration.json`), clones, and the log. Delete a memory entry to
undo it.

## Sharing what it learned

Memory stays on the box that wrote it. To give teammates the same reviewer,
export the scopes into a pack and have them install it next to this one —
its entries are recalled read-only, merged with their own:

```sh
conductor memory export \
  --scope global --scope your-org/app --scope standard:your-org/app --scope emphasis:your-org/app \
  --out ../review-training --name review-training
```

Commit `../review-training` to a repo (a private one is fine — give the pack
source an `auth:` credential) and install it:

```yaml
packs:
  training:
    source: github.com/your-org/review-training
```

Its manifest is just pack metadata plus `memory: { entries: memory }`.
Re-export and push to update it.

## Upgrading from 2.x

3.0.0 is a major version: a `~> 2.x` pin won't pick it up.

- **Needs conductor >= 0.69.0.**
- **Routing is now emphasis.** `assess` reads `emphasis:<repo>` itself. If you
  override `review-flow/assess`'s `decide.state` to inject routing rules,
  delete that override; it also references the `diffcap` step 2.10 removed.
  Move routing entries to the new scope — with the `dir:` backend, change
  each file's `scope: routing:<repo>` line to `scope: emphasis:<repo>`.
- **The lens panel reads memory by default** (limit 100, the per-repo layers
  above). A `review-team/review: { memory: … }` override is no longer needed;
  if you keep one, include the `extra_scopes`.
- **Calibration moved into the pack.** If you run your own calibration or
  backfill triggers, the pack's `review-calibration` and `review-backfill`
  replace them; arm those and remove yours. The pack keeps its own position
  file, so its first run looks back `calibration_bootstrap_days`.

## 2.10.0 — whole-PR reviews

- **The lens reviewers get a real PR checkout** (`checkout: checkout-pr`)
  instead of an inlined, 60 KB-capped diff. The prompt carries the PR
  metadata and the complete file list, and tells the reviewer the full change
  is `git diff origin/<base>...HEAD`. The step carries `output_schema`, so in
  the jail its `gh` and `git` are read-only and it has no write verbs.
- **`verify` judges each finding against bounded, per-finding context**: the
  flagged file's own patch and a window of that file at the PR head (±30
  lines, capped at 8000 bytes), computed in the pack's `windows` js step.
- **`assess` sees every changed file** plus as many patches as fit a 50 KB
  budget, in `pr_files`' own order.
- `review-team`'s `diff:` output was removed; call `github.pr_diff` if you
  need the raw diff.

Each lens reviewer provisions its own PR worktree, so the default six lenses
make six checkouts.

## Tuning

- **Models** — two fleets: `heavy` (the lens panel, calibration) and `light`
  (triage, refute-verify, the hand-off agent, backfill extraction). They
  resolve against whatever your runtimes offer; pin one with
  `models: { heavy: ["claude-opus-*"], light: ["claude-sonnet-*"] }`.
- **Triage and refute-verify are `decide:` steps** — typed questions answered
  with probabilities. To have a decision runtime such as Jev answer first, add
  its models to the tier — `models: { light: ["jev-*", "claude-sonnet-*"] }` —
  with the runtime configured ([jev runtime](https://github.com/NodeSpy/conductor-plugins/blob/main/docs/runtimes/jev.md)).
  `verify` refutes a finding only when P(refuted) ≥ 0.8. Record every decision
  with `decide: { observe: <your SQL store> }` on the pack instance
  ([Decide steps](https://github.com/NodeSpy/conductor/blob/main/docs/wiki/Decide-Steps.md)).
- **Step overrides** — override an exported step by its qualified reference,
  e.g. `steps: { review-flow/handoff: { model: ... } }`. See `exports:` in the
  manifest.
- **Lenses** — the six lenses are the `review-team` workflow's `lenses` input
  default; override that input to review through a subset or add your own.

## Consent is load-bearing

Every trigger ships **disarmed**. `on_review_request` does nothing until you
set `enabled: true` **and** name `repos:` — a github trigger is rejected at
load if armed without repos. The calibration triggers do nothing until you
arm them and grant their script steps.

## Authoring

`conductor-pack.yaml` is the manifest; `scripts/calibration.py` is the
calibration triggers' deterministic half. Before publishing:

```
conductor pack lint .
python3 -m unittest discover -s tests      # needs node and PyYAML
```
