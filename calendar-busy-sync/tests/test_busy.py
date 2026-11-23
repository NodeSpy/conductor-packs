"""busy.js — the pack's reconcile and BusyBridge-cleanup logic — run under node exactly
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


def by(cals):
    return {"by_member": {k: {"items": list(v)} for k, v in cals.items()}}


def reads(p1=(), p2=(), c1=(), c2=()):
    """The four `over:` read steps' outputs, keyed by connector name: two
    source-only calendars (personal_1/2) and two targets (client_1/2). With
    nothing repeating, the raw reads are the expanded ones. A target is in
    both lists, as BusyBridge's clients are both read and written."""
    src = {"personal_1": p1, "personal_2": p2, "client_1": c1, "client_2": c2}
    dst = {"client_1": c1, "client_2": c2}
    return {"src": by(src), "srcraw": by(src), "dst": by(dst), "dstraw": by(dst)}


def per_target(out, kinds):
    """Regroup the flat, target-tagged op lists per target calendar."""
    for t in ("client_1", "client_2"):
        out[t] = {k: [] for k in kinds}
    for k in kinds:
        for op in out[k]:
            out[op["target"]][k].append({x: v for x, v in op.items() if x != "target"})
    return out


def plan(p1=(), p2=(), c1=(), c2=(), **cfg):
    """BusyBridge rules, its block look ([Busy] description, no colour)."""
    c = {"mode": "plan", "rules": "busybridge", "managed_tag": "[Busy]", "color_id": ""}
    c.update(cfg)
    ctx = {"cfg": c}
    ctx.update(reads(p1, p2, c1, c2))
    return per_target(run(ctx), ("creates", "updates", "deletes"))


def blocked(out, target):
    """Source event ids blocked on target (creates), by the block key."""
    return sorted(o["event"]["extendedProperties"]["private"]["conductorBusySync"].split("/", 1)[1]
                  for o in out[target]["creates"])


def on(out, block):
    """The block as Google would hand it back."""
    return dict(block, id="g" + str(abs(hash(json.dumps(block, sort_keys=True))) % 10**9), status="confirmed")


T = "2026-11-25T10:00:00-07:00"


class BusyBridgeRules(unittest.TestCase):
    def test_personal_timed_event_blocks_both_clients(self):
        out = plan(p1=[ev("a", T, summary="Dentist", location="Main St")])
        for c in ("client_1", "client_2"):
            self.assertEqual(len(out[c]["creates"]), 1)
            body = out[c]["creates"][0]["event"]
            self.assertEqual(body["summary"], "Busy")
            self.assertEqual(body["description"], "[Busy]")
            self.assertEqual(body["transparency"], "opaque")
            self.assertEqual(body["visibility"], "private")
            self.assertEqual(body["reminders"], {"useDefault": False, "overrides": []})
            self.assertEqual(body["start"], {"dateTime": T, "timeZone": "America/Denver"})
            self.assertNotIn("location", body)
            self.assertNotIn("colorId", body)
            self.assertEqual(body["extendedProperties"]["private"]["conductorBusySync"], "personal_1/a")

    def test_personal_free_event_still_blocks(self):
        out = plan(p1=[ev("a", T, transparency="transparent")])
        self.assertEqual(blocked(out, "client_1"), ["a"])

    def test_personal_all_day_skipped_unless_enabled(self):
        a = ev("a", "2026-11-25", "2026-11-26")
        self.assertEqual(blocked(plan(p1=[a]), "client_1"), [])
        self.assertEqual(blocked(plan(p1=[a], mirror_all_day="true"), "client_1"), ["a"])

    def test_client_source_blocks_only_the_other_client(self):
        out = plan(c1=[ev("m", T)])
        self.assertEqual(blocked(out, "client_1"), [])
        self.assertEqual(blocked(out, "client_2"), ["m"])

    def test_client_all_day_blocks_but_free_does_not(self):
        out = plan(c1=[ev("d", "2026-11-25", "2026-11-26"), ev("f", T, transparency="transparent")])
        self.assertEqual(blocked(out, "client_2"), ["d"])
        self.assertEqual(out["client_2"]["creates"][0]["event"]["start"], {"date": "2026-11-25"})

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
        self.assertEqual(blocked(out, "client_1"), [])
        self.assertEqual(blocked(out, "client_2"), [])

    def test_out_of_office_mirrors_when_enabled(self):
        out = plan(c1=[ev("o", T, eventType="outOfOffice")], sync_out_of_office="true")
        self.assertEqual(blocked(out, "client_2"), ["o"])

    def test_our_own_blocks_never_feed_back(self):
        first = plan(c1=[ev("m", T)])
        blk = on(first, first["client_2"]["creates"][0]["event"])
        out = plan(c1=[ev("m", T)], c2=[blk])
        self.assertEqual(blocked(out, "client_1"), [])
        self.assertEqual((out["creates"], out["updates"], out["deletes"]), ([], [], []))


