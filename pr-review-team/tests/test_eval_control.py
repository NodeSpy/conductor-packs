"""review-eval's control arm: each PR replayed memory off (A), memory off again
(B) and memory on (M). Memory retention (A vs M) is judged against the noise
floor two memory-off runs set (control agreement, A vs B), not a fixed 90%."""

import datetime as dt
import importlib.util
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_calibration_script import SCRIPT, Fixture, post  # noqa: E402
from test_manifest_js import eval_step, f, run_js  # noqa: E402


def calib():
    spec = importlib.util.spec_from_file_location("calibration", SCRIPT)
    c = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(c)
    return c


C = calib()
T, F = True, False


def unit(a_in_b, b_in_a, a_in_m):
    return {"a_in_b": list(a_in_b), "b_in_a": list(b_in_a), "a_in_m": list(a_in_m)}


def pr_unit(n_a, n_b, ab, am):
    """A PR with n_a A findings and n_b B ones; the first `ab` matched A-B, the first `am` A-M."""
    return unit([i < ab for i in range(n_a)], [i < ab for i in range(n_b)], [i < am for i in range(n_a)])


class AgreementTest(unittest.TestCase):

    def test_control_agreement_averages_both_directions(self):
        ctrl, mem, gap = C.control_point([unit([T, T, F, T], [T, T, T, F, F], [T, F, F, T])])
        self.assertAlmostEqual(ctrl, (3 / 4 + 3 / 5) / 2)       # A's found by B, B's found by A
        self.assertAlmostEqual(mem, 2 / 4)                     # A's kept by M
        self.assertAlmostEqual(gap, ctrl - mem)

    def test_pooled_over_findings_not_averaged_over_prs(self):
        units = [pr_unit(10, 10, 9, 9), pr_unit(2, 2, 0, 0)]
        ctrl, mem, _ = C.control_point(units)
        self.assertAlmostEqual(ctrl, 9 / 12)
        self.assertAlmostEqual(mem, 9 / 12)

    def test_nothing_found_is_nothing_missed(self):
        self.assertEqual(C.control_point([unit([], [], [])]), (1.0, 1.0, 0.0))
        ctrl, mem, _ = C.control_point([unit([], [F, F], [])])     # B alone found two
        self.assertEqual((ctrl, mem), (0.5, 1.0))


class IntervalTest(unittest.TestCase):

    UNITS = [pr_unit(6, 5, 3, 2), pr_unit(4, 4, 3, 3), pr_unit(8, 7, 4, 2), pr_unit(3, 5, 2, 2), pr_unit(5, 5, 4, 1)]

    def test_the_same_seed_gives_the_same_interval(self):
        a = C.control_interval(self.UNITS, 7)
        self.assertEqual(a, C.control_interval(self.UNITS, 7))
        self.assertEqual(a, calib().control_interval(self.UNITS, 7))     # a fresh interpreter too
        self.assertNotEqual(a, C.control_interval(self.UNITS, 8))

    def test_seeded_values_are_pinned(self):
        # regression pin: the interval for this data and seed 1 never moves
        lo, hi = C.control_interval(self.UNITS, 1)
        self.assertEqual((round(lo, 4), round(hi, 4)), PINNED)

    def test_the_interval_brackets_the_gap_and_narrows_with_more_prs(self):
        gap = C.control_point(self.UNITS)[2]
        lo, hi = C.control_interval(self.UNITS, 1)
        self.assertLess(lo, gap)
        self.assertGreater(hi, gap)
        lo4, hi4 = C.control_interval(self.UNITS * 4, 1)
        self.assertLess(hi4 - lo4, hi - lo)

    def test_a_wider_level_is_a_wider_interval(self):
        lo, hi = C.control_interval(self.UNITS, 1, level=0.9)
        lo5, hi5 = C.control_interval(self.UNITS, 1, level=0.5)
        self.assertLess(lo, lo5)
        self.assertGreater(hi, hi5)

    def test_the_interval_is_the_5th_and_95th_percentile(self):
        # resampling a PR's findings: a PR whose A findings are all kept by M and found
        # by B has gap 0 whatever is drawn, so a mix of PRs bounds the draws
        lo, hi = C.control_interval(self.UNITS, 1, resamples=4000)
        lo2, hi2 = C.control_interval(self.UNITS, 1, resamples=4000, level=0.98)
        self.assertLess(lo2, lo)
        self.assertGreater(hi2, hi)

    def test_runs_that_agree_exactly_have_no_spread(self):
        same = [pr_unit(4, 4, 4, 4), pr_unit(3, 3, 3, 3)]
        self.assertEqual(C.control_interval(same, 1), (0.0, 0.0))

    def test_one_pr_resamples_its_findings(self):
        lo, hi = C.control_interval([pr_unit(10, 10, 6, 6)], 1)
        self.assertLess(lo, 0)
        self.assertGreater(hi, 0)


