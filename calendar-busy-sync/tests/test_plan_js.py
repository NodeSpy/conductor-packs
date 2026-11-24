"""The reconcile's deterministic `plan` step and the
manifest's `window` step, run under node exactly as the js engine runs them:
which busy blocks get created, moved and removed.

    python3 -m unittest discover -s calendar-busy-sync/tests

Needs node and PyYAML.
"""
import json
import os
import subprocess
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "..", "conductor-pack.yaml")
TAG = "conductorBusySync"

CFG = {
    "window_days": "30",
    "block_title": "Busy",
    "show_details": "false",
    "color_id": "8",
    "mirror_all_day": "false",
    "tag": TAG,
    "max_deletes": "50",
    "batch_size": "50",
    "remove_all": "false",
    "managed_tag": "",
}


def step_code(step_id):
    with open(MANIFEST) as f:
        man = yaml.safe_load(f)
    for s in man["workflows"]["reconcile"]["steps"]:
        if s.get("id") == step_id:
            return s["code"]
    raise KeyError(step_id)


def run_js(code, ctx):
    """Run a js step body the way the engine does: `ctx` global, body returns."""
    src = "const ctx = JSON.parse(process.argv[1]);\n" \
          "const out = (function () {\n" + code + "\n})();\n" \
          "process.stdout.write(JSON.stringify(out));\n"
    p = subprocess.run(["node", "-e", src, json.dumps(ctx)], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    return json.loads(p.stdout)


def ev(eid, start, end, **extra):
    e = {"id": eid, "status": "confirmed", "iCalUID": eid + "@google.com",
         "summary": "Thing " + eid, "start": {"dateTime": start}, "end": {"dateTime": end}}
    e.update(extra)
    return e


def block(eid, key, start, end, **extra):
    e = {"id": eid, "status": "confirmed", "iCalUID": eid + "@google.com", "summary": "Busy",
         "start": {"dateTime": start}, "end": {"dateTime": end},
         "transparency": "opaque", "visibility": "private", "colorId": "8",
         "extendedProperties": {"private": {TAG: key}}}
    e.update(extra)
    return e


def plan(src, dst, src_raw=None, dst_raw=None, **cfg):
    """`src`/`dst` are the expanded reads (one item per occurrence); the raw
    reads default to the same lists, which is what they are when nothing repeats."""
    by = lambda d: {"by_member": {k: {"items": v} for k, v in d.items()}}
    ctx = {
        "cfg": dict(CFG, mode="plan", **cfg),
        "src": by(src), "srcraw": by(src if src_raw is None else src_raw),
        "dst": by(dst), "dstraw": by(dst if dst_raw is None else dst_raw),
    }
    return run_js(step_code("plan"), ctx)


TZ = "America/Denver"
WEEKLY = ["RRULE:FREQ=WEEKLY;BYDAY=TU"]


def master(mid, start, end, rules=WEEKLY, **extra):
    """A repeating event as the raw read returns it."""
    e = {"id": mid, "status": "confirmed", "iCalUID": mid + "@google.com", "summary": "Weekly " + mid,
         "start": {"dateTime": start, "timeZone": TZ}, "end": {"dateTime": end, "timeZone": TZ},
         "recurrence": list(rules)}
    e.update(extra)
    return e


def occ(mid, orig, start=None, end=None, **extra):
    """One occurrence as the expanded read returns it (an hour long unless moved)."""
    start = start or orig
    end = end or start.replace("T15:", "T16:")
    e = {"id": mid + "_" + orig.replace("-", "").replace(":", ""), "status": "confirmed",
         "iCalUID": mid + "@google.com", "summary": "Weekly " + mid, "recurringEventId": mid,
         "originalStartTime": {"dateTime": orig, "timeZone": TZ},
         "start": {"dateTime": start, "timeZone": TZ}, "end": {"dateTime": end, "timeZone": TZ}}
    e.update(extra)
    return e


W = ["2026-12-01T15:00:00Z", "2026-12-08T15:00:00Z", "2026-12-15T15:00:00Z", "2026-12-22T15:00:00Z"]
M1 = master("m1", "2026-01-06T15:00:00Z", "2026-01-06T16:00:00Z")


T1, T2 = "2026-12-01T15:00:00Z", "2026-12-01T16:00:00Z"


class PlanTest(unittest.TestCase):
    def test_personal_event_blocks_every_work_calendar(self):
        out = plan({"personal": [ev("p1", T1, T2)], "work": [], "side": []},
                   {"work": [], "side": []})
        self.assertEqual([c["target"] for c in out["creates"]], ["side", "work"])
        b = out["creates"][0]["event"]
        self.assertEqual(b["summary"], "Busy")
        self.assertEqual(b["visibility"], "private")
        self.assertEqual(b["transparency"], "opaque")
        self.assertEqual(b["reminders"], {"useDefault": False, "overrides": []})
        self.assertEqual(b["extendedProperties"]["private"][TAG], "personal/p1")
        self.assertEqual(b["start"], {"dateTime": T1})
        self.assertEqual((out["updates"], out["deletes"]), ([], []))
        self.assertIn("occurrences to block: personal -> side 1, personal -> work 1", out["summary"])

    def test_a_calendar_never_blocks_itself(self):
        out = plan({"work": [ev("w1", T1, T2)], "side": []}, {"work": [], "side": []})
        self.assertEqual([c["target"] for c in out["creates"]], ["side"])

    def test_second_run_is_a_noop_even_across_time_zones(self):
        # The target hands the same instant back in its own offset.
        have = block("b1", "personal/p1", "2026-12-01T08:00:00-07:00", "2026-12-01T09:00:00-07:00")
        out = plan({"personal": [ev("p1", T1, T2)]}, {"work": [have]})
        self.assertEqual((out["creates"], out["updates"], out["deletes"]), ([], [], []))

    def test_moved_event_moves_its_block(self):
        have = block("b1", "personal/p1", T1, T2)
        out = plan({"personal": [ev("p1", "2026-12-01T17:00:00Z", "2026-12-01T18:00:00Z")]},
                   {"work": [have]})
        self.assertEqual(out["creates"], [])
        self.assertEqual(out["updates"][0]["event_id"], "b1")
        self.assertEqual(out["updates"][0]["event"]["start"], {"dateTime": "2026-12-01T17:00:00Z"})

    def test_gone_or_cancelled_event_removes_its_block(self):
        have = [block("b1", "personal/p1", T1, T2), block("b2", "personal/p2", T1, T2)]
        out = plan({"personal": [ev("p2", T1, T2, status="cancelled")]}, {"work": have})
        self.assertEqual(sorted(d["event_id"] for d in out["deletes"]), ["b1", "b2"])

    def test_events_that_do_not_block_time_are_skipped(self):
        src = [
            ev("free", T1, T2, transparency="transparent"),
            ev("declined", T1, T2, attendees=[{"self": True, "responseStatus": "declined"}]),
            ev("loc", T1, T2, eventType="workingLocation"),
            {"id": "allday", "status": "confirmed", "start": {"date": "2026-12-01"}, "end": {"date": "2026-12-02"}},
        ]
        out = plan({"personal": src}, {"work": []})
        self.assertEqual(out["creates"], [])
        out = plan({"personal": src}, {"work": []}, mirror_all_day="true")
        self.assertEqual([c["event"]["start"] for c in out["creates"]], [{"date": "2026-12-01"}])

    def test_accepted_invite_blocks(self):
        src = [ev("ok", T1, T2, attendees=[{"self": True, "responseStatus": "accepted"}])]
        self.assertEqual(len(plan({"personal": src}, {"work": []})["creates"]), 1)

    def test_a_block_is_never_mirrored_onward(self):
        # work holds a block for a personal event; side must get ONE block (from
        # personal), not a second one copied from work's block.
        work_block = block("b1", "personal/p1", T1, T2)
        out = plan({"personal": [ev("p1", T1, T2)], "work": [work_block], "side": []},
                   {"work": [work_block], "side": []})
        self.assertEqual([(c["target"], c["event"]["extendedProperties"]["private"][TAG])
                          for c in out["creates"]], [("side", "personal/p1")])

    def test_meeting_already_on_the_target_gets_no_block(self):
        # Invited on both accounts: same iCalUID and start on both calendars.
        w = ev("m1", T1, T2)
        s = ev("m1x", "2026-12-01T08:00:00-07:00", "2026-12-01T09:00:00-07:00", iCalUID=w["iCalUID"])
        out = plan({"work": [w], "side": [s]}, {"work": [w], "side": [s]})
        self.assertEqual(out["creates"], [])

    def test_same_meeting_on_two_sources_blocks_a_third_once(self):
        w = ev("m1", T1, T2)
        s = ev("m1x", T1, T2, iCalUID=w["iCalUID"])
        out = plan({"work": [w], "side": [s]}, {"personal": []})
        self.assertEqual(len(out["creates"]), 1)

    def test_duplicate_blocks_are_collapsed(self):
        have = [block("b1", "personal/p1", T1, T2), block("b2", "personal/p1", T1, T2)]
        out = plan({"personal": [ev("p1", T1, T2)]}, {"work": have})
        self.assertEqual([d["event_id"] for d in out["deletes"]], ["b2"])
        self.assertEqual((out["creates"], out["updates"]), ([], []))

    def test_show_details_copies_the_title(self):
        out = plan({"personal": [ev("p1", T1, T2)]}, {"work": []}, show_details="true")
        self.assertEqual(out["creates"][0]["event"]["summary"], "Thing p1")

    def test_other_events_on_a_target_are_left_alone(self):
        out = plan({"personal": []}, {"work": [ev("w1", T1, T2)]})
        self.assertEqual((out["creates"], out["updates"], out["deletes"]), ([], [], []))

    def test_a_run_is_capped_at_batch_size_and_the_rest_carries_over(self):
        src = [ev("p%d" % i, T1, T2) for i in range(7)]
        out = plan({"personal": src}, {"work": []}, batch_size="3")
        self.assertEqual(len(out["creates"]), 3)
        self.assertEqual(out["pending"], 4)
        self.assertIn("7 to create", out["summary"])
        self.assertIn("batch_size 3, 4 left for later runs", out["summary"])
        # The next run sees the three blocks that now exist and plans the rest.
        have = [block("b%d" % i, c["event"]["extendedProperties"]["private"][TAG], T1, T2)
                for i, c in enumerate(out["creates"])]
        out = plan({"personal": src}, {"work": have}, batch_size="3")
        self.assertEqual((len(out["creates"]), out["pending"]), (3, 1))

    def test_nothing_pending_is_not_mentioned(self):
        out = plan({"personal": [ev("p1", T1, T2)]}, {"work": []})
        self.assertEqual(out["pending"], 0)
        self.assertNotIn("left for later", out["summary"])

    def test_time_already_blocked_by_hand_gets_no_block(self):
        # The same commitment entered separately on both calendars.
        p, w = ev("p1", T1, T2), ev("w1", "2026-12-01T08:00:00-07:00", "2026-12-01T09:00:00-07:00")
        out = plan({"personal": [p], "work": [w]}, {"work": [w], "personal": [p]})
        self.assertEqual((out["creates"], out["updates"], out["deletes"]), ([], [], []))
        self.assertIn("2 already blocked on the target", out["summary"])

    def test_a_longer_event_on_the_target_covers_a_shorter_one(self):
        w = ev("w1", "2026-12-01T14:00:00Z", "2026-12-01T17:00:00Z")
        out = plan({"personal": [ev("p1", T1, T2)]}, {"work": [w]})
        self.assertEqual(out["creates"], [])

    def test_partial_overlap_still_gets_a_block(self):
        w = ev("w1", "2026-12-01T15:30:00Z", "2026-12-01T16:30:00Z")
        out = plan({"personal": [ev("p1", T1, T2)]}, {"work": [w]})
        self.assertEqual(len(out["creates"]), 1)

    def test_free_or_declined_target_events_do_not_cover(self):
        for extra in ({"transparency": "transparent"},
                      {"attendees": [{"self": True, "responseStatus": "declined"}]}):
            out = plan({"personal": [ev("p1", T1, T2)]}, {"work": [ev("w1", T1, T2, **extra)]})
            self.assertEqual(len(out["creates"]), 1, extra)

    def test_a_block_does_not_count_as_the_targets_own_time(self):
        have = block("b1", "personal/p1", T1, T2)
        out = plan({"personal": [ev("p1", T1, T2)]}, {"work": [have]})
        self.assertEqual((out["creates"], out["deletes"]), ([], []))

    def test_redundant_blocks_are_removed_whatever_their_number(self):
        # Blocks made before the target's own events were taken into account.
        src = [ev("p%d" % i, T1, T2) for i in range(5)]
        have = [block("b%d" % i, "personal/p%d" % i, T1, T2) for i in range(5)]
        out = plan({"personal": src}, {"work": have + [ev("w1", T1, T2)]}, max_deletes="1")
        self.assertEqual(len(out["deletes"]), 5)
        self.assertEqual(out["creates"], [])

    def test_mass_delete_is_refused(self):
        have = [block("b%d" % i, "personal/p%d" % i, T1, T2) for i in range(4)]
        with self.assertRaisesRegex(RuntimeError, "refusing to delete 4 blocks"):
            plan({"personal": []}, {"work": have}, max_deletes="3")

    def test_unbinding_a_source_removes_its_blocks_without_tripping_the_guard(self):
        have = [block("b%d" % i, "side/s%d" % i, T1, T2) for i in range(4)]
        out = plan({"personal": []}, {"work": have}, max_deletes="1")
        self.assertEqual(len(out["deletes"]), 4)

    def test_remove_all_clears_every_block_and_creates_none(self):
        have = [block("b%d" % i, "personal/p%d" % i, T1, T2) for i in range(4)]
        out = plan({"personal": [ev("p9", T1, T2)]}, {"work": have + [ev("w1", T1, T2)]},
                   remove_all="true", max_deletes="1")
        self.assertEqual(sorted(d["event_id"] for d in out["deletes"]), ["b0", "b1", "b2", "b3"])
        self.assertEqual((out["creates"], out["updates"]), ([], []))
        self.assertIn("remove_all is on", out["summary"])


class SeriesTest(unittest.TestCase):
    """A repeating event gets one repeating block."""

    def series(self, out):
        return [c["event"] for c in out["creates"] if "recurrence" in c["event"]]

    def test_repeating_event_gets_one_repeating_block(self):
        out = plan({"personal": [occ("m1", w) for w in W]}, {"work": []}, src_raw={"personal": [M1]})
        self.assertEqual(len(out["creates"]), 1)
        b = out["creates"][0]["event"]
        self.assertEqual(b["recurrence"], WEEKLY)
        # Starts at the first occurrence ahead, not when the series began.
        self.assertEqual(b["start"], {"dateTime": W[0], "timeZone": TZ})
        self.assertEqual(b["end"], {"dateTime": "2026-12-01T16:00:00Z", "timeZone": TZ})
        self.assertEqual(b["extendedProperties"]["private"][TAG], "personal/m1")
        self.assertTrue(b["extendedProperties"]["private"][TAG + "Sig"])
        self.assertIn("personal -> work 4", out["summary"])
        self.assertIn("1 repeating block(s)", out["summary"])

    def test_cancelled_and_declined_occurrences_are_left_out(self):
        gone = {"id": "m1_x", "status": "cancelled", "recurringEventId": "m1",
                "originalStartTime": {"dateTime": W[1], "timeZone": TZ}}
        src = [occ("m1", W[0]), occ("m1", W[2], attendees=[{"self": True, "responseStatus": "declined"}]),
               occ("m1", W[3]), occ("m1", "2026-12-29T15:00:00Z")]
        out = plan({"personal": src}, {"work": []}, src_raw={"personal": [M1, gone]})
        b = self.series(out)[0]
        self.assertEqual(b["recurrence"], WEEKLY + ["EXDATE:20261208T150000Z", "EXDATE:20261215T150000Z"])
        self.assertEqual(len(out["creates"]), 1)

    def test_moved_occurrence_is_left_out_and_blocked_where_it_went(self):
        moved = occ("m1", W[1], start="2026-12-09T18:00:00Z", end="2026-12-09T19:00:00Z")
        out = plan({"personal": [occ("m1", W[0]), moved, occ("m1", W[2])]}, {"work": []},
                   src_raw={"personal": [M1]})
        self.assertEqual(self.series(out)[0]["recurrence"], WEEKLY + ["EXDATE:20261208T150000Z"])
        singles = [c["event"] for c in out["creates"] if "recurrence" not in c["event"]]
        self.assertEqual([b["start"]["dateTime"] for b in singles], ["2026-12-09T18:00:00Z"])

    def test_second_run_is_a_noop_and_stays_one_as_time_passes(self):
        src = [occ("m1", w) for w in W]
        first = plan({"personal": src}, {"work": []}, src_raw={"personal": [M1]})["creates"][0]["event"]
        have = dict(first, id="blk", status="confirmed")
        # What the target hands back: the block once (raw), and once per occurrence (expanded).
        expanded = [dict(have, id="blk_%d" % i, recurringEventId="blk",
                         start={"dateTime": w}, end={"dateTime": w.replace("T15:", "T16:")})
                    for i, w in enumerate(W)]
        for e in expanded:
            e.pop("recurrence")
        out = plan({"personal": src}, {"work": expanded}, src_raw={"personal": [M1]}, dst_raw={"work": [have]})
        self.assertEqual((out["creates"], out["updates"], out["deletes"]), ([], [], []))
        self.assertEqual(out["adrift"], 0)
        # A week later the first occurrence has passed; the block is left as it is.
        out = plan({"personal": src[1:]}, {"work": expanded[1:]}, src_raw={"personal": [M1]},
                   dst_raw={"work": [have]})
        self.assertEqual((out["creates"], out["updates"], out["deletes"]), ([], [], []))

    def test_a_newly_declined_occurrence_changes_the_block(self):
        src = [occ("m1", w) for w in W]
        have = dict(plan({"personal": src}, {"work": []}, src_raw={"personal": [M1]})["creates"][0]["event"],
                    id="blk", status="confirmed")
        src[2]["attendees"] = [{"self": True, "responseStatus": "declined"}]
        out = plan({"personal": src}, {"work": []}, src_raw={"personal": [M1]}, dst_raw={"work": [have]})
        self.assertEqual([u["event_id"] for u in out["updates"]], ["blk"])
        self.assertIn("EXDATE:20261215T150000Z", out["updates"][0]["event"]["recurrence"])
        self.assertEqual(out["creates"], [])

    def test_a_counted_rule_falls_back_to_one_block_per_occurrence(self):
        m = master("m1", "2026-01-06T15:00:00Z", "2026-01-06T16:00:00Z", rules=["RRULE:FREQ=WEEKLY;COUNT=60"])
        out = plan({"personal": [occ("m1", w) for w in W]}, {"work": []}, src_raw={"personal": [m]})
        self.assertEqual(self.series(out), [])
        self.assertEqual(len(out["creates"]), 4)

    def test_mostly_unneeded_series_gets_single_blocks(self):
        declined = [{"self": True, "responseStatus": "declined"}]
        src = [occ("m1", W[0]), occ("m1", W[1], attendees=declined), occ("m1", W[2], attendees=declined)]
        out = plan({"personal": src}, {"work": []}, src_raw={"personal": [M1]})
        self.assertEqual(self.series(out), [])
        self.assertEqual(len(out["creates"]), 1)

    def test_series_already_blocked_by_hand_gets_nothing(self):
        mine = [ev("w%d" % i, w, w.replace("T15:", "T16:")) for i, w in enumerate(W)]
        out = plan({"personal": [occ("m1", w) for w in W]}, {"work": mine}, src_raw={"personal": [M1]})
        self.assertEqual((out["creates"], out["updates"], out["deletes"]), ([], [], []))
        self.assertIn("4 already blocked on the target", out["summary"])

    def test_old_single_blocks_give_way_to_the_repeating_block(self):
        src = [occ("m1", w) for w in W]
        old = [block("b%d" % i, "personal/" + o["id"], o["start"]["dateTime"], o["end"]["dateTime"])
               for i, o in enumerate(src)]
        out = plan({"personal": src}, {"work": old}, src_raw={"personal": [M1]}, max_deletes="1")
        self.assertEqual(sorted(d["event_id"] for d in out["deletes"]), ["b0", "b1", "b2", "b3"])
        self.assertEqual(len(self.series(out)), 1)

    def test_all_day_series_uses_date_exclusions(self):
        m = {"id": "d1", "status": "confirmed", "iCalUID": "d1@google.com", "summary": "Trip",
             "start": {"date": "2026-01-05"}, "end": {"date": "2026-01-06"}, "recurrence": ["RRULE:FREQ=WEEKLY"]}
        day = lambda d, **x: dict({"id": "d1_" + d, "status": "confirmed", "iCalUID": "d1@google.com",
                                   "recurringEventId": "d1", "originalStartTime": {"date": d},
                                   "start": {"date": d}, "end": {"date": d[:8] + "%02d" % (int(d[8:]) + 1)}}, **x)
        src = [day("2026-12-07"), day("2026-12-14", transparency="transparent"), day("2026-12-21")]
        out = plan({"personal": src}, {"work": []}, src_raw={"personal": [m]}, mirror_all_day="true")
        b = self.series(out)[0]
        self.assertEqual(b["start"], {"date": "2026-12-07"})
        self.assertEqual(b["recurrence"], ["RRULE:FREQ=WEEKLY", "EXDATE;VALUE=DATE:20261214"])


class WindowTest(unittest.TestCase):
    def test_clearing_reaches_back_a_year(self):
        out = run_js(step_code("window"), {"cfg": dict(CFG, remove_all="true")})
        from datetime import datetime
        lo = datetime.fromisoformat(out["timeMin"].replace("Z", "+00:00"))
        hi = datetime.fromisoformat(out["timeMax"].replace("Z", "+00:00"))
        self.assertEqual((hi - lo).days, 366 + 30)

    def test_window_spans_the_setting(self):
        out = run_js(step_code("window"), {"cfg": dict(CFG, window_days="7")})
        from datetime import datetime
        lo = datetime.fromisoformat(out["timeMin"].replace("Z", "+00:00"))
        hi = datetime.fromisoformat(out["timeMax"].replace("Z", "+00:00"))
        self.assertEqual((hi - lo).days, 7)

    def test_bad_window_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "window_days"):
            run_js(step_code("window"), {"cfg": dict(CFG, window_days="0")})


if __name__ == "__main__":
    unittest.main()
