"""busy.js — the pack's sync / cleanup / backup logic — run under node exactly
as the js engine runs it (the file is an IIFE body; `ctx` is the step scope).

    python3 -m unittest discover -s calendar-busy-sync/tests

Needs node and PyYAML.
"""
import json
import os
import subprocess
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.join(HERE, "..")
SRC = open(os.path.join(PACK, "busy.js")).read()
NOW = "2026-11-20T12:00:00Z"


def run(ctx):
    prog = "const ctx = " + json.dumps(ctx) + ";\nconsole.log(JSON.stringify((function(){\n" + SRC + "\n})() ?? null));"
    p = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
    if p.returncode != 0:
        errs = [l for l in p.stderr.splitlines() if l.startswith(("Error:", "TypeError:", "ReferenceError:", "SyntaxError:"))]
        raise RuntimeError(errs[0] if errs else p.stderr.strip() or "node failed")
    return json.loads(p.stdout)


def ev(id, start, end=None, **kw):
    if "T" in start:
        e = {"id": id, "status": "confirmed", "start": {"dateTime": start, "timeZone": "America/Denver"},
             "end": {"dateTime": end or start.replace("T10", "T11"), "timeZone": "America/Denver"}}
    else:
        e = {"id": id, "status": "confirmed", "start": {"date": start}, "end": {"date": end}}
    e.update(kw)
    return e


def reads(p1=(), p2=(), c1=(), c2=()):
    """The two `over:` read steps' outputs, keyed by connector name — here
    the connectors are simply called personal_1 … client_2."""
    return {
        "read_sources": {"by_member": {"personal_1": {"items": list(p1)}, "personal_2": {"items": list(p2)}}},
        "read_targets": {"by_member": {"client_1": {"items": list(c1)}, "client_2": {"items": list(c2)}}},
    }


def per_target(out, kinds):
    """Regroup the flat, target-tagged op lists per target calendar, so the
    assertions read per calendar (and the target tag is checked to exist)."""
    for t in ("client_1", "client_2"):
        out[t] = {k: [] for k in kinds}
    for k in kinds:
        for op in out[k]:
            out[op["target"]][k].append({x: v for x, v in op.items() if x != "target"})
    return out


def plan(p1=(), p2=(), c1=(), c2=(), **cfg):
    c = {"mode": "plan", "dry_run": "false"}
    c.update(cfg)
    ctx = {"cfg": c}
    ctx.update(reads(p1, p2, c1, c2))
    return per_target(run(ctx), ("create", "update", "delete"))


T = "2026-11-25T10:00:00-07:00"


