"""review_passes: each lens reviews N times in independent sessions, and the
findings are merged before verify — one finding per issue, with the number of
passes that raised it. One pass (the default) is the 3.0.2 review, exactly."""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_calibration_script import Fixture, post  # noqa: E402
from test_manifest_js import eval_step, flow_step, load, run_js, run_js_mem  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT = os.path.join(HERE, "testdata", "passes1_snapshot.json")
MAN = load()


def step(sid):
    return flow_step(MAN, "review-team", sid)["code"]


def F(path, line, body, blocking=False, **kw):
    return dict({"path": path, "line": line, "blocking": blocking, "body": body, "confidence": "high"}, **kw)


def lens(name):
    return {"name": name, "focus": name + " things."}


def base_ctx(lenses, passes="", setting="1"):
    ctx = {"inputs": {"repo": "acme/api", "pr": 7, "lenses": lenses, "memory": "on", "prior_comments": "on",
                      "scope_key": "", "passes": passes},
           "notes": {"mode": "on", "limit": 40, "key": "acme/api", "count": 0, "text": ""},
           "files": {"files": []}, "prior": {"comments": []}}
    ctx["lenses"] = run_js(step("lenses"), ctx, settings={"review_passes": setting})
    return ctx


def review(ctx, items, same=()):
    """flatten -> mpairs -> mjudge (answers `same`, in mpairs.list order) -> merged."""
    ctx = copy.deepcopy(ctx)
    ctx["review"] = {"items": items}
    ctx["flatten"] = run_js(step("flatten"), ctx)
    ctx["mpairs"] = run_js(step("mpairs"), ctx)
    ctx["mjudge"] = {"items": [{"same_issue": {"noul": p}} for p in same]}
    ctx["merged"] = run_js(step("merged"), ctx)
    return ctx


def reconcile(ctx, refuted=()):
    ctx = copy.deepcopy(ctx)
    ctx["verify"] = {"items": [{"refuted": {"noul": 0.9 if f["path"] in refuted else 0.1}} for f in ctx["merged"]["value"]]}
    return run_js(step("reconcile"), ctx)


class OnePassIsTheOldReviewTest(unittest.TestCase):
    """The 3.0.2 steps' outputs on a fixed PR, recorded before this change
    (testdata/passes1_snapshot.json): one pass reproduces every one."""

    def setUp(self):
        self.snap = json.load(open(SNAPSHOT))

    def chain(self, settings):
        ctx = copy.deepcopy(self.snap["fixture"])
        ctx["inputs"].setdefault("passes", "")
        out = {}
        for sid in ("lenses", "flatten", "mpairs", "merged", "paths"):
            ctx[sid] = out[sid] = run_js(step(sid), ctx, settings=settings)
        ctx["windows"] = out["windows"] = run_js(step("windows"), ctx)
        ctx["reconcile"] = out["reconcile"] = run_js(step("reconcile"), ctx)
        return out

    def test_every_step_matches_the_snapshot(self):
        for settings in ({}, {"review_passes": "1"}, {"review_passes": " 1 "}):
            out = self.chain(settings)
            s = self.snap
            self.assertEqual(out["lenses"]["list"], s["lenses"]["list"])
            self.assertEqual(out["lenses"]["runs"], s["lenses"]["list"])        # the panel dispatches the same sessions
            self.assertEqual(out["flatten"], s["flatten"])
            self.assertEqual(out["mpairs"]["count"], 0)                         # no judge calls
            self.assertEqual(out["merged"]["value"], s["flatten"]["value"])
            self.assertEqual(out["paths"], s["paths"])
            self.assertEqual(out["windows"], s["windows"])
            for k in ("event", "summary", "comments", "review", "findings"):
                self.assertEqual(out["reconcile"][k], s["reconcile"][k], k)
            self.assertEqual(out["reconcile"]["passes"], 1)

    def test_the_lens_step_is_unchanged_but_for_what_it_iterates(self):
        now = dict(flow_step(load(), "review-team", "review"))
        then = dict(self.snap["review_step"])
        self.assertEqual((now.pop("for_each"), then.pop("for_each")), ("{{.lenses.runs}}", "{{.lenses.list}}"))
        # 3.1.1: a lens session (and its checkout) ends with its step
        now["extends"] = dict(now["extends"])
        self.assertIs(now["extends"].pop("archive_when_done"), True)
        self.assertNotIn("archive_when_done", then["extends"])
        # 3.2: a lens starts from the review's one shared snapshot of the PR
        # tree instead of checking the PR out itself
        self.assertEqual(now.pop("workspace"), {"from": "{{.snapshot.blob}}"})
        self.assertEqual((then.pop("checkout"), then.pop("checkout_pr")),
                         ("checkout-pr", {"repo": "{{.inputs.repo}}", "pr": "{{.inputs.pr}}"}))
        self.assertEqual(now, then)

    def test_one_pass_ignores_an_agreement_a_lens_made_up(self):
        ctx = base_ctx([lens("Security"), lens("Performance")])
        items = [{"findings": [F("a.go", 3, "first", agreement=0)]}, {"findings": [F("a.go", 3, "second", agreement=9)]}]
        out = reconcile(review(ctx, items))
        self.assertEqual([c["body"] for c in out["comments"]], ["first"])        # first come, as before
        self.assertNotIn("agreement", json.dumps(out["findings"]))


