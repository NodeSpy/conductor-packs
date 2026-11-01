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


FAKE_MEMORY = """
const store = %s;
let next = 1;
ctx.memory = {
  recall: function (o) { return store.filter(function (m) { return !o || o.scope == null || (m.scope || "") === o.scope; })
                                      .map(function (m) { return Object.assign({}, m); }); },
  remember: function (text, tags, scope) { const m = { id: "n" + (next++), text: text, tags: tags || [], scope: scope || "" };
                                           store.push(m); return m; },
  forget: function (id) { const i = store.findIndex(function (m) { return m.id === id; });
                          if (i < 0) return false; store.splice(i, 1); return true; },
};
"""


def run_js_mem(code, ctx, store):
    """Run a step with an in-memory ctx.memory; returns (output, the store afterwards)."""
    src = ("const ctx = %s;\n" % json.dumps(ctx) + FAKE_MEMORY % json.dumps(store) +
           "const out = (function () {\n%s\n})();\nprocess.stdout.write(JSON.stringify({out: out, store: store}));" % code)
    res = subprocess.run(["node", "-e", src], capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(res.stderr)
    r = json.loads(res.stdout)
    return r["out"], r["store"]


def entry(key, kind, text, evidence, **kw):
    e = {"key": key, "kind": kind, "text": text, "evidence": evidence}
    e.update(kw)
    return e


def pr_facts(ref, **meta):
    """A PR's facts as collect emits them (scripts/calibration.py pr_facts)."""
    repo, n = ref.split("#")
    owner, name = repo.split("/")
    f = {"repo": repo, "owner": owner, "name": name, "number": int(n), "pr": int(n), "issue": 0,
         "kind": "review_requested", "inputs": {"repo": repo, "pr": int(n)}}
    f.update(meta)
    return f


def scopekeys(template, ctx):
    return run_js(load()["x-templates"]["scopekeys"]["code"], ctx, {"scope_key": template})


def keys_for(refs, template="{{.repo}}"):
    """The `keys` step's output for these PRs under a scope_key template."""
    return scopekeys(template, {"collect": {"prs": [{"ref": r, "facts": pr_facts(r)} for r in refs]}})


RUN = ("acme/api#1", "acme/api#2", "acme/api#3", "acme/api#4", "acme/api#5", "acme/api#6", "acme/api#7", "acme/api#9",
       "acme/web#1", "acme/web#2", "acme/web#3", "other/svc#3")


class PlanTest(unittest.TestCase):
    """The write restriction: the scope comes from kind + key, and a key's
    entries must rest on PRs that map to that key. With the default scope_key
    ({{.repo}}) a key is a repo — the 3.0 behavior."""

    @classmethod
    def setUpClass(cls):
        cls.code = load()["x-templates"]["plan"]["code"]
        cls.repo_keys = keys_for(RUN)

    def plan(self, entries, keys=None, known=(), via="calibrate"):
        ctx = {"keys": keys or self.repo_keys, "known": {"list": list(known)}, via: {"entries": entries}}
        return run_js(self.code, ctx)

    def test_each_kind_lands_in_its_own_scope(self):
        out = self.plan([
            entry("acme/api", "fact", "the worker is deployed by a cron job (acme/api#4)", ["acme/api#4"]),
            entry("acme/api", "standard", "handlers return typed errors (acme/api#5)", ["acme/api#5"]),
            entry("acme/api", "emphasis", "a PR that touches billing/ needs close review (acme/api#6)", ["acme/api#6"]),
            entry("acme/api", "global", "a swallowed error is blocking (acme/api#7, acme/web#2)",
                  ["acme/api#7", "acme/web#2"]),
        ])
        self.assertEqual([(w["scope"], w["label"]) for w in out["writes"]], [
            ("acme/api", "acme/api"),
            ("standard:acme/api", "standard:acme/api"),
            ("emphasis:acme/api", "emphasis:acme/api"),
            ("", "global"),          # the shared set: a config step writes it with an empty scope
        ])
        self.assertEqual(out["count"], 4)
        self.assertEqual(out["rejected"], [])
        self.assertEqual(out["downgraded"], [])
        self.assertEqual(out["writes"][2]["tags"], ["review-tuning", "calibration", "emphasis"])
        self.assertEqual(out["writes"][3]["tags"], ["review-tuning", "calibration", "standard"])

    def test_a_global_resting_on_one_key_stays_in_that_key(self):
        out = self.plan([entry("acme/api", "global", "a swallowed error is blocking (acme/api#7)", ["acme/api#7"])])
        self.assertEqual([(w["scope"], w["label"], w["tags"]) for w in out["writes"]],
                         [("standard:acme/api", "standard:acme/api", ["review-tuning", "calibration", "standard"])])
        self.assertEqual(len(out["downgraded"]), 1)
        self.assertIn("global -> standard:acme/api", out["downgraded"][0])
        self.assertEqual(out["rejected"], [])

    def test_several_prs_of_one_key_are_still_one_key(self):
        out = self.plan([entry("acme/api", "global", "x", ["acme/api#1", "acme/api#2", "acme/api#3"])])
        self.assertEqual([w["scope"] for w in out["writes"]], ["standard:acme/api"])
        self.assertEqual(len(out["downgraded"]), 1)

    def test_a_downgrade_lands_in_the_cited_key_not_the_named_one(self):
        out = self.plan([entry("acme/web", "global", "x", ["acme/api#1"])])
        self.assertEqual([w["scope"] for w in out["writes"]], ["standard:acme/api"])

    def test_two_distinct_keys_make_a_global(self):
        out = self.plan([entry("acme/web", "global", "x", ["acme/web#1", "acme/api#9"])])
        self.assertEqual([w["scope"] for w in out["writes"]], [""])
        self.assertEqual(out["downgraded"], [])

    def test_a_downgraded_global_supersedes_only_within_its_key(self):
        known = [{"id": "m1", "scope": "global", "text": "old"}, {"id": "m2", "scope": "standard:acme/api", "text": "old"}]
        out = self.plan([entry("acme/api", "global", "a", ["acme/api#1"], supersedes="m1"),
                         entry("acme/api", "global", "b", ["acme/api#1"], supersedes="m2")], known=known)
        self.assertEqual(out["forget"], ["m2"])

    def test_a_key_outside_the_run_is_rejected(self):
        out = self.plan([entry("other/elsewhere", "fact", "x", ["other/svc#3"])])
        self.assertEqual(out["writes"], [])
        self.assertIn("not a key this run collected evidence for", out["rejected"][0])

    def test_evidence_must_be_prs_this_run_collected(self):
        out = self.plan([
            entry("acme/api", "fact", "cites a PR the run never showed", ["acme/api#99"]),
            entry("acme/api", "global", "global, cites a foreign PR", ["acme/api#2", "elsewhere/x#1"]),
            entry("acme/api", "standard", "no evidence", []),
            entry("acme/api", "fact", "bad ref", ["acme/api"]),
        ])
        self.assertEqual(out["writes"], [])
        self.assertEqual(len(out["rejected"]), 4)
        self.assertIn("acme/api#99 is not a PR this run collected", out["rejected"][0])

    def test_every_evidence_pr_must_map_to_the_entrys_key(self):
        out = self.plan([entry("acme/api", "fact", "one PR of another key", ["acme/api#1", "acme/web#3"]),
                         entry("acme/api", "fact", "only another key", ["acme/web#3"])])
        self.assertEqual(out["writes"], [])
        self.assertIn("evidence must all map to acme/api (also cites acme/web)", out["rejected"][0])
        self.assertEqual(len(out["rejected"]), 2)

    def test_refs_are_normalized(self):
        out = self.plan([entry("acme/api", "fact", "x", [" acme/api#01 "])])
        self.assertEqual([w["scope"] for w in out["writes"]], ["acme/api"])

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


class OwnerKeyTest(unittest.TestCase):
    """scope_key = org:{{.owner}}: an owner's repos share one layer; another owner's don't."""

    @classmethod
    def setUpClass(cls):
        cls.plan_code = load()["x-templates"]["plan"]["code"]
        cls.keys = keys_for(RUN, "org:{{.owner}}")

    def plan(self, entries):
        return run_js(self.plan_code, {"keys": self.keys, "known": {"list": []}, "calibrate": {"entries": entries}})

    def test_prs_of_one_owner_map_to_one_key(self):
        k = self.keys
        self.assertEqual(k["key_list"], ["org:acme", "org:other"])
        self.assertEqual(k["keys"]["acme/api#1"], "org:acme")
        self.assertEqual(k["keys"]["acme/web#2"], "org:acme")
        self.assertEqual(k["keys"]["other/svc#3"], "org:other")
        self.assertIn("- org:acme: acme/api#1", k["text"])

    def test_two_repos_of_one_owner_share_a_layer(self):
        out = self.plan([entry("org:acme", "standard", "handlers return typed errors", ["acme/api#1", "acme/web#2"])])
        self.assertEqual([w["scope"] for w in out["writes"]], ["standard:org:acme"])

    def test_a_different_owner_cannot_be_written_from_this_evidence(self):
        out = self.plan([entry("org:other", "fact", "x", ["acme/api#1"]),
                         entry("org:acme", "fact", "y", ["acme/api#1", "other/svc#3"])])
        self.assertEqual(out["writes"], [])
        self.assertEqual(len(out["rejected"]), 2)

    def test_global_needs_two_distinct_keys_not_two_repos(self):
        two_repos_one_key = self.plan([entry("org:acme", "global", "x", ["acme/api#1", "acme/web#2"])])
        self.assertEqual([w["scope"] for w in two_repos_one_key["writes"]], ["standard:org:acme"])
        self.assertEqual(len(two_repos_one_key["downgraded"]), 1)
        two_keys = self.plan([entry("org:acme", "global", "x", ["acme/api#1", "other/svc#3"])])
        self.assertEqual([w["scope"] for w in two_keys["writes"]], [""])

    def test_the_lens_panel_reads_the_same_layers(self):
        review = flow_step(load(), "review-team", "review")
        self.assertEqual(review["memory"]["extra_scopes"],
                         ["${settings.scope_key}", "standard:${settings.scope_key}", "emphasis:${settings.scope_key}"])


class ScopeKeyTest(unittest.TestCase):
    """The pack's js port of core's scope-key renderer, and the scope_key rules."""

    @classmethod
    def setUpClass(cls):
        cls.code = load()["x-templates"]["scopekeys"]["code"]

    def vectors(self, name):
        with open(os.path.join(HERE, "testdata", name)) as f:
            v = json.load(f)
        # render every case through the step's renderer, in one node run
        src = self.code[self.code.index("// ---- the scope-key renderer"):self.code.index("// ---- end of the renderer ----")]
        prog = "const ctx = {};\n" + src + "\nconst v = %s;\nprocess.stdout.write(JSON.stringify(v.cases.map(function (c) { return renderKey(c.template, v.facts); })));" % json.dumps(v)
        res = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        got = json.loads(res.stdout)
        for c, g in zip(v["cases"], got):
            self.assertEqual(g, None if c.get("drop") else c["want"], c["template"])
        return len(got)

    def test_cores_vectors(self):
        self.assertEqual(self.vectors("scope_key_vectors.json"), 37)

    def test_extra_cases_rendered_by_core(self):
        self.assertEqual(self.vectors("scope_key_vectors_extra.json"), 48)

    def test_the_aliases_pass_through_the_setting(self):
        ctx = {"inputs": {"repo": "acme/api", "pr": 7}}
        self.assertEqual(scopekeys("standard:${repo}", ctx)["key"], "standard:acme/api")

    def test_the_default_key_is_the_repo(self):
        man = load()
        self.assertEqual(man["settings"]["scope_key"]["default"], "{{.repo}}")
        self.assertEqual(scopekeys("{{.repo}}", {"inputs": {"repo": "acme/api", "pr": 7}}), {"key": "acme/api", "applies": True})

    def test_a_review_renders_over_its_pr_metadata(self):
        ctx = {"inputs": {"repo": "acme/api", "pr": 7},
               "prmeta": {"title": "t", "labels": [{"name": "payments"}, "x"], "base": "release-2", "url": "u"}}
        self.assertEqual(scopekeys("area:{{index .labels 0}}", ctx)["key"], "area:payments")
        self.assertEqual(scopekeys("{{.repo}}@{{.base}}", ctx)["key"], "acme/api@release-2")
        self.assertEqual(scopekeys("area:{{index .labels 0}}", {"inputs": {"repo": "acme/api", "pr": 7}, "prmeta": {"labels": []}}),
                         {"key": "", "applies": False})   # no label: the key doesn't apply, nothing is read

    def test_emphasis_reads_this_reviews_key(self):
        code = flow_step(load(), "review-flow", "emphasis")["code"]
        store = [{"id": "e1", "scope": "emphasis:org:acme", "text": "billing needs a hard look", "tags": []},
                 {"id": "e2", "scope": "emphasis:acme/api", "text": "the repo's own rule", "tags": []},
                 {"id": "e3", "scope": "emphasis:org:other", "text": "another owner's rule", "tags": []}]
        out, _ = run_js_mem(code, {"scopekey": {"key": "org:acme", "applies": True}}, store)
        self.assertEqual(out, {"key": "org:acme", "memories": [{"text": "billing needs a hard look"}]})
        out, _ = run_js_mem(code, {"scopekey": {"key": "", "applies": False}}, store)
        self.assertEqual(out["memories"], [])                    # a key that doesn't apply reads nothing

    def test_review_flow_renders_the_key_before_emphasis(self):
        ids = [s.get("id") for s in load()["workflows"]["review-flow"]["steps"]]
        self.assertLess(ids.index("prmeta"), ids.index("scopekey"))
        self.assertLess(ids.index("scopekey"), ids.index("emphasis"))
        self.assertLess(ids.index("emphasis"), ids.index("assess"))

    def test_unrebuildable_facts_fail_loudly(self):
        for t in ("team:{{.inputs.team}}", "{{.steps.triage.outputs.area}}", "{{.head}}", "{{.me.login}}",
                  "{{.workflow}}", "{{index .inputs \"team\"}}", "{{.}}", "x:${step}"):
            with self.assertRaises(AssertionError) as cm:
                scopekeys(t, {"inputs": {"repo": "acme/api", "pr": 7}})
            self.assertIn("a key may read only", str(cm.exception), t)
        for t in ("{{.owner", "{{if .owner}}x{{end}}", "   "):
            with self.assertRaises(AssertionError) as cm:
                scopekeys(t, {})
            self.assertIn("the scope_key setting", str(cm.exception), t)

    def test_keyspec_says_when_collect_must_fetch_pr_metadata(self):
        self.assertEqual(scopekeys("org:{{.owner}}", {}), {"needs_meta": False})
        self.assertEqual(scopekeys("{{printf \"%s/%s\" .repo (index .labels 0)}}", {}), {"needs_meta": True})

    def test_a_pr_whose_key_does_not_render_cannot_be_cited(self):
        out = scopekeys("area:{{index .labels 0}}", {"collect": {"prs": [
            {"ref": "acme/api#1", "facts": pr_facts("acme/api#1", labels=["billing"])},
            {"ref": "acme/api#2", "facts": pr_facts("acme/api#2", labels=[])}]}})
        self.assertEqual(out["keys"], {"acme/api#1": "area:billing"})
        self.assertEqual(out["unkeyed"], ["acme/api#2"])
        plan = run_js(load()["x-templates"]["plan"]["code"],
                      {"keys": out, "known": {"list": []}, "calibrate": {"entries": [entry("area:billing", "fact", "x", ["acme/api#2"])]}})
        self.assertIn("acme/api#2 is not a PR this run collected under a scope key", plan["rejected"][0])


class AutoModeTest(unittest.TestCase):
    """The auto setting is read once, by the automode step, and a value that is
    neither true nor false fails the run instead of quietly meaning gated."""

    @classmethod
    def setUpClass(cls):
        cls.code = flow_step(load(), "review-flow", "automode")["code"]

    def mode(self, raw):
        return run_js(self.code, {}, {"auto": raw})

    def test_spellings_of_true(self):
        for raw in ("true", "True", " yes ", "YES", "1", "\tTRUE\n"):
            self.assertEqual(self.mode(raw), {"auto": True, "mode": "auto"}, raw)

    def test_spellings_of_false(self):
        for raw in ("false", "False", " no ", "0", "FALSE"):
            self.assertEqual(self.mode(raw), {"auto": False, "mode": "gated"}, raw)

    def test_garbage_fails_loudly(self):
        for raw in ("ture", "", "on", "2", "auto"):
            with self.assertRaises(AssertionError) as cm:
                self.mode(raw)
            self.assertIn("the auto setting must be true or false", str(cm.exception), raw)

    def test_quotes_and_newlines_are_just_garbage(self):
        for raw in ('tr"ue', "yes\nno", "'true'"):
            with self.assertRaises(AssertionError) as cm:
                self.mode(raw)
            self.assertIn("the auto setting must be true or false", str(cm.exception), raw)
        # one that breaks the step's own syntax (a trailing backslash) still fails the run
        with self.assertRaises(AssertionError):
            self.mode("true\\")

    def test_it_runs_first_and_drives_every_condition(self):
        man = load()
        steps = man["workflows"]["review-flow"]["steps"]
        self.assertEqual(steps[0]["id"], "automode")
        raw = yaml.safe_dump(man)
        self.assertEqual(raw.count("${settings.auto}"), 1)   # only the automode step reads the text


class GateTest(unittest.TestCase):
    """auto=true never hands off; auto=false is the 2.x gate."""

    @classmethod
    def setUpClass(cls):
        man = load()
        cls.automode = flow_step(man, "review-flow", "automode")["code"]
        cls.gate = flow_step(man, "review-flow", "gate")["code"]
        cls.assessed = flow_step(man, "review-flow", "assessed")["code"]

    def decide(self, auto, decision, comments):
        m = run_js(self.automode, {}, {"auto": auto})
        assess = {"decision": {"choice": decision}, "tests_sufficient": {"noul": 0.9},
                  "docs_sufficient": {"noul": 0.9}, "glaring_holes": {"noul": 0.1}}
        assessed = run_js(self.assessed, {"automode": m, "assess": assess})
        ctx = {"automode": m, "assessed": assessed, "review": {"comments": [{"path": "a.go", "line": 1}] * comments}}
        return assessed, run_js(self.gate, ctx)

    def test_auto_posts_even_when_assess_says_manual(self):
        assessed, g = self.decide("true", "manual", 2)
        self.assertEqual(assessed["decision"], "auto")
        self.assertEqual(g, {"mode": "auto", "post": True, "handoff": False})

    def test_the_gate_itself_never_hands_off_in_auto(self):
        # even if an overridden assess/assessed step says manual
        ctx = {"assessed": {"decision": "manual"}, "review": {"comments": [{"path": "a.go", "line": 1}]}}
        on, off = dict(ctx, automode={"auto": True, "mode": "auto"}), dict(ctx, automode={"auto": False, "mode": "gated"})
        self.assertEqual(run_js(self.gate, on), {"mode": "auto", "post": True, "handoff": False})
        self.assertEqual(run_js(self.gate, off), {"mode": "gated", "post": False, "handoff": True})

    def test_auto_spelled_True_posts(self):
        assessed, g = self.decide("True", "manual", 2)
        self.assertEqual((assessed["decision"], g["handoff"], g["post"]), ("auto", False, True))

    def test_gate_without_the_automode_step_fails(self):
        with self.assertRaises(AssertionError):
            run_js(self.gate, {"assessed": {"decision": "auto"}, "review": {"comments": []}})

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
        out = run_js(self.assessed, {"automode": {"auto": False, "mode": "gated"}, "assess": assess,
                                     "emphasis": {"memories": [{"text": "r"}]}})
        self.assertIn("emphasis rule", out["reason"])

    def test_assess_and_emphasis_are_skipped_in_auto(self):
        man = load()
        for sid in ("emphasis", "assess"):
            self.assertEqual(flow_step(man, "review-flow", sid)["if"], "!automode.auto")


class FreshTrainTest(unittest.TestCase):
    """review-backfill --input fresh=true: learned entries of the trained keys move to
    archive:<scope>; hand-written ones, the shared set and other keys stay."""

    @classmethod
    def setUpClass(cls):
        t = load()["x-templates"]
        cls.fresh, cls.archive, cls.plan = t["fresh"]["code"], t["archive"]["code"], t["plan"]["code"]

    def test_fresh_spellings(self):
        for raw, on in (("true", True), (" Yes ", True), ("1", True), ("false", False), ("", False), ("NO", False)):
            self.assertEqual(run_js(self.fresh, {"inputs": {"fresh": raw}}), {"on": on}, raw)
        self.assertEqual(run_js(self.fresh, {"inputs": {}}), {"on": False})
        with self.assertRaises(AssertionError) as cm:
            run_js(self.fresh, {"inputs": {"fresh": "please"}})
        self.assertIn("fresh= takes true or false", str(cm.exception))

    def store(self):
        L = ["review-tuning", "backfill", "standard"]
        return [
            {"id": "a1", "scope": "acme/api", "text": "learned fact", "tags": ["review-tuning", "calibration", "fact"]},
            {"id": "a2", "scope": "standard:acme/api", "text": "learned standard", "tags": L},
            {"id": "a3", "scope": "emphasis:acme/api", "text": "learned emphasis", "tags": ["review-tuning", "calibration", "emphasis"]},
            {"id": "h1", "scope": "standard:acme/api", "text": "hand-written rule", "tags": ["house"]},
            {"id": "h2", "scope": "acme/api", "text": "outcome note", "tags": ["outcome"]},
            {"id": "g1", "scope": "", "text": "global learned", "tags": L},
            {"id": "o1", "scope": "standard:other/svc", "text": "another key's standard", "tags": L},
            {"id": "x1", "scope": "standard:acme/api-extra", "text": "a key that only shares a prefix", "tags": L},
        ]

    def test_moves_only_the_learned_entries_of_the_trained_keys(self):
        out, store = run_js_mem(self.archive, {"keys": {"key_list": ["acme/api"]}}, self.store())
        self.assertEqual(sorted(out["ids"]), ["a1", "a2", "a3"])
        by_scope = {}
        for m in store:
            by_scope.setdefault(m["scope"], []).append(m["text"])
        self.assertEqual(sorted(by_scope["archive:acme/api"]), ["learned fact"])
        self.assertEqual(by_scope["archive:standard:acme/api"], ["learned standard"])
        self.assertEqual(by_scope["archive:emphasis:acme/api"], ["learned emphasis"])
        self.assertEqual(by_scope["standard:acme/api"], ["hand-written rule"])   # untagged: kept
        self.assertEqual(by_scope["acme/api"], ["outcome note"])                 # not a lesson: kept
        self.assertEqual(by_scope[""], ["global learned"])                       # global: never archived
        self.assertEqual(by_scope["standard:other/svc"], ["another key's standard"])
        self.assertEqual(by_scope["standard:acme/api-extra"], ["a key that only shares a prefix"])
        moved = [m for m in store if m["scope"].startswith("archive:")]
        self.assertTrue(all("review-tuning" in m["tags"] for m in moved))       # tags kept on the move

    def test_plan_does_not_dedup_against_what_was_archived(self):
        known = [{"id": "a2", "scope": "standard:acme/api", "text": "learned standard"}]
        ctx = {"keys": keys_for(["acme/api#1"]), "known": {"list": known}, "archive": {"ids": ["a2"]},
               "merge": {"entries": [entry("acme/api", "standard", "learned standard", ["acme/api#1"], supersedes="a2")]}}
        out = run_js(self.plan, ctx)
        self.assertEqual([w["scope"] for w in out["writes"]], ["standard:acme/api"])   # re-learned, not "already recorded"
        self.assertEqual(out["forget"], [])                                            # and the archived one isn't forgotten

    def test_archive_runs_after_the_agents_and_before_plan(self):
        backfill = [t for t in load()["triggers"] if t["name"] == "review-backfill"][0]
        steps = [s.get("id") for s in backfill["steps"]]
        self.assertLess(steps.index("merge"), steps.index("archive"))
        self.assertLess(steps.index("archive"), steps.index("plan"))
        self.assertLess(steps.index("fresh"), steps.index("extract"))


if __name__ == "__main__":
    unittest.main()
