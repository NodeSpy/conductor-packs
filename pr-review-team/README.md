# pr-review-team

A distributable [conductor](https://github.com/NodeSpy/conductor) **pack**:
multi-lens pull-request review with an optional human hand-off, and a
calibration loop that teaches the reviewer from what happened after its
reviews — under a learning scope you choose (the repo by default).

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

- **`conductor: >=0.69.0`** — memory scope keys that are templates over the
  step's facts (`standard:{{.repo}}`, `org:{{.owner}}`), and `ctx.memory.key`
  — core's scope-key renderer, which the pack renders every key it computes
  with (so its keys are exactly the keys core reads).
- **A js engine plugin with `ctx.memory.key`** — newer than `engines/js`
  v1.2.0. `conductor init` / `conductor plugin update` fetch it; with an older
  engine every run stops at the `scopekey`/`keys` step with a message saying
  so. It covers the older floors too:
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
| `scope_key` | `{{.repo}}` | Where a review's learning lives: a memory scope-key template over the review's facts. See [Scope keys](#scope-keys). |
| `memory_limit` | `40` | At most this many learned notes per lens reviewer, filled standard → emphasis → facts (facts go first when the cap bites). `0` = none. A whole number; anything else fails the review run. |
| `state_dir` | `""` | conductor's state directory, for the calibration triggers. Empty: `$XDG_STATE_HOME/conductor`, else `~/.local/state/conductor`. Set it if your daemon runs with another state directory — code steps don't inherit `XDG_STATE_HOME`. |
| `audit_log` | `""` | The audit log calibration reads reviews from. Empty: `<state_dir>/audit.jsonl` (its rotated `.1` is read too). Set it if your config moves `store.audit_log`. |
| `git_base` | `https://github.com` | Where calibration clones reviewed repos from (`<git_base>/<owner>/<repo>.git`). https clones authenticate through the `gh` CLI's login. |
| `calibration_window_days` | `14` | How long after a reviewed PR merges a later commit that changes its lines, or a comment of yours on it, counts as a follow-up. |
| `calibration_bootstrap_days` | `30` | How far back the first calibration run looks. |
| `calibration_unmerged_days` | `30` | Stop waiting for a reviewed PR to merge after this many days (and stop reading your comments on it). |
| `calibration_discard_days` | `7` | A hand-off draft nobody posted within this many days counts as discarded. |
| `calibration_min_lines` | `3` | A later commit is a follow-up only if it changes or removes at least this many lines the reviewed PR added. |
| `calibration_team_pages` | `15` | Pages (100 comments each) of teammates' review comments read per repo per calibration run. |
| `trusted_associations` | `OWNER,MEMBER,COLLABORATOR` | Calibration and backfill learn only from comments (and follow-up commits) by authors with one of these GitHub `author_association` values; your own account always counts. `none` = only yours. See [Training data and prompt injection](#training-data-and-prompt-injection). |
| `backfill_max_pages` | `300` | Pages (100 comments each) read per repo by `review-backfill` — a safety cap. A repo that hits it is reported as truncated; raise it for a `days=all` train of a long history. |
| `backfill_min_chars` | `40` | `review-backfill` ignores comments shorter than this. |
| `backfill_reviewer_since` | `""` | `review-backfill`: the date (`YYYY-MM-DD`) this pack started posting under your account. From then on your own comments count as yours only when the audit log shows the pack didn't post them; from before the audit log's oldest record they are dropped. Empty: the date of the first review this pack posted, from the audit log. |

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

The lens reviewers read **learned notes** — conductor memory written by the
calibration triggers (or by you) — layered by a **scope key**, the
`scope_key` setting:

| Scope | Layer | What lives there |
|---|---|---|
| `global` (the shared set) | standard | The reviewer-wide standard — rules that apply everywhere. |
| `standard:<key>` | standard | This key's additions to the review standard. |
| `emphasis:<key>` | emphasis | "Look hard at X" rules: kinds of change that past reviews got wrong. |
| `<key>` | facts | Facts the diff can't show: how the code is built, deployed, operated, used. |

**Notes steer the review; they never replace it.** Each lens reviewer is told
to do a full, independent review of the whole change through its lens first,
whatever the notes say. The notes come after everything else in the prompt,
framed as judgment aids: what matters here, what is known to be safe, where
past reviews missed things. They are not a checklist, not the scope of the
review, and never a reason to skip or soften an issue. A note alone never
makes a finding blocking — that still takes the change actually breaking
something. Your repo's own CLAUDE.md/AGENTS.md/CONTRIBUTING keep their
blocking weight; learned `standard` notes are strong guidance, not law.

The pack reads the notes itself (the review's `notes` step) rather than having
conductor inject them, so they arrive in a fixed order — standard (the shared
set, then `standard:<key>`), then emphasis, then facts, newest first within a
layer — and under a cap: **`memory_limit`** notes per review (default 40;
`0` turns them off). When the cap bites, facts go first. A lens that raised a
finding because of a note says so (the finding's `note`), which `review-eval`
counts. The lens step itself has conductor memory turned off, so it neither
reads memory on its own nor writes any back.

`emphasis:<key>` also feeds `assess`: with `auto: false`, a PR that matches
an emphasis rule leans to the hand-off — routing only; the review team reviews
the whole change either way. With `auto: true` the rules only add review
emphasis.

You can write entries by hand too — with the `dir:` backend each one is a
Markdown file in your memory directory, and the `scope:` line picks its
layer. To check that the notes help rather than crowd out the review, run
[`review-eval`](#measuring-it-review-eval).

### Scope keys

The default, `{{.repo}}`, gives every repository its own layers — what one
repo teaches stays in that repo. Any other grouping is a template over the
review's facts, in core's scope-key syntax (`{{.fact}}`, `| default "x"`,
`coalesce`, `index`, `print`, `printf`; no conditionals or variables):

| `scope_key` | One layer per… |
|---|---|
| `{{.repo}}` (default) | repository: `acme/app`, `standard:acme/app`, … |
| `org:{{.owner}}` | owner/org: every repo of `acme` shares `org:acme`, `standard:org:acme`, … |
| `area:{{index .labels 0}}` | the PR's first label — a monorepo area, if you label PRs by area. A PR with no label has no key (`default` can't help: `index` of an empty list fails first), so it reads and teaches only the shared set. |
| `{{.repo}}@{{.base}}` | repo and base branch: a release branch learns apart from `main`. |

A key may read `repo`, `owner`, `name`, `number`, `pr`, `kind`, `title`,
`labels`, `base`, `url`, `inputs.repo` and `inputs.pr` — the facts
calibration can rebuild for a past PR. Anything else (`head`, `me`, step
outputs) fails the run with an error naming the fact. Changed paths are not a
fact; for a monorepo area, label the PRs. `labels`, `base` and `title` are
chosen by the PR's author, and calibration reads their *current* values, so
relabeling a PR moves its lessons. A key that reads a fact the PR has no
value for (an empty label list, a blank title) doesn't apply to that PR:
it reads only the shared set, and calibration won't cite it.

To check what a key renders to, ask core:
`echo '{"repo":"acme/app","owner":"acme"}' | conductor memory key --template 'org:{{.owner}}' --facts -`.

Changing `scope_key` doesn't move entries already written: the old layers
stay put and stop being read. Move them with `conductor memory rescope` (see
[Starting over](#starting-over-a-fresh-train)) or retrain with a fresh
backfill.

## Calibration

Two triggers turn what happened after past reviews into memory entries. Both
ship **disarmed**, and their script steps ship **confined**: a pack's code
steps run in a jail with no network and no view of conductor's state, and
these need both. Arming them is your call.

### review-calibration

Reads what is new since its last run:

- **your own comments on reviewed PRs ("owner catches")** — reviews post as
  your account, so anything that account wrote on a PR *after* the pack
  reviewed it, and that the pack didn't post, is you correcting it: a later
  `CHANGES_REQUESTED` review, an inline comment (a reply to the pack's comment
  is shown with it), a conversation comment, or a dismissal of the pack's
  review. They count until `calibration_window_days` after the PR merges
  (`calibration_unmerged_days` if it doesn't), and they are the strongest
  signal the agent gets. The pack's own posts are told apart by the audit
  log's `submit_review` records (review body and time, and the inline
  comments it sent) — never by author — so a PR whose audit records have
  rotated away is skipped rather than guessed at;
- **reviews this pack posted** — from the audit log, so the repos it covers
  are exactly the repos it reviewed; nothing is configured per repo;
- **follow-up commits** — commits that changed or removed lines a reviewed PR
  added, within `calibration_window_days` of its merge, read from a partial
  clone it keeps under `<state_dir>/pr-review-team/clones/`;
- **hand-off outcomes** — each draft it handed to a human compared with what
  was actually posted: as-is, edited (dropped / added / reworded comments, a
  changed verdict), or discarded;
- **teammates' review comments** on those repos (bots, replies, and your own
  account excluded — yours are the owner catches above), from trusted authors
  only (`trusted_associations`).

**Teaching it in auto mode.** With `auto: true` nothing is handed off, so
there are no drafts to learn from: comment on the PR instead. Request changes
on a PR the pack approved, reply to one of its inline comments to say it was
wrong, or note what it missed in the conversation — the next calibration run
reads that as a miss and turns it into an entry for the PR's key.

An agent reads all of that inline — no network, no files, no memory access —
and proposes entries, each with a `kind` (`fact`, `standard`, `emphasis`,
`global`, or `none` for a candidate it dropped), a key, and the PRs it rests
on. Calibration maps every PR it shows the agent to that PR's key by
rendering `scope_key` over the PR's facts (fetching title, labels, base and
URL from GitHub only when the key reads them), so a lesson lands exactly
where reviews of such PRs read. A deterministic step then checks every
proposal before anything is written:

- the scope comes from the kind and key — `fact` → `<key>`, `standard` →
  `standard:<key>`, `emphasis` → `emphasis:<key>`, `global` → the shared
  set — never from the agent;
- what one key teaches stays under that key: every PR a per-key entry cites
  must map to that key, and the key must be one this run collected evidence
  for;
- a `global` entry is accepted only when its evidence maps to **at least two
  different keys**. One resting on a single key (however many of its PRs) is
  downgraded to `standard:<that key>`, and the downgrade is logged;
- evidence must be PRs this run collected under a key, at least one of them
  with trusted evidence;
- a proposal may replace an existing entry only in the scope it writes;
- the screening in [Training data and prompt injection](#training-data-and-prompt-injection).

Anything else is rejected and logged. Repo names read from the audit log or
the run's state must be plain `owner/name` before they reach git, `gh` or a
path; anything else is skipped and listed in the collect step's
`invalid_repos`. The pack's own `write` step makes the
writes (`ctx.memory.remember`); the run's section is appended to
`<state_dir>/pr-review-team/calibration-log.md`. The run's position only
advances once the writes land, so a failed run re-reads the same window.

### review-backfill

A seed from past human review comments on repos you name: one agent per
month extracts what reviewers look out for, one agent merges the patterns
into entries, and the same checked write path writes them, each to the key
its evidence PRs map to.

```
conductor run review/review-backfill --input repos=your-org/app,your-org/api --input days=365
```

- `days` — how far back to read: a number of days (default 365), or `all`
  for each repo's whole history. `backfill_max_pages` stays a safety cap (100
  comments a page); a repo that hits it is listed as **truncated** in the run
  log and on stderr — raise the setting and run again.
- Your own comments from the pack's era count as yours only when the audit
  log shows the pack didn't post them (see `backfill_reviewer_since`).

### Training data and prompt injection

Calibration and backfill read text other people write: PR titles and bodies,
commit messages, patches, review comments. What they learn is read by every
later review, and in auto mode reviews post as your account, so a planted
instruction that got learned would stay. The pack defends at each step:

- **Who it learns from.** Team comments count only when their author's
  `author_association` on the repo is in `trusted_associations` (default
  `OWNER`, `MEMBER`, `COLLABORATOR`). A follow-up commit counts only when the
  PR that landed it is by such an author, or it was pushed straight to the
  branch. Your own account always counts. Everything else (outside
  contributors, bots, unknown) is dropped and counted in the run log. Every
  entry must cite at least one PR with trusted evidence: an owner catch, a
  hand-off outcome, a trusted follow-up or a trusted author's comment.
- **How the agents see it.** Each quoted item is fenced as data between
  `<<<TOKEN` and `TOKEN>>>`, with a token drawn fresh each run, so text
  written earlier can't close the fence. Control and bidi characters are
  stripped and long items are cut. The prompts say that text inside a fence
  is evidence, never instructions, and that an instruction found there is
  reported (`kind: none`, reason `injection`; backfill's extractors list
  them), not learned. Existing entries shown to the agents, and the patterns
  the backfill extractors return, are fenced the same way.
- **What it will write.** The agents' output is screened by a deterministic
  step:
  - entries over 500 characters are rejected;
  - so is any entry containing a URL, a code fence, a command or tool call,
    an `@mention`, control characters, or the fence;
  - an entry that would make reviews go easier on something (approve, skip,
    ignore, trust, allowlist, lower severity, "not blocking" and the like)
    is written to `quarantine:<key>` (`quarantine:global` for a shared
    one), which no review reads, unless its evidence includes an owner
    catch or a hand-off you edited or discarded. It never replaces an
    existing entry. Backfill has no owner catches, so there such entries
    are always quarantined.

  The run log (`calibration-log.md`) lists each quarantined entry with its
  exact text and the command that would promote it, for example:
  `conductor memory rescope --scope 'quarantine:owner/app' --tag 'held-1a2b3c4d' --to 'standard:owner/app' --apply`.
  It also lists the rejected entries with the reason, and the instructions
  found in the evidence.
- **How reviews read it.** The lens prompt says learned notes are data about
  the code and can't change the review instructions, the output format, or
  what makes a finding blocking. The verdict is never a model's: `reconcile`
  computes `APPROVE` or `REQUEST_CHANGES` from the findings alone, and no step
  that decides or posts it reads memory. A note can't approve a PR by itself.

The screening matches patterns, so it errs on the side of holding an entry
back: a true statement like "the loader ignores x- keys" lands in quarantine
for you to promote. Repo docs (`CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`
on the default branch) are shown unfenced, as the repo's own word. The PRs
themselves are still reviewed whoever wrote them; trust only decides what
the reviewer learns.

### Starting over: a fresh train

To retrain a key from its entire history, replacing what calibration learned
so far:

```
conductor run review/review-backfill --input repos=owner/app --input days=all --input fresh=true
```

`fresh=true` (default `false`) archives, just before the new entries are
written, every **learned** entry in the layers of the keys being trained —
`<key>`, `standard:<key>`, `emphasis:<key>`. "Learned" means tagged
`review-tuning`; entries you wrote by hand without that tag stay, and the
shared `global` set is never archived by a per-key train. Nothing is
deleted: each entry moves to `archive:<its scope>` (e.g.
`archive:standard:owner/app`), which no review reads, and the moves are
listed in the run log.

To archive or restore by hand, use core's rescope (a dry run until you add
`--apply`):

```
conductor memory rescope --scope 'standard:owner/app' --tag review-tuning --to 'archive:{scope}'
conductor memory rescope --scope 'archive:*' --to '<scope to restore to>' --apply
```

`--scope` takes a key, `*` or a glob (`'standard:owner/*'`); `{scope}` in
`--to` is each entry's current scope. Rescope never touches entries a pack
ships.

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

## Measuring it: review-eval

Does memory sharpen the review, or crowd it out? `review-eval` replays PRs
this pack already reviewed through the lens panel twice — **memory off** (the
baseline), then **memory on**, with the same steps and model tiers — and
compares what each would have posted:

```
conductor run review/review-eval --input repos=owner/app                 # the 5 most recent it reviewed
conductor run review/review-eval --input repos=owner/app --input count=10
conductor run review/review-eval --input prs=owner/app#12,owner/app#15
conductor run review/review-eval --input repos=owner/app --input scope_key='org:{{.owner}}'   # try another key
```

It **posts nothing** (it runs the review engine, never the posting flow or
the hand-off), **writes no memory** (notes are only read), and replays each PR
as if it had no comments yet — the pack's own earlier comments would
otherwise hide exactly the findings being compared. Its one write is the
report, `<state_dir>/pr-review-team/eval/<date>.md`; the summary line is the
`report` step's output and goes to the daemon log.

Findings are matched one-to-one across the two runs: same file, lines at most
5 apart, and either identical text or a light-model judge saying they raise
the same issue. Per PR and overall it reports:

- **retained** — baseline findings memory-on also raised;
- **dropped** — baseline findings memory-on did not raise;
- **added** — memory-on findings the baseline did not have, and how many cite a note.

**Target: memory-on retains nearly all baseline findings** — 90% or more reads
`ok`, 75–90% `watch`, below that `crowding`: the notes are pulling the review
away from what it would otherwise catch. Look at what was dropped, and prune or
reword the notes behind it (or lower `memory_limit`). Model runs vary from one
run to the next, so judge over a handful of PRs, not one.

Arm it like the calibration triggers (its `pick` step reads the audit log and
`report` writes the report):

```yaml
    steps:
      review-eval/pick:   { trust: full }
      review-eval/report: { trust: full }
    triggers:
      review-eval: { enabled: true }
```

When an eval ran since the last calibration, `review-calibration` adds its
summary to the calibration log — a note to read, not a gate.

## Sharing what it learned

Memory stays on the box that wrote it. To give teammates the same reviewer,
export the scopes into a pack and have them install it next to this one —
its entries are recalled read-only, merged with their own:

```sh
conductor memory export \
  --scope global --scope your-org/app --scope standard:your-org/app --scope emphasis:your-org/app \
  --out ../review-training --name review-training
```

(Use your `scope_key`'s keys — e.g. `org:your-org`, `standard:org:your-org`,
… — if it isn't the default.) Commit `../review-training` to a repo (a private one is fine — give the pack
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

- **Needs conductor >= 0.69.0** and a js engine plugin with `ctx.memory.key`
  (newer than `engines/js` v1.2.0) — run `conductor plugin update`.
- **Learning has a scope key.** `scope_key` defaults to `{{.repo}}`, so the
  layers are `<repo>`, `standard:<repo>` and `emphasis:<repo>` — entries you
  already keep per repo are read as before.
- **Routing is now emphasis.** `assess` reads `emphasis:<key>` itself. If you
  override `review-flow/assess`'s `decide.state` to inject routing rules,
  delete that override; it also references the `diffcap` step 2.10 removed.
  Move routing entries to the new scope — with the `dir:` backend, change
  each file's `scope: routing:<repo>` line to `scope: emphasis:<repo>`.
- **The lens panel reads learned notes by default** — at most `memory_limit`
  (40), in layer order, framed so they steer the review rather than replace
  it. They come from the pack's `notes` step, not conductor's memory
  injection: **remove any `review-team/review: { memory: … }` override** —
  it would inject the notes a second time, unframed. Only the shared set and
  the key's three layers are read (not the workflow or step scopes conductor's
  default injection added).
- **Only trusted authors teach it.** Teammates' comments count only from
  `trusted_associations` (default `OWNER,MEMBER,COLLABORATOR`), and entries
  that would make reviews go easier on something are quarantined unless an
  owner catch backs them. If your teammates show up as `CONTRIBUTOR` (for
  example, org membership the reviewer's token can't see), the run log's
  "Untrusted" line counts them; widen the setting only if you mean it.
- **Your own comments teach it.** Calibration used to skip everything your
  account wrote; it now reads what you wrote after the pack's review as owner
  catches, and backfill counts your pack-era comments unless the audit log
  shows the pack posted them.
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

The unit tests run the pack's js steps under node, with a test double for
`ctx.memory.key`. `tests/test_scope_key_binding.py` checks the real thing: it
starts a throwaway daemon (its own HOME and XDG directories) and renders
core's scope-key vectors and the pack's `scopekeys` step through the binding.
It is skipped unless you point it at a conductor build:

```
CONDUCTOR_BIN=$(command -v conductor) python3 -m unittest tests.test_scope_key_binding
# a local js engine build, until a release has ctx.memory.key:
CONDUCTOR_BIN=… CONDUCTOR_JS_ENGINE=…/conductor-js python3 -m unittest tests.test_scope_key_binding
```