class VerdictTest(unittest.TestCase):

    def test_crowding_only_when_memory_is_credibly_below_the_control(self):
        self.assertEqual(C.control_verdict(0.01, 0.3), "crowding")
        self.assertEqual(C.control_verdict(0.0, 0.3), "ok")        # the interval reaches 0
        self.assertEqual(C.control_verdict(-0.1, 0.3), "ok")
        self.assertEqual(C.control_verdict(-0.3, -0.01), "better")  # memory credibly better
        self.assertEqual(C.control_verdict(-0.3, 0.0), "ok")

    def test_44_percent_retention_is_ok_when_two_memory_off_runs_agree_as_little(self):
        # 20 PRs: memory keeps 44% of A, and B finds only 45% of A — the noise floor
        prs = [{"control": pr_unit(5, 5, 2 + (i % 2), 2 + (i % 3 == 0))} for i in range(20)]
        s = C.control_stats(prs, 1)
        self.assertAlmostEqual(s["retention"], sum(2 + (i % 3 == 0) for i in range(20)) / 100)
        self.assertLess(s["lo"], 0)
        self.assertEqual(s["verdict"], "ok")
        self.assertFalse(s["better"])
        self.assertEqual(len(s["prs"]), 20)

    def test_crowding_when_memory_loses_far_more_than_the_control(self):
        prs = [{"control": pr_unit(5, 5, 4, 1)} for _ in range(20)]
        s = C.control_stats(prs, 1)
        self.assertGreater(s["lo"], 0)
        self.assertEqual(s["verdict"], "crowding")

    def test_memory_better_is_ok(self):
        prs = [{"control": pr_unit(5, 5, 1, 5)} for _ in range(20)]
        s = C.control_stats(prs, 1)
        self.assertLess(s["hi"], 0)
        self.assertEqual((s["verdict"], s["better"]), ("ok", True))

    def test_no_control_arm_no_control_stats(self):
        self.assertIsNone(C.control_stats([{"ref": "acme/api#1"}], 1))
        self.assertIsNone(C.control_stats([{"control": pr_unit(1, 1, 1, 1)}, {"ref": "acme/api#2"}], 1))
        self.assertIsNone(C.control_stats([], 1))

    def test_per_pr_numbers(self):
        prs = [{"control": pr_unit(4, 4, 4, 2)}, {"control": pr_unit(4, 4, 2, 4)}]
        per = C.control_stats(prs, 1)["prs"]
        self.assertEqual([(p["agreement"], p["retention"]) for p in per], [(1.0, 0.5), (0.5, 1.0)])
        self.assertEqual([round(p["gap"], 2) for p in per], [0.5, -0.5])
        for p in per:
            self.assertLessEqual(p["lo"], p["hi"])


# the pinned interval for IntervalTest.UNITS, seed 1
PINNED = (0.0078, 0.4357)


def runs(*refs, control=True):
    out = []
    for r in refs:
        repo, n = r.split("#")
        out.append({"repo": repo, "pr": int(n), "memory": "off", "arm": "A"})
        if control:
            out.append({"repo": repo, "pr": int(n), "memory": "off", "arm": "B"})
        out.append({"repo": repo, "pr": int(n), "memory": "on", "arm": "M"})
    return out


def compare(run_list, items, p_same=()):
    pairs = run_js(eval_step("pairs")["code"], {"pick": {"runs": run_list}, "replay": {"items": items}})
    judge = {"items": [{"same_issue": {"noul": x}} for x in p_same]}
    return pairs, run_js(eval_step("compare")["code"], {"pairs": pairs, "judge": judge})


A = [f("a.go", 10, "Return the error."), f("a.go", 40, "One query per row."), f("b.go", 3, "Validate the path.")]
B = [f("a.go", 10, "Return the error."), f("c.go", 5, "Close the file."), f("d.go", 1, "Bound the retry.")]
M = [f("a.go", 10, "Return the error."), f("c.go", 5, "Close the file."), f("e.go", 2, "Set DB_URL.", note="deploy env")]


