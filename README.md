# conductor-packs

Distributable [conductor](https://github.com/NodeSpy/conductor) **packs**
maintained by NodeSpy — installable, versioned bundles of behavior (workflows,
agents, policy, checks, disarmed triggers) you drop into your config's `packs:`
block.

## Packs

| Pack | What it does | Source |
|------|--------------|--------|
| [`pr-review-team`](pr-review-team/) | Multi-lens PR review (parallel lens reviewers → refute-verify → deterministic reconcile) with auto-post or human hand-off | `github.com/NodeSpy/conductor-packs//pr-review-team` |

## Installing a pack

Each pack lives in its own subdirectory; reference it with the `//subdir` source
syntax. In your config's `packs:` block:

```yaml
packs:
  review:
    source: github.com/NodeSpy/conductor-packs//pr-review-team
    version: "=2.0.0"                                # exact pin; a bare "2.0.0" floats to the newest >=2.0.0
    connectors: { github: gh }                      # bind the pack's required env to yours
    triggers:
      on_review_request: { enabled: true, repos: [your-org/app] }
```

Then:

```
conductor init          # fetch + lock + vendor
conductor pack plan     # preview what it adds (agents, skill grants, armed triggers)
conductor validate
```

See each pack's own README for its `requires:` (what you bind) and settings.
This is a monorepo of several packs under one repo, so each pack's git tags are
component-prefixed — `<pack-dir>/vX.Y.Z` (e.g. `pr-review-team/v2.0.0`), not a
bare `vX.Y.Z` — and `conductor init` resolves that prefix for you from the
`version:` constraint. Pin with `version: "=X.Y.Z"` (exact) or `version: X.Y.Z`
(floor, floats forward on the next `conductor init`/`pack update`); the
lockfile records the resolved sha either way.

## Trust

These packs ship **behavior only** — no connectors, no secrets. You bind your own
environment, and every trigger arrives disarmed (a github trigger can't even be
armed without naming its repos). Review what a pack adds with `conductor pack
plan` before arming anything.

## Contributing a pack

A pack is a directory with a `conductor-pack.yaml` manifest (+ a README). Run
`conductor pack lint <dir>` before opening a PR. See the
[Packs wiki](https://github.com/NodeSpy/conductor/wiki/Packs) for the format.
