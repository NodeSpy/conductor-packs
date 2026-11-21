# calendar-busy-sync

Keeps private **"Busy"** blocks in sync across Google calendars, so that every
calendar people book you through shows the time you're taken on the others.
It follows BusyBridge's "Ghost Main" rules. There is no UI and no database:
the calendars themselves are the state.

You give it two lists of google-calendar connectors, as many as you like in each:

| list | role |
|---|---|
| `sources` | read-only calendars (e.g. a personal Gmail, a calendar at another org) |
| `targets` (2 or more) | receive busy blocks, and each is a source for every other target |

## Rules

| source | blocks | all-day | marked Free | declined / out-of-office / `[nosync]` |
|---|---|---|---|---|
| a source | every target | skipped (`sync_personal_all_day`) | **still blocks** | skipped |
| a target | every other target | blocks | skipped | skipped |

- **What a block looks like.** Title `Busy`, opaque, private, description
  `[Busy]`. It carries no other detail from the source.
- **One block per meeting.** A meeting the target calendar already holds (same
  iCalUID and start) gets no block. A meeting on two sources gets one.
- **Hand edits are reverted.** If someone moves, renames, or deletes a block by
  hand, the next run puts it back.
- **Only today onward.** The window is now → `lookahead_days` (60). Past
  blocks are never touched.

## How it works

- **Each run recomputes everything.** It reads every calendar's window
  (expanded instances), computes the blocks each target should hold, and
  diffs them against the blocks it holds now. Then it deletes, creates, and
  repairs the difference.
- **Block ids are deterministic.** Each id is `cbs` + 32 hex, derived from
  target, source, source event, and time. Each block also carries
  `extendedProperties.private.cbs=1`. That makes runs idempotent, and a block
  deleted by hand is restored rather than duplicated.
- **Two ways a sync starts.** A Google push notification on any calendar
  (`<conn>.changed`) starts one within seconds. The 5-minute cron is the
  backstop.
- **One sync at a time.** Syncs never overlap (`serialize:`). A burst of pushes
  queues one more run, and that run re-reads everything.
- **Guards:**
  - `max_writes` (200) per run. Leftovers wait for the next run, so the first
    fill takes a few runs.
  - `max_fan_out` (100) per kind of write (delete / create / update).
  - `max_deletes` (50): a run that would delete more fails instead. A source
    calendar that read back empty looks exactly like a mass delete.
- **`dry_run` is on by default.** The run log shows what it would write.

## Install

You need conductor ≥ 0.106 (list sockets), google-calendar ≥ 1.2.0 (it
follows every result page), and backup ≥ 1.0.0.

```yaml
vaults:
  # One token vault per account, so one connector can't read another's token.
  gcal-gmail:       { type: conductor }
  gcal-tap:         { type: conductor }
  gcal-teachermade: { type: conductor }
  gcal-ednition:    { type: conductor }

connectors:
  # One google-calendar connector per account. They all share one Google
  # OAuth client (Desktop app, redirect http://localhost:8400/callback).
  gcal-gmail:
    use: google-calendar
    auth:
      grant: authorization_code
      client_id: ${GOOGLE_CLIENT_ID}
      client_secret: ${GOOGLE_CLIENT_SECRET}
      token_vault: gcal-gmail
    watch: { listen: 127.0.0.1:8471, expose: funnel }   # push; see below
  gcal-tap:         { use: google-calendar, auth: { …, token_vault: gcal-tap },         watch: { listen: 127.0.0.1:8472, expose: funnel } }
  gcal-teachermade: { use: google-calendar, auth: { …, token_vault: gcal-teachermade }, watch: { listen: 127.0.0.1:8473, expose: funnel } }
  gcal-ednition:    { use: google-calendar, auth: { …, token_vault: gcal-ednition },    watch: { listen: 127.0.0.1:8474, expose: funnel } }

  timer:
    use: cron
    schedules:
      busy_sync:   { every: 5m }            # backstop for push
      busy_backup: { cron: "17 3 * * *" }   # nightly
      busy_verify: { cron: "41 4 * * 0" }   # weekly restore test

  calbackup:
    use: backup
    tiers:
      - { name: local, driver: local, dir: ~/backups/calendar, keep_daily: 14, keep_weekly: 8, keep_monthly: 12 }
      # - { name: offsite, driver: s3, … }   # any backup tier
    allow_paths: [~/backups/calendar]

packs:
  busy:
    source: github.com/NodeSpy/conductor-packs//calendar-busy-sync
    version: "~> 1.0"
    connectors:
      sources: [gcal-gmail, gcal-tap]               # any number, each named
      targets: [gcal-teachermade, gcal-ednition]    # at least two
      cron: timer
      backup: calbackup
    settings:
      dry_run: true        # flip to false once the dry runs look right
    triggers:
      sync:    { enabled: true }
      backup:  { enabled: true }
      verify:  { enabled: true }
      cleanup: { enabled: true }   # manual only
```