class CompareTest(unittest.TestCase):

    def test_three_arms_mark_every_finding(self):
        _, out = compare(runs("acme/api#1"), [{"findings": A}, {"findings": B}, {"findings": M, "notes": 2}])
        pr = out["prs"][0]
        self.assertEqual(pr["control"], {"a_in_b": [T, F, F], "b_in_a": [T, F, F], "a_in_m": [T, F, F]})
        # the old numbers, A vs M, unchanged
        self.assertEqual((pr["baseline"], pr["with_memory"], len(pr["retained"]), len(pr["dropped"])), (3, 3, 1, 2))
        self.assertEqual([(x["path"], x["in_control"]) for x in pr["added"]], [("c.go", True), ("e.go", False)])
        self.assertEqual((pr["added_in_control"], pr["added_citing_note"]), (1, 1))
        t = out["totals"]
        self.assertEqual((t["baseline"], t["control_findings"], t["with_memory"], t["added"], t["added_in_control"]), (3, 3, 3, 2, 1))
        self.assertEqual(t["verdict"], "crowding")       # the fixed 90% target, kept

    def test_marks_follow_each_arm_s_own_order(self):
        a = [f("a.go", 10, "Return the error."), f("b.go", 3, "Validate the path.")]
        b = [f("x.go", 1, "Close the file."), f("y.go", 1, "Bound the retry."), f("a.go", 10, "Return the error.")]
        m = [f("b.go", 3, "Validate the path."), f("z.go", 9, "Set DB_URL.")]
        _, out = compare(runs("acme/api#1"), [{"findings": a}, {"findings": b}, {"findings": m}])
        self.assertEqual(out["prs"][0]["control"], {"a_in_b": [T, F], "b_in_a": [F, F, T], "a_in_m": [F, T]})

    def test_the_judge_decides_near_pairs_in_every_comparison(self):
        a, b, m = [f("a.go", 40, "loop queries per row")], [f("a.go", 42, "N+1 in this loop")], [f("a.go", 44, "per-row query")]
        pairs, _ = compare(runs("acme/api#1"), [{"findings": a}, {"findings": b}, {"findings": m}])
        self.assertEqual([p["g"] for p in pairs["list"]], ["acme/api#1|AM", "acme/api#1|AB", "acme/api#1|BM"])
        _, out = compare(runs("acme/api#1"), [{"findings": a}, {"findings": b}, {"findings": m}], [0.1, 0.9, 0.9])
        pr = out["prs"][0]
        self.assertEqual(pr["control"], {"a_in_b": [T], "b_in_a": [T], "a_in_m": [F]})
        self.assertEqual([x["in_control"] for x in pr["added"]], [T])

    def test_without_the_control_the_old_two_arm_output(self):
        items = [{"findings": A}, {"findings": M}]
        _, two = compare(runs("acme/api#1", control=False), items)
        legacy = [{"repo": "acme/api", "pr": 1, "memory": "off"}, {"repo": "acme/api", "pr": 1, "memory": "on"}]
        _, old = compare(legacy, items)
        self.assertEqual(two, old)
        self.assertNotIn("control", two["prs"][0])
        self.assertNotIn("control_findings", two["totals"])
        self.assertNotIn("in_control", json.dumps(two))


class PickTest(unittest.TestCase):

    def setUp(self):
        self.f = Fixture()
        self.f.audit(post(self.f.now - dt.timedelta(days=1), "acme/api", 4))

    def tearDown(self):
        self.f.close()

    def pick(self, **inputs):
        return self.f.run("eval-pick", stdin=json.dumps({"inputs": inputs}))

    def test_three_runs_a_pr_by_default_two_without_the_control(self):
        out = json.loads(self.pick(repos="acme/api").stdout)
        self.assertEqual(([r["arm"] for r in out["runs"]], [r["memory"] for r in out["runs"]]), (["A", "B", "M"], ["off", "off", "on"]))
        self.assertEqual((out["control"], out["seed"]), (True, C.EVAL_SEED))
        for v in ("false", "no", "off", " False "):
            out = json.loads(self.pick(repos="acme/api", control=v).stdout)
            self.assertEqual(([r["arm"] for r in out["runs"]], out["control"]), (["A", "M"], False))
        self.assertTrue(json.loads(self.pick(repos="acme/api", control="yes").stdout)["control"])
        self.assertEqual(json.loads(self.pick(repos="acme/api", seed="42").stdout)["seed"], 42)

    def test_bad_switches_fail_plainly(self):
        for inputs, says in (({"control": "maybe"}, "control= takes true or false"), ({"seed": "-1"}, "seed=")):
            res = self.pick(repos="acme/api", **inputs)
            self.assertNotEqual(res.returncode, 0, inputs)
            self.assertIn(says, res.stderr)


