// calendar-busy-sync — the pack's logic, run as a `use: js` code step body
// (an IIFE: top-level `return` is the step's outputs). One file, three modes,
// picked by the workflow's `cfg` step:
//
//   plan      the sync diff: which busy blocks to create / update / delete on
//             each target calendar so it mirrors the others
//   cleanup   the one-time BusyBridge cleanup plan: delete its future blocks,
//             end its recurring series at now, keep its past
//   snapshots per-calendar snapshots (raw JSON + ICS) to store
//
// Calendars arrive keyed by connector name, from `over:` read steps over the
// pack's two list sockets: ctx.read_sources.by_member / read_targets.by_member
// = { <connector>: { items: [...] } }. Every write op names its `target`
// connector, which the workflow passes to `member:`.
//
// Everything here is pure: it reads ctx and returns data. The workflow does
// the I/O with verb steps, so this file runs unchanged under node in tests.

const cfg = ctx.cfg || {};
const mode = cfg.mode;
const MANAGED_KEY = "cbs"; // extendedProperties.private[MANAGED_KEY] === "1" marks our blocks

function bool(v, dflt) {
  if (v === undefined || v === null || v === "") return dflt;
  if (typeof v === "boolean") return v;
  return String(v).trim().toLowerCase() === "true";
}
function num(v, dflt) {
  const n = Number(v);
  return Number.isFinite(n) && v !== "" && v !== null ? n : dflt;
}
// calendars(id) → { <connector>: [events] } from an `over:` read step.
function calendars(id, required) {
  const s = ctx[id];
  const by = s && s.by_member;
  if (!by || typeof by !== "object") {
    if (required) throw new Error("step " + id + " returned no calendars");
    return {};
  }
  const out = {};
  for (const name of Object.keys(by).sort()) {
    const r = by[name];
    if (!r || !Array.isArray(r.items)) throw new Error("step " + id + ": " + name + " returned no items");
    out[name] = r.items;
  }
  return out;
}
function priv(ev) {
  return (ev.extendedProperties && ev.extendedProperties.private) || {};
}
// Ours: the private property, or our id shape (a block deleted by hand reads
// back cancelled and may carry nothing but its id).
const OUR_ID = /^cbs[0-9a-f]{32}$/;
function isOurs(ev) {
  return priv(ev)[MANAGED_KEY] === "1" || OUR_ID.test(ev.id || "");
}
// BusyBridge's blocks: deterministic ids `bb` + 13 base32hex chars (a
// modified instance carries `_<stamp>` after it), or its private property.
const BB_ID = /^bb[0-9a-v]{13}(_|$)/;
function isBusyBridge(ev) {
  return BB_ID.test(ev.id || "") || priv(ev).bb_proj_id !== undefined;
}
function isAllDay(ev) {
  return !!(ev.start && ev.start.date);
}
function startMs(ev) {
  return timeMs(ev.start);
}
function timeMs(t) {
  if (!t) return NaN;
  if (t.dateTime) return Date.parse(t.dateTime);
  if (t.date) return Date.parse(t.date + "T00:00:00Z");
  return NaN;
}
function selfDeclined(ev) {
  return (ev.attendees || []).some((a) => a.self && a.responseStatus === "declined");
}
function hasNoSync(ev, tag) {
  if (!tag) return false;
  return (ev.description || "").toLowerCase().indexOf(tag.toLowerCase()) >= 0;
}

// 128-bit FNV-1a (four 32-bit lanes, distinct offsets) → 32 hex chars. Hex is
// a subset of Google's base32hex id alphabet, so the id is valid as-is.
function hash128(s) {
  const seeds = [0x811c9dc5, 0x01000193, 0x9e3779b9, 0x85ebca6b];
  let out = "";
  for (let lane = 0; lane < 4; lane++) {
    let h = seeds[lane] >>> 0;
    const salt = String.fromCharCode(97 + lane);
    const str = salt + s;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    // final avalanche so lanes differ beyond their seed
    h ^= h >>> 16; h = Math.imul(h, 0x85ebca6b) >>> 0;
    h ^= h >>> 13; h = Math.imul(h, 0xc2b2ae35) >>> 0;
    h ^= h >>> 16;
    out += ("00000000" + (h >>> 0).toString(16)).slice(-8);
  }
  return out;
}

