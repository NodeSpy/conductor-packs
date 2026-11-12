# pr-autopilot

Autopilot for the routine events on **your** PRs. One agent per event, scoped
to the repos you arm:

| trigger | event | the agent… |
|---|---|---|
| `on_failing_checks` | CI concluded failing | finds why and pushes a fix, from what failed as conductor read it (re-runs once first: most red CI is a flake) |
| `on_merge_conflict` | the PR conflicts with its base | resolves the conflict and pushes |
| `on_changes_requested` | a review requested changes (or sweep: feedback still waiting on you) | addresses ALL review feedback still waiting on you, pushes, resolves the threads its change addressed, re-requests every reviewer with changes outstanding |
| `on_new_comment` | a comment, or a review no changes-requested trigger takes | makes the change or answers the question; resolves the threads its pushed change addressed |
| `on_merged` | your PR merged | no agent: deletes its head branch ([Merged PRs](#merged-prs-the-head-branch-is-deleted)) |

Install and arm it in your config's `packs:` block. The example is in the
header of [`conductor-pack.yaml`](conductor-pack.yaml).

## Progress on the PR

Every flow carries plain `hooks:` that show, **as you**, that the event was
taken and how it went. None of it is hidden or automatic: the hooks are in
`conductor-pack.yaml`, and every word they post is a setting.

| when | reaction (review/comment flows only) | commit status (one row per flow) |
|---|---|---|
| queued: the run waits for a slot or for another run on the PR | — | `pending`, "…: queued — waiting for …" |
| start, before any worktree or agent exists | 👀 on the review or comment | `pending` on the commit the run starts on |
| while it works, at most every 30s | — | `pending`, updated with what it is doing and for how long |
| done, pushed | 🚀 | `success` on that **start** commit |
| done, nothing pushed | 👍 | `success` on that start commit (= the PR's head) |
| fail (including a dispatch that never came up, and a park) | 😕 | `failure` on the PR's **current** head, "gave up: …" |
| stop: the PR merged or closed under the run | the 👀 is **removed** | `success` on the start commit, "stopped — the PR merged" |

**The row disappears after a push.** GitHub can't delete a commit status, so
the success goes on the commit the run started on, never on the new head.
When the run pushed, the PR's head is a new commit with no row of that
context, and the row is simply gone from the PR. When nothing was pushed, a
green ✓ stays on the unchanged head. A failure goes on whatever the PR's head
is when the run gives up, so it's what you see.

**Nothing is left behind on a closed PR.** When the PR merges or closes while
a run is on it, conductor stops the agent, and the `stop` hooks resolve the
pending row ("stopped — the PR merged" / "… closed") and take the 👀 back off.
A daemon restart is not a stop: the run resumes and ends normally.

**The pending row moves while the run works.** Each row's description is
the flow's pending line, then what the run is doing and for how long:

```
replying to bob's comment: queued — waiting for a run of autopilot/on_changes_requested to finish · <1m
replying to bob's comment: starting · <1m
replying to bob's comment: agent started · 1m
replying to bob's comment: editing adapters/adapter.go · 3m
replying to bob's comment: running the tests · 6m
replying to bob's comment: pushing · 9m
pushed 4f2a9c1
```

"What it is doing" is, in order of preference: the fixer's own words (it is
asked to say what it is doing with `conductor call step.progress`), the tool
the agent last used (on runtimes that show it), and the step it is on. A
row is updated at most every 30 seconds, and only when its text changed; a
change of state (queued → working) shows at once. GitHub lists every status
as an event on the commit, not on the PR's conversation, so the updates
don't fill the PR's timeline.

Each row's **Details** link opens the run on your dashboard, when the
dashboard has a `public_url` (conductor ≥ 0.97.0; see the dashboard docs).
Anyone who can see the PR sees that address; switch the link off with
`status_link: false`, or the moving text with `progress: false`.

Rows are named after you, one context per flow:

| flow | default context | pending says |
|---|---|---|
| `on_changes_requested` | `<you> / review` | addressing alice's review (sweep: addressing unresolved review threads) |
| `on_new_comment` | `<you> / comment` | replying to bob's comment / review |
| `on_failing_checks` | `<you> / ci-fix` | fixing failing check build |
| `on_merge_conflict` | `<you> / conflict` | resolving the merge conflict |

`<you>` is `{{.me.login}}`, the login your writes act as (discovered from your
write identity, or your first `me:` login). Nothing hardcodes a username, and
nothing says "conductor".

### Rename, reword, or switch off

Everything is a pack setting. Set it in your instance block, with no fork:

```yaml
packs:
  autopilot:
    use: conductor-packs/pr-autopilot
    version: "~> 1.3"
    settings:
      ci_context:     "{{.me.login}} / autofix"     # rename a row
      review_working: "on it: {{.author}}'s review" # reword the pending line
      status_failed:  "autopilot gave up: {{.run.reason}}"
      ci_status:      false                         # no row for failing-checks runs
      reactions:      false                         # no reactions anywhere
    triggers:
      "*": { enabled: true, repos: [your-org/app] }
```

| setting | default |
|---|---|
| `reactions` | `true` |
| `review_status` / `comment_status` / `ci_status` / `conflict_status` | `true` (per flow) |
| `review_context` / `comment_context` / `ci_context` / `conflict_context` | `{{.me.login}} / review` … |
| `review_working` / `comment_working` / `ci_working` / `conflict_working` | the pending descriptions above |
| `status_done` | `{{if .run.pushed}}pushed {{.run.head_short}}{{else}}done — no changes pushed{{end}}` |
| `status_failed` | `gave up: {{.run.reason}}` |
| `status_stopped` | `stopped — {{.run.reason}}` |
| `progress` | `true`: update the pending row as the run works |
| `status_progress` | `{{.progress.text}} · {{.progress.elapsed}}`: what follows the pending line while it works ([fields](https://github.com/NodeSpy/conductor/wiki/Workflows#progress)) |
| `status_link` | `true`: link each row to the run on the dashboard (`{{.run.url}}`; nothing without a `public_url`) |
| `progress_guidance` | the fixer's instruction to report what it is doing with `step.progress`; set `""` to drop it |
| `stop_unreact` | `true`: on a stop, remove the 👀 |
| `resolve_threads` | `true`: after a changes-requested or new-comment run whose change conductor pushed, resolve the threads its agent reports addressed (see below) |
| `delete_merged_branch` | `true`: `on_merged` deletes your merged PR's head branch (see [Merged PRs](#merged-prs-the-head-branch-is-deleted)) |

Contexts and descriptions are templates rendered per run. `{{.run.reason}}`
is a short public-safe phrase ("the agent couldn't be started", "timed out",
"the change was never pushed", …), never the error text.

### Failing checks: what the fixer is handed

The fixer runs in conductor's agent sandbox: no GitHub credentials and no
route to github.com. It sees CI through conductor instead:

1. **`ci`** reads the failures with your github connector's credentials
   (`github.check_failures`): every failing check run on the PR's head as of
   now, with what it reported (title, summary, details, annotations), and for
   a GitHub Actions job the failed step and the part of its log that shows
   the failure, size-capped. Checks this trigger ignores (`ignore_checks`)
   are left out. If the read fails, the run goes on without it.
2. **`ci_text`** turns that into text inside a data fence: between
   `<<<evTOKEN` and `evTOKEN>>>` markers, with a token drawn per run, the
   convention pr-review-team uses for PR text. A test log prints whatever the
   code under test or the PR's author wants, including text addressed to an
   agent, so nothing inside may look like a marker: any `ev`+16-hex token,
   `<<<` and `>>>` are broken up, and control and invisible format
   characters are removed. The prompt tells the agent the fenced text is data
   to diagnose from, never instructions.
3. The fixer gets it in its prompt, and one conductor verb,
   `check_failures`, scoped to the PR's repo. After it pushes, it calls the
   verb to read the new head's checks (still-running ones are listed as
   pending) instead of guessing whether the fix took.

To leave a check out, list it on the trigger. The fixer is never dispatched
for it, and it doesn't appear in what the fixer reads:

```yaml
packs:
  autopilot:
    triggers:
      on_failing_checks:
        enabled: true
        repos: [your-org/app]
        options: { ignore_checks: [pr-title-lint] }
```

The flaky re-run (`flaky_rerun: { enabled: true, max: 1 }`) is unchanged.
Failed jobs re-run once before an agent is engaged, and the fixer reads the
re-run's results.

### Changes requested: what a run covers, resolves, and re-requests

A `changes_requested` run is handed every piece of review feedback on the PR
that is still waiting on you, not only the review that fired it: unresolved
threads whose latest comments aren't yours, and changes-requests made on the
current head. Two reviews a moment apart, or one that lands while a run is
working, end up in one run (the github connector docs define "waiting on you").

The agent reports the comments its pushed change fully addresses
(`addressed_comments`, part of its result). The `resolve` step then resolves
exactly those threads — **only when conductor itself pushed the change** — and
leaves a thread open when:

- nothing was pushed: the agent's report is a claim, and the gate is
  conductor's own record of its push (`address.push.pushed`, from the agent
  sandbox's end-of-step push or a `git.push` during the step). With the sandbox
  off (`sandbox.agents: off`, or `isolation: {mode: none}`) the agent pushes
  for itself, conductor has no push of its own to go by, and no thread is
  resolved;
- the agent only replied, disagreed, or changed no code for it (not reported);
- someone other than you commented in it after the feedback the run was given.

`resolve_threads: false` turns the step off; threads are then left for you or
the reviewer to resolve.

A `new_comment` run (a comment, or a review with inline threads that no
changes-requested trigger takes) resolves the same way: the agent reports
`addressed_comments`, and the `resolve` step resolves those threads only when
conductor pushed the change, including when the branch had moved and the change
landed on its new head, or a second attempt was pushed. A conversation comment
has no thread, so nothing is resolved for it. A `new_comment` event carries no
`feedback_cursor`, so the newer-feedback check above does not apply: the ids
the agent can report are the event's own comments.

The `rerequest` step pings back the event's reviewer and every other reviewer
whose latest review still requests changes on an older commit, so a run fired
by one reviewer's comment doesn't leave another's changes-request blocking the
merge. Bots, the PR's author, approvers and reviewers already requested are
never pinged.

### Moved head: the PR branch moved while a fixer worked

Conductor pushes a fixer's commits at the end of its step, fast-forward only.
When someone pushed to the PR branch meanwhile (its author, another fixer),
that push is refused: the step's `push` output says the branch diverged and how
many commits were left unpushed, and its `diff` output holds the change. The
pack takes it from there, with the github plugin's `apply_diff` (github ≥ 2.6.0):

1. **Keep** (`kept`): the change (the fixer's `diff`) is stored as a run
   artifact (`blob.put`), beside the step outputs that keep it in the run's
   history.
2. **Apply** (`landed`): `github.apply_diff` applies the committed change to
   the branch's **current** head as one commit, through GitHub's API, as you
   (the github connector's write identity, the same one conductor pushes
   as). It is a fast-forward and never forced: a branch that moved yet again
   is `rejected`. A hunk the new head already has is skipped. One agent, one
   run. On `on_changes_requested`, `resolve` then resolves the threads the
   change addressed.
3. **Conflict or moved again → run it again, once** (`refix`): the same
   fixer step runs one more time with the same task, on the new head. Its
   task also says that this is a second attempt, which files conflicted, and
   the first attempt's diff (fenced as data). Conductor pushes its work as
   usual. If that push fails too, the run fails (the change was never
   pushed). There is no third attempt, and nothing is forced.
   `moved_head_guidance` is the note's text.
4. **CI config** (`unpushed`): a change to CI config (`.github/workflows/`
   and the other CI paths conductor's own push refuses by default) is never
   applied. The run fails, and the change stays in the run's artifact and
   the step's `diff`.

**What is never applied.** The `diff` output is capped at 64 KiB, and any
secret in it is redacted. A clipped or redacted diff is not the whole change,
so the fixer does not go on past its push: the run fails as before, with its
agent and workspace kept, and the commits are still in that workspace.
Uncommitted edits (the diff's `# uncommitted` section) are not applied, just
as conductor's push never pushes them.

**Setup.** None beyond the github connector: `apply_diff` uses its existing
auth, so the commit is yours. `moved_head_message` is the commit's message.

The steps are plain `uses:` steps in [`conductor-pack.yaml`](conductor-pack.yaml)
(the `kept`, `pr_now`, `landed` and `unpushed` templates). The fixer steps
reach them through a `continue_on_error:` condition: the run goes on past a
fixer whose push was refused because the branch diverged, with a whole diff,
and stops on any other failure.

[`e2e/moved-head.sh`](e2e/moved-head.sh) runs these steps end to end: the
real conductor (`conductor once`), this pack, and the github plugin against a
GitHub fake. A sandboxed stand-in fixer commits while the branch moves under
it, and the script checks a clean landing, a conflict's second attempt, and a
clipped diff that is never applied.

### One fixer per PR

The four fixer triggers (`on_failing_checks`, `on_merge_conflict`,
`on_changes_requested`, `on_new_comment`) share one `serialize:` key,
`{{.repo}}#{{.pr}}`. So only one autopilot fixer works a PR at a time. When a
second event arrives on a PR a fixer is working (a review lands while a comment
is being handled, say), its run waits, and the daemon logs
`waiting on <repo>#<pr> (held by autopilot/on_new_comment)`. It runs once the
first ends, on the PR's head as it is then. Conductor reads the PR's current head
when the fixer's step dispatches, so the fixer starts from what the first one
pushed and its own push is a plain fast-forward.

To change the key for a trigger, set it in that trigger's arm, e.g.
`on_new_comment: { …, serialize: { key: "…" } }`. To let that trigger's runs
overlap again, use `serialize: { key: "" }`.

[`e2e/moved-head.sh`](e2e/moved-head.sh) runs it through the daemon: a
comment's fixer is working when a changes-requested review arrives, and the
review's fixer waits, then runs on the pushed head and fast-forwards it.

### Merged PRs: the head branch is deleted

Armed, `on_merged` takes every PR close in its repos, and acts only when:

- the PR merged;
- it's yours (its author is the connector's identity, `me`);
- its branch is in the repo, not a fork's;
- and its branch isn't the default branch.

It then calls `github.delete_branch` with the merged head as `expect_sha`. If
someone pushed to the branch after the merge, the branch is left alone. A
protected branch is refused, and a branch that's already gone counts as done.
The setting `delete_merged_branch` is `true` by default; `false` keeps the
trigger armed but deletes nothing.

[`e2e/moved-head.sh`](e2e/moved-head.sh) checks this against the GitHub fake:
your merged PR's branch is deleted, a branch pushed to after the merge is
kept, and so is the branch of someone else's PR.

### The same hooks on your own triggers

They're ordinary hooks, so copy them onto any trigger (use your github
connector's name in place of `github`):

```yaml
hooks:
  - { at: start, if: reaction_subjects, uses: gh.react,
      options: { repo: "{{.repo}}", pr: "{{.pr}}", subjects: "{{.reaction_subjects}}", content: eyes } }
  - { at: start, uses: gh.set_status,
      options: { repo: "{{.repo}}", sha: "{{.run.start_sha}}", state: pending,
                 context: "{{.me.login}} / mine", description: "working" } }
  # while it runs (and while it waits to start): every 30s at most
  - { at: progress, every: 30s, uses: gh.set_status,
      options: { repo: "{{.repo}}", sha: "{{.run.start_sha}}", state: pending,
                 context: "{{.me.login}} / mine", target_url: "{{.run.url}}",
                 description: "working: {{.progress.text}} · {{.progress.elapsed}}" } }
  - { at: done, if: reaction_subjects, uses: gh.react,
      options: { repo: "{{.repo}}", pr: "{{.pr}}", subjects: "{{.reaction_subjects}}",
                 content: "{{if .run.pushed}}rocket{{else}}+1{{end}}" } }
  - { at: done, uses: gh.set_status,
      options: { repo: "{{.repo}}", sha: "{{.run.start_sha}}", state: success,
                 context: "{{.me.login}} / mine", description: "done", target_url: "{{.run.url}}" } }
  - { at: fail, if: reaction_subjects, uses: gh.react,
      options: { repo: "{{.repo}}", pr: "{{.pr}}", subjects: "{{.reaction_subjects}}", content: confused } }
  - { at: fail, uses: gh.set_status,
      options: { repo: "{{.repo}}", pr: "{{.pr}}", state: failure,
                 context: "{{.me.login}} / mine", description: "gave up: {{.run.reason}}" } }
  # the PR merged/closed under the run
  - { at: stop, if: reaction_subjects, uses: gh.react,
      options: { repo: "{{.repo}}", pr: "{{.pr}}", subjects: "{{.reaction_subjects}}", content: eyes, remove: true } }
  - { at: stop, uses: gh.set_status,
      options: { repo: "{{.repo}}", sha: "{{.run.start_sha}}", state: success,
                 context: "{{.me.login}} / mine", description: "stopped — {{.run.reason}}" } }
```

`reaction_subjects` exists only on `changes_requested` and `new_comment`
events. Leave the reaction hooks off triggers for other events. Hooks are
best-effort: a reaction or status that fails to post is logged and never fails
the run. Conductor never reads a status it posted back as CI, so a `failure`
row can't trigger the next fix.

## Upgrading

v2.4.0: the pending row says what the run is doing and for how long, and
links the run on your dashboard ([Progress on the PR](#progress-on-the-pr)).
It needs **conductor ≥ 0.97.0** (`at: progress` hooks, `{{.run.url}}`). An
older conductor refuses the pack at load, so keep v2.3.x there.

v2.3.1: a fixer replies on every review thread it changes code for ("Fixed in <sha>: ..."), not only the ones it answers without a change, before the thread is resolved.

v2.3.0: one fixer per PR ([One fixer per PR](#one-fixer-per-pr)). It needs
**conductor ≥ 0.86.0** (a trigger's `serialize:`). An older conductor refuses
the pack at load, so keep v2.2.x there.

v2.2.0 adds `on_merged` ([Merged
PRs](#merged-prs-the-head-branch-is-deleted)), which ships disarmed like the
other triggers. It needs the **github connector plugin ≥ 2.9.0**
(`github.delete_branch` and the close event's head facts). The conductor
requirement is unchanged.

v2.1.0: `on_new_comment` resolves the review threads its pushed change
addressed, as `on_changes_requested` does ([Changes
requested](#changes-requested-what-a-run-covers-resolves-and-re-requests));
`resolve_threads: false` turns it off for both. Its fixer now reports a result
(`addressed_comments`, `replied_comments`, `summary`) and declares
`expect_push`. Same requirements as v2.0.0.

v2.0.0 handles a PR branch that moved while a fixer worked ([Moved
head](#moved-head-the-pr-branch-moved-while-a-fixer-worked)). It needs
**conductor ≥ 0.82.0** (`continue_on_error` with a condition) and the
**github connector plugin ≥ 2.6.0** (`github.apply_diff`). Without them the
pack doesn't load (`requires`). The fixer steps of `on_failing_checks`,
`on_merge_conflict` and `on_new_comment` now have the id `fix` (they had none;
`on_changes_requested`'s is still `address`). An overlay that targets a step
by id needs the new one. A fixer no longer sets `archive_when_done: true`:
one that finishes cleanly is still released when its step ends, but a failed
one keeps its agent and workspace (its unpushed work is in it), and one the
run goes on past is released when the run ends.

v1.7.0 lets the `on_changes_requested` and `on_new_comment` agents answer on
the PR. Both steps now grant `github.reply` and `github.comment`, pinned to the
repo the run fired for and posting as you. The guidance makes every review
comment the agent doesn't change code for get a reply on its thread (where the
current code already does it, or why not), and `address` reports those ids as
`replied_comments`. Before this, a sandboxed agent had no way to post: a review
whose point was already fixed got 👀 and 👍 and no answer. Same requirements
as v1.6.0.

v1.6.0 changes how `on_changes_requested` treats an agent's report that it
pushed, with the same requirements as v1.5.0 (conductor ≥ 0.68.0, github
plugin ≥ 2.2.0). `resolve` now resolves threads only when conductor itself
pushed the run's change (`address.push.pushed`), never on the agent's word
alone. The `address` step sets `expect_push: true`: a step with an
`output_schema` is otherwise a review, which conductor never pushes, so an
agent that committed a fix had it dropped while its threads were resolved
anyway. A fix left unpushed now fails the run (`no_progress`: the failure
status and 😕 instead of a success). Conductor releases after 0.70.0 also fail
such a step on their own and credit a `git.push` made during the step.

v1.5.0 needs the **github connector plugin ≥ 2.2.0** and **conductor ≥
0.68.0**:

- Plugin 2.2.0 adds `github.check_failures` and the `failing_checks`
  event's `ignore_checks`. It also declares egress to GitHub's Actions log
  hosts, which it reaches without your GitHub credential. Update the plugin
  first; an older one refuses v1.5.0's `ci` step at `conductor validate`.
- Conductor 0.68.0 is the agent sandbox. It also gives a sandboxed fixer the
  skill endpoint it calls `check_failures` through.

The other flows are unchanged.

v1.4.0 needs the **github connector plugin ≥ 2.1.0** (`requires.connectors`):
the release whose `changes_requested` carries all waiting feedback
(`review_comments` ids, `outstanding_reviews`, `feedback_cursor`) and that adds
`github.resolve_threads` and `rerequest_review`'s `all_outstanding`. Update the
plugin first, then the pack; an older plugin refuses v1.4.0's steps at
`conductor validate`. Conductor's own requirement is unchanged (≥ 0.60.0).

With plugin 2.1.0, the first sweep fires `on_changes_requested` once on each of
your PRs that has feedback waiting on you by the rules above (its cursor starts
fresh), including threads fixed in code but never replied to or resolved.

v1.3.0 needs **conductor ≥ 0.60.0** (`requires.conductor`), the release that
adds run facts, the fail-hook guarantees, `at: stop`, `github.react` (with
`remove: true`) / `github.set_status`, and `{{.me.login}}`. Order matters:

1. Update conductor to ≥ 0.60.0. An existing lock on v1.1.x keeps working
   unchanged: no hooks, no rows.
2. Then update the pack (`conductor init`, or `conductor pack update`). A
   conductor older than 0.60.0 refuses v1.3.0 at load, so a box can't pick up
   the hooks before it can run them.