class Rules(unittest.TestCase):
    def test_personal_timed_event_blocks_both_clients(self):
        out = plan(p1=[ev("a", T, summary="Dentist", location="Main St")])
        for c in ("client_1", "client_2"):
            self.assertEqual(len(out[c]["create"]), 1)
            body = out[c]["create"][0]["event"]
            self.assertEqual(body["summary"], "Busy")
            self.assertEqual(body["description"], "[Busy]")
            self.assertEqual(body["transparency"], "opaque")
            self.assertEqual(body["visibility"], "private")
            self.assertEqual(body["start"], {"dateTime": T, "timeZone": "America/Denver"})
            self.assertNotIn("location", body)
            self.assertEqual(body["extendedProperties"]["private"]["cbs"], "1")
            self.assertRegex(body["id"], r"^cbs[0-9a-f]{32}$")
        self.assertNotEqual(out["client_1"]["create"][0]["event"]["id"], out["client_2"]["create"][0]["event"]["id"])

    def test_personal_free_event_still_blocks(self):
        out = plan(p1=[ev("a", T, transparency="transparent")])
        self.assertEqual(len(out["client_1"]["create"]), 1)

    def test_personal_all_day_skipped_unless_enabled(self):
        a = ev("a", "2026-11-25", "2026-11-26")
        self.assertEqual(plan(p1=[a])["summary"]["client_1"]["want"], 0)
        self.assertEqual(plan(p1=[a], sync_personal_all_day="true")["summary"]["client_1"]["want"], 1)

    def test_client_source_blocks_only_the_other_client(self):
        out = plan(c1=[ev("m", T)])
        self.assertEqual(out["summary"]["client_1"]["want"], 0)
        self.assertEqual(out["summary"]["client_2"]["want"], 1)

    def test_client_all_day_blocks_but_free_does_not(self):
        out = plan(c1=[ev("d", "2026-11-25", "2026-11-26"), ev("f", T, transparency="transparent")])
        self.assertEqual(out["summary"]["client_2"]["want"], 1)
        self.assertEqual(out["client_2"]["create"][0]["event"]["start"], {"date": "2026-11-25"})

    def test_skips(self):
        skipped = [
            ev("x1", T, status="cancelled"),
            ev("x2", T, eventType="outOfOffice"),
            ev("x3", T, description="lunch [NoSync] please"),
            ev("x4", T, attendees=[{"email": "me@x", "self": True, "responseStatus": "declined"}]),
            ev("bb0123456789abc", T),  # a BusyBridge block
            ev("x5", T, extendedProperties={"private": {"bb_proj_id": "7"}}),
        ]
        out = plan(p1=skipped, c1=skipped)
        self.assertEqual(out["summary"]["client_1"]["want"], 0)
        self.assertEqual(out["summary"]["client_2"]["want"], 0)

    def test_out_of_office_mirrors_when_enabled(self):
        self.assertEqual(plan(c1=[ev("o", T, eventType="outOfOffice")], sync_out_of_office="true")["summary"]["client_2"]["want"], 1)

    def test_our_own_blocks_never_feed_back(self):
        first = plan(c1=[ev("m", T)])
        block = first["client_2"]["create"][0]["event"]
        # client_2 now holds our block; it must not cast a block back onto client_1
        out = plan(c1=[ev("m", T)], c2=[dict(block, status="confirmed")])
        self.assertEqual(out["summary"]["client_1"]["want"], 0)
        self.assertEqual(out["summary"]["client_2"], {"want": 1, "have": 1, "create": 0, "update": 0, "delete": 0})