class DispatchTest(unittest.TestCase):

    def test_each_lens_runs_once_per_pass_in_its_own_session(self):
        lenses = [lens("Security"), lens("Performance")]
        for n in (2, 3):
            out = base_ctx(lenses, setting=str(n))["lenses"]
            self.assertEqual(out["passes"], n)
            self.assertEqual(out["list"], lenses)
            self.assertEqual(len(out["runs"]), 2 * n)
            self.assertEqual([(r["name"], r["pass"]) for r in out["runs"]],
                             [(l["name"], p) for l in lenses for p in range(1, n + 1)])
            for r in out["runs"]:                                               # the same lens, nothing else
                self.assertEqual({k: v for k, v in r.items() if k != "pass"}, [l for l in lenses if l["name"] == r["name"]][0])

    def test_the_passes_get_the_same_prompt_and_never_each_other(self):
        st = flow_step(load(), "review-team", "review")
        self.assertEqual(st["for_each"], "{{.lenses.runs}}")                    # one for_each item = one agent session
        self.assertTrue(st["parallel"])
        self.assertNotIn(".item.pass", json.dumps(st))                          # the prompt never names the pass
        self.assertNotIn(".review.", json.dumps(st))                            # nor reads another session's output
        # every pass starts from its own copy of the one snapshot (3.2)
        self.assertEqual((st["workspace"], st["memory"]), ({"from": "{{.snapshot.blob}}"}, False))

    def test_the_passes_input_overrides_the_setting(self):
        self.assertEqual(base_ctx([lens("Security")], passes="2", setting="1")["lenses"]["passes"], 2)
        self.assertEqual(base_ctx([lens("Security")], passes="", setting="3")["lenses"]["passes"], 3)

    def test_a_bad_value_fails_the_review_loudly(self):
        for bad in ("0", "4", "1.5", "two", "", "-1", "2 passes"):
            with self.assertRaises(AssertionError) as e:
                base_ctx([lens("Security")], setting=bad)
            self.assertIn("the review_passes setting must be a whole number from 1 to 3", str(e.exception))
        with self.assertRaises(AssertionError) as e:
            base_ctx([lens("Security")], passes="5", setting="1")
        self.assertIn("the passes input must be a whole number from 1 to 3", str(e.exception))

    def test_promoted_perspective_lenses_run_every_pass_too(self):
        store = [{"id": "l0", "scope": "perspective:acme/api", "text": "w0 looks for: idempotent migrations",
                  "tags": ["perspective", "who:w0", "lens", "quarantined", "held-0000000a"]}]
        code = step("lenses").replace("${settings.perspectives_as_lenses}", "true").replace("${settings.review_passes}", "2")
        ctx = {"inputs": {"lenses": [lens("Security")], "passes": ""}, "notes": {"mode": "on", "limit": 40, "key": "acme/api"}}
        out, _ = run_js_mem(code, ctx, store)
        self.assertEqual([(r["name"], r["pass"]) for r in out["runs"]],
                         [("Security", 1), ("Security", 2), ("Perspective: w0", 1), ("Perspective: w0", 2)])