class ReportTest(unittest.TestCase):

    def setUp(self):
        self.f = Fixture()

    def tearDown(self):
        self.f.close()

    def report(self, runs_list, items, seed=1):
        _, cmp = compare(runs_list, items)
        res = self.f.run("eval-report", stdin=json.dumps({"compare": cmp, "pick": {"scope_key": "", "seed": seed}}))
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        return out, open(out["path"]).read()

    def test_the_control_report(self):
        out, text = self.report(runs("acme/api#1"), [{"findings": A}, {"findings": B}, {"findings": M, "notes": 2}])
        c = out["totals"]["control"]
        self.assertAlmostEqual(c["agreement"], 1 / 3)
        self.assertAlmostEqual(c["retention"], 1 / 3)
        self.assertEqual((out["totals"]["verdict"], out["totals"]["verdict_fixed"]), ("ok", "crowding"))
        for must in ("reviewed three times", "| findings: A (memory off) | 3 |", "| findings: B (memory off, control) | 3 |",
                     "| findings: M (memory on) | 3 |", "| control agreement (A vs B) | 33% |", "| memory retention (A vs M) | 33% |",
                     "| gap (control - memory) | +0 pts, 90% interval", "| verdict | ok |",
                     "| retained (A in M) | 1 (33%) |",
                     "| added with memory | 2 (1 also found by the control, so not memory's doing; 1 citing a note) |",
                     "| old verdict (fixed 90% target) | crowding |",
                     "Findings A 3, B 3, M 3; control agreement 33%, memory retention 33%, gap +0 pts (90% interval",
                     "c.go:5 — Close the file. [the control found it too]", "e.go:2 — Set DB_URL. [note: deploy env]\n"):
            self.assertIn(must, text)
        s = out["summary"]
        for must in ("findings A 3 / B 3 / M 3", "control agreement 33%", "memory retention 33%", "within the noise",
                     "1 retained with memory (33%)", "2 added (1 also found by the control, 1 citing a note)",
                     "[fixed 90% target: crowding]"):
            self.assertIn(must, s)
        latest = json.load(open(os.path.join(self.f.state, "pr-review-team", "eval", "latest.json")))
        self.assertEqual(latest["totals"]["control"]["verdict"], "ok")

    def test_crowding_and_better_are_said_plainly(self):
        many = ["acme/api#%d" % i for i in range(1, 13)]
        keep = [f("a.go", 100 * k, "f%d" % k) for k in range(4)]
        crowd = [it for _ in many for it in ({"findings": keep}, {"findings": keep}, {"findings": keep[:1]})]
        out, text = self.report(runs(*many), crowd)
        self.assertEqual(out["totals"]["verdict"], "crowding")
        self.assertIn("credibly below", out["summary"])
        self.assertIn("| verdict | crowding |", text)
        better = [it for _ in many for it in ({"findings": keep}, {"findings": keep[:1]}, {"findings": keep})]
        out, text = self.report(runs(*many), better)
        self.assertEqual(out["totals"]["verdict"], "ok")
        self.assertIn("memory retains more than", out["summary"])
        self.assertIn("| verdict | ok (memory better) |", text)

    def test_the_same_runs_and_seed_give_the_same_report(self):
        items = [{"findings": A}, {"findings": B}, {"findings": M}] * 3
        one, t1 = self.report(runs("acme/api#1", "acme/api#2", "acme/api#3"), items, seed=5)
        two, t2 = self.report(runs("acme/api#1", "acme/api#2", "acme/api#3"), items, seed=5)
        self.assertEqual(one["summary"], two["summary"])
        self.assertEqual(t1.split("\n", 1)[1], t2.split("\n", 1)[1])
        self.assertIn("(seed 5, 2000 resamples)", t1)                   # the pick's seed, not the default

    def test_without_the_control_the_old_report(self):
        out, text = self.report(runs("acme/api#1", control=False), [{"findings": A}, {"findings": M}])
        self.assertNotIn("control", out["totals"])
        self.assertEqual(out["totals"]["verdict"], "crowding")
        self.assertIn("Target: memory-on retains nearly all baseline findings", text)
        self.assertNotIn("control", text)


if __name__ == "__main__":
    unittest.main()