function timeOf(t) {
  if (t.date) return { date: t.date };
  const o = { dateTime: t.dateTime };
  if (t.timeZone) o.timeZone = t.timeZone;
  return o;
}

// ---------------------------------------------------------------------------
// plan — the sync
// ---------------------------------------------------------------------------

// Which source events become a busy block, mirroring BusyBridge's rules.
//   personal (read-only) source: timed events only (all-day only with
//     sync_personal_all_day); a Free event STILL blocks.
//   client source: all-day events block too; a Free event does not.
//   both: skip cancelled, declined, out-of-office, `[nosync]`, our own blocks
//     and BusyBridge's.
function blocks(ev, kind, rules) {
  if (ev.status === "cancelled") return false;
  if (isOurs(ev) || isBusyBridge(ev)) return false;
  if (ev.eventType === "outOfOffice" && !rules.syncOOO) return false;
  if (hasNoSync(ev, rules.noSyncTag)) return false;
  if (selfDeclined(ev)) return false;
  if (!ev.start || !ev.end) return false;
  if (kind === "personal") {
    if (isAllDay(ev) && !rules.personalAllDay) return false;
    return true;
  }
  return ev.transparency !== "transparent";
}

function blockBody(id, ev, src, rules) {
  const body = {
    id: id,
    summary: rules.title,
    start: timeOf(ev.start),
    end: timeOf(ev.end),
    transparency: "opaque",
    visibility: "private",
    extendedProperties: { private: { [MANAGED_KEY]: "1", cbs_src: src } },
  };
  if (rules.tag) body.description = rules.tag;
  return body;
}

// The fields a block must carry; anything else on it (colour, reminders the
// user set) is left alone.
function drifted(have, want) {
  if (have.status === "cancelled") return true;
  if ((have.summary || "") !== want.summary) return true;
  if ((have.description || "") !== (want.description || "")) return true;
  if ((have.transparency || "opaque") !== want.transparency) return true;
  if ((have.visibility || "default") !== want.visibility) return true;
  if (timeMs(have.start) !== timeMs(want.start) || timeMs(have.end) !== timeMs(want.end)) return true;
  if (isAllDay(have) !== isAllDay(want)) return true;
  return false;
}

