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

- **`conductor: >=0.9.0`** — the step surface this pack is written against: no
  `agents:` registry, behavior lives on the step, `x-` anchors with `extends:`,
  and skill grants bounded by `requires.connectors`.
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
- **Step overrides** — override any exported step by its qualified reference
  in the `packs:` block, e.g. `steps: { review-flow/handoff: { model: ... } }`.
  See `exports:` in the manifest for the full list of overridable steps.
- **Lenses** — the six review lenses are the `review-team` workflow's `lenses`
  input default; override that input to review through a subset.
- **Clean reviews auto-post** — when the team finds nothing (an APPROVE with no
  comments), it posts the approval and skips the hand-off. A human is only pulled
  in when there's something to weigh in on (a manual-flagged PR *with* comments).
- **Large diffs** — the reviewer/verifier prompts cap the inlined diff at 60 KB.

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
