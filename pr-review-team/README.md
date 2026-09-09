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

## Install

Add it to your config's `packs:` block, bind your github connector and (optionally)
your own agent profiles, then arm the trigger:

```yaml
packs:
  review:
    source: github.com/NodeSpy/conductor-packs//pr-review-team   # or a local path
    connectors: { github: gh }                    # bind the pack's github -> your connector
    agents:                                        # bind roles to your profiles (omit for defaults)
      reviewer: opus
      assessor: sonnet
      handoff:  sonnet-interactive
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

## What it needs (`requires:`)

- **`conductor: >=0.8.0`** — the packs mechanism. (`>=0.8.1` also gives the
  oversized-prompt guard, though this pack caps the diff itself.)
- **A `github` connector**, bound in the `packs:` block.
- **Three agent roles**, bound to your profiles (or left as the bundled defaults,
  which fall through to your default runtime):
  - `reviewer` — runs each lens.
  - `assessor` — triages auto-vs-manual and refute-verifies findings.
  - `handoff` — presents the draft to a human and posts it on approval; it needs
    the `github.submit_review` skill verb (declared for you on the bundled role).

**No secrets.** Posting uses your github connector's own write identity, so the
pack requires no broker secret.

## Consent is load-bearing

The `on_review_request` trigger ships **disarmed**. It does nothing until you set
`enabled: true` **and** name `repos:` — and a github trigger is **rejected at
load** if armed without repos, so it can never silently review every repo your
connector can reach. The repo list is the consent.

## Tuning

- **Lenses** — the six review lenses are the `review-team` workflow's `lenses`
  input default. Review through a subset by overriding that input (e.g. from a
  manual trigger or a `conductor run` wrapper).
- **Large diffs** — the reviewer/verifier prompts cap the inlined unified diff
  at 60 KB (a note marks the truncation) so a big PR never overflows the agent
  runner's argument limit.

## Authoring

This is a single `conductor-pack.yaml` manifest. Validate changes with
`conductor pack lint .` before publishing.