def two_pass(*lenses_names):
    return base_ctx([lens(n) for n in lenses_names], setting="2")


class MergeTest(unittest.TestCase):

    def test_the_same_finding_from_both_passes_is_one_with_agreement_2_of_2(self):
        ctx = review(two_pass("Security"), [{"findings": [F("a.go", 10, "Return the error.")]},
                                            {"findings": [F("a.go", 11, "return the  error.")]}])
        self.assertEqual(ctx["mpairs"]["count"], 0)                             # identical text: no judge call
        [m] = ctx["merged"]["value"]
        self.assertEqual((m["agreement"], m["passes"], m["raised_by"], m["lenses"], m["also_at"]), (2, 2, 2, ["Security"], ["a.go:11"]))
        self.assertEqual((m["line"], m["body"]), (10, "Return the error."))      # the first, unchanged

    def test_the_judge_decides_reworded_findings(self):
        items = [{"findings": [F("a.go", 40, "This loop queries once per row.")]}, {"findings": [F("a.go", 43, "N+1: one query per row.")]}]
        ctx = review(two_pass("Performance"), items)
        self.assertEqual(ctx["mpairs"]["count"], 1)
        self.assertEqual(len(review(two_pass("Performance"), items, [0.9])["merged"]["value"]), 1)
        apart = review(two_pass("Performance"), items, [0.2])["merged"]["value"]
        self.assertEqual([m["agreement"] for m in apart], [1, 1])

    def test_lenses_dedupe_too_and_agreement_counts_passes_not_lenses(self):
        # runs: Security p1, Security p2, Architecture p1, Architecture p2
        items = [{"findings": [F("a.go", 5, "Validate the path.")]}, {"findings": []},
                 {"findings": [F("a.go", 5, "Validate the path.")]}, {"findings": []}]
        [m] = review(two_pass("Security", "Architecture"), items)["merged"]["value"]
        self.assertEqual((m["agreement"], m["raised_by"], m["lenses"]), (1, 2, ["Security", "Architecture"]))

    def test_the_same_bug_in_two_related_files_is_one_finding(self):
        a = F("api/invoice.go", 12, "Missing tenant check: getInvoice returns any tenant's invoice when the id is guessed.", True)
        b = F("api/invoice_list.go", 40, "Missing tenant check: listInvoices lets a caller read another tenant's invoices.", True)
        ctx = review(two_pass("Security"), [{"findings": [a]}, {"findings": [b]}])
        self.assertEqual(ctx["mpairs"]["count"], 1)
        self.assertEqual((ctx["mpairs"]["list"][0]["base_path"], ctx["mpairs"]["list"][0]["mem_path"]),
                         ("api/invoice.go", "api/invoice_list.go"))
        [m] = review(two_pass("Security"), [{"findings": [a]}, {"findings": [b]}], [0.9])["merged"]["value"]
        self.assertEqual((m["path"], m["agreement"], m["also_at"]), ("api/invoice.go", 2, ["api/invoice_list.go:40"]))
        # unrelated text in another file is never even a candidate
        c = F("web/app.ts", 3, "Debounce the search box.")
        self.assertEqual(review(two_pass("Security"), [{"findings": [a]}, {"findings": [c]}])["mpairs"]["count"], 0)

    def test_other_file_judge_calls_are_capped_in_the_merge_too(self):
        text = "The retry loop never sleeps between attempts number %d."
        items = [{"findings": [F("a.go", 1, text % 0)]}, {"findings": [F("f%d.go" % i, 1, text % i) for i in range(1, 7)]}]
        self.assertEqual(review(two_pass("Security"), items)["mpairs"]["count"], 3)

    def test_matching_is_one_to_one_between_two_sessions(self):
        items = [{"findings": [F("a.go", 10, "x one"), F("a.go", 12, "x two")]}, {"findings": [F("a.go", 11, "y")]}]
        ctx = review(two_pass("Security"), items, [0.6, 0.95])
        out = ctx["merged"]["value"]
        self.assertEqual(len(out), 2)
        merged = [m for m in out if m["raised_by"] == 2]
        self.assertEqual([m["body"] for m in merged], ["x two"])                 # the likelier pair

    def test_a_session_s_own_findings_never_merge_through_a_chain(self):
        ctx = base_ctx([lens("Security")], setting="3")
        items = [{"findings": [F("a.go", 10, "p one"), F("a.go", 14, "p two")]},   # pass 1: two findings
                 {"findings": [F("a.go", 12, "q")]}, {"findings": [F("a.go", 13, "r")]}]
        ctx = review(ctx, items)
        n = ctx["mpairs"]["count"]
        out = review(base_ctx([lens("Security")], setting="3"), items, [0.9] * n)["merged"]["value"]
        for m in out:
            self.assertLessEqual(m["raised_by"], 3)
        self.assertEqual(sorted(m["raised_by"] for m in out), [1, 3])           # p one/two stay apart

    def test_a_blocking_finding_represents_its_group(self):
        items = [{"findings": [F("a.go", 10, "Return the error.")]}, {"findings": [F("a.go", 10, "Return the error.", True)]}]
        [m] = review(two_pass("Security"), items)["merged"]["value"]
        self.assertTrue(m["blocking"])

    def test_a_finding_one_pass_raised_is_kept(self):
        items = [{"findings": [F("a.go", 10, "Return the error."), F("b.go", 3, "Only pass one saw this.")]},
                 {"findings": [F("a.go", 10, "Return the error.")]}]
        ctx = review(two_pass("Security"), items)
        out = reconcile(ctx)
        self.assertEqual(sorted(c["body"] for c in out["comments"]), ["Only pass one saw this.", "Return the error."])
        lone = [f for f in out["findings"] if f["path"] == "b.go"][0]
        self.assertEqual((lone["agreement"], lone["passes"]), (1, 2))

    def test_agreement_breaks_a_tie_and_never_reaches_the_review_body(self):
        # two findings on one line that the judge calls different: the one more passes raised is posted
        items = [{"findings": [F("a.go", 10, "Lone concern.")]},
                 {"findings": [F("a.go", 10, "Shared concern.")]},
                 {"findings": [F("a.go", 10, "Shared concern.")]}]
        ctx = review(base_ctx([lens("Security")], setting="3"), items, [0.1, 0.1])
        out = reconcile(ctx)
        self.assertEqual([c["body"] for c in out["comments"]], ["Shared concern."])
        self.assertEqual(out["findings"][0]["agreement"], 2)
        review_json = json.loads(out["review"])
        self.assertEqual([c["body"] for c in review_json["comments"]], ["Shared concern."])
        self.assertNotIn("agreement", out["review"])
        self.assertNotIn("2/3", out["review"] + out["summary"])
        self.assertEqual(out["passes"], 3)

    def test_blocking_still_outranks_agreement(self):
        items = [{"findings": [F("a.go", 10, "Lone blocker.", True)]},
                 {"findings": [F("a.go", 10, "Shared nit.")]}, {"findings": [F("a.go", 10, "Shared nit.")]}]
        out = reconcile(review(base_ctx([lens("Security")], setting="3"), items, [0.1, 0.1]))
        self.assertEqual([c["body"] for c in out["comments"]], ["Lone blocker."])

    def test_verify_runs_on_the_merged_set(self):
        items = [{"findings": [F("a.go", 10, "Return the error."), F("c.go", 1, "Wrong claim.")]}, {"findings": [F("a.go", 10, "Return the error.")]}]
        ctx = review(two_pass("Security"), items)
        self.assertEqual(len(ctx["merged"]["value"]), 2)
        ctx["paths"] = run_js(step("paths"), ctx)
        ctx["headfiles"] = {"items": [{"text": "x"} for _ in ctx["paths"]["value"]]}
        self.assertEqual(len(run_js(step("windows"), ctx)["value"]), 2)
        out = reconcile(ctx, refuted=("c.go",))
        self.assertEqual([c["path"] for c in out["comments"]], ["a.go"])


