# calendar-backup

Nightly snapshots of any number of Google calendars into a
[`backup`](https://github.com/NodeSpy/conductor-plugins/blob/main/docs/connectors/backup.md)
connector's tiers, and a weekly restore test. Read-only on the calendars: it
never writes to them.

Pure verbs and one js step. No agent, no model, no cost.

## What it does

- **`backup` (nightly).** Per calendar, two snapshots:
  - `<prefix>-<connector>-json`: the raw API events, lossless and re-insertable.
  - `<prefix>-<connector>-ics`: for any calendar app.

  Plus `<prefix>-index`, the list of sets, which the restore test reads. The
  window is a year back to two years ahead, repeating events kept as series.
  Unchanged snapshots are skipped; each tier's retention then applies.
- **`verify` (weekly).** Restores the index, then each set's newest snapshot
  from every tier, checks its sha256 and size, and runs the backend's own
  check. The run fails if anything doesn't match.

## Install

It uses the same `google-calendar` connectors as
[`calendar-busy-sync`](../calendar-busy-sync/) (see its README for the
Google OAuth setup); list every calendar you want kept.

```yaml
connectors:
  timer:
    use: cron
    schedules:
      busy_backup: { cron: "17 3 * * *" }   # nightly
      busy_verify: { cron: "41 4 * * 0" }   # weekly restore test

  calbackup:
    use: backup
    tiers:
      - { name: local, driver: local, dir: ~/backups/calendar, keep_daily: 14, keep_weekly: 8, keep_monthly: 12 }
      # - { name: offsite, driver: s3, … }   # any backup tier
    allow_paths: [~/backups/calendar]

packs:
  calbak:
    source: github.com/NodeSpy/conductor-packs//calendar-backup
    version: "~> 1.0"
    connectors:
      calendars: [cal-personal, cal-work, cal-side]
      cron: timer
      backup: calbackup
    triggers:
      backup: { enabled: true }
      verify: { enabled: true }
```

A failed backup or restore test is an ordinary `conductor.failed` event; route
it to your alerts like any other.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `backup_tiers` | all | Comma-separated tiers to write and verify |
| `backup_prefix` | `calendar` | Set-name prefix |
| `backup_days_back` / `_ahead` | `365` / `730` | Backup window |

## Tests

```sh
python3 -m unittest discover -s calendar-backup/tests   # needs node + PyYAML
```
