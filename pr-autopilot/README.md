# pr-autopilot

Autopilot for the routine events on **your** PRs. One agent per event, scoped
to the repos you arm:

| trigger | event | the agent… |
|---|---|---|
| `on_failing_checks` | CI concluded failing | finds why and pushes a fix (re-runs once first: most red CI is a flake) |
| `on_merge_conflict` | the PR conflicts with its base | resolves the conflict and pushes |
| `on_changes_requested` | a review requested changes (or sweep: unresolved threads) | addresses the feedback, pushes, re-requests the reviewer |
| `on_new_comment` | a comment, or a review no changes-requested trigger takes | makes the change or answers the question |

Install and arm it in your config's `packs:` block. The example is in the
header of [`conductor-pack.yaml`](conductor-pack.yaml).

## Progress on the PR

Every flow carries plain `hooks:` that show, **as you**, that the event was
taken and how it went. None of it is hidden or automatic: the hooks are in
`conductor-pack.yaml`, and every word they post is a setting.

| when | reaction (review/comment flows only) | commit status (one row per flow) |
|---|---|---|
| start, before any worktree or agent exists | 👀 on the review or comment | `pending` on the commit the run starts on |
| done, pushed | 🚀 | `success` on that **start** commit |
| done, nothing pushed | 👍 | `success` on that start commit (= the PR's head) |
| fail (including a dispatch that never came up, and a park) | 😕 | `failure` on the PR's **current** head, "gave up: …" |

**The row disappears after a push.** GitHub can't delete a commit status, so
the success goes on the commit the run started on, never on the new head.
When the run pushed, the PR's head is a new commit with no row of that
context, and the row is simply gone from the PR. When nothing was pushed, a
green ✓ stays on the unchanged head. A failure goes on whatever the PR's head
is when the run gives up, so it's what you see.

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
    version: "~> 1.2"
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

Contexts and descriptions are templates rendered per run. `{{.run.reason}}`
is a short public-safe phrase ("the agent couldn't be started", "timed out",
"the change was never pushed", …), never the error text.

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
  - { at: done, if: reaction_subjects, uses: gh.react,
      options: { repo: "{{.repo}}", pr: "{{.pr}}", subjects: "{{.reaction_subjects}}",
                 content: "{{if .run.pushed}}rocket{{else}}+1{{end}}" } }
  - { at: done, uses: gh.set_status,
      options: { repo: "{{.repo}}", sha: "{{.run.start_sha}}", state: success,
                 context: "{{.me.login}} / mine", description: "done" } }
  - { at: fail, if: reaction_subjects, uses: gh.react,
      options: { repo: "{{.repo}}", pr: "{{.pr}}", subjects: "{{.reaction_subjects}}", content: confused } }
  - { at: fail, uses: gh.set_status,
      options: { repo: "{{.repo}}", pr: "{{.pr}}", state: failure,
                 context: "{{.me.login}} / mine", description: "gave up: {{.run.reason}}" } }
```

`reaction_subjects` exists only on `changes_requested` and `new_comment`
events. Leave the reaction hooks off triggers for other events. Hooks are
best-effort: a reaction or status that fails to post is logged and never fails
the run. Conductor never reads a status it posted back as CI, so a `failure`
row can't trigger the next fix.

## Upgrading

v1.2.0 needs **conductor ≥ 0.60.0** (`requires.conductor`), the release that
adds run facts, the fail-hook guarantees, `github.react` / `github.set_status`,
and `{{.me.login}}`. Order matters:

1. Update conductor to ≥ 0.60.0. An existing lock on v1.1.x keeps working
   unchanged: no hooks, no rows.
2. Then update the pack (`conductor init`, or `conductor pack update`). A
   conductor older than 0.60.0 refuses v1.2.0 at load, so a box can't pick up
   the hooks before it can run them.