class UniformRules(unittest.TestCase):
    def test_default_rules_treat_every_source_alike(self):
        c = {"mode": "plan"}
        ctx = {"cfg": c}
        ctx.update(reads(p1=[ev("free", T, transparency="transparent"), ev("ooo", T.replace("T10", "T13"), eventType="outOfOffice")],
                         c1=[ev("d", "2026-11-25", "2026-11-26")]))
        out = per_target(run(ctx), ("creates", "updates", "deletes"))
        # a free event does not block, out-of-office does, all-day does not
        self.assertEqual(blocked(out, "client_1"), ["ooo"])
        self.assertEqual(blocked(out, "client_2"), ["ooo"])
        self.assertEqual(out["client_1"]["creates"][0]["event"]["colorId"], "8")
        self.assertNotIn("description", out["client_1"]["creates"][0]["event"])

    def test_unknown_rules_refused(self):
        ctx = {"cfg": {"mode": "plan", "rules": "ghost"}}
        ctx.update(reads())
        with self.assertRaisesRegex(RuntimeError, "rules must be uniform or busybridge"):
            run(ctx)

    def test_busybridge_blocks_are_never_mirrored(self):
        ctx = {"cfg": {"mode": "plan"}}
        ctx.update(reads(p1=[ev("bb0123456789abc", T)]))
        self.assertEqual(run(ctx)["creates"], [])


class Diff(unittest.TestCase):
    def test_idempotent(self):
        p = [ev("a", T)]
        first = plan(p1=p)
        c1 = [on(first, o["event"]) for o in first["client_1"]["creates"]]
        c2 = [on(first, o["event"]) for o in first["client_2"]["creates"]]
        again = plan(p1=p, c1=c1, c2=c2)
        self.assertEqual((again["creates"], again["updates"], again["deletes"]), ([], [], []))

    def test_edited_block_is_repaired(self):
        first = plan(p1=[ev("a", T)])
        edited = dict(on(first, first["client_1"]["creates"][0]["event"]), summary="mine now", visibility="public")
        out = plan(p1=[ev("a", T)], c1=[edited])
        self.assertEqual(len(out["client_1"]["updates"]), 1)
        patch = out["client_1"]["updates"][0]["event"]
        self.assertEqual((patch["summary"], patch["visibility"]), ("Busy", "private"))

    def test_dry_run_is_off_by_default_in_the_manifest(self):
        with open(os.path.join(PACK, "conductor-pack.yaml")) as f:
            man = yaml.safe_load(f)
        self.assertIs(man["settings"]["dry_run"]["default"], False)

    def test_any_number_of_calendars(self):
        # three targets, one source-only: each target gets the source and the two others
        src = {"home": [ev("h", T)], "w1": [ev("a", "2026-11-26T10:00:00Z", "2026-11-26T11:00:00Z")], "w2": [], "w3": []}
        dst = {"w1": src["w1"], "w2": [], "w3": []}
        ctx = {"cfg": {"mode": "plan", "rules": "busybridge"},
               "src": by(src), "srcraw": by(src), "dst": by(dst), "dstraw": by(dst)}
        out = run(ctx)
        self.assertIn("home -> w1 1, home -> w2 1, home -> w3 1, w1 -> w2 1, w1 -> w3 1", out["summary"])
        self.assertEqual(sorted({o["target"] for o in out["creates"]}), ["w1", "w2", "w3"])

    def test_missing_read_fails(self):
        ctx = {"cfg": {"mode": "plan"}}
        ctx.update(reads())
        del ctx["dstraw"]
        with self.assertRaisesRegex(RuntimeError, "dstraw returned no calendars"):
            run(ctx)
        ctx = {"cfg": {"mode": "plan"}}
        ctx.update(reads())
        ctx["dst"]["by_member"]["client_2"] = {}
        with self.assertRaisesRegex(RuntimeError, "client_2 returned no items"):
            run(ctx)

    def test_repeating_blocks_can_be_turned_off(self):
        m = {"id": "m1", "status": "confirmed", "iCalUID": "m1@google.com", "recurrence": ["RRULE:FREQ=WEEKLY"],
             "start": {"dateTime": "2026-01-06T15:00:00Z", "timeZone": "UTC"}, "end": {"dateTime": "2026-01-06T16:00:00Z", "timeZone": "UTC"}}
        occ = [dict(ev("m1_%d" % d, "2026-12-%02dT15:00:00Z" % d, "2026-12-%02dT16:00:00Z" % d),
                    recurringEventId="m1", iCalUID="m1@google.com",
                    originalStartTime={"dateTime": "2026-12-%02dT15:00:00Z" % d}) for d in (1, 8, 15)]
        for flag, want in (("true", 1), ("false", 3)):
            ctx = {"cfg": {"mode": "plan", "repeating_blocks": flag},
                   "src": by({"p": occ}), "srcraw": by({"p": [m]}), "dst": by({"w": []}), "dstraw": by({"w": []})}
            self.assertEqual(len(run(ctx)["creates"]), want, flag)


