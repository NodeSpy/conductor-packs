"""The backup pack's snapshot step (`snap`) — run under node exactly as
the js engine runs it, and the manifest wiring.

    python3 -m unittest discover -s calendar-backup/tests

Needs node and PyYAML.
"""
import json
import os
import subprocess
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.join(HERE, "..")
with open(os.path.join(PACK, "conductor-pack.yaml")) as _f:
    SRC = next(s for s in yaml.safe_load(_f)["workflows"]["backup"]["steps"] if s.get("id") == "snap")["code"]


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


def reads(*names, items=()):
    return {"read": {"by_member": {n: {"items": list(items)} for n in names}}}


T = "2026-11-25T10:00:00-07:00"


class Backup(unittest.TestCase):
    def test_snapshots_per_calendar(self):
        long = "x" * 200
        evs = [ev("a", T, summary="Plan; review, then\nship", description=long, iCalUID="u1@google.com",
                  recurrence=["RRULE:FREQ=WEEKLY"], updated="2026-11-01T00:00:00Z"),
               ev("d", "2026-11-25", "2026-11-26", summary="Holiday"),
               ev("z", T, status="cancelled")]
        ctx = {"cfg": {"backup_prefix": "cal"}}
        ctx.update(reads("personal_1", "personal_2", "client_1", "client_2", items=evs))
        out = run(ctx)
        self.assertEqual(len(out["snapshots"]), 9)  # 4 calendars x (json, ics), + the index
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

    def test_a_calendar_that_read_back_without_items_fails(self):
        bad = reads("a")
        bad["read"]["by_member"]["b"] = {}
        with self.assertRaisesRegex(RuntimeError, "b returned no items"):
            run(dict(bad, cfg={}))


class Manifest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(PACK, "conductor-pack.yaml")) as f:
            self.man = yaml.safe_load(f)

    def test_reads_every_calendar_unexpanded(self):
        read = next(s for s in self.man["workflows"]["backup"]["steps"] if s.get("id") == "read")
        self.assertTrue(read["over"])
        self.assertEqual(read["uses"], "calendars.events")
        self.assertIs(read["options"]["singleEvents"], False)

    def test_never_writes_to_a_calendar(self):
        uses = [s.get("uses", "") for w in self.man["workflows"].values() for s in w["steps"]]
        self.assertFalse([u for u in uses if u.startswith("calendars.") and u != "calendars.events"])

    def test_triggers_lift_the_attempt_limit(self):
        for t in self.man["triggers"]:
            self.assertGreaterEqual(t["policy"]["max_attempts_per_head"], 10**9)


if __name__ == "__main__":
    unittest.main()