class MatcherTest(unittest.TestCase):

    def block(self, code):
        return code[code.index("// THE MATCHER"):code.index("// END MATCHER")]

    def test_the_merge_and_review_eval_share_one_matcher_and_one_judge(self):
        self.assertEqual(self.block(step("mpairs")), self.block(eval_step("pairs")["code"]))
        self.assertEqual(flow_step(load(), "review-team", "mjudge")["decide"], eval_step("judge")["decide"])
        q = eval_step("judge")["decide"]["questions"]["same_issue"]["instructions"]
        self.assertIn("two places in related files", q)

    def eval_pairs(self, a, b):
        runs = [{"repo": "acme/api", "pr": 1, "memory": "off", "arm": "A"}, {"repo": "acme/api", "pr": 1, "memory": "on", "arm": "M"}]
        return run_js(eval_step("pairs")["code"], {"pick": {"runs": runs}, "replay": {"items": [{"findings": a}, {"findings": b}]}})

    def test_review_eval_now_matches_the_same_bug_across_files(self):
        a = [F("api/invoice.go", 12, "Missing tenant check: getInvoice returns any tenant's invoice when the id is guessed.")]
        b = [F("api/invoice_list.go", 40, "Missing tenant check: listInvoices lets a caller read another tenant's invoices.")]
        pairs = self.eval_pairs(a, b)
        self.assertEqual(pairs["count"], 1)
        self.assertEqual((pairs["list"][0]["base_path"], pairs["list"][0]["mem_path"]), ("api/invoice.go", "api/invoice_list.go"))
        judge = {"items": [{"same_issue": {"noul": 0.9}}]}
        out = run_js(eval_step("compare")["code"], {"pairs": pairs, "judge": judge})
        self.assertEqual(out["prs"][0]["retention"], 1)

    def test_closeness_needs_real_shared_words(self):
        cases = (("Add a test.", "Add a test."),                                              # too few words
                 ("Return the error instead of logging it.", "Validate the input path."),
                 ("Debounce the search box on every keypress.", "Cache the search results per tenant."))
        for x, y in cases:
            self.assertEqual(self.eval_pairs([F("a.go", 1, x)], [F("b.go", 1, y)])["count"], 0, (x, y))
        # three shared words are not enough when they are a small part of long texts
        self.assertEqual(self.eval_pairs(
            [F("a.go", 1, "The retry loop in the worker never sleeps between attempts and hammers the queue.")],
            [F("b.go", 1, "Cache the tenant lookup in the worker; this loop repeats the same query on every attempt.")])["count"], 0)
        self.assertEqual(self.eval_pairs([F("a.go", 1, "The retry loop never sleeps between attempts.")],
                                         [F("b.go", 9, "This retry loop has no sleep between attempts.")])["count"], 1)

    def test_same_file_far_apart_is_still_not_a_pair(self):
        self.assertEqual(self.eval_pairs([F("a.go", 1, "Validate the input path here.")], [F("a.go", 30, "Validate the input path here.")])["count"], 0)

    def test_a_near_same_file_match_beats_one_across_files(self):
        a = [F("a.go", 10, "The retry loop never sleeps between attempts.")]
        b = [F("b.go", 10, "The retry loop never sleeps between attempts!"), F("a.go", 12, "Retry loop never sleeps between attempts.")]
        pairs = self.eval_pairs(a, b)
        judge = {"items": [{"same_issue": {"noul": 0.9}} for _ in pairs["list"]]}
        out = run_js(eval_step("compare")["code"], {"pairs": pairs, "judge": judge})
        self.assertEqual([r["memory"] for r in out["prs"][0]["retained"]], ["a.go:12"])

    def test_nearer_lines_break_a_tie(self):
        a = [F("a.go", 10, "x")]
        b = [F("a.go", 14, "y one"), F("a.go", 11, "y two")]
        pairs = self.eval_pairs(a, b)
        judge = {"items": [{"same_issue": {"noul": 0.9}} for _ in pairs["list"]]}
        out = run_js(eval_step("compare")["code"], {"pairs": pairs, "judge": judge})
        self.assertEqual([r["memory"] for r in out["prs"][0]["retained"]], ["a.go:11"])

    def test_other_file_judge_calls_are_capped_per_finding(self):
        text = "The retry loop never sleeps between attempts number %d."
        a = [F("a.go", 1, text % 0)]
        b = [F("f%d.go" % i, 1, text % i) for i in range(1, 7)]
        self.assertEqual(self.eval_pairs(a, b)["count"], 3)


