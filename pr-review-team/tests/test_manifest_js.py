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


# A TEST DOUBLE for core's ctx.memory.key (memory.RenderScopeKey), so the
# pack's own logic runs under node: plain fields, `index .x N` and
# `printf "…%s…" args` — only what these tests write — with core's rule that a
# fact with no value ("", missing, 0, a blank list element) means the key
# doesn't apply. The real renderer is asserted against core's vectors through
# the real binding on a core build: test_scope_key_binding.py.
KEY_DOUBLE = r"""
const keyDouble = function (t, facts) {
  if (typeof t !== "string") throw new Error("memory.key: template must be a string");
  facts = facts || {};
  const src = t.split("$" + "{repo}").join("{{.repo}}").split("$" + "{workflow}").join("{{.workflow}}").split("$" + "{step}").join("{{.step}}");
  const no = function (why) { return { key: "", ok: false, reason: "scope key " + JSON.stringify(t) + " " + why }; };
  if (src.split("{{").length !== src.split("}}").length) return no("does not parse");
  if (/\{\{-?\s*(if|range|with|define|template|end)\b/.test(src)) return no("uses a construct a scope key may not");
  const empty = function (v) { return v === undefined || v === null || v === "" || v === 0; };
  let missing = false;
  const arg = function (tok) {
    let m;
    if ((m = /^"(.*)"$/.exec(tok))) return m[1];
    if ((m = /^\(?index \.(\w+) (\d+)\)?$/.exec(tok))) { const l = facts[m[1]] || []; const v = l[+m[2]]; if (empty(v)) missing = true; return v; }
    if ((m = /^\.([\w.]+)$/.exec(tok))) { let v = facts; for (const k of m[1].split(".")) v = v == null ? undefined : v[k]; if (empty(v)) missing = true; return v; }
    throw new Error("the key double can't evaluate " + tok);
  };
  const out = src.replace(/\{\{([\s\S]*?)\}\}/g, function (_, body) {
    const toks = body.trim().match(/"[^"]*"|\([^)]*\)|[^\s()]+(?: \.\w+ \d+)?/g) || [];
    if (toks[0] === "printf") { const args = toks.slice(1).map(arg); const f = args.shift(); let i = 0; return f.replace(/%s/g, function () { return String(args[i++]); }); }
    return String(arg(body.trim()));
  }).trim();
  return missing || !out ? no("reads a fact this step has no value for") : { key: out, ok: true, reason: "" };
};
"""


def with_settings(code, settings=None):
    """The step's code with ${settings.x} filled in: the given values, else the manifest's defaults."""
    for k, v in (settings or {}).items():
        code = code.replace("${settings.%s}" % k, v)
    for k, spec in (load().get("settings") or {}).items():
        code = code.replace("${settings.%s}" % k, str(spec.get("default", "")))
    return code


def run_js(code, ctx, settings=None, binding=True):
    code = with_settings(code, settings)
    src = ("const ctx = %s;\n" % json.dumps(ctx) + KEY_DOUBLE +
           ("ctx.memory = Object.assign({ key: keyDouble }, ctx.memory || {});\n" if binding else "") +
           "const out = (function () {\n%s\n})();\nprocess.stdout.write(JSON.stringify(out));" % code)
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
  key: keyDouble,
};
"""


def run_js_mem(code, ctx, store):
    """Run a step with an in-memory ctx.memory; returns (output, the store afterwards)."""
    code = with_settings(code)
    src = ("const ctx = %s;\n" % json.dumps(ctx) + KEY_DOUBLE + FAKE_MEMORY % json.dumps(store) +
           "const out = (function () {\n%s\n})();\nprocess.stdout.write(JSON.stringify({out: out, store: store}));" % code)
    res = subprocess.run(["node", "-e", src], capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(res.stderr)
    r = json.loads(res.stdout)
    return r["out"], r["store"]


def run_notes(ctx, store, limit="40", memory="on"):
    """review-team's `notes` step over an in-memory store."""
    code = flow_step(load(), "review-team", "notes")["code"].replace("${settings.memory_limit}", limit)
    ctx = dict(ctx)
    ctx.setdefault("inputs", {"repo": "acme/api", "pr": 7})
    ctx["inputs"] = dict(ctx["inputs"], memory=memory)
    return run_js_mem(code, ctx, store)


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


def scopekeys(template, ctx, binding=True):
    return run_js(load()["x-templates"]["scopekeys"]["code"], ctx, {"scope_key": template}, binding=binding)


