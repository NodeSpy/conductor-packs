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
    version: 1.0.0                                  # pin: source ...//pr-review-team@v1.0.0
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

See each pack's own README for its `requires:` (what you bind), roles, and
settings. Pin a version with `@<tag>` on the `source:`; the lockfile records the
resolved sha.

## Trust

These packs ship **behavior only** — no connectors, no secrets. You bind your own
environment, and every trigger arrives disarmed (a github trigger can't even be
armed without naming its repos). Review what a pack adds with `conductor pack
plan` before arming anything.

## Contributing a pack

A pack is a directory with a `conductor-pack.yaml` manifest (+ a README). Run
`conductor pack lint <dir>` before opening a PR. See the
[Packs wiki](https://github.com/NodeSpy/conductor/wiki/Packs) for the format.