class Diff(unittest.TestCase):
    def test_idempotent(self):
        p = [ev("a", T)]
        first = plan(p1=p)
        c1 = [dict(o["event"], status="confirmed") for o in first["client_1"]["create"]]
        c2 = [dict(o["event"], status="confirmed") for o in first["client_2"]["create"]]
        again = plan(p1=p, c1=c1, c2=c2)
        self.assertEqual(again["writes"], 0)

    def test_moved_source_replaces_block(self):
        first = plan(p1=[ev("a", T)])
        old = dict(first["client_1"]["create"][0]["event"], status="confirmed")
        out = plan(p1=[ev("a", "2026-11-25T14:00:00-07:00", "2026-11-25T15:00:00-07:00")], c1=[old])
        self.assertEqual([d["event_id"] for d in out["client_1"]["delete"]], [old["id"]])
        self.assertEqual(len(out["client_1"]["create"]), 1)

    def test_removed_source_deletes_block(self):
        first = plan(p1=[ev("a", T)])
        old = dict(first["client_1"]["create"][0]["event"], status="confirmed")
        out = plan(c1=[old])
        self.assertEqual(out["client_1"]["delete"], [{"event_id": old["id"]}])

    def test_edited_block_is_repaired(self):
        first = plan(p1=[ev("a", T)])
        edited = dict(first["client_1"]["create"][0]["event"], status="confirmed", summary="mine now", visibility="public")
        out = plan(p1=[ev("a", T)], c1=[edited])
        self.assertEqual(len(out["client_1"]["update"]), 1)
        patch = out["client_1"]["update"][0]["event"]
        self.assertEqual((patch["summary"], patch["visibility"]), ("Busy", "private"))
        self.assertNotIn("id", patch)

    def test_hand_deleted_block_is_restored_not_recreated(self):
        first = plan(p1=[ev("a", T)])
        gone = {"id": first["client_1"]["create"][0]["event"]["id"], "status": "cancelled"}
        out = plan(p1=[ev("a", T)], c1=[gone])
        self.assertEqual(out["summary"]["client_1"]["create"], 0)
        self.assertEqual(out["client_1"]["update"][0]["event"]["status"], "confirmed")

    def test_meeting_already_on_target_is_not_duplicated(self):
        meet = dict(iCalUID="abc@google.com")
        out = plan(c1=[ev("m1", T, **meet)], c2=[ev("m2", T, **meet)])
        self.assertEqual(out["summary"]["client_2"]["want"], 0)
        self.assertEqual(out["summary"]["client_1"]["want"], 0)

    def test_same_meeting_on_two_sources_blocks_once(self):
        meet = dict(iCalUID="abc@google.com")
        out = plan(p1=[ev("m1", T, **meet)], p2=[ev("m2", T, **meet)])
        self.assertEqual(out["summary"]["client_1"]["want"], 1)

    def test_declined_on_target_still_gets_block(self):
        meet = dict(iCalUID="abc@google.com")
        mine = ev("m2", T, attendees=[{"self": True, "responseStatus": "declined"}], **meet)
        out = plan(c1=[ev("m1", T, **meet)], c2=[mine])
        self.assertEqual(out["summary"]["client_2"]["want"], 1)

    def test_dry_run_writes_nothing(self):
        out = plan(p1=[ev("a", T)], dry_run="true")
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["writes"], 0)
        self.assertEqual(out["client_1"], {"create": [], "update": [], "delete": []})
        self.assertEqual(out["summary"]["client_1"]["create"], 1)
        self.assertEqual(out["would"]["create"][0]["from"], "personal_1")
        self.assertIn(out["would"]["create"][0]["on"], ("client_1", "client_2"))

    def test_dry_run_is_the_default(self):
        ctx = {"cfg": {"mode": "plan"}}
        ctx.update(reads())
        out = run(ctx)
        self.assertTrue(out["dry_run"])

    def test_write_cap_defers(self):
        many = [ev("a%d" % i, "2026-11-25T10:%02d:00-07:00" % i, "2026-11-25T11:%02d:00-07:00" % i) for i in range(10)]
        out = plan(p1=many, max_writes="12", max_fan_out="100")
        self.assertEqual(out["writes"], 12)
        self.assertEqual(out["deferred"], 8)

    def test_fan_out_cap_per_list(self):
        many = [ev("a%d" % i, "2026-11-25T%02d:%02d:00-07:00" % (i // 60, i % 60), "2026-11-25T23:59:00-07:00") for i in range(7)]
        out = plan(p1=many, max_fan_out="3")
        self.assertEqual(len(out["create"]), 3)  # one for_each step for every target
        self.assertEqual(out["deferred"], 11)

    def test_mass_delete_refused(self):
        first = plan(p1=[ev("a%d" % i, "2026-11-25T10:%02d:00-07:00" % i, "2026-11-25T11:%02d:00-07:00" % i) for i in range(4)])
        have = [dict(o["event"], status="confirmed") for o in first["client_1"]["create"]]
        with self.assertRaisesRegex(RuntimeError, "refusing to delete 4"):
            plan(c1=have, max_deletes="3")

    def test_missing_read_fails(self):
        with self.assertRaisesRegex(RuntimeError, "read_targets returned no calendars"):
            run({"cfg": {"mode": "plan"}, "read_sources": reads()["read_sources"]})
        bad = reads()
        bad["read_targets"]["by_member"]["client_2"] = {}
        with self.assertRaisesRegex(RuntimeError, "client_2 returned no items"):
            run(dict(bad, cfg={"mode": "plan"}))

    def test_any_number_of_calendars(self):
        # three targets, one source: each target gets the source and the two others
        ctx = {"cfg": {"mode": "plan", "dry_run": "false"},
               "read_sources": {"by_member": {"home": {"items": [ev("h", T)]}}},
               "read_targets": {"by_member": {
                   "w1": {"items": [ev("a", "2026-11-26T10:00:00Z", "2026-11-26T11:00:00Z")]},
                   "w2": {"items": []}, "w3": {"items": []}}}}
        out = run(ctx)
        self.assertEqual({k: v["want"] for k, v in out["summary"].items()}, {"w1": 1, "w2": 2, "w3": 2})
        self.assertEqual(sorted({o["target"] for o in out["create"]}), ["w1", "w2", "w3"])

    def test_no_sources_is_fine(self):
        ctx = {"cfg": {"mode": "plan", "dry_run": "false"},
               "read_targets": {"by_member": {"w1": {"items": [ev("a", T)]}, "w2": {"items": []}}}}
        out = run(ctx)
        self.assertEqual(out["summary"]["w2"]["want"], 1)

    def test_calendar_in_both_lists_refused(self):
        ctx = {"cfg": {"mode": "plan"},
               "read_sources": {"by_member": {"x": {"items": []}}},
               "read_targets": {"by_member": {"x": {"items": []}, "y": {"items": []}}}}
        with self.assertRaisesRegex(RuntimeError, "both a source and a target: x"):
            run(ctx)


def cleanup(c1=(), c2=(), **cfg):
    c = {"mode": "cleanup", "now": NOW, "cleanup_apply": "true"}
    c.update(cfg)
    ctx = {"cfg": c, "read_targets": reads(c1=c1, c2=c2)["read_targets"]}
    return per_target(run(ctx), ("delete", "patch"))


class Cleanup(unittest.TestCase):
    BB = "bb0123456789abc"

    def test_future_one_off_deleted_past_kept(self):
        out = cleanup(c1=[ev(self.BB, "2026-11-25T10:00:00Z"), ev("bb0123456789abd", "2026-11-01T10:00:00Z"),
                          ev("mine", "2026-11-25T10:00:00Z")])
        self.assertEqual([d["event_id"] for d in out["client_1"]["delete"]], [self.BB])
        self.assertEqual(out["summary"]["client_1"]["kept_past"], 1)

    def test_series_started_in_past_is_ended_now(self):
        s = ev(self.BB, "2026-10-01T10:00:00Z", recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=50", "EXDATE:20261012T100000Z"])
        out = cleanup(c1=[s])
        self.assertEqual(out["client_1"]["delete"], [])
        rec = out["client_1"]["patch"][0]["event"]["recurrence"]
        self.assertEqual(rec, ["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261120T120000Z", "EXDATE:20261012T100000Z"])

    def test_all_day_series_until_is_a_date(self):
        s = ev(self.BB, "2026-10-01", "2026-10-02", recurrence=["RRULE:FREQ=DAILY"])
        rec = cleanup(c1=[s])["client_1"]["patch"][0]["event"]["recurrence"]
        self.assertEqual(rec, ["RRULE:FREQ=DAILY;UNTIL=20261119"])

    def test_future_series_deleted_with_its_exceptions(self):
        s = ev(self.BB, "2026-12-01T10:00:00Z", recurrence=["RRULE:FREQ=DAILY"])
        exc = ev(self.BB + "_20261203T100000Z", "2026-12-03T11:00:00Z", recurringEventId=self.BB)
        out = cleanup(c1=[s, exc])
        self.assertEqual([d["event_id"] for d in out["client_1"]["delete"]], [self.BB])

    def test_future_exception_of_ended_series_deleted(self):
        s = ev(self.BB, "2026-10-01T10:00:00Z", recurrence=["RRULE:FREQ=DAILY"])
        exc = ev(self.BB + "_20261203T100000Z", "2026-12-03T11:00:00Z", recurringEventId=self.BB)
        out = cleanup(c1=[s, exc])
        self.assertEqual([d["event_id"] for d in out["client_1"]["delete"]], [exc["id"]])
        self.assertEqual(len(out["client_1"]["patch"]), 1)

    def test_tagged_by_property_only(self):
        e = ev("random", "2026-11-25T10:00:00Z", extendedProperties={"private": {"bb_proj_id": "9"}})
        self.assertEqual(len(cleanup(c2=[e])["client_2"]["delete"]), 1)

    def test_report_only_by_default(self):
        out = run({"cfg": {"mode": "cleanup", "now": NOW},
                   "read_targets": reads(c1=[ev(self.BB, "2026-11-25T10:00:00Z")])["read_targets"]})
        self.assertFalse(out["apply"])
        self.assertEqual(out["delete"], [])
        self.assertEqual(out["summary"]["client_1"]["delete"], 1)
        self.assertEqual(out["sample"]["delete"][0], {"target": "client_1", "event_id": self.BB, "why": "future one-off"})

    def test_fan_out_cap_leaves_remainder(self):
        many = [ev("bb0123456789a%02d" % i, "2026-11-25T10:00:00Z") for i in range(5)]
        out = cleanup(c1=many, max_fan_out="2")
        self.assertEqual(len(out["delete"]), 2)
        self.assertEqual(out["remaining"], 3)

    def test_cap(self):
        many = [ev("bb0123456789a%02d" % i, "2026-11-25T10:00:00Z") for i in range(5)]
        with self.assertRaisesRegex(RuntimeError, "over cleanup_max_ops 3"):
            cleanup(c1=many, cleanup_max_ops="3")


class Backup(unittest.TestCase):
    def test_snapshots_per_calendar(self):
        long = "x" * 200
        evs = [ev("a", T, summary="Plan; review, then\nship", description=long, iCalUID="u1@google.com",
                  recurrence=["RRULE:FREQ=WEEKLY"], updated="2026-11-01T00:00:00Z"),
               ev("d", "2026-11-25", "2026-11-26", summary="Holiday"),
               ev("z", T, status="cancelled")]
        ctx = {"cfg": {"mode": "snapshots", "backup_prefix": "cal"}}
        ctx.update(reads(evs, evs, evs, evs))
        out = run(ctx)
        self.assertEqual(len(out["snapshots"]), 9)
        self.assertEqual(out["sets"][:2], ["cal-client_1-json", "cal-client_1-ics"])
        index = out["snapshots"][-1]
        self.assertEqual(index["set"], "cal-index")
        self.assertEqual(json.loads(index["text"])["sets"], out["sets"])
        j, ics = out["snapshots"][0], out["snapshots"][1]
        self.assertEqual(json.loads(j["text"]), evs)
        self.assertEqual(ics["ext"], "ics")
        text = ics["text"]
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n"))
        self.assertEqual(text.count("BEGIN:VEVENT"), 2)  # cancelled dropped
        self.assertIn("SUMMARY:Plan\\; review\\, then\\nship", text)
        self.assertIn("DTSTART:20261125T170000Z", text)
        self.assertIn("DTSTART;VALUE=DATE:20261125", text)
        self.assertIn("RRULE:FREQ=WEEKLY", text)
        self.assertIn("UID:u1@google.com", text)
        for line in text.split("\r\n"):
            self.assertLessEqual(len(line), 75)


class Manifest(unittest.TestCase):
    """The manifest wires busy.js the way these tests assume."""

    def setUp(self):
        with open(os.path.join(PACK, "conductor-pack.yaml")) as f:
            self.man = yaml.safe_load(f)

    def test_settings_defaults(self):
        s = self.man["settings"]
        self.assertIs(s["dry_run"]["default"], True)
        self.assertEqual(s["lookahead_days"]["default"], 60)

    def test_reads_are_over_the_two_lists(self):
        steps = self.man["workflows"]["sync"]["steps"]
        reads = [b[0] for s in steps if isinstance(s.get("parallel"), list) for b in s["parallel"]]
        self.assertEqual(sorted(r["id"] for r in reads), ["read_sources", "read_targets"])
        for r in reads:
            self.assertTrue(r["over"])
            self.assertTrue(r["options"]["singleEvents"] and r["options"]["showDeleted"])

    def test_writes_go_to_the_ops_target(self):
        writes = [s for s in self.man["workflows"]["sync"]["steps"] if str(s.get("id", "")).startswith("apply_")]
        self.assertEqual(len(writes), 3)
        for w in writes:
            self.assertTrue(w["uses"].startswith("targets."))
            self.assertEqual(w["member"], "{{.item.target}}")

    def test_sockets(self):
        req = self.man["pack"]["requires"]["connectors"]
        self.assertEqual(set(req), {"sources", "targets", "cron", "backup"})
        self.assertTrue(req["sources"]["many"] and req["targets"]["many"])
        self.assertEqual(req["targets"]["min"], 2)

if __name__ == "__main__":
    unittest.main()