def keys_for(refs, template="{{.repo}}"):
    """The `keys` step's output for these PRs under a scope_key template."""
    return scopekeys(template, {"collect": {"prs": [{"ref": r, "facts": pr_facts(r)} for r in refs]}})


RUN = ("acme/api#1", "acme/api#2", "acme/api#3", "acme/api#4", "acme/api#5", "acme/api#6", "acme/api#7", "acme/api#9",
       "acme/web#1", "acme/web#2", "acme/web#3", "other/svc#3")
FENCE = "ev0123456789abcdef"
# collect's output as the plan sees it: every PR carries trusted evidence, none a catch
COLLECTED = {"fence": FENCE, "trusted_refs": list(RUN), "catch_refs": []}


class PlanTest(unittest.TestCase):
    """The write restriction: the scope comes from kind + key, and a key's
    entries must rest on PRs that map to that key. With the default scope_key
    ({{.repo}}) a key is a repo — the 3.0 behavior."""

    @classmethod
    def setUpClass(cls):
        cls.code = load()["x-templates"]["plan"]["code"]
        cls.repo_keys = keys_for(RUN)

    def plan(self, entries, keys=None, known=(), via="calibrate"):
        ctx = {"keys": keys or self.repo_keys, "known": {"list": list(known)}, via: {"entries": entries}, "collect": COLLECTED}
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
        return run_js(self.plan_code, {"keys": self.keys, "known": {"list": []}, "calibrate": {"entries": entries}, "collect": COLLECTED})

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
        store = [{"id": "a", "scope": "standard:org:acme", "text": "acme standard", "tags": []},
                 {"id": "b", "scope": "standard:org:other", "text": "other standard", "tags": []}]
        notes, _ = run_notes({"notekey": scopekeys("org:{{.owner}}", {"inputs": {"repo": "acme/web", "pr": 2}})}, store)
        self.assertEqual(notes["standard"], ["acme standard"])