function planMode() {
  const rules = {
    title: cfg.busy_title || "Busy",
    tag: cfg.managed_tag === undefined ? "[Busy]" : String(cfg.managed_tag),
    noSyncTag: cfg.nosync_tag === undefined ? "[nosync]" : String(cfg.nosync_tag),
    personalAllDay: bool(cfg.sync_personal_all_day, false),
    syncOOO: bool(cfg.sync_out_of_office, false),
  };
  const dry = bool(cfg.dry_run, true);
  const maxWrites = num(cfg.max_writes, 200);
  const maxDeletes = num(cfg.max_deletes, 50);
  // One for_each step per list, and conductor refuses a for_each over
  // policy.max_fan_out (100 by default) items.
  const maxFanOut = num(cfg.max_fan_out, 100);

  const personal = calendars("read_sources", false);
  const targets = calendars("read_targets", true);
  const both = Object.keys(personal).filter((n) => n in targets);
  if (both.length) throw new Error("a calendar cannot be both a source and a target: " + both.join(", "));
  const cal = Object.assign({}, personal, targets);
  const targetNames = Object.keys(targets);

  const out = { dry_run: dry, summary: {}, deferred: 0, create: [], update: [], delete: [] };
  let writes = 0;
  const allDeletes = [];

  for (const target of targetNames) {
    // Sources for this target: every personal calendar, and every OTHER target.
    const sources = Object.keys(personal).map((s) => [s, "personal"]).concat(
      targetNames.filter((c) => c !== target).map((c) => [c, "client"]),
    );
    // A meeting the target already holds natively (same iCalUID, same start,
    // not declined there) needs no block — it would only duplicate it.
    const native = new Set();
    for (const ev of cal[target]) {
      if (isOurs(ev) || isBusyBridge(ev) || ev.status === "cancelled" || selfDeclined(ev)) continue;
      if (ev.iCalUID) native.add(ev.iCalUID + "|" + startMs(ev));
    }
    const want = new Map();
    const seen = new Set(); // one block per meeting even when two sources carry it
    for (const [src, kind] of sources) {
      for (const ev of cal[src]) {
        if (!blocks(ev, kind, rules)) continue;
        const key = ev.iCalUID ? ev.iCalUID + "|" + startMs(ev) : null;
        if (key && (native.has(key) || seen.has(key))) continue;
        if (key) seen.add(key);
        const id = "cbs" + hash128([target, src, ev.id, JSON.stringify(ev.start), JSON.stringify(ev.end)].join("\u0000"));
        want.set(id, blockBody(id, ev, src, rules));
      }
    }
    const have = new Map();
    for (const ev of cal[target]) if (isOurs(ev)) have.set(ev.id, ev);

    let nc = 0, nu = 0, nd = 0;
    for (const [id, body] of want) {
      const h = have.get(id);
      if (!h) { out.create.push({ target: target, event: body }); nc++; }
      else if (drifted(h, body)) {
        const patch = Object.assign({}, body, { status: "confirmed" });
        delete patch.id;
        out.update.push({ target: target, event_id: id, event: patch });
        nu++;
      }
    }
    for (const [id, h] of have) {
      if (h.status === "cancelled") continue;
      if (!want.has(id)) { const d = { target: target, event_id: id }; out.delete.push(d); allDeletes.push(d); nd++; }
    }
    out.summary[target] = { want: want.size, have: have.size, create: nc, update: nu, delete: nd };
  }

  if (allDeletes.length > maxDeletes) {
    throw new Error(
      "refusing to delete " + allDeletes.length + " busy blocks in one run (max_deletes " + maxDeletes +
      "). A source calendar that read back empty looks exactly like this; if the deletes are real, raise max_deletes for one run.",
    );
  }

  // Cap the writes per run: deletes first (they free time), then creates, then
  // updates. Each kind is ONE for_each step, so each is also held under
  // policy.max_fan_out. What is left over is picked up by the next run.
  for (const kind of ["delete", "create", "update"]) {
    const room = Math.min(maxFanOut, Math.max(0, maxWrites - writes));
    if (out[kind].length > room) {
      out.deferred += out[kind].length - room;
      out[kind] = out[kind].slice(0, room);
    }
    writes += out[kind].length;
  }
  if (dry) {
    out.would = {
      create: out.create.slice(0, 10).map((o) => ({ on: o.target, from: o.event.extendedProperties.private.cbs_src, start: o.event.start })),
      delete: out.delete.slice(0, 10),
    };
    out.create = []; out.update = []; out.delete = [];
  }
  out.writes = dry ? 0 : writes;
  return out;
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
  const now = cfg.now ? new Date(cfg.now) : new Date();
  const nowMs = now.getTime();
  const out = { apply: apply, summary: {}, delete: [], patch: [] };
  const targets = calendars("read_targets", true);
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
  const maxFanOut = num(cfg.max_fan_out, 100);
  out.remaining = Math.max(0, out.delete.length - maxFanOut) + Math.max(0, out.patch.length - maxFanOut);
  out.delete = out.delete.slice(0, maxFanOut);
  out.patch = out.patch.slice(0, maxFanOut);
  if (!apply) {
    out.sample = { delete: out.delete.slice(0, 10), end_series: out.patch.slice(0, 10).map((p) => p.target + ":" + p.event_id) };
    out.delete = []; out.patch = [];
  }
  return out;
}