class EvalPassesTest(unittest.TestCase):

    def setUp(self):
        self.f = Fixture()
        import datetime as dt
        self.f.audit(post(self.f.now - dt.timedelta(days=1), "acme/api", 4))

    def tearDown(self):
        self.f.close()

    def pick(self, env=None, **inputs):
        return self.f.run("eval-pick", stdin=json.dumps({"inputs": inputs}), **(env or {}))

    def test_passes_default_to_the_setting_and_go_to_every_arm(self):
        self.assertEqual(json.loads(self.pick(repos="acme/api").stdout)["passes"], "1")
        self.assertEqual(json.loads(self.pick({"REVIEW_PASSES": "2"}, repos="acme/api").stdout)["passes"], "2")
        self.assertEqual(json.loads(self.pick({"REVIEW_PASSES": "1"}, repos="acme/api", passes="2").stdout)["passes"], "2")
        replay = eval_step("replay")
        self.assertEqual(replay["with"]["passes"], "{{.pick.passes}}")          # A, B and M alike
        self.assertIn("REVIEW_PASSES", json.dumps(eval_step("pick")["env"]))

    def test_bad_passes_fail_plainly(self):
        for env, inputs, says in (({}, {"passes": "4"}, "--input passes= takes a whole number from 1 to 3"),
                                  ({}, {"passes": "x"}, "--input passes="),
                                  ({"REVIEW_PASSES": "9"}, {}, "the review_passes setting must be a whole number from 1 to 3")):
            res = self.pick(env, repos="acme/api", **inputs)
            self.assertNotEqual(res.returncode, 0, (env, inputs))
            self.assertIn(says, res.stderr)

    def test_the_report_names_the_pass_count(self):
        cmp = {"totals": {"prs": 1, "baseline": 1, "with_memory": 1, "retained": 1, "retention": 1, "verdict": "ok"},
               "prs": [{"ref": "acme/api#4", "baseline": 1, "with_memory": 1, "retention": 1, "retained": [], "dropped": [], "added": []}]}
        two = json.loads(self.f.run("eval-report", stdin=json.dumps({"compare": cmp, "pick": {"passes": "2"}})).stdout)
        self.assertTrue(two["summary"].endswith("[passes 2]"))
        one = json.loads(self.f.run("eval-report", stdin=json.dumps({"compare": cmp, "pick": {"passes": "1"}})).stdout)
        self.assertNotIn("passes", one["summary"])


if __name__ == "__main__":
    unittest.main()