class ScopeKeyTest(unittest.TestCase):
    """The scope_key rules. Keys are rendered by core (ctx.memory.key); here by its test double."""

    @classmethod
    def setUpClass(cls):
        cls.code = load()["x-templates"]["scopekeys"]["code"]

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
                  "{{.workflow}}", "{{index .inputs \"team\"}}", "{{.}}", "x:${step}", "{{.repo.x}}", "{{.inputs.repo.x}}"):
            with self.assertRaises(AssertionError) as cm:
                scopekeys(t, {"inputs": {"repo": "acme/api", "pr": 7}})
            self.assertIn("a key may read only", str(cm.exception), t)
        for t in ("{{.owner", "{{if .owner}}x{{end}}", "   "):
            with self.assertRaises(AssertionError) as cm:
                scopekeys(t, {})
            self.assertIn("the scope_key setting", str(cm.exception), t)

    def test_text_in_quotes_is_not_a_fact(self):
        ctx = {"inputs": {"repo": "acme/api", "pr": 7}}
        self.assertEqual(scopekeys('{{printf "%s@.v1" .repo}}', ctx)["key"], "acme/api@.v1")

    def test_a_key_core_will_not_render_fails_with_cores_reason(self):
        with self.assertRaises(AssertionError) as cm:
            scopekeys("{{if .owner}}x{{end}}", {})
        self.assertIn("does not render: scope key", str(cm.exception))

    def test_no_binding_names_what_to_upgrade(self):
        with self.assertRaises(AssertionError) as cm:
            scopekeys("{{.repo}}", {"inputs": {"repo": "acme/api", "pr": 7}}, binding=False)
        self.assertIn("no ctx.memory.key", str(cm.exception))
        self.assertIn("conductor >= 0.69.0", str(cm.exception))

    def test_an_old_daemon_refusing_the_call_names_the_version(self):
        code = load()["x-templates"]["scopekeys"]["code"].replace("${settings.scope_key}", "{{.repo}}")
        ctx = {"inputs": {"repo": "acme/api", "pr": 7}}
        old = ("ctx.memory = { key: function () { throw new Error('memory: no operation \"key\"'); } };\n")
        src = "const ctx = %s;\n%sconst out = (function () {\n%s\n})();" % (json.dumps(ctx), old, code)
        res = subprocess.run(["node", "-e", src], capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('ctx.memory.key failed (memory: no operation "key")', res.stderr)
        self.assertIn("conductor >= 0.69.0", res.stderr)

    def test_keys_are_rendered_by_core_not_the_pack(self):
        code = load()["x-templates"]["scopekeys"]["code"]
        self.assertIn("ctx.memory.key(TEMPLATE, facts)", code)
        for gone in ("renderKey", "PROBE", "parsePipe", "lexAction"):
            self.assertNotIn(gone, code)

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


FRAMING = "Learned notes steer"


def all_steps(man):
    """Every step of every workflow and trigger: (where, step)."""
    for name, wf in man["workflows"].items():
        for st in wf["steps"]:
            yield name, st
    for t in man["triggers"]:
        for st in t["steps"]:
            yield t["name"], st


def text_of(st):
    return (st.get("prompt") or "") + ((st.get("decide") or {}).get("state") or "")


class MemorySteeringTest(unittest.TestCase):
    """Learned memory steers the lens reviewers; it never replaces their review."""

    @classmethod
    def setUpClass(cls):
        cls.man = load()
        cls.lens = flow_step(cls.man, "review-team", "review")["prompt"]
        cls.key = {"notekey": {"key": "acme/api", "applies": True}}

    def test_no_step_reads_memory_through_core(self):
        # memory reaches a prompt only through the pack's own framed steps
        for where, st in all_steps(self.man):
            self.assertIn(st.get("memory", False), (False, None), "%s/%s" % (where, st.get("id")))

    def test_every_step_that_shows_notes_frames_them(self):
        shown = [(w, st.get("id")) for w, st in all_steps(self.man)
                 if ".notes." in text_of(st) or ".emphasis.memories" in text_of(st)]
        self.assertEqual(sorted(shown), [("review-flow", "assess"), ("review-team", "review")])
        for w, st in all_steps(self.man):
            if (w, st.get("id")) in shown:
                self.assertIn(FRAMING, text_of(st), st.get("id"))
                self.assertIn("never replace", text_of(st), st.get("id"))

    def test_the_lens_reviews_first_and_notes_come_last(self):
        p = self.lens
        self.assertLess(p.index("YOUR JOB: a full, independent review"), p.index("REPO STANDARDS"))
        self.assertLess(p.index("CHANGED FILES"), p.index("LEARNED NOTES"))
        for must in ("not a checklist and not the scope of your review",
                     "never a\nreason to skip or soften",
                     "A note alone never\nmakes a finding blocking",
                     "STANDARD notes are strong guidance, not law",
                     "violating these is blocking"):
            self.assertIn(must, p)
        self.assertNotIn("weigh like REPO", p)
        self.assertIn("STANDARD — strong guidance, not law", flow_step(self.man, "review-team", "notes")["code"])

    def test_memory_limit_is_a_setting_the_notes_step_reads(self):
        self.assertEqual(self.man["settings"]["memory_limit"]["default"], "40")
        self.assertIn("${settings.memory_limit}", flow_step(self.man, "review-team", "notes")["code"])

    def store(self):
        s = []
        for layer, scope, n in (("fact", "acme/api", 3), ("emphasis", "emphasis:acme/api", 2),
                                ("std", "standard:acme/api", 2), ("global", "global", 1),
                                ("other", "standard:other/x", 2)):
            s += [{"id": "%s%d" % (layer, i), "scope": scope, "text": "%s %d" % (layer, i), "tags": []} for i in range(n)]
        return s

    def test_notes_come_in_layer_order_under_the_cap(self):
        out, _ = run_notes(self.key, self.store(), limit="40")
        self.assertEqual(out["standard"], ["global 0", "std 0", "std 1"])
        self.assertEqual(out["emphasis"], ["emphasis 0", "emphasis 1"])
        self.assertEqual(out["facts"], ["fact 0", "fact 1", "fact 2"])
        self.assertEqual(out["count"], 8)
        t = out["text"]
        self.assertLess(t.index("STANDARD"), t.index("EMPHASIS"))
        self.assertLess(t.index("EMPHASIS"), t.index("FACTS"))
        self.assertNotIn("other", t)
        capped, _ = run_notes(self.key, self.store(), limit="4")     # facts are the first to go
        self.assertEqual((capped["count"], capped["standard"], capped["emphasis"], capped["facts"]),
                         (4, ["global 0", "std 0", "std 1"], ["emphasis 0"], []))

    def test_memory_off_or_zero_reads_nothing(self):
        for kw in ({"memory": "off"}, {"limit": "0"}, {"memory": " OFF "}):
            out, _ = run_notes(self.key, self.store(), **kw)
            self.assertEqual((out["count"], out["text"]), (0, ""), kw)

    def test_a_key_that_does_not_apply_reads_only_the_shared_set(self):
        out, _ = run_notes({"notekey": {"key": "", "applies": False}}, self.store())
        self.assertEqual((out["standard"], out["emphasis"], out["facts"]), (["global 0"], [], []))

    def test_bad_values_fail_the_run(self):
        for kw in ({"limit": "lots"}, {"limit": "-1"}, {"memory": "maybe"}):
            with self.assertRaises(AssertionError):
                run_notes(self.key, self.store(), **kw)

    def test_no_memory_section_is_no_notes(self):
        code = flow_step(self.man, "review-team", "notes")["code"].replace("${settings.memory_limit}", "40")
        ctx = dict(self.key, inputs={"repo": "acme/api", "pr": 7, "memory": "on"})
        broken = "ctx.memory = { recall: function () { throw new Error('memory: not configured'); } };\n"
        src = "const ctx = %s;\n%sconst out = (function () {\n%s\n})();\nprocess.stdout.write(JSON.stringify(out));" % (json.dumps(ctx), broken, code)
        res = subprocess.run(["node", "-e", src], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)["count"], 0)