Then:

```
conductor init
conductor connector auth gcal-gmail          # once per account, in a browser
conductor connector auth gcal-tap
conductor connector auth gcal-teachermade
conductor connector auth gcal-ednition
conductor validate
```

**Push** needs a public https URL that Google can post to. Use `expose:` with an
exposure connector (tailscale funnel, cloudflared, ngrok), or set `address:`.
A push from any calendar in either list starts a sync. The connector opens the
`events.watch` channel, renews it before it expires,
and stops it on shutdown. Without `watch:` the `changed` source stays idle and
the cron does all the work.

## Alerts

A failed run (a write Google refused, the delete guard, a failed restore test)
is an ordinary `conductor.failed` event. Route it like any other:

```yaml
triggers:
  - name: busy-alerts
    on: conductor.failed
    steps:
      - { uses: alerts.publish, options: { title: calendar sync, message: "{{.message}}" } }
```

## Backups

- **Nightly snapshots.** `backup` writes two snapshots per calendar into the
  backup connector's tiers:
  - `calendar-<connector>-json`: the raw API events, lossless and re-insertable.
  - `calendar-<connector>-ics`: for any calendar app.
  - `calendar-index`: the list of sets, which the restore test reads.
- **What they cover.** A year back to two years ahead, with recurring events
  kept as series.
- **Unchanged snapshots are skipped.** Each tier's retention then applies.
- **Weekly restore test.** `verify` restores the index, then each set's newest snapshot from
  every tier, checks its sha256 and size, and runs the backend's own check.
  The run fails if anything doesn't match.

## Moving from BusyBridge

Don't let the two run side by side. Each would mirror the other's blocks.
`calendar-busy-sync` already ignores BusyBridge's blocks (its `bb…` ids or
`bb_proj_id` property), but BusyBridge does not ignore these.

1. **Dry runs.** Install with `dry_run: true` and let a few syncs run alongside
   BusyBridge. The run log's `would` shows what it would write, and the
   summary gives the counts per calendar.
2. **Stop BusyBridge.**
3. **Clean up its blocks.** `conductor run busy/cleanup` only reports. Then
   `conductor run busy/cleanup --input apply=true` does it:
   - deletes BusyBridge's future one-off blocks
   - deletes recurring series that start in the future
   - **ends** its other recurring series at now (adds `UNTIL`), so past
     occurrences stay
   - leaves past one-offs alone

   It refuses more than `cleanup_max_ops` (1000) changes in total, and makes
   at most `max_fan_out` (100) of each kind per run. The log says how many
   remain; run it again until none do, or to retry anything that failed.
4. **Go live.** Set `dry_run: false`. The first syncs fill the next 60 days
   (`max_writes` per run).

## Settings

| setting | default | |
|---|---|---|
| `dry_run` | `true` | plan and report only |
| `lookahead_days` | `60` | how far ahead blocks are kept |
| `busy_title` | `Busy` | block title |
| `managed_tag` | `[Busy]` | block description (search for it to find every block) |
| `nosync_tag` | `[nosync]` | opt an event out (description, any case) |
| `sync_personal_all_day` | `false` | mirror all-day events from `sources` |
| `sync_out_of_office` | `false` | mirror out-of-office events |
| `max_writes` | `200` | writes per run |
| `max_deletes` | `50` | fail rather than delete more |
| `max_fan_out` | `100` | per kind of write; keep ≤ `policy.max_fan_out` |
| `backup_tiers` | all | comma-separated tiers for backup/verify |
| `backup_prefix` | `calendar` | set-name prefix |
| `backup_days_back` / `_ahead` | `365` / `730` | backup window |
| `cleanup_max_ops` | `1000` | BusyBridge cleanup cap |

## Tests

```
python3 -m unittest discover -s calendar-busy-sync/tests
```

`busy.js` runs under node exactly as the js engine runs it. The tests cover
the rules, the diff, the guards, the cleanup, and the ICS output.
