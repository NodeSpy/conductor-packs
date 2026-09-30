# pr-review-team

A distributable [conductor](https://github.com/NodeSpy/conductor) **pack**:
multi-lens pull-request review with an optional human hand-off.

On a review request it runs a panel of lens reviewers in parallel
(Architecture, Complexity, Performance, Refactoring, Security, Test quality),
refute-verifies every finding to drop the ones that are positively wrong,
reconciles the survivors deterministically into a single review (one comment per
file:line, blocking-first), then **either auto-posts** the review or **hands the
draft to a human** to approve, edit, or discard.

```
review_requested ─▶ assess ─▶ review-team (always) ─▶ post      (auto)
                                                    └▶ hand-off  (manual)
```

The multi-lens review always runs, so the manual branch has a real drafted
review to hand off — the human edits/approves it instead of reviewing from
scratch.

## 2.10.0 — whole-PR reviews

Earlier versions inlined a single, 60 KB-capped unified diff into every
prompt — a lens reviewer, `verify`, and `assess` all read the same truncated
text. 2.10.0 (conductor issue #154 §6) replaces that with real, uncapped
access to the PR, sized to what each step actually needs:

- **The lens reviewers get a real PR checkout.** `review-team/review` now
  runs with `checkout: checkout-pr` (conductor's own PR worktree) instead of
  `checkout: none` plus an inlined diff. The prompt carries the PR metadata
  and the **complete** file list (every changed file's status and line
  counts, from `github.pr_files` with `all: true` — no 300-file diff cap, no
  60 KB text cap) and tells the reviewer the full change is `git diff
  origin/<base>...HEAD` in its own checkout, and that it may read any file
  for context. The step carries `output_schema`, which makes it a review
  step: in the jail its `gh` and `git` are read-only (no push, no gh writes),
  and it is granted no write verbs.
- **`verify` judges each finding against bounded, per-finding context**
  instead of the whole PR's diff: the flagged file's own patch (looked up
  from `github.pr_files`) and a window of that file **at the PR head**,
  centered on the flagged line (±30 lines, hard-capped at 8000 bytes). This
  is bounded per finding regardless of the PR's overall size. conductor's
  template layer has no function to slice a file by line number (see
  `internal/flow/template.go`), so the window is computed in the pack's own
  `run: js` step (`windows`) — the same mechanism the old diff cap already
  used — rather than inventing a new template function.
- **`assess` sees every changed file** (path, status, additions/deletions)
  plus as many file patches as fit a 50 KB budget, chosen in `pr_files`'
  own deterministic order; the file list itself is never truncated, only the
  patches bundled alongside it.
- `review-team`'s public `diff:` output (the old capped-diff artifact) is
  **removed** — nothing inside the pack renders a capped copy of the diff
  anymore. A caller that wants the raw diff should call `github.pr_diff`
  itself.
- Bumped `requires.conductor` to `>=0.57.0`, the release that shipped the
  agent workspace jail (#154) and `github.pr_files[].patch`.

One tradeoff worth knowing: each lens reviewer now provisions its own PR
worktree, so a review with the default six lenses does six parallel
checkouts instead of one diff fetch. That is the cost of giving every lens
its own uncapped, read-only view of the tree.

## Install (drop-in)

Bind your github connector and arm the trigger — that's it. The pack brings its
own review-team/assess/hand-off steps, so you don't define any agents:

```yaml
packs:
  review:
    source: github.com/NodeSpy/conductor-packs//pr-review-team   # or a local path
    version: "=2.0.0"                              # exact pin; a bare "2.0.0" floats to the newest >=2.0.0 tag
    preset: claude                                 # or codex; omit for your default runtime
    connectors: { github: gh }                    # bind the pack's github -> your connector
    triggers:
      on_review_request:
        enabled: true                              # arm it
        repos:   [your-org/app]                    # scope it — REQUIRED for a github trigger
```

Then:

```
conductor init            # fetch + lock + vendor the pack
conductor pack plan       # preview what it adds (agents, skill grants, armed triggers)
conductor validate
```

The only thing you *must* bind is the `github` connector — it carries your
credentials, so the pack can't ship it.

## What it needs (`requires:`)

- **`conductor: >=0.57.0`** — the release with the agent workspace jail
  (#154) and `github.pr_files[].patch`, which the lens
  reviewers, `verify`, and `assess` all rely on (see "2.10.0 — whole-PR
  reviews" above). It supersedes the older floors this pack also needs:
  0.54.0 for `decide:` steps (`assess` and `verify`), and 0.33.0 for the
  reactive hand-off surface the handoff step uses.
- **A `github` connector**, bound in the `packs:` block. That's the only bind.
- The reviewer / assessor / hand-off behavior ships as steps with working
  defaults (see Tuning). Override one by qualified reference — `steps: {
  review-team/review: { model: my-fleet } }` — only if you want to change it.

## Tuning

- **Models** — pick a bundled `preset:` (`claude` or `codex`), or set the
  typed settings directly without touching the pack:

  ```yaml
  settings: { heavy_model: <your reviewer model>, light_model: <your other model> }
  ```

  `heavy_model` drives the lens panel (the strongest pass); `light_model`
  drives triage, refute-verify, and the hand-off agent. Both default to `"*"`
  (any model your runtime offers) when no preset or setting is given.
- **Triage and refute-verify are `decide:` steps** — typed yes/no and choice
  questions answered with probabilities, not agent runs. Any runtime in the
  light tier answers them (an agent runtime through conductor's adapter, in one
  restricted session). To have a decision runtime such as Jev answer first, add
  its models to the tier — `models: { light: ["jev-*", "claude-sonnet-*"] }` —
  with the runtime configured ([jev runtime](https://github.com/NodeSpy/conductor-plugins/blob/main/docs/runtimes/jev.md)).
  `verify` refutes a finding only when P(refuted) ≥ 0.8. Record every decision
  for calibration with `decide: { observe: <your SQL store> }` on the pack
  instance ([Decide steps](https://github.com/NodeSpy/conductor/blob/main/docs/wiki/Decide-Steps.md)).
- **Step overrides** — override any exported step by its qualified reference
  in the `packs:` block, e.g. `steps: { review-flow/handoff: { model: ... } }`.
  See `exports:` in the manifest for the full list of overridable steps.
- **Lenses** — the six review lenses are the `review-team` workflow's `lenses`
  input default; override that input to review through a subset.
- **Clean reviews auto-post** — when the team finds nothing (an APPROVE with no
  comments), it posts the approval and skips the hand-off. A human is only pulled
  in when there's something to weigh in on (a manual-flagged PR *with* comments).
- **Large PRs** — nothing is capped by file count or diff size anymore (2.10):
  the lens reviewers work from a real checkout, `verify` reads a bounded
  per-finding patch + file window, and `assess` reads every file's line
  counts plus a byte-budgeted slice of patches. See "2.10.0 — whole-PR
  reviews" above for the exact bounds.

**No secrets.** Posting uses your github connector's own write identity, so the
pack requires no broker secret.

## Consent is load-bearing

The `on_review_request` trigger ships **disarmed**. It does nothing until you set
`enabled: true` **and** name `repos:` — and a github trigger is **rejected at
load** if armed without repos, so it can never silently review every repo your
connector can reach. The repo list is the consent.

## Authoring

This is a single `conductor-pack.yaml` manifest. Validate changes with
`conductor pack lint .` before publishing.