def eval_trigger(man=None):
    return [t for t in (man or load())["triggers"] if t["name"] == "review-eval"][0]


def eval_step(step_id):
    return [s for s in eval_trigger()["steps"] if s.get("id") == step_id][0]


def f(path, line, body, note=""):
    return {"path": path, "line": line, "blocking": False, "body": body, "lens": "X", "note": note}


class EvalTest(unittest.TestCase):
    """review-eval: replay with memory off and on, and compare."""

    def pairs(self, runs, items):
        return run_js(eval_step("pairs")["code"], {"pick": {"runs": runs}, "replay": {"items": items}})

    def compare(self, pairs, p_same):
        judge = {"items": [{"same_issue": {"noul": x}} for x in p_same]}
        return run_js(eval_step("compare")["code"], {"pairs": pairs, "judge": judge})

    def runs(self, *refs):
        out = []
        for r in refs:
            repo, n = r.split("#")
            out += [{"repo": repo, "pr": int(n), "memory": "off"}, {"repo": repo, "pr": int(n), "memory": "on"}]
        return out

    def test_matching_retains_drops_and_adds(self):
        base = [f("a.go", 10, "Return the error instead of logging it."),
                f("a.go", 40, "This loop queries once per row."),
                f("b.go", 3, "Validate the input path.")]
        mem = [f("a.go", 10, "return the  error instead of logging it."),      # same text: retained, not judged
               f("a.go", 43, "N+1: one query per row in this loop."),        # near, judge says same
               f("b.go", 30, "Validate the input path."),                    # same text but far away: not a pair
               f("c.go", 1, "Deploy job needs the new env var.", note="A new required env var")]
        pairs = self.pairs(self.runs("acme/api#1"), [{"findings": base, "notes": "0"}, {"findings": mem, "notes": "3"}])
        self.assertEqual((len(pairs["exact"]), pairs["count"]), (1, 1))
        self.assertEqual((pairs["list"][0]["base_line"], pairs["list"][0]["mem_line"]), (40, 43))
        out = self.compare(pairs, [0.9])
        pr = out["prs"][0]
        self.assertEqual([r["base"] for r in pr["retained"]], ["a.go:10", "a.go:40"])
        self.assertEqual([x["path"] for x in pr["dropped"]], ["b.go"])
        self.assertEqual([x["path"] for x in pr["added"]], ["b.go", "c.go"])
        self.assertEqual(pr["added_citing_note"], 1)
        self.assertEqual(pr["notes"], 3)
        t = out["totals"]
        self.assertEqual((t["baseline"], t["retained"], t["dropped"], t["added"]), (3, 2, 1, 2))
        self.assertAlmostEqual(t["retention"], 2 / 3)
        self.assertEqual(t["verdict"], "crowding")

    def test_the_judge_decides_near_pairs(self):
        base, mem = [f("a.go", 40, "loop queries per row")], [f("a.go", 44, "rename this variable")]
        pairs = self.pairs(self.runs("acme/api#1"), [{"findings": base}, {"findings": mem}])
        self.assertEqual(pairs["count"], 1)
        kept = self.compare(pairs, [0.2])["prs"][0]
        self.assertEqual((len(kept["retained"]), len(kept["dropped"]), len(kept["added"])), (0, 1, 1))
        self.assertEqual(len(self.compare(pairs, [0.5])["prs"][0]["retained"]), 1)

    def test_matching_is_one_to_one_best_first(self):
        base = [f("a.go", 10, "x one"), f("a.go", 12, "x two")]
        mem = [f("a.go", 11, "y")]
        pairs = self.pairs(self.runs("acme/api#1"), [{"findings": base}, {"findings": mem}])
        out = self.compare(pairs, [0.6, 0.95])["prs"][0]
        self.assertEqual([r["base"] for r in out["retained"]], ["a.go:12"])
        self.assertEqual(len(out["dropped"]), 1)

    def test_totals_across_prs_and_an_empty_baseline(self):
        items = [{"findings": [f("a.go", 1, "same")]}, {"findings": [f("a.go", 1, "same")]},
                 {"findings": []}, {"findings": [f("z.go", 1, "new")]}]
        out = self.compare(self.pairs(self.runs("acme/api#1", "acme/web#2"), items), [])
        self.assertEqual([p["ref"] for p in out["prs"]], ["acme/api#1", "acme/web#2"])
        self.assertEqual(out["prs"][1]["retention"], 1)
        self.assertEqual((out["totals"]["retention"], out["totals"]["verdict"]), (1, "ok"))

    def test_verdict_thresholds(self):
        def verdict(kept, total):
            base = [f("a.go", 100 * i, "b%d" % i) for i in range(total)]
            mem = base[:kept]
            return self.compare(self.pairs(self.runs("acme/api#1"), [{"findings": base}, {"findings": mem}]), [])["totals"]["verdict"]
        self.assertEqual(verdict(9, 10), "ok")
        self.assertEqual(verdict(8, 10), "watch")
        self.assertEqual(verdict(7, 10), "crowding")

    def test_eval_never_posts_hands_off_or_writes_memory(self):
        man = load()
        steps = eval_trigger(man)["steps"]
        self.assertEqual([s.get("id") for s in steps], ["pick", "replay", "pairs", "judge", "compare", "report"])
        replay = steps[1]
        self.assertEqual(replay["workflow"], "review-team")       # the engine, never review-flow (post / hand-off)
        self.assertEqual(replay["with"]["prior_comments"], "off")
        self.assertEqual(replay["with"]["memory"], "{{.item.memory}}")
        # nothing in the eval or the review engine names a write verb, a hand-off or a memory write
        engine = [st for w, st in all_steps(man) if w in ("review-team", "review-eval")]
        blob = json.dumps(engine)
        for bad in ("submit_review", "remember(", "memory.remember", "handoff", "skill"):
            self.assertNotIn(bad, blob)
        for st in engine:
            self.assertNotIn("uses", st) if not str(st.get("uses", "")).startswith("github.") else \
                self.assertIn(st["uses"], ("github.pr_get", "github.pr_files", "github.file", "github.review_comments"))

    def test_a_replay_can_ignore_the_prs_existing_comments(self):
        code = flow_step(load(), "review-team", "prior")["code"]
        existing = {"comments": [{"path": "a.go", "line": 3, "body": "the pack's own earlier comment"}]}
        on = run_js(code, {"inputs": {"prior_comments": "on"}, "existing": existing})
        off = run_js(code, {"inputs": {"prior_comments": " OFF "}, "existing": existing})
        self.assertEqual((len(on["comments"]), off["comments"]), (1, []))
        self.assertEqual(len(run_js(code, {"inputs": {}, "existing": existing})["comments"]), 1)   # default: on
        with self.assertRaises(AssertionError):
            run_js(code, {"inputs": {"prior_comments": "maybe"}, "existing": existing})
        reconcile = flow_step(load(), "review-team", "reconcile")["code"]
        self.assertIn("ctx.prior && ctx.prior.comments", reconcile)

    def test_runs_pair_off_then_on(self):
        out = self.pairs(self.runs("acme/api#1"), [{"findings": [], "notes": "0"}, {"findings": [], "notes": "5"}])
        self.assertEqual(out["prs"][0]["on"]["notes"], 5)
        self.assertEqual(out["prs"][0]["off"]["notes"], 0)


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
        ctx = {"keys": keys_for(["acme/api#1"]), "known": {"list": known}, "archive": {"ids": ["a2"]}, "collect": COLLECTED,
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
