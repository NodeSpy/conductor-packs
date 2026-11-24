// calendar-busy-sync — the pack's logic, run as a `use: js` code step body
// (an IIFE: top-level `return` is the step's outputs). One file, two modes,
// picked by the workflow's `cfg` step:
//
//   plan      the reconcile: which busy blocks to create / change / remove on
//             each target calendar so it matches the source calendars
//   cleanup   the one-time BusyBridge cleanup: delete its future blocks, end
//             its recurring series at now, keep its past
//
// Calendars arrive keyed by connector name, from `over:` read steps over the
// pack's two list sockets: ctx.<step>.by_member = { <connector>: { items } }.
// Every write op names its `target` connector, which the workflow passes to
// `member:`.
//
// Pure: it reads ctx and returns data. The workflow does the I/O with verb
// steps, so this file runs unchanged under node in the tests.

const cfg = ctx.cfg || {};
const mode = cfg.mode;

function bool(v, dflt) {
  if (v === undefined || v === null || v === "") return dflt;
  if (typeof v === "boolean") return v;
  return String(v).trim().toLowerCase() === "true";
}
function num(v, dflt) {
  const n = Number(v);
  return v !== "" && v !== null && v !== undefined && Number.isFinite(n) ? n : dflt;
}
// calendars(id) → { <connector>: [events] } from an `over:` read step. A
// calendar that read back without an item list fails the run: a partial
// picture would look like every one of its events had gone.
function calendars(id) {
  const s = ctx[id];
  const by = s && s.by_member;
  if (!by || typeof by !== "object") throw new Error("step " + id + " returned no calendars");
  const out = {};
  for (const name of Object.keys(by).sort()) {
    const r = by[name];
    const items = Array.isArray(r) ? r : r && r.items;
    if (!Array.isArray(items)) throw new Error("step " + id + ": " + name + " returned no items");
    out[name] = items;
  }
  return out;
}

const TAG = cfg.tag || "conductorBusySync"; // private extended property: the block's key
const SIG = TAG + "Sig";                    // a repeating block's signature
const priv = (e) => (e && e.extendedProperties && e.extendedProperties.private) || {};
const tagOf = (e) => priv(e)[TAG] || "";

// BusyBridge's blocks: deterministic ids `bb` + 13 base32hex chars (a
// modified instance carries `_<stamp>` after it), or its private property.
const BB_ID = /^bb[0-9a-v]{13}(_|$)/;
function isBusyBridge(e) {
  return BB_ID.test(e.id || "") || priv(e).bb_proj_id !== undefined;
}
const isAllDay = (e) => !!(e.start && e.start.date);
function timeMs(t) {
  if (!t) return NaN;
  if (t.dateTime) return Date.parse(t.dateTime);
  if (t.date) return Date.parse(t.date + "T00:00:00Z");
  return NaN;
}
const startMs = (e) => timeMs(e.start);
const selfDeclined = (e) => (e.attendees || []).some((a) => a.self && a.responseStatus === "declined");
function hasNoSync(e, tag) {
  return !!tag && (e.description || "").toLowerCase().indexOf(tag.toLowerCase()) >= 0;
}

