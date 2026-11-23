# calendar-busy-sync

Keep several Google calendars honest about when you are busy.

If you have a work calendar, a second work calendar and a personal one, people
booking you on one cannot see what the others hold. This pack copies busy time
across: every event on a **source** calendar becomes a private "Busy" block on
every other **target** calendar. Blocks follow the source event — moved when it
moves, removed when it is cancelled, declined or marked free.

Pure verbs and js steps. No agent, no model, no cost, no database: the
calendars are the state.

You give it two lists of google-calendar connectors, as many as you like in each:

| list | role |
|---|---|
| `sources` | whose events block time |
| `targets` | where blocks are written |

A calendar may sit in both lists (two work calendars that block each other);
it never gets a block for its own events. Leave a calendar out of `targets` to
keep blocks off it.

## What it does

One workflow, `reconcile`, shared by three triggers:

| Trigger | Fires on | Why |
|---|---|---|
| `on_change` | a source calendar changing (Google push, via the connector's `changed` source) | blocks appear seconds after an edit |
| `on_target_change` | a target calendar changing | a block deleted or moved by hand comes back in seconds, not at the next sweep |
| `sweep` | your `busy_sync` cron schedule, or `conductor run <inst>/sweep` | Google push is best-effort and nothing arrives while the daemon is down; the sweep also rolls the window forward |

Each run reads every source and target for the next `window_days`, works out
which blocks should exist, and creates, changes or removes blocks until the
targets match. A block carries a private extended property naming the source
event it stands for, so a second run is a no-op and a missed run is caught up
by the next one. Every trigger that writes shares one queue (`serialize:`), so
two reconciles never race.

A repeating event gets **one repeating block**, not one block per occurrence.
The block copies the event's own recurrence rule, starts at the first
occurrence still ahead, and leaves out the occurrences that need no block:
cancelled, declined, moved (a moved one gets a block of its own where it
went), or already busy on the target. Two cases fall back to a block per
occurrence: a rule that ends after a set number of times (`COUNT`), and a
series where most of the occurrences ahead need no block. Set
`repeating_blocks: false` for a block per occurrence throughout.

It does not double up. A block is never copied onward, and BusyBridge's
blocks are never mirrored either. A meeting the target already holds (same
iCalUID and start) gets no block, and a meeting on two sources gets one. Time
the target already shows as busy gets no block either (`skip_already_busy`):
a slot you had already blocked by hand. If the target's own event goes away
later, the block appears on the next run.

## Rules

Two rule sets, picked with `rules:`.

**`uniform`** (the default) treats every source the same:

| blocks | skipped |
|---|---|
| what shows as busy and you have not declined, out-of-office included | marked Free, declined, working-location entries, `[nosync]`, all-day (unless `mirror_all_day`) |

**`busybridge`** follows BusyBridge's "Ghost Main" rules. A calendar only in
`sources` is a read-only, personal calendar; a calendar in `targets` is one
you are booked through:

| source | blocks | all-day | marked Free | declined / out-of-office / `[nosync]` |
|---|---|---|---|---|
| only in `sources` | every target | skipped (`mirror_all_day`) | **still blocks** | skipped |
| in `targets` | every other target | blocks | skipped | skipped |

`sync_out_of_office: true` or `false` overrides either rule set's choice.

## Requires

- conductor `>=0.106.0` (list sockets).
- The [`google-calendar`](https://github.com/NodeSpy/conductor-plugins/blob/main/docs/connectors/google-calendar.md)
  connector `>=1.2.0`, one instance per calendar.
- A `cron` connector with a `busy_sync` schedule.
- For `on_change` / `on_target_change`: an exposure connector (smee, tailscale
  funnel, cloudflared, ngrok) so Google can reach each connector's `watch`
  receiver. Without one, arm only `sweep`.

Backups of the same calendars are a separate pack,
[`calendar-backup`](../calendar-backup/).

## Google setup

One Google Cloud project and one OAuth client serve every account, work and
personal alike. Each calendar then logs in on its own.

### 1. Create the OAuth client

1. In the [Google Cloud console](https://console.cloud.google.com), create a
   project. A personal Google account is the simplest owner: a project inside a
   Workspace organisation can be limited to that organisation's users.
2. **APIs & Services → Library** → enable the **Google Calendar API**.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID.**
   The console first asks for the consent-screen basics: an **app name**, a
   **support email**, audience **External**, and a contact email.
4. Application type **Web application**. Under **Authorized redirect URIs** add
   `http://localhost:8400/callback`. Leave JavaScript origins empty.
5. Put the client id and secret in conductor's environment
   (`conductor.env`) as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

That is enough to log in and try the pack. The app is now in **Testing**.

### 2. If you stop there: Testing, with only an app name

It works, with two catches:

- Only accounts listed under **Audience → Test users** can log in. Add each
  calendar's account.
- **Google expires every refresh token after 7 days.** The sweep then fails
  until you run `conductor connector auth <name>` again for each calendar,
  every week.

Fine for a trial. For anything you want to leave running, publish.

### 3. Publish: branding, then audience

Publishing does not mean submitting for Google's verification, and nobody else
gains access. It only lifts the 7-day expiry.

1. **Branding.** Google will not publish without an **application home page**,
   and a home page needs its domain listed under **Authorized domains**. Use a
   domain you control: home page `https://example.com`, authorized domain
   `example.com`. Ownership is only checked if you submit for verification.
   Leave the **logo** empty — uploading one makes Google ask for verification.
2. **Audience → Publishing status → Publish app**, and confirm.

Google will say the app needs verification because it asks for calendar
access. You can leave it unverified: the login shows a "Google hasn't verified
this app" page (**Advanced → Go to …**), and the app is capped at 100 users.

**Log in again after publishing.** A token issued while the app was in Testing
keeps its 7-day expiry; only a login made after publishing gets one that lasts.

### 4. Work accounts

A Google Workspace admin can block unverified third-party apps. If a work
account refuses the login, its admin has to trust your client id: Admin console
→ **Security → API controls → App access control**.

### 5. Log in each calendar

With the connectors from [Install](#install) in your config, log in once per
connector, choosing the matching Google account in the browser:

```sh
conductor connector auth cal-personal
conductor connector auth cal-work
conductor connector auth cal-side
```

`conductor connector auth ls` shows each one as logged in. The access token it
lists lasts an hour; conductor renews it from the stored refresh token, so
there is nothing to redo unless that refresh token expires (step 2) or you
revoke access.

## Install

```yaml
vaults:
  calsync: { type: conductor }          # holds the refresh tokens

connectors:
  timer: { use: cron, schedules: { busy_sync: { every: 15m } } }

  cal-personal:
    use: google-calendar
    auth: &gauth
      grant: authorization_code
      client_id: ${GOOGLE_CLIENT_ID}
      client_secret: ${GOOGLE_CLIENT_SECRET}
      token_vault: calsync
  cal-work:
    use: google-calendar
    auth: *gauth
  cal-side:
    use: google-calendar
    auth: *gauth

packs:
  busy:
    source: github.com/NodeSpy/conductor-packs//calendar-busy-sync
    version: "~> 1.0"
    connectors:
      sources: [cal-personal, cal-work, cal-side]   # whose events block time
      targets: [cal-work, cal-side]                 # where blocks are written
      cron: timer
    settings:
      dry_run: true                                 # first run: log the plan only
    triggers:
      sweep: { enabled: true }
```

Then `conductor vault calsync init`, `conductor init`, `conductor pack plan`,
`conductor validate`.

That is sweep-only: a block appears within one `busy_sync` interval of the
event. To get blocks within seconds, give each connector a `watch:` block and
arm the push triggers as well:

```yaml
  cal-personal:
    use: google-calendar
    auth: *gauth
    watch: { listen: 127.0.0.1:8411, expose: cal-relay }   # an exposure connector of its own
```

```yaml
    triggers:
      on_change:        { enabled: true }
      on_target_change: { enabled: true }
      sweep:            { enabled: true }   # keep it: push is best-effort
```

Give each calendar its own exposure. A smee channel shared with another
listener (your GitHub webhooks, a second calendar) delivers every message to
all of them.

Nobody books you through your personal calendar, so the example above reads it
but never writes to it — which also means a work meeting shows twice in a
merged view, not three times.

Start with `dry_run: true` and read the plan line in the daemon log:

```
calendar-busy-sync: 14 to create, 0 to change, 0 to remove across 2 target(s) from 3 source(s); occurrences to block: cal-personal -> cal-side 10, cal-personal -> cal-work 10; 4 repeating block(s); 20 occurrence(s) not yet in step (dry run: nothing written)
```

"Occurrences to block" counts calendar slots; "to create" counts events, and a
repeating block is one event however many slots it covers. "Not yet in step"
is how many slots still differ from the plan: it drops to zero once the runs
have caught up, and a number that stays up means a block is not landing where
the plan expects.

When the split looks right, remove `dry_run` and reload.

## Alerts

A failed run (a write Google refused, the delete guard) is an ordinary
`conductor.failed` event. Route it like any other:

```yaml
triggers:
  - name: busy-alerts
    on: conductor.failed
    steps:
      - { uses: alerts.publish, options: { title: calendar sync, message: "{{.message}}" } }
```

## Moving from BusyBridge

Don't let the two run side by side: BusyBridge would mirror this pack's
blocks. This pack already ignores BusyBridge's (its `bb…` ids or `bb_proj_id`
property).

1. **Dry runs.** Install with `rules: busybridge` and `dry_run: true`, and let
   a few sweeps run alongside BusyBridge. Put the calendars BusyBridge treats
   as personal only in `sources`, and its client calendars in both lists.
2. **Stop BusyBridge.**
3. **Clean up its blocks.** Arm the `cleanup` trigger. `conductor run
   busy/cleanup` only reports; `conductor run busy/cleanup --input apply=true`
   does it, on every target:
   - deletes BusyBridge's future one-off blocks
   - deletes its repeating series that start in the future
   - **ends** its other repeating series at now (adds `UNTIL`), so past
     occurrences stay
   - leaves past one-offs alone

   It refuses more than `cleanup_max_ops` (1000) changes in total, and makes
   at most `batch_size` of each kind per run. The log says how many remain;
   run it again until none do.
4. **Go live.** Set `dry_run: false`. The first sweeps fill the window,
   `batch_size` blocks of each kind per run.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `rules` | `uniform` | `uniform` or `busybridge` (see [Rules](#rules)) |
| `window_days` | `30` | How far ahead to mirror |
| `mirror_all_day` | `false` | Also mirror all-day events that are marked busy (`busybridge`: those of calendars only in `sources`) |
| `sync_out_of_office` | (rules) | `true` / `false` overrides the rule set |
| `nosync_tag` | `[nosync]` | An event whose description has this (any case) is never mirrored. Empty turns it off |
| `skip_already_busy` | `true` | No block where the target already shows busy on its own |
| `repeating_blocks` | `true` | One repeating block per repeating event; `false`: one per occurrence |
| `block_title` | `Busy` | Title of a block |
| `show_details` | `false` | Use the source event's title instead. Blocks stay private either way |
| `color_id` | `8` | Google event colour for blocks (1–11; 8 is graphite). Empty keeps the calendar's colour |
| `managed_tag` | (empty) | Written as each block's description, e.g. `[Busy]`, to search a calendar for its blocks |
| `tag` | `conductorBusySync` | Extended-property name marking a block as this pack's |
| `dry_run` | `false` | Plan and log only |
| `max_deletes` | `50` | A run that would delete more than this many blocks whose source events have vanished fails instead |
| `batch_size` | `50` | Most blocks created, moved or removed per run (and cleanup changes). The rest follow on the next runs. Keep it under your `policy.max_fan_out` (100 by default) |
| `remove_all` | `false` | Remove every block in the window instead of syncing |
| `cleanup_max_ops` | `1000` | BusyBridge cleanup cap |

## Good to know

- **Blocks are real events on the target's calendar.** That is what makes other
  people's scheduling see them, and it is why a calendar app showing all your
  accounts at once shows the event and its blocks side by side. A muted
  `color_id` keeps them in the background.
- **Blocks are private and silent**: visibility `private`, no reminders, no
  attendees, so nobody is notified.
- **A repeating block reaches past the window.** It follows the event's rule
  for as long as that runs, so a change to one occurrence months out (a
  decline, a cancellation) only reaches the block once that occurrence comes
  within `window_days`.
- **You should not need to tidy blocks by hand.** A block goes on its own as
  soon as it is no longer needed. If you do delete one, the next run puts it
  back while its event still needs it.
- **To clear every block**, set `remove_all: true` and leave it for a few
  sweeps (each removes `batch_size` blocks). Do this before uninstalling;
  uninstalling alone leaves the blocks in place.
- **Removing a calendar from `sources` removes its blocks** over the next runs.
- **Renaming a connector re-creates its blocks**, since a block is keyed by the
  source connector's name.
- **The triggers lift conductor's attempt limit.** conductor backs off and
  then parks a trigger that keeps firing for one target (`max_attempts_per_head`),
  which would stop a sweep after six runs. The pack's triggers set that limit
  out of reach and run one at a time.
- **A big first sync takes a few runs.** Each run writes at most `batch_size`
  blocks of each kind, so 130 new blocks fill in over three sweeps.
- **Past blocks are left in place.** Only the window ahead is reconciled.

## Tests

```sh
python3 -m unittest discover -s calendar-busy-sync/tests   # needs node + PyYAML
```

The tests run `busy.js` and the manifest's `window` step under node, exactly
as the js engine runs them. The workflow's Google calls are not exercised.