def cleanup(c1=(), c2=(), **cfg):
    c = {"mode": "cleanup", "now": NOW, "cleanup_apply": "true"}
    c.update(cfg)
    ctx = {"cfg": c, "dstraw": reads(c1=c1, c2=c2)["dstraw"]}
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
                   "dstraw": reads(c1=[ev(self.BB, "2026-11-25T10:00:00Z")])["dstraw"]})
        self.assertFalse(out["apply"])
        self.assertEqual(out["delete"], [])
        self.assertEqual(out["summary"]["client_1"]["delete"], 1)
        self.assertEqual(out["sample"]["delete"][0], {"target": "client_1", "event_id": self.BB, "why": "future one-off"})

    def test_fan_out_cap_leaves_remainder(self):
        many = [ev("bb0123456789a%02d" % i, "2026-11-25T10:00:00Z") for i in range(5)]
        out = cleanup(c1=many, batch_size="2")
        self.assertEqual(len(out["delete"]), 2)
        self.assertEqual(out["remaining"], 3)

    def test_cap(self):
        many = [ev("bb0123456789a%02d" % i, "2026-11-25T10:00:00Z") for i in range(5)]
        with self.assertRaisesRegex(RuntimeError, "over cleanup_max_ops 3"):
            cleanup(c1=many, cleanup_max_ops="3")


class Manifest(unittest.TestCase):
    """The manifest wires busy.js the way these tests assume, and keeps the
    0.1.0 contract (#42): its settings, triggers and sockets."""

    def setUp(self):
        with open(os.path.join(PACK, "conductor-pack.yaml")) as f:
            self.man = yaml.safe_load(f)

    def test_0_1_0_settings_and_defaults_are_kept(self):
        s = self.man["settings"]
        want = {"window_days": 30, "block_title": "Busy", "show_details": False, "color_id": "8",
                "mirror_all_day": False, "tag": "conductorBusySync", "max_deletes": 50,
                "remove_all": False, "batch_size": 50, "dry_run": False}
        self.assertEqual({k: s[k]["default"] for k in want}, want)

    def test_0_1_0_triggers_and_sockets_are_kept(self):
        trig = {t["name"]: t for t in self.man["triggers"]}
        self.assertEqual(trig["on_change"]["on"], "sources.changed")
        self.assertIn("cron.busy_sync", trig["sweep"]["on"])
        req = self.man["pack"]["requires"]["connectors"]
        self.assertTrue(req["sources"]["many"] and req["targets"]["many"])
        self.assertNotIn("min", req["targets"])
        self.assertNotIn("backup", req)
        self.assertEqual(req["cron"], "*")

    def test_sweep_and_push_share_one_queue_and_lift_the_attempt_limit(self):
        trig = {t["name"]: t for t in self.man["triggers"]}
        for n in ("on_change", "on_target_change", "sweep", "cleanup"):
            self.assertEqual(trig[n]["serialize"]["key"], "calendar-busy-sync")
        for n in ("on_change", "on_target_change", "sweep"):
            self.assertGreaterEqual(trig[n]["policy"]["max_attempts_per_head"], 10**9)

    def test_reads_are_over_the_two_lists(self):
        steps = self.man["workflows"]["reconcile"]["steps"]
        reads = [b[0] for s in steps if isinstance(s.get("parallel"), list) for b in s["parallel"]]
        self.assertEqual(sorted(r["id"] for r in reads), ["dst", "dstraw", "src", "srcraw"])
        for r in reads:
            self.assertTrue(r["over"])

    def test_writes_go_to_the_ops_target(self):
        writes = [s for s in self.man["workflows"]["reconcile"]["steps"] if s.get("id") in ("create", "move", "remove")]
        self.assertEqual(len(writes), 3)
        for w in writes:
            self.assertTrue(w["uses"].startswith("targets."))
            self.assertEqual(w["member"], "{{.item.target}}")

    def test_cfg_passes_every_setting_busy_js_reads(self):
        cfg = next(s for s in self.man["workflows"]["reconcile"]["steps"] if s.get("id") == "cfg")["set"]
        for k, v in cfg.items():
            if k != "mode":
                self.assertEqual(v, "${settings.%s}" % k)
                self.assertIn(k, self.man["settings"])


if __name__ == "__main__":
    unittest.main()