// ---------------------------------------------------------------------------
// plan — the reconcile
// ---------------------------------------------------------------------------
function planMode() {
  const rules = {
    title: cfg.block_title || "Busy",
    showDetails: bool(cfg.show_details, false),
    colorId: cfg.color_id === undefined ? "8" : String(cfg.color_id),
    description: cfg.managed_tag ? String(cfg.managed_tag) : "",
    mirrorAllDay: bool(cfg.mirror_all_day, true),
    syncOOO: bool(cfg.sync_out_of_office, true),
    repeatingBlocks: bool(cfg.repeating_blocks, true),
    noSyncTag: cfg.nosync_tag === undefined ? "[nosync]" : String(cfg.nosync_tag),
    skipAlreadyBusy: bool(cfg.skip_already_busy, true),
  };
  const removeAll = bool(cfg.remove_all, false);
  const maxDeletes = num(cfg.max_deletes, 50);
  const batch = num(cfg.batch_size, 50);
  if (!(batch > 0)) throw new Error("batch_size must be a positive integer");

  const srcBy = calendars("src"), srcRawBy = calendars("srcraw");
  const dstBy = calendars("dst"), dstRawBy = calendars("dstraw");
  const srcNames = Object.keys(srcBy).sort();
  const dstNames = Object.keys(dstBy).sort();

  const live = (e) => e && e.status !== "cancelled" && e.start && e.end;
  // Someone's real event: never a block, ours or BusyBridge's.
  const real = (e) => live(e) && !tagOf(e) && !isBusyBridge(e);
  const edge = (t) => (t.dateTime ? String(Date.parse(t.dateTime)) : "d" + t.date);
  const len = (e) => timeMs(e.end) - timeMs(e.start);
  // The same meeting on two calendars shares an iCalUID; the start pins the
  // occurrence.
  const meeting = (e) => (e.iCalUID ? e.iCalUID + "@" + edge(e.start) : "");

  // Busy on its own calendar: shows as busy, and you have not declined it.
  const busy = (e) => real(e) && e.transparency !== "transparent" && e.eventType !== "workingLocation" && !selfDeclined(e);

  // What gets a block on the other calendars: an event that shows as busy
  // and you have not declined. Google makes all-day events Free unless you
  // mark them busy, so a busy all-day event is a deliberate one.
  const blocks = (e) => {
    if (!busy(e)) return false;
    if (e.eventType === "outOfOffice" && !rules.syncOOO) return false;
    if (hasNoSync(e, rules.noSyncTag)) return false;
    return !isAllDay(e) || rules.mirrorAllDay;
  };

  const title = (e) => (rules.showDetails && e.summary ? e.summary : rules.title);
  const shell = (key, summary) => {
    const b = {
      summary,
      transparency: "opaque",
      visibility: "private",
      reminders: { useDefault: false, overrides: [] },
      extendedProperties: { private: { [TAG]: key } },
    };
    if (rules.description) b.description = rules.description;
    if (rules.colorId) b.colorId = rules.colorId;
    return b;
  };
  const when = (t) => {
    if (t.date) return { date: t.date };
    const out = { dateTime: t.dateTime };
    if (t.timeZone) out.timeZone = t.timeZone;
    return out;
  };
  // A block for one event or one occurrence.
  const single = (key, e) => Object.assign(shell(key, title(e)), { start: when(e.start), end: when(e.end) });

  // A repeating event can be mirrored as one repeating block when its rule can
  // be copied as it stands. A COUNT rule cannot: the block starts at the first
  // occurrence still ahead, which would restart the count. Those fall back to
  // one block per occurrence.
  const repeats = (m) => rules.repeatingBlocks && !!m && Array.isArray(m.recurrence) && m.recurrence.length > 0 &&
    !m.recurrence.some((r) => /[:;]COUNT=/i.test(r)) && !!(m.start && (m.start.date || m.start.timeZone));
  const pad = (n) => String(n).padStart(2, "0");
  // The RFC 5545 line that leaves one occurrence out, by its original start.
  const exdate = (t) => {
    if (t.date) return "EXDATE;VALUE=DATE:" + t.date.replace(/-/g, "");
    const d = new Date(Date.parse(t.dateTime));
    return "EXDATE:" + d.getUTCFullYear() + pad(d.getUTCMonth() + 1) + pad(d.getUTCDate()) + "T" +
      pad(d.getUTCHours()) + pad(d.getUTCMinutes()) + pad(d.getUTCSeconds()) + "Z";
  };
  const hash = (s) => {
    let a = 2166136261, b = 5381;
    for (let i = 0; i < s.length; i++) {
      const c = s.charCodeAt(i);
      a = Math.imul(a ^ c, 16777619) >>> 0;
      b = (Math.imul(b, 33) + c) >>> 0;
    }
    return a.toString(16) + b.toString(16);
  };
  // A repeating block: the source's own rule, starting at the first occurrence
  // that needs it, minus the occurrences that do not. Google may hand a rule
  // back reworded, so the block carries a signature of what it was built
  // from, and that is what a later run compares.
  const series = (key, master, first, skipped) => {
    const lines = master.recurrence.concat(Array.from(new Set(skipped.map(exdate))).sort());
    const b = shell(key, title(master));
    if (first.start.date) {
      b.start = { date: first.start.date };
      b.end = { date: first.end.date };
    } else {
      b.start = { dateTime: first.start.dateTime, timeZone: master.start.timeZone };
      b.end = { dateTime: first.end.dateTime, timeZone: (master.end && master.end.timeZone) || master.start.timeZone };
    }
    b.recurrence = lines;
    b.extendedProperties.private[SIG] = hash(JSON.stringify([master.start, master.end, lines]));
    return b;
  };
  // Does the block on the calendar still look the way the plan wants it? A
  // colour or reminder someone set by hand is left alone.
  const same = (have, want) => {
    if (!!have.recurrence !== !!want.recurrence) return false;
    const look = (have.summary || "") === want.summary &&
      (have.description || "") === (want.description || "") &&
      (have.transparency || "opaque") === "opaque" &&
      have.visibility === "private" &&
      (!want.colorId || have.colorId === want.colorId);
    if (want.recurrence) return look && priv(have)[SIG] === want.extendedProperties.private[SIG];
    return look && edge(have.start) === edge(want.start) && edge(have.end) === edge(want.end);
  };

  const creates = [], updates = [], deletes = [];
  const wanted = {};   // "<source> -> <target>": occurrences that should be blocked, for the log line
  let covered = 0;     // occurrences the target had already blocked itself
  let vanished = 0;    // blocks going because their source event is no longer returned
  let repeating = 0;   // repeating blocks wanted
  let adrift = 0;      // occurrences blocked but not wanted, or wanted but not blocked

  // Every event the source calendars returned, whether or not it earns a block.
  const present = new Set();
  for (const source of srcNames) {
    for (const e of srcBy[source]) if (e && e.id) present.add(source + "/" + e.id);
    for (const e of srcRawBy[source] || []) if (e && e.id) present.add(source + "/" + e.id);
  }

  for (const target of dstNames) {
    const existing = dstBy[target].filter(live);
    // Meetings the target holds itself (not declined there): no block for them.
    const held = new Set(existing.filter((e) => real(e) && !selfDeclined(e)).map(meeting).filter(Boolean));

    // Time the target already shows as busy on its own. A source event that
    // sits wholly inside one of these needs no block: you blocked it by hand,
    // or the same commitment is on both calendars as two separate events.
    const own = rules.skipAlreadyBusy ? existing.filter(busy) : [];
    const timed = own.filter((e) => e.start.dateTime)
      .map((e) => [Date.parse(e.start.dateTime), Date.parse(e.end.dateTime)]);
    const days = own.filter((e) => e.start.date).map((e) => [e.start.date, e.end.date]);
    const alreadyBusy = (e) => (e.start.dateTime
      ? timed.some(([a, b]) => a <= Date.parse(e.start.dateTime) && b >= Date.parse(e.end.dateTime))
      : days.some(([a, b]) => a <= e.start.date && b >= e.end.date));

    // Which blocks SHOULD be on this target: "<source>/<event id>" for one
    // event or occurrence, "<source>/<repeating event id>" for a repeating block.
    const want = new Map();
    const seen = new Set();
    const due = {};   // start -> occurrences that should be blocked then
    const add = (source, n) => {
      const pair = source + " -> " + target;
      wanted[pair] = (wanted[pair] || 0) + n;
    };
    const one = (source, e) => {
      const key = source + "/" + e.id;
      want.set(key, single(key, e));
      due[edge(e.start)] = (due[edge(e.start)] || 0) + 1;
      add(source, 1);
    };

    for (const source of removeAll ? [] : srcNames) {
      if (source === target) continue; // a calendar never gets a block for its own events
      const masters = new Map();   // repeating events, by id
      const dropped = new Map();   // repeating event id -> original starts of its cancelled occurrences
      for (const r of srcRawBy[source] || []) {
        if (!r || !r.id) continue;
        if (Array.isArray(r.recurrence) && r.recurrence.length) masters.set(r.id, r);
        else if (r.recurringEventId && r.status === "cancelled" && r.originalStartTime) {
          if (!dropped.has(r.recurringEventId)) dropped.set(r.recurringEventId, []);
          dropped.get(r.recurringEventId).push(r.originalStartTime);
        }
      }

      const runs = new Map();      // repeating event id -> its occurrences in the window
      for (const e of srcBy[source]) {
        if (!live(e)) continue;
        let needs = blocks(e);
        if (needs) {
          const m = meeting(e);
          // The target already has this meeting itself, or another source
          // already supplied a block for it.
          if (m && (held.has(m) || seen.has(m))) needs = false;
          else if (alreadyBusy(e)) { covered++; needs = false; }
          else if (m) seen.add(m);
        }
        const rid = e.recurringEventId;
        if (rid && e.originalStartTime && repeats(masters.get(rid))) {
          if (!runs.has(rid)) runs.set(rid, []);
          runs.get(rid).push({ e, needs });
        } else if (needs) {
          one(source, e);
        }
      }

      for (const [rid, occ] of runs) {
        const master = masters.get(rid);
        // On the rule: where and as long as the rule puts it.
        const onRule = (e) => edge(e.start) === edge(e.originalStartTime) && len(e) === len(master);
        const keep = occ.filter((o) => o.needs && onRule(o.e));
        const skip = occ.filter((o) => !(o.needs && onRule(o.e)));
        const gone = dropped.get(rid) || [];
        // One repeating block when most occurrences ahead need it. Otherwise
        // the few that do get a block each.
        if (keep.length && keep.length >= skip.length + gone.length) {
          keep.sort((x, y) => startMs(x.e) - startMs(y.e));
          const key = source + "/" + rid;
          want.set(key, series(key, master, keep[0].e, skip.map((o) => o.e.originalStartTime).concat(gone)));
          repeating++;
          add(source, keep.length);
          for (const o of keep) due[edge(o.e.start)] = (due[edge(o.e.start)] || 0) + 1;
          // Moved or resized, yet still busy: a block of its own.
          for (const o of skip) if (o.needs) one(source, o.e);
        } else {
          for (const o of occ) if (o.needs) one(source, o.e);
        }
      }
    }

    // How far the calendar is from that, counted in occurrences. Non-zero
    // before a run writes; a number that stays up afterwards means a
    // repeating block is not landing where the plan expects.
    const have = {};
    for (const e of existing) if (tagOf(e)) have[edge(e.start)] = (have[edge(e.start)] || 0) + 1;
    for (const k of new Set(Object.keys(due).concat(Object.keys(have)))) {
      adrift += Math.abs((due[k] || 0) - (have[k] || 0));
    }

    // Which blocks ARE on it: this pack's events, a repeating block counted
    // once. A key seen twice is a duplicate; the first is kept, the rest go.
    const mine = (dstRawBy[target] || []).filter((e) => live(e) && tagOf(e) && !e.recurringEventId);
    const kept = new Set();
    for (const e of mine) {
      const key = tagOf(e);
      if (!want.has(key) || kept.has(key)) {
        deletes.push({ target, event_id: e.id });
        // Its source calendar is still bound, yet the event is gone.
        if (!want.has(key) && !present.has(key) && srcNames.includes(key.split("/")[0])) vanished++;
        continue;
      }
      kept.add(key);
      const w = want.get(key);
      if (!same(e, w)) updates.push({ target, event_id: e.id, event: w });
    }
    for (const [key, w] of want) {
      if (!kept.has(key)) creates.push({ target, event: w });
    }
  }

  // Blocks going because their event was cancelled, or because a source came
  // back empty. The two look alike, so a large number stops the run. Blocks
  // going for a reason the plan can see (declined, marked free, already
  // blocked on the target, source unbound) are not counted.
  if (!removeAll && vanished > maxDeletes) {
    throw new Error(
      "refusing to delete " + vanished + " blocks whose source events are gone (max_deletes is " +
      maxDeletes + "); check the source calendars read correctly, or raise max_deletes"
    );
  }
  // A run does at most `batch` of each; a for_each over more than
  // policy.max_fan_out items is refused outright. What is left over is simply
  // still missing on the next run, which plans it again.
  const left = Math.max(creates.length - batch, 0) + Math.max(updates.length - batch, 0) +
    Math.max(deletes.length - batch, 0);
  return {
    creates: creates.slice(0, batch),
    updates: updates.slice(0, batch),
    deletes: deletes.slice(0, batch),
    pending: left,
    adrift,
    summary: creates.length + " to create, " + updates.length + " to change, " + deletes.length +
      " to remove across " + dstNames.length + " target(s) from " + srcNames.length + " source(s)" +
      "; occurrences to block: " + (Object.keys(wanted).sort().map((k) => k + " " + wanted[k]).join(", ") || "none") +
      (repeating ? "; " + repeating + " repeating block(s)" : "") +
      (covered ? "; " + covered + " already blocked on the target" : "") +
      (adrift ? "; " + adrift + " occurrence(s) not yet in step" : "") +
      (removeAll ? "; remove_all is on" : "") +
      (left ? "; batch_size " + batch + ", " + left + " left for later runs" : ""),
  };
}

