# Set up a self-training PR reviewer (pr-review-team)

This sets up conductor to review PRs you're requested on, posting as you.
- **It trains itself on your repos:** an optional one-time backfill from past
  review comments, then a learning run every week.
- **It's fully automatic:** it posts and approves without asking first.

## 0. Before you start

- **Linux or macOS**, with `git`, `gh` and `python3` installed.
- **`gh auth login`** done with your own GitHub account. Reviews post as that account.
- **Claude Code installed** (`claude`).
  - Agents run in conductor's sandbox, which doesn't see your normal login.
  - So create a long-lived token: run `claude setup-token` and keep the token.

## 1. Install conductor

```sh
curl -fsSL https://raw.githubusercontent.com/NodeSpy/conductor/main/scripts/install-release.sh | bash
conductor version        # needs v0.70.0 or newer
```

Accept the offer to install the background service (systemd/launchd).

## 2. Give sandboxed agents a model credential

Create `~/.config/conductor/conductor.env`, readable only by you
(`chmod 600 ~/.config/conductor/conductor.env`). Put **one** of these in it:

```sh
# Claude subscription (the token from `claude setup-token`):
CLAUDE_CODE_OAUTH_TOKEN=<token>

# Or an API key:
# ANTHROPIC_API_KEY=<key>

# Or a local model router/proxy (it must listen on 127.0.0.1):
# ANTHROPIC_BASE_URL=http://127.0.0.1:<port>
# ANTHROPIC_API_KEY=<key the router expects>
```

## 3. Config

Replace `~/.config/conductor/config.yaml` with the config below.
- Change `REVIEWER_REPOS` to the repos you want reviewed.
- Change the schedule time if you like.

```yaml
connectors:
  gh:
    use: github            # credentials come from your `gh` login; events arrive by polling (no webhook needed)
  timer:
    use: cron
    schedules:
      review_calibration: { cron: "23 8 * * 1" }   # weekly learning run, Mondays 08:23

memory:
  dir: /home/YOU/.config/conductor/memory   # full path: ~ is not expanded here (macOS: /Users/YOU/...). Your own learned notes, one Markdown file each.

packs:
  review:
    use: conductor-packs/pr-review-team
    version: "~> 3.1"
    settings:
      auto: true            # never hand off: every review posts as you, approvals included
      review_passes: 2      # each lens reviews twice; findings are merged
    steps:
      # The learning/eval scripts need gh, git and conductor's state directory.
      review-calibration/collect: { trust: full }
      review-calibration/record:  { trust: full }
      review-backfill/collect:    { trust: full }
      review-backfill/record:     { trust: full }
      review-eval/pick:           { trust: full }
      review-eval/report:         { trust: full }
    triggers:
      on_review_request:
        enabled: true
        repos: [your-org/your-repo]   # REVIEWER_REPOS: the repos it may review
        filter:
          not_draft: true
      review-calibration: { enabled: true }
      review-backfill:    { enabled: true }
      review-eval:        { enabled: true }

policy:
  pause_label: conductor:off   # label a PR with this to make conductor leave it alone

triggers:
  - name: weekly-review-calibration
    on: timer.review_calibration
    steps:
      - uses: conductor.run
        options: { name: review/review-calibration }
```

Then:

```sh
conductor init                          # fetches the packs and plugins
conductor validate                      # must print "ok: …"
systemctl --user restart conductor      # macOS: launchctl kickstart -k gui/$(id -u)/conductor
```

From now on, whenever you're requested as a reviewer on one of those repos, conductor reviews the PR and posts as you.

## 4. Train it on your own history (optional, recommended)

Seed it once from the repo's past review comments. You can use the whole
history or a window.

```sh
conductor run review/review-backfill --input repos=your-org/your-repo --input days=365
# whole history, starting clean (archives earlier learned notes, doesn't delete them):
conductor run review/review-backfill --input repos=your-org/your-repo --input days=all --input fresh=true
```

What it learns from:
- It only learns from repo owners, org members and collaborators.
- Bots and outside contributors are ignored.

The weekly run then keeps learning from:
- comments engineers leave after the reviewer approved, which are the strongest signal;
- follow-up fixes and teammates' review comments.

In auto mode, **you teach it by commenting** on a PR it reviewed when it missed something.

## 5. Approve what it holds

Some lessons are held in quarantine and never shown to reviews:
- anything that could make reviews weaker;
- new standards with no human catch behind them;
- every per-engineer "perspective".

Each run lists them, with the exact text and a one-line promote command, in:

```
~/.local/state/conductor/pr-review-team/calibration-log.md
```

Read the text first, then run its command to make it live.

## 6. Check it's helping (optional)

```sh
# memory on vs off, against the run-to-run noise floor:
conductor run review/review-eval --input repos=your-org/your-repo --input count=20
# against what engineers actually commented (needs PRs reviewed after training):
conductor run review/review-eval --input repos=your-org/your-repo --input baseline=team
```

Reports go in `~/.local/state/conductor/pr-review-team/eval/`.

## 7. Back up or move what it learned (optional)

Export your learned notes as a pack, e.g. to keep a backup or to move them to
another machine:

```sh
conductor memory export --out ~/review-memory-backup --name my-review-memory \
  --scope <owner/repo> --scope standard:<owner/repo> --scope emphasis:<owner/repo>
```

Notes about the export:
- It drops provenance: the run, step and source fields.
- Held (quarantined) notes aren't included.

## Turning things down

| To… | Do this |
|---|---|
| Have it ask you before posting | Set `auto: false`. It hands drafts to you when it isn't sure. |
| Halve model usage | Set `review_passes: 1`. |
| Pause it on one PR | Add the label `conductor:off`. |
