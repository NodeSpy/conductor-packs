"""Checkouts on a manual run: a manual trigger's own target is synthetic
("manual:<name>"), so a step with `checkout: checkout-pr` that a manual run
reaches must say which PR to check out (`checkout_pr:`) — else the daemon
clones "manual:<name>" and the step fails. review-eval's replay is that run:
it calls review-team, whose `snapshot` step fetches the PR for the lenses, named
by the workflow's inputs — never the run's own target.

    python3 -m unittest discover -s pr-review-team/tests

Needs PyYAML.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_manifest_js import load  # noqa: E402


def version(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


class ReplayCheckoutTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.man = load()
        cls.workflows = cls.man["workflows"]

    def reached(self, steps, seen=None):
        """Every (workflow, step) a list of steps reaches through workflow calls."""
        seen = set() if seen is None else seen
        out = []
        for s in steps:
            out.append((None, s))
            name = s.get("workflow")
            if name and name not in seen:
                seen.add(name)
                for _, t in self.reached(self.workflows[name]["steps"], seen):
                    out.append((name, t))
        return out

    def manual_checkouts(self):
        found = []
        for trig in self.man["triggers"]:
            on = trig.get("on", trig.get(True))   # YAML 1.1 reads the key `on` as true
            on = on if isinstance(on, list) else [on]
            if "manual" not in on:
                continue
            for wf, s in self.reached(trig["steps"]):
                if s.get("checkout") == "checkout-pr":
                    found.append((trig["name"], wf, s))
        return found

    def test_a_manual_replay_checks_out_the_replayed_pr(self):
        replay = next(s for s in next(t for t in self.man["triggers"] if t["name"] == "review-eval")["steps"] if s.get("id") == "replay")
        self.assertEqual(replay["workflow"], "review-team")
        self.assertEqual((replay["with"]["repo"], replay["with"]["pr"]), ("{{.item.repo}}", "{{.item.pr}}"))
        tree = next(s for s in self.workflows["review-team"]["steps"] if s.get("id") == "snapshot")
        self.assertEqual(tree["uses"], "github.pr_snapshot")
        # the replayed PR, as the workflow received it — not the run's own target
        self.assertEqual(tree["options"], {"repo": "{{.inputs.repo}}", "pr": "{{.inputs.pr}}"})

    def test_every_checkout_a_manual_run_reaches_names_its_pr(self):
        for trig, wf, s in self.manual_checkouts():
            where = "%s -> %s/%s" % (trig, wf, s.get("id"))
            self.assertIsNotNone(wf, where + ": a manual trigger's own step has no PR of its own to name")
            inputs = self.workflows[wf].get("inputs") or {}
            self.assertIn("repo", inputs, where)
            self.assertIn("pr", inputs, where)
            self.assertEqual(s.get("checkout_pr"), {"repo": "{{.inputs.repo}}", "pr": "{{.inputs.pr}}"}, where)

    def test_the_floor_is_a_daemon_that_has_checkout_pr(self):
        floor = re.search(r">=\s*([\d.]+)", self.man["pack"]["requires"]["conductor"]).group(1)
        self.assertGreaterEqual(version(floor), (0, 81, 0))


if __name__ == "__main__":
    unittest.main()