// ---------------------------------------------------------------------------
// cleanup — retire BusyBridge's blocks from now on, keep its history
// ---------------------------------------------------------------------------
function untilStamp(now, allDay) {
  const p = (n) => ("0" + n).slice(-2);
  if (allDay) {
    // An all-day series' UNTIL is a DATE; end it yesterday (UTC), so nothing
    // from today on remains.
    const y = new Date(now.getTime() - 86400000);
    return "" + y.getUTCFullYear() + p(y.getUTCMonth() + 1) + p(y.getUTCDate());
  }
  return "" + now.getUTCFullYear() + p(now.getUTCMonth() + 1) + p(now.getUTCDate()) + "T" +
    p(now.getUTCHours()) + p(now.getUTCMinutes()) + p(now.getUTCSeconds()) + "Z";
}

function truncateRecurrence(rec, until) {
  return rec.map((line) => {
    if (!/^RRULE:/i.test(line)) return line;
    const parts = line.slice(6).split(";").filter((p) => p && !/^(UNTIL|COUNT)=/i.test(p));
    parts.push("UNTIL=" + until);
    return "RRULE:" + parts.join(";");
  });
}

function cleanupMode() {
  const apply = bool(cfg.cleanup_apply, false);
  const maxOps = num(cfg.cleanup_max_ops, 1000);
  const batch = num(cfg.batch_size, 50);
  const now = cfg.now ? new Date(cfg.now) : new Date();
  const nowMs = now.getTime();
  const out = { apply: apply, summary: {}, delete: [], patch: [] };
  const targets = calendars("dstraw");
  for (const target of Object.keys(targets)) {
    const evs = targets[target].filter((e) => e.status !== "cancelled" && isBusyBridge(e));
    const masters = new Map();
    for (const e of evs) if (Array.isArray(e.recurrence) && e.recurrence.length) masters.set(e.id, e);
    const del = [], patch = [];
    const doomed = new Set();
    let keptPast = 0;
    for (const m of masters.values()) {
      if (startMs(m) >= nowMs) {
        del.push({ target: target, event_id: m.id, why: "series starts in the future" });
        doomed.add(m.id);
      } else {
        patch.push({ target: target, event_id: m.id, event: { recurrence: truncateRecurrence(m.recurrence, untilStamp(now, isAllDay(m))) } });
      }
    }
    for (const e of evs) {
      if (masters.has(e.id)) continue;
      if (e.recurringEventId && doomed.has(e.recurringEventId)) continue; // goes with its series
      if (startMs(e) >= nowMs) del.push({ target: target, event_id: e.id, why: e.recurringEventId ? "future exception" : "future one-off" });
      else keptPast++;
    }
    out.summary[target] = { busybridge_events: evs.length, series: masters.size, delete: del.length, end_series: patch.length, kept_past: keptPast };
    out.delete.push(...del);
    out.patch.push(...patch);
  }
  const total = out.delete.length + out.patch.length;
  if (total > maxOps) {
    throw new Error("cleanup would make " + total + " changes, over cleanup_max_ops " + maxOps + "; raise it to proceed");
  }
  // Each list is one for_each step, held under policy.max_fan_out: a big
  // cleanup takes a few runs (each run re-reads and does what is left).
  out.remaining = Math.max(0, out.delete.length - batch) + Math.max(0, out.patch.length - batch);
  out.delete = out.delete.slice(0, batch);
  out.patch = out.patch.slice(0, batch);
  if (!apply) {
    out.sample = { delete: out.delete.slice(0, 10), end_series: out.patch.slice(0, 10).map((p) => p.target + ":" + p.event_id) };
    out.delete = []; out.patch = [];
  }
  return out;
}

switch (mode) {
  case "plan": return planMode();
  case "cleanup": return cleanupMode();
  default: throw new Error("busy.js: unknown mode " + JSON.stringify(mode));
}
