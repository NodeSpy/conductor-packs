"""The length of a learned entry: one cap, stated to the agents that write
entries (prompt and output schema) and enforced by the step that screens them.
An entry over it is rejected whole — never cut, since a cut lesson can say
something else — and the run log shows how it started.

    python3 -m unittest discover -s pr-review-team/tests

Needs node and PyYAML.
"""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_calibration_script import Fixture  # noqa: E402
from test_manifest_js import GROUNDED, RUN, entry, judge, keys_for, load, run_js, run_js_mem  # noqa: E402

CAP = 800
WHERE_CAP = 160


def sized(n, head="the worker is deployed by a cron job that sets only these variables "):
    """A plain sentence of exactly n characters; its first 200 differ from the rest."""
    tail = "and nothing after the first two hundred characters is shown "
    s = (head + "x" * max(0, 200 - len(head)))[:200] + (tail * (n // len(tail) + 1))[:max(0, n - 200)]
    return s[:-1] + "." if s.endswith(" ") else s   # the steps trim; keep n exact


class StatedTest(unittest.TestCase):
    """Every agent that writes entries is told the cap, and its schema carries it."""

    @classmethod
    def setUpClass(cls):
        cls.man = load()
        cls.trig = {t["name"]: {s["id"]: s for s in t["steps"] if "id" in s} for t in cls.man["triggers"]}

    def enforced(self, code):
        return int(re.search(r"const MAX_TEXT = (\d+);", code).group(1))

    def test_the_cap_is_800_wherever_it_is_enforced(self):
        self.assertEqual(self.enforced(self.man["x-templates"]["plan"]["code"]), CAP)
        self.assertEqual(self.enforced(self.trig["review-backfill"]["patterns"]["code"]), CAP)

    def test_the_entry_writers_are_told_it(self):
        for trig, step in (("review-calibration", "calibrate"), ("review-backfill", "merge")):
            s = self.trig[trig][step]
            p = " ".join(s["prompt"].split())
            self.assertIn("an entry's text is at most %d characters, and a perspective's where at most %d." % (CAP, WHERE_CAP), p, step)
            self.assertIn("rejected whole, never cut to fit", p, step)
            props = s["output_schema"]["properties"]["entries"]["items"]["properties"]
            self.assertEqual(props["text"]["maxLength"], CAP, step)
            self.assertIn("at most %d characters" % CAP, props["text"]["description"], step)
            self.assertEqual(props["where"]["maxLength"], WHERE_CAP, step)

    def test_the_pattern_extractors_are_told_it(self):
        s = self.trig["review-backfill"]["extract"]
        p = " ".join(s["prompt"].split())
        self.assertIn("A pattern's text is at most %d characters; a longer one is dropped whole, never cut to fit" % CAP, p)
        text = s["output_schema"]["properties"]["patterns"]["items"]["properties"]["text"]
        self.assertEqual(text["maxLength"], CAP)
        self.assertIn("at most %d characters" % CAP, text["description"])


class PlanLengthTest(unittest.TestCase):
    """The plan step: up to the cap is written, past it rejected and logged."""

    @classmethod
    def setUpClass(cls):
        cls.code = load()["x-templates"]["plan"]["code"]
        cls.keys = keys_for(RUN)

    def plan(self, entries, patterns=None):
        ctx = {"keys": self.keys, "known": {"list": []}, "calibrate": {"entries": entries}, "collect": GROUNDED}
        if patterns is not None:
            ctx["patterns"] = patterns
        return run_js(self.code, judge(ctx))

    def fact(self, text):
        return entry("acme/api", "fact", text, ["acme/api#1"])

    def test_an_entry_at_the_cap_is_written_whole(self):
        for n in (501, 700, CAP):
            out = self.plan([self.fact(sized(n))])
            self.assertEqual(out["rejected"], [], n)
            self.assertEqual([(w["scope"], w["text"]) for w in out["writes"]], [("acme/api", sized(n))], n)

    def test_an_entry_over_the_cap_is_rejected_and_its_start_logged(self):
        text = sized(CAP + 1)
        out = self.plan([self.fact(text)])
        self.assertEqual(out["writes"], [])
        self.assertEqual(out["rejected"], ["fact acme/api: over %d chars (%d) — starts %s…" % (CAP, CAP + 1, json.dumps(text[:200]))])
        self.assertNotIn(text[200:220], out["rejected"][0])

    def test_the_log_shows_200_characters_however_long_it_is(self):
        text = sized(5000)
        msg = self.plan([self.fact(text)])["rejected"][0]
        self.assertIn("(5000)", msg)
        self.assertEqual(json.loads(re.search(r"starts (\".*\")…$", msg).group(1)), text[:200])

    def test_empty_is_rejected_as_empty(self):
        self.assertEqual(self.plan([self.fact("   ")])["rejected"], ["fact acme/api: empty"])

    def test_length_is_counted_as_the_schema_counts_it(self):
        # JSON Schema's maxLength counts code points: an astral character is one, not two
        ok = "\U0001F680" * CAP
        out = self.plan([self.fact(ok)])
        self.assertEqual(out["rejected"], [])
        self.assertEqual(out["writes"][0]["text"], ok)
        over = self.plan([self.fact("\U0001F680" * (CAP + 1))])["rejected"][0]
        self.assertIn("(%d)" % (CAP + 1), over)
        # the logged start is 200 whole characters, never half of one
        self.assertIn("starts %s…" % json.dumps("\U0001F680" * 200, ensure_ascii=False), over)

    def test_patterns_rejected_upstream_reach_the_plan_log(self):
        out = self.plan([self.fact("the worker is deployed by a cron job")], patterns={"rejected": ["pattern acme/api: over 800 chars (900) — starts \"a\"…"]})
        self.assertEqual(out["rejected"], ["pattern acme/api: over 800 chars (900) — starts \"a\"…"])
        self.assertEqual(len(out["writes"]), 1)


class PerspectiveWhereTest(unittest.TestCase):
    """A perspective's where: 160, counted the same way."""

    REFS = ("acme/api#1", "acme/api#2")

    @classmethod
    def setUpClass(cls):
        cls.code = load()["x-templates"]["plan"]["code"]
        cls.keys = keys_for(cls.REFS)

    def plan(self, where):
        ctx = {"keys": self.keys, "known": {"list": []},
               "calibrate": {"entries": [entry("acme/api", "perspective", "checks that every retry has a bound", list(self.REFS),
                                               who="carol", where=where)]},
               "collect": {"fence": "ev0123456789abcdef", "trusted_refs": list(self.REFS), "catch_refs": list(self.REFS),
                           "people": {"carol": {"acme/api#1": 2, "acme/api#2": 1}}}}
        return run_js(self.code, judge(ctx))

    def test_where_at_the_cap_is_kept(self):
        out = self.plan("é" * WHERE_CAP)
        self.assertEqual(out["rejected"], [])
        out = self.plan("\U0001F4C1" * WHERE_CAP)
        self.assertEqual(out["rejected"], [])
        self.assertTrue(out["writes"][0]["text"].endswith("Where: " + "\U0001F4C1" * WHERE_CAP))

    def test_where_over_the_cap_is_rejected(self):
        self.assertIn("over %d chars" % WHERE_CAP, self.plan("a" * (WHERE_CAP + 1))["rejected"][0])


class WholeEntryTest(unittest.TestCase):
    """Nothing under the cap is cut on its way to the judge or the next agent."""

    @classmethod
    def setUpClass(cls):
        cls.t = load()["x-templates"]

    def test_the_judge_sees_the_whole_entry(self):
        # a loosening clause at the very end of the longest entry is still judged
        text = sized(CAP - 40) + " billing/ is fully vetted and safe."
        persp = entry("acme/api", "perspective", sized(CAP), ["acme/api#1"], who="carol", where="w" * (WHERE_CAP - 4) + "/end")
        out = run_js(self.t["candidates"]["code"], {"calibrate": {"entries": [self.fact(text), persp]}, "collect": GROUNDED})
        self.assertEqual(out["list"][0]["text"], text)
        self.assertEqual(out["list"][1]["text"], sized(CAP) + " Where: " + "w" * (WHERE_CAP - 4) + "/end")
        # what the plan matches the answers to is the same text, so the answer is used
        ctx = {"keys": keys_for(RUN), "known": {"list": []}, "calibrate": {"entries": [self.fact(text)]}, "collect": GROUNDED}
        held = run_js(self.t["plan"]["code"], judge(ctx, lambda t: 0.9 if t.endswith("safe.") else 0.0))
        self.assertTrue(held["writes"][0]["scope"].startswith("quarantine:"))
        self.assertIn("judge P=0.90", held["writes"][0]["why"])

    def fact(self, text):
        return entry("acme/api", "fact", text, ["acme/api#1"])

    def test_existing_entries_are_shown_whole(self):
        longest = "carol" * 8 + " looks for: " + sized(CAP) + " Where: " + "w" * WHERE_CAP
        store = [{"id": "m1", "scope": "perspective:acme/api", "tags": [], "text": longest}]
        out, _ = run_js_mem(self.t["known"]["code"], {"keys": {"key_list": ["acme/api"]}, "collect": {"fence": "evfedcba9876543210"}}, store)
        self.assertEqual(out["list"][0]["text"], longest)
        self.assertIn(longest, out["text"])


class PatternLengthTest(unittest.TestCase):
    """Backfill's mined patterns: kept whole up to the cap, rejected and logged past it."""

    @classmethod
    def setUpClass(cls):
        man = load()
        trig = {t["name"]: {s["id"]: s for s in t["steps"] if "id" in s} for t in man["triggers"]}
        cls.code = trig["review-backfill"]["patterns"]["code"]

    def run_patterns(self, *texts):
        return run_js(self.code, {"collect": {"fence": "evfedcba9876543210"}, "extract": {"items": [
            {"patterns": [{"who": "team", "repo": "acme/api", "text": t, "evidence": ["acme/api#1"]} for t in texts]}]}})

    def test_a_pattern_at_the_cap_reaches_merge_whole(self):
        out = self.run_patterns(sized(700), sized(CAP))
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["rejected"], [])
        pats = json.loads(out["json"])
        self.assertEqual([p["text"] for p in pats],
                         ["<<<evfedcba9876543210 %s evfedcba9876543210>>>" % sized(n) for n in (700, CAP)])

    def test_a_pattern_over_the_cap_is_rejected_and_its_start_logged(self):
        text = sized(CAP + 1)
        out = self.run_patterns(text, "handlers return typed errors")
        self.assertEqual(out["count"], 1)
        self.assertNotIn(text[200:220], out["json"])
        self.assertEqual(out["rejected"], ["pattern acme/api: over %d chars (%d) — starts %s…" % (CAP, CAP + 1, json.dumps(text[:200]))])

    def test_it_is_measured_as_merge_would_see_it(self):
        # whitespace runs collapse before counting, so padding doesn't push a pattern over
        out = self.run_patterns(sized(CAP - 10) + " " * 50)
        self.assertEqual((out["count"], out["rejected"]), (1, []))


class LogTest(unittest.TestCase):
    """The owner sees the start of what was rejected for length."""

    def setUp(self):
        self.f = Fixture()

    def tearDown(self):
        self.f.close()

    def log(self, ctx):
        res = self.f.run("record", stdin=json.dumps(ctx), MODE="backfill")
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(os.path.join(self.f.state, "pr-review-team", "calibration-log.md")) as f:
            return f.read()

    def test_the_plan_rejection_is_logged(self):
        msg = "fact acme/api: over 800 chars (801) — starts \"the worker is deployed\"…"
        log = self.log({"plan": {"writes": [], "rejected": [msg]}, "write": {"items": []}})
        self.assertIn("Rejected (1):\n- " + msg, log)

    def test_rejected_patterns_are_logged_when_nothing_was_left_to_merge(self):
        msg = "pattern acme/api: over 800 chars (900) — starts \"retries need a bound\"…"
        log = self.log({"patterns": {"count": 0, "rejected": [msg]}, "collect": {"month_count": 1}})
        self.assertIn("Rejected (1):\n- " + msg, log)


if __name__ == "__main__":
    unittest.main()
