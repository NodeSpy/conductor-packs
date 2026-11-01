"""The pack's deterministic js steps, run under node exactly as written in the
manifest: the calibration write restriction (`plan`), and the auto/gated
post-vs-hand-off decision (`assessed`, `gate`).

    python3 -m unittest discover -s pr-review-team/tests

Needs node and PyYAML.
"""
import json
import os
import subprocess
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "..", "conductor-pack.yaml")


def load():
    with open(MANIFEST) as f:
        return yaml.safe_load(f)


def flow_step(man, workflow, step_id):
    for s in man["workflows"][workflow]["steps"]:
        if s.get("id") == step_id:
            return s
    raise KeyError(step_id)


def run_js(code, ctx, settings=None):
    for k, v in (settings or {}).items():
        code = code.replace("${settings.%s}" % k, v)
    src = "const ctx = %s;\nconst out = (function () {\n%s\n})();\nprocess.stdout.write(JSON.stringify(out));" % (
        json.dumps(ctx), code)
    res = subprocess.run(["node", "-e", src], capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(res.stderr)
    return json.loads(res.stdout)


def entry(repo, kind, text, evidence, **kw):
    e = {"repo": repo, "kind": kind, "text": text, "evidence": evidence}
    e.update(kw)
    return e


class PlanTest(unittest.TestCase):
    """The write restriction: scope comes from kind + repo, and only for repos
    this run collected evidence for."""

    @classmethod
    def setUpClass(cls):
        cls.code = load()["x-templates"]["plan"]["code"]

    def plan(self, entries, repos=("acme/api",), known=(), via="calibrate"):
        ctx = {"collect": {"repos": list(repos)}, "known": {"list": list(known)}, via: {"entries": entries}}
        return run_js(self.code, ctx)

    def test_each_kind_lands_in_its_own_scope(self):
        out = self.plan([
            entry("acme/api", "fact", "the worker is deployed by a cron job (acme/api#4)", ["acme/api#4"]),
            entry("acme/api", "standard", "handlers return typed errors (acme/api#5)", ["acme/api#5"]),
            entry("acme/api", "emphasis", "a PR that touches billing/ needs close review (acme/api#6)", ["acme/api#6"]),
            entry("acme/api", "global", "a swallowed error is blocking (acme/api#7)", ["acme/api#7"]),
        ])
        self.assertEqual([(w["scope"], w["label"]) for w in out["writes"]], [
            ("acme/api", "acme/api"),
            ("standard:acme/api", "standard:acme/api"),
            ("emphasis:acme/api", "emphasis:acme/api"),
            ("", "global"),          # the shared set: a config step writes it with an empty scope
        ])
        self.assertEqual(out["count"], 4)
        self.assertEqual(out["rejected"], [])
        self.assertEqual(out["writes"][2]["tags"], ["review-tuning", "calibration", "emphasis"])
        self.assertEqual(out["writes"][3]["tags"], ["review-tuning", "calibration", "standard"])

    def test_a_repo_outside_the_run_is_rejected(self):
        out = self.plan([entry("other/svc", "fact", "x", ["other/svc#1"])])
        self.assertEqual(out["writes"], [])
        self.assertIn("not a repo this run collected evidence for", out["rejected"][0])

    def test_evidence_must_stay_inside_the_run(self):
        out = self.plan([
            entry("acme/api", "fact", "cites a foreign PR", ["other/svc#1"]),
            entry("acme/api", "global", "global, cites a foreign PR", ["acme/api#2", "other/svc#1"]),
            entry("acme/api", "standard", "no evidence", []),
            entry("acme/api", "fact", "bad ref", ["acme/api"]),
        ])
        self.assertEqual(out["writes"], [])
        self.assertEqual(len(out["rejected"]), 4)

    def test_a_per_repo_entry_must_cite_its_own_repo(self):
        out = self.plan([entry("acme/api", "fact", "evidence is the other in-run repo", ["acme/web#3"])],
                        repos=("acme/api", "acme/web"))
        self.assertEqual(out["writes"], [])
        self.assertIn("does not cite acme/api", out["rejected"][0])

    def test_the_agent_cannot_name_a_scope(self):
        out = self.plan([entry("acme/api", "routing", "x", ["acme/api#1"]),
                         entry("acme/api", "fact", "x", ["acme/api#1"], scope="global")])
        self.assertEqual(len(out["writes"]), 1)
        self.assertEqual(out["writes"][0]["scope"], "acme/api")
        self.assertIn("unknown kind", out["rejected"][0])

    def test_none_is_dropped_and_logged_not_written(self):
        out = self.plan([entry("acme/api", "none", "planned follow-up, not a miss", [])])
        self.assertEqual(out["writes"], [])
        self.assertEqual(out["dropped"], ["acme/api: planned follow-up, not a miss"])

    def test_empty_and_oversized_text_rejected(self):
        out = self.plan([entry("acme/api", "fact", "  ", ["acme/api#1"]),
                         entry("acme/api", "fact", "x" * 801, ["acme/api#1"])])
        self.assertEqual(out["writes"], [])
        self.assertEqual(len(out["rejected"]), 2)

    def test_an_already_recorded_entry_is_not_written_twice(self):
        known = [{"id": "m1", "scope": "acme/api", "text": "same text"}]
        out = self.plan([entry("acme/api", "fact", "same text", ["acme/api#1"])], known=known)
        self.assertEqual(out["writes"], [])
        self.assertIn("already recorded", out["dropped"][0])

    def test_supersedes_only_within_the_scope_written(self):
        known = [{"id": "m1", "scope": "acme/api", "text": "old fact"},
                 {"id": "m2", "scope": "global", "text": "old standard"}]
        out = self.plan([
            entry("acme/api", "fact", "sharper fact", ["acme/api#1"], supersedes="m1"),
            entry("acme/api", "fact", "tries to retire a global entry", ["acme/api#1"], supersedes="m2"),
            entry("acme/api", "fact", "names an unknown id", ["acme/api#1"], supersedes="m9"),
        ], known=known)
        self.assertEqual(out["forget"], ["m1"])
        self.assertEqual(len(out["writes"]), 3)
        self.assertEqual(len([r for r in out["rejected"] if r.startswith("supersedes")]), 2)

    def test_team_tag_and_backfill_origin(self):
        out = self.plan([entry("acme/api", "fact", "the team flags N+1 queries (acme/api#2)", ["acme/api#2"], team=True)],
                        via="merge")
        self.assertEqual(out["writes"][0]["tags"], ["review-tuning", "backfill", "fact", "team"])


class GateTest(unittest.TestCase):
    """auto=true never hands off; auto=false is the 2.x gate."""

    @classmethod
    def setUpClass(cls):
        man = load()
        cls.gate = flow_step(man, "review-flow", "gate")["code"]
        cls.assessed = flow_step(man, "review-flow", "assessed")["code"]

    def decide(self, auto, decision, comments):
        s = {"auto": auto}
        assess = {"decision": {"choice": decision}, "tests_sufficient": {"noul": 0.9},
                  "docs_sufficient": {"noul": 0.9}, "glaring_holes": {"noul": 0.1}}
        assessed = run_js(self.assessed, {"assess": assess}, s)
        ctx = {"assessed": assessed, "review": {"comments": [{"path": "a.go", "line": 1}] * comments}}
        return assessed, run_js(self.gate, ctx, s)

    def test_auto_posts_even_when_assess_says_manual(self):
        assessed, g = self.decide("true", "manual", 2)
        self.assertEqual(assessed["decision"], "auto")
        self.assertEqual(g, {"mode": "auto", "post": True, "handoff": False})

    def test_the_gate_itself_never_hands_off_in_auto(self):
        # even if an overridden assess/assessed step says manual
        ctx = {"assessed": {"decision": "manual"}, "review": {"comments": [{"path": "a.go", "line": 1}]}}
        self.assertEqual(run_js(self.gate, ctx, {"auto": "true"}), {"mode": "auto", "post": True, "handoff": False})
        self.assertEqual(run_js(self.gate, ctx, {"auto": "false"}), {"mode": "gated", "post": False, "handoff": True})

    def test_auto_posts_a_clean_review(self):
        _, g = self.decide("true", "auto", 0)
        self.assertEqual(g, {"mode": "auto", "post": True, "handoff": False})

    def test_gated_manual_with_comments_hands_off(self):
        assessed, g = self.decide("false", "manual", 2)
        self.assertEqual(assessed["decision"], "manual")
        self.assertEqual(g, {"mode": "gated", "post": False, "handoff": True})

    def test_gated_manual_clean_review_still_posts(self):
        _, g = self.decide("false", "manual", 0)
        self.assertEqual(g, {"mode": "gated", "post": True, "handoff": False})

    def test_gated_auto_posts(self):
        _, g = self.decide("false", "auto", 3)
        self.assertEqual(g, {"mode": "gated", "post": True, "handoff": False})

    def test_gated_reason_names_emphasis(self):
        assess = {"decision": {"choice": "manual"}, "tests_sufficient": {"noul": 0.9},
                  "docs_sufficient": {"noul": 0.9}, "glaring_holes": {"noul": 0.1}}
        out = run_js(self.assessed, {"assess": assess, "emphasis": {"memories": [{"text": "r"}]}}, {"auto": "false"})
        self.assertIn("emphasis rule", out["reason"])

    def test_assess_and_emphasis_are_skipped_in_auto(self):
        man = load()
        for sid in ("emphasis", "assess"):
            self.assertEqual(flow_step(man, "review-flow", sid)["if"], "!${settings.auto}")


class LayeringTest(unittest.TestCase):
    def test_lens_panel_reads_the_repo_layers(self):
        review = flow_step(load(), "review-team", "review")
        self.assertEqual(review["memory"]["extra_scopes"], ["standard:${repo}", "emphasis:${repo}"])


if __name__ == "__main__":
    unittest.main()