// ---------------------------------------------------------------------------
// snapshots — raw JSON (lossless, re-insertable through the API) + ICS
// ---------------------------------------------------------------------------
function icsEscape(s) {
  return String(s).replace(/\\/g, "\\\\").replace(/;/g, "\\;").replace(/,/g, "\\,")
    .replace(/\r\n|\r|\n/g, "\\n");
}
function icsFold(line) {
  // RFC 5545 §3.1: lines over 75 octets fold with CRLF + space. Count UTF-16
  // units conservatively (≤ 74) so a multi-byte character never splits badly.
  const out = [];
  while (line.length > 74) {
    out.push(line.slice(0, 74));
    line = " " + line.slice(74);
  }
  out.push(line);
  return out.join("\r\n");
}
function icsTime(name, t) {
  if (t.date) return name + ";VALUE=DATE:" + t.date.replace(/-/g, "");
  const d = new Date(t.dateTime);
  const p = (n) => ("0" + n).slice(-2);
  return name + ":" + d.getUTCFullYear() + p(d.getUTCMonth() + 1) + p(d.getUTCDate()) + "T" +
    p(d.getUTCHours()) + p(d.getUTCMinutes()) + p(d.getUTCSeconds()) + "Z";
}
function toICS(name, evs) {
  const L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//NodeSpy//calendar-busy-sync//EN", "X-WR-CALNAME:" + icsEscape(name)];
  for (const e of evs) {
    if (e.status === "cancelled" || !e.start || !e.end) continue;
    L.push("BEGIN:VEVENT");
    L.push("UID:" + icsEscape(e.iCalUID || e.id));
    if (e.recurringEventId && e.originalStartTime) L.push(icsTime("RECURRENCE-ID", e.originalStartTime));
    L.push(icsTime("DTSTART", e.start));
    L.push(icsTime("DTEND", e.end));
    if (e.updated) L.push(icsTime("DTSTAMP", { dateTime: e.updated }));
    for (const r of e.recurrence || []) L.push(r);
    if (e.summary) L.push("SUMMARY:" + icsEscape(e.summary));
    if (e.description) L.push("DESCRIPTION:" + icsEscape(e.description));
    if (e.location) L.push("LOCATION:" + icsEscape(e.location));
    if (e.status) L.push("STATUS:" + e.status.toUpperCase());
    if (e.transparency === "transparent") L.push("TRANSP:TRANSPARENT");
    L.push("END:VEVENT");
  }
  L.push("END:VCALENDAR");
  return L.map(icsFold).join("\r\n") + "\r\n";
}
function backupMode() {
  const prefix = cfg.backup_prefix || "calendar";
  const snaps = [];
  const cal = Object.assign({}, calendars("read_sources", false), calendars("read_targets", true));
  for (const s of Object.keys(cal).sort()) {
    const evs = cal[s];
    const set = prefix + "-" + s.replace(/[^A-Za-z0-9._-]/g, "-");
    snaps.push({ set: set + "-json", ext: "json", text: JSON.stringify(evs) });
    snaps.push({ set: set + "-ics", ext: "ics", text: toICS(s, evs) });
  }
  // The index lists every set, so the restore test knows what to verify
  // without reading the calendars.
  const sets = snaps.map((x) => x.set);
  snaps.push({ set: prefix + "-index", ext: "json", text: JSON.stringify({ sets: sets }) });
  return { snapshots: snaps, sets: sets };
}

switch (mode) {
  case "plan": return planMode();
  case "cleanup": return cleanupMode();
  case "snapshots": return backupMode();
  default: throw new Error("busy.js: unknown mode " + JSON.stringify(mode));
}
