// calendar-backup — the snapshot step, run as a `use: js` code step body (an
// IIFE: top-level `return` is the step's outputs).
//
// The calendars arrive keyed by connector name from the `over:` read step
// (ctx.read.by_member = { <connector>: { items } }); for each one this returns
// two snapshots to store — the raw API JSON (lossless, re-insertable) and an
// ICS file any calendar app can import — plus an index of every set, which
// the restore test reads. Pure: the workflow does the I/O.

const cfg = ctx.cfg || {};

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
function snapshotsMode() {
  const prefix = cfg.backup_prefix || "calendar";
  const snaps = [];
  const cal = calendars("read");
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


return snapshotsMode();
