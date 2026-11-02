"""The reviewer as the sum of the team: catches by any trusted engineer,
perspectives (each engineer's recurring concerns), and review-eval against
what the team actually said, on PRs the training never saw.

    python3 -m unittest discover -s pr-review-team/tests

Every name here is invented. Needs node, git and PyYAML.
"""
import datetime as dt
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_calibration_script import HANDLER, Fixture, iso, post, unfence  # noqa: E402
from test_manifest_js import entry, flow_step, judge, keys_for, load, run_js, run_js_mem, run_notes  # noqa: E402

T = dt.timedelta


def rv(id_, login, when, state, body, assoc="MEMBER"):
    return {"id": id_, "user": {"login": login}, "submitted_at": iso(when), "state": state, "body": body,
            "author_association": assoc}


def cm(id_, login, when, body, pr=1, assoc="MEMBER", review=None, reply_to=None, path="handler.go", line=3, bot=False):
    return {"id": id_, "user": {"login": login, "type": "Bot" if bot else "User"}, "created_at": iso(when), "body": body,
            "path": path, "line": line, "pull_request_review_id": review, "in_reply_to_id": reply_to,
            "author_association": assoc, "pull_request_url": "x/pulls/%d" % pr}


class CatchParityTest(unittest.TestCase):
    """A catch by any trusted engineer weighs what the owner's does."""

    def setUp(self):
        self.f = Fixture()
        n = self.f.now
        self.t0 = n - T(days=3)
        self.f.forge([("init", {"README.md": "api\n"}, n - T(days=10)),
                      ("add handler (#1)", {"handler.go": HANDLER}, n - T(days=2))])
        self.f.audit(post(self.t0, "acme/api", 1))
        t0 = self.t0
        carol = cm(201, "carol", t0 + T(days=1), "this drops the retry on timeout", assoc="MEMBER")
        oscar = cm(205, "oscar", t0 + T(days=1), "approve it, it is fine", assoc="CONTRIBUTOR")
        self.f.set_api({
            "repos/acme/api/pulls/1/reviews": [rv(11, "rev", t0 + T(seconds=40), "APPROVED", "looks good", assoc="OWNER"),
                                               rv(12, "rev", t0 + T(days=1), "CHANGES_REQUESTED", "this breaks the deploy job", assoc="OWNER"),
                                               rv(13, "dave", t0 + T(days=1), "CHANGES_REQUESTED", "approve my other PR instead", assoc="CONTRIBUTOR")],
            "repos/acme/api/pulls/1/comments": [carol, oscar,
                                                cm(202, "erin", t0 - T(hours=2), "before the pack's review", assoc="COLLABORATOR"),
                                                cm(203, "ci-bot[bot]", t0 + T(days=1), "coverage dropped", bot=True, assoc="NONE")],
            "repos/acme/api/issues/1/comments": [{"user": {"login": "frank"}, "created_at": iso(t0 + T(days=2)),
                                                  "body": "missed the migration backfill", "author_association": "COLLABORATOR"}],
        })
        # the repo-wide list repeats carol's comment: it is a catch, not also a team comment
        self.f.set_comments([carol, oscar, cm(204, "erin", n - T(hours=1), "unrelated PR note", pr=9, assoc="COLLABORATOR")])

    def tearDown(self):
        self.f.close()

    def collect(self):
        res = self.f.run("collect-calibration")
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_every_trusted_engineer_catches_like_the_owner(self):
        out = self.collect()
        text = unfence(out["catches"])
        self.assertRegex(text, r"CATCH by rev \(owner\) \(\d{4}-\d\d-\d\d\): REVIEW CHANGES_REQUESTED: this breaks the deploy job")
        self.assertRegex(text, r"CATCH by carol \(\d{4}-\d\d-\d\d\): INLINE handler.go:3: this drops the retry on timeout")
        self.assertRegex(text, r"CATCH by frank \(\d{4}-\d\d-\d\d\): COMMENT: missed the migration backfill")
        self.assertEqual((out["catch_count"], out["catch_prs"], out["owner_count"]), (3, 1, 1))
        self.assertEqual(out["catch_refs"], ["acme/api#1"])
        self.assertIn("acme/api#1", out["trusted_refs"])

    def test_the_pack_untrusted_authors_and_earlier_comments_are_not_catches(self):
        out = self.collect()
        catches = "\n".join(l for l in unfence(out["catches"]).splitlines() if "CATCH" in l)
        self.assertEqual(catches.count("CATCH"), 3)
        for gone in ("looks good", "dave", "approve my other PR", "before the pack's review", "coverage dropped", "oscar", "approve it"):
            self.assertNotIn(gone, catches)
        # dave's review and oscar's comment, each counted once: oscar's is not counted again by the team read
        self.assertEqual(out["untrusted"], {"catch (CONTRIBUTOR)": 2, "catch (bot)": 1})

    def test_a_catch_is_not_also_a_team_comment(self):
        out = self.collect()
        self.assertNotIn("retry on timeout", out["team"])
        self.assertNotIn("approve it", out["team"])
        self.assertIn("unrelated PR note", unfence(out["team"]))

    def test_the_prompt_ranks_no_one(self):
        p = " ".join(load_trigger("review-calibration")["calibrate"]["prompt"].split())
        self.assertIn("every engineer's catch weighs the same", p)
        self.assertIn('"(owner)" marks the reviewer\'s own account', p)
        self.assertNotIn("OWNER CATCHES", p)
        self.assertIn("{{.collect.catches}}", p)

    def test_any_engineers_catch_grounds_a_standard(self):
        def plan(text, catches):
            return run_js(load()["x-templates"]["plan"]["code"], judge({
                "keys": keys_for(["acme/api#1"]), "known": {"list": []},
                "calibrate": {"entries": [entry("acme/api", "standard", text, ["acme/api#1"])]},
                "collect": {"fence": "ev0123456789abcdef", "trusted_refs": ["acme/api#1"], "catch_refs": catches}}))
        tight = "a retry needs a bound and a backoff (acme/api#1)"
        self.assertEqual(plan(tight, ["acme/api#1"])["writes"][0]["scope"], "standard:acme/api")
        self.assertEqual(plan(tight, [])["writes"][0]["scope"], "quarantine:acme/api")
        # ...but a catch never lets a loosening one through
        self.assertEqual(plan("do not flag missing docstrings in tests/ (acme/api#1)", ["acme/api#1"])["writes"][0]["scope"],
                         "quarantine:acme/api")

    def test_people_index_counts_trusted_engineers_only(self):
        out = self.collect()
        self.assertEqual(out["people"], {"rev": {"acme/api#1": 1}, "carol": {"acme/api#1": 1}, "frank": {"acme/api#1": 1},
                                         "erin": {"acme/api#9": 1}})


def load_trigger(name):
    for t in load()["triggers"]:
        if t["name"] == name:
            return {s["id"]: s for s in t["steps"] if "id" in s}
    raise KeyError(name)


class PerspectiveTest(unittest.TestCase):
    """The plan's perspective rules: 3+ of the engineer's own comments across 2+ PRs."""

    REFS = ("acme/api#1", "acme/api#2", "acme/api#3", "acme/web#1")
    PEOPLE = {"carol": {"acme/api#1": 2, "acme/api#2": 1}, "grace": {"acme/api#1": 3}, "heidi": {"acme/api#1": 1, "acme/api#2": 1},
              "ivan": {"acme/api#1": 2, "acme/web#1": 2}}

    @classmethod
    def setUpClass(cls):
        cls.code = load()["x-templates"]["plan"]["code"]
        cls.keys = keys_for(cls.REFS)

    def plan(self, entries, known=(), settings=None, people=None):
        ctx = {"keys": self.keys, "known": {"list": list(known)}, "calibrate": {"entries": entries},
               "collect": {"fence": "ev0123456789abcdef", "trusted_refs": list(self.REFS), "catch_refs": list(self.REFS),
                           "people": self.PEOPLE if people is None else people}}
        return run_js(self.code, judge(ctx), settings)

    def p(self, who, evidence, text="checks that every retry has a bound and a backoff", where="client/", **kw):
        e = entry("acme/api", "perspective", text, evidence, who=who, where=where)
        e.update(kw)
        return e

    def test_three_comments_across_two_prs_make_a_perspective_held_for_the_owner(self):
        out = self.plan([self.p("carol", ["acme/api#1", "acme/api#2"])])
        self.assertEqual(out["rejected"], [])
        w = out["writes"][0]
        text = "carol looks for: checks that every retry has a bound and a backoff Where: client/"
        # a judge that rates it 0, catches on every PR it cites: still held — a perspective has no catch of its own
        self.assertEqual((w["scope"], w["target_scope"], w["text"], w["why"]),
                         ("quarantine:acme/api", "perspective:acme/api", text, "a perspective is held until you promote it"))
        self.assertEqual(w["tags"][:5], ["review-tuning", "calibration", "perspective", "who:carol", "quarantined"])
        self.assertRegex(w["tags"][5], r"^held-[0-9a-f]{8}$")
        # only learning_review: live writes it straight to its scope
        live = self.plan([self.p("carol", ["acme/api#1", "acme/api#2"])], settings={"learning_review": "live"})["writes"][0]
        self.assertEqual((live["scope"], live["tags"]), ("perspective:acme/api", ["review-tuning", "calibration", "perspective", "who:carol"]))

    def test_the_thresholds(self):
        for who, ev, why in (("grace", ["acme/api#1"], "3 comments on 1 PR"),
                             ("heidi", ["acme/api#1", "acme/api#2"], "2 comments on 2 PRs"),
                             ("carol", ["acme/api#1", "acme/api#2", "acme/api#3"], "cites a PR they never commented on"),
                             ("mallory", ["acme/api#1", "acme/api#2"], "not in the people index: untrusted"),
                             ("carol;rm", ["acme/api#1", "acme/api#2"], "not a login")):
            out = self.plan([self.p(who, ev)])
            self.assertEqual(out["writes"], [], why)
            self.assertTrue(out["rejected"], why)
        self.assertIn("needs 3+ comments across 2+ cited PRs", self.plan([self.p("grace", ["acme/api#1"])])["rejected"][0])
        # one PR cited twice is still one PR
        self.assertEqual(self.plan([self.p("grace", ["acme/api#1", "acme/api#1"])])["writes"], [])
        self.assertIn("who must be a GitHub login", self.plan([self.p("carol;rm", ["acme/api#1", "acme/api#2"])])["rejected"][0])
        # even when the people index somehow holds the bad name
        odd = dict(self.PEOPLE, **{"carol;rm": {"acme/api#1": 2, "acme/api#2": 1}})
        self.assertEqual(self.plan([self.p("carol;rm", ["acme/api#1", "acme/api#2"])], people=odd)["writes"], [])

    def test_a_perspective_stays_under_one_key(self):
        out = self.plan([self.p("ivan", ["acme/api#1", "acme/web#1"])])
        self.assertEqual(out["writes"], [])
        self.assertIn("evidence must all map to acme/api", out["rejected"][0])

    def test_where_is_screened_and_suppression_is_held(self):
        out = self.plan([self.p("carol", ["acme/api#1", "acme/api#2"], where="see https://x.example/areas")])
        self.assertIn("where contains a URL", out["rejected"][0])
        held = self.plan([self.p("carol", ["acme/api#1", "acme/api#2"], text="skips style comments on generated code")])
        self.assertEqual(held["writes"][0]["scope"], "quarantine:acme/api")
        self.assertEqual(held["writes"][0]["target_scope"], "perspective:acme/api")

    def test_untrusted_authors_never_form_one(self):
        f = Fixture()
        try:
            n = f.now
            long = " — a recurring concern about retry bounds in the client"
            f.set_comments([cm(300 + i, "mallory", n - T(days=40 + i), "retries need bounds" + long, pr=3 + i % 3, assoc="CONTRIBUTOR")
                            for i in range(6)] +
                           [cm(400 + i, "carol", n - T(days=40 + i), "retries need bounds" + long, pr=3 + i % 2, assoc="MEMBER")
                            for i in range(3)])
            res = f.run("collect-backfill", stdin=json.dumps({"inputs": {"repos": "acme/api"}}), BACKFILL_DAYS="90")
            self.assertEqual(res.returncode, 0, res.stderr)
            people = json.loads(res.stdout)["people"]
            self.assertEqual(people, {"carol": {"acme/api#3": 2, "acme/api#4": 1}})
            plan = self.plan([self.p("mallory", ["acme/api#3", "acme/api#4"])], people=people)
            self.assertEqual(plan["writes"], [])
        finally:
            f.close()


class PerspectiveLensTest(unittest.TestCase):
    """perspectives_as_lenses: off by default; on, at most 3 per key."""

    @classmethod
    def setUpClass(cls):
        cls.man = load()
        cls.plan_code = cls.man["x-templates"]["plan"]["code"]
        cls.lenses = flow_step(cls.man, "review-team", "lenses")["code"]
        cls.keys = keys_for(["acme/api#1", "acme/api#2"])

    def plan(self, entries, known=(), on=None):
        ctx = {"keys": self.keys, "known": {"list": list(known)}, "calibrate": {"entries": entries},
               "collect": {"fence": "ev0123456789abcdef", "trusted_refs": ["acme/api#1", "acme/api#2"], "catch_refs": [],
                           "people": {n: {"acme/api#1": 2, "acme/api#2": 1} for n in ("carol", "judy", "kim", "leo")}}}
        return run_js(self.plan_code, ctx, None if on is None else {"perspectives_as_lenses": on})

    def lens_entry(self, who, text="looks for idempotent migrations"):
        return entry("acme/api", "perspective", text, ["acme/api#1", "acme/api#2"], who=who, where="db/", as_lens=True)

    def test_off_by_default(self):
        self.assertEqual(self.man["settings"]["perspectives_as_lenses"]["default"], "false")
        out = self.plan([self.lens_entry("carol")])
        self.assertNotIn("lens", out["writes"][0]["tags"])

    def test_on_caps_lenses_per_key_counting_those_kept(self):
        known = [{"id": "p1", "scope": "perspective:acme/api", "text": "x", "tags": ["perspective", "lens"]},
                 {"id": "p2", "scope": "perspective:acme/api", "text": "y", "tags": ["perspective", "lens"]}]
        out = self.plan([self.lens_entry("carol", "a"), self.lens_entry("judy", "b"), self.lens_entry("kim", "c")], known=known, on="true")
        self.assertEqual(["lens" in w["tags"] for w in out["writes"]], [True, False, False])
        self.assertEqual(len(out["lens_capped"]), 2)
        self.assertIn("already 3 perspective lenses", out["lens_capped"][0])

    def test_a_bad_setting_fails(self):
        with self.assertRaises(AssertionError) as e:
            self.plan([], on="maybe")
        self.assertIn("perspectives_as_lenses", str(e.exception))

    # what a perspective you promoted out of quarantine carries: the move keeps its tags
    PROMOTED = ["quarantined", "held-0000000a"]

    def run_lenses(self, on, mode="on", n=5, plain_first=False, first=None):
        plain = first or {"id": "plain", "scope": "perspective:acme/api", "text": "carol looks for: x",
                          "tags": ["perspective", "who:carol"] + self.PROMOTED}
        store = ([plain] if plain_first else []) + [{"id": "l%d" % i, "scope": "perspective:acme/api", "text": "w%d looks for: idempotent migrations" % i,
                  "tags": ["perspective", "who:w%d" % i, "lens"] + self.PROMOTED} for i in range(n)]
        if not plain_first:
            store.append(plain)
        code = self.lenses.replace("${settings.perspectives_as_lenses}", on)
        ctx = {"inputs": {"lenses": [{"name": "Security", "focus": "s"}]}, "notes": {"mode": mode, "limit": 40, "key": "acme/api"}}
        out, _ = run_js_mem(code, ctx, store)
        return out

    def test_the_review_adds_at_most_three_and_only_when_on(self):
        self.assertEqual([l["name"] for l in self.run_lenses("false")["list"]], ["Security"])
        names = [l["name"] for l in self.run_lenses("true")["list"]]
        self.assertEqual(names, ["Security", "Perspective: w0", "Perspective: w1", "Perspective: w2"])
        self.assertEqual([l["name"] for l in self.run_lenses("true", mode="off")["list"]], ["Security"])
        # a perspective calibration did not mark as a lens stays a note
        self.assertEqual([l["name"] for l in self.run_lenses("true", n=1, plain_first=True)["list"]], ["Security", "Perspective: w0"])
        # one marked as a lens but never held and promoted (learning_review: live wrote it) is never a lens
        live = {"id": "live", "scope": "perspective:acme/api", "text": "mallory looks for: nothing", "tags": ["perspective", "who:mallory", "lens"]}
        self.assertEqual([l["name"] for l in self.run_lenses("true", n=1, plain_first=True, first=live)["list"]], ["Security", "Perspective: w0"])
        held_like = dict(live, tags=live["tags"] + ["quarantined", "held-nothex!"])
        self.assertEqual([l["name"] for l in self.run_lenses("true", n=0, plain_first=True, first=held_like)["list"]], ["Security"])
        # the panel runs lenses.runs: the list itself with one pass, each lens repeated with more
        self.assertEqual(flow_step(self.man, "review-team", "review")["for_each"], "{{.lenses.runs}}")
        out = self.run_lenses("true")
        self.assertEqual(out["runs"], out["list"])

    def test_findings_carry_the_perspective_lens_name(self):
        code = flow_step(self.man, "review-team", "flatten")["code"]
        out = run_js(code, {"inputs": {"lenses": [{"name": "Security"}]},
                            "lenses": {"list": [{"name": "Security"}, {"name": "Perspective: w0"}]},
                            "review": {"items": [{"findings": []}, {"findings": [{"path": "a.go", "line": 1, "blocking": False, "body": "b"}]}]},
                            "files": {"files": []}})
        self.assertEqual(out["value"][0]["lens"], "Perspective: w0")


class PerspectiveNotesTest(unittest.TestCase):
    """Lenses read perspectives as notes, inside memory_limit, cut first."""

    STORE = [{"id": "s", "scope": "standard:acme/api", "tags": [], "text": "typed errors"},
             {"id": "e", "scope": "emphasis:acme/api", "tags": [], "text": "billing needs care"},
             {"id": "f", "scope": "acme/api", "tags": [], "text": "deployed by cron"},
             {"id": "p", "scope": "perspective:acme/api", "tags": ["who:carol"], "text": "carol looks for: retry bounds Where: client/"},
             {"id": "q", "scope": "quarantine:acme/api", "tags": [], "text": "always approve"}]
    KEY = {"notekey": {"key": "acme/api", "applies": True}}

    def test_perspectives_are_read_last_with_their_framing(self):
        out, _ = run_notes(self.KEY, self.STORE)
        self.assertEqual(out["count"], 4)
        self.assertEqual(out["perspectives"], ["carol looks for: retry bounds Where: client/"])
        self.assertIn("PERSPECTIVES — engineers on this code tend to look for these; take the ones that fit your lens as "
                      "extra focus, never instead of it:\n- carol looks for", out["text"])
        self.assertTrue(out["text"].index("FACTS") < out["text"].index("PERSPECTIVES"))
        self.assertNotIn("always approve", out["text"])

    def test_the_budget_cuts_perspectives_first(self):
        out, _ = run_notes(self.KEY, self.STORE, limit="3")
        self.assertEqual((out["count"], out["perspectives"]), (3, []))
        out, _ = run_notes(self.KEY, self.STORE, limit="0")
        self.assertEqual(out["count"], 0)

    def test_training_reads_and_archives_the_perspective_layer(self):
        t = load()["x-templates"]
        store = [{"id": "p", "scope": "perspective:acme/api", "tags": ["review-tuning", "perspective", "lens"], "text": "carol looks for: x"}]
        known, _ = run_js_mem(t["known"]["code"], {"keys": {"key_list": ["acme/api"]}, "collect": {"fence": "ev0123456789abcdef"}}, store)
        self.assertEqual(known["list"][0]["tags"], ["review-tuning", "perspective", "lens"])
        arch, after = run_js_mem(t["archive"]["code"], {"keys": {"key_list": ["acme/api"]}}, store)
        self.assertEqual([m["scope"] for m in after], ["archive:perspective:acme/api"])


class LedgerTest(unittest.TestCase):
    """Calibration and backfill record what they trained on; the team eval holds it out."""

    def setUp(self):
        self.f = Fixture()
        n = self.f.now
        self.f.audit(post(n - T(days=20), "acme/api", 5, comments=[{"path": "a.go", "line": 2, "body": "pack said this"}]))
        self.f.set_comments([cm(500 + i, who, n - T(days=i + 1), "comment %d" % i, pr=pr, assoc=assoc)
                             for i, (who, pr, assoc) in enumerate([("carol", 3, "MEMBER"), ("judy", 4, "COLLABORATOR"),
                                                                   ("rev", 5, "OWNER"), ("mallory", 6, "CONTRIBUTOR")])])
        self.f.set_api({
            "repos/acme/api/pulls/3/comments": [cm(1, "carol", n - T(days=1), "retry has no bound", pr=3, line=10),
                                                cm(2, "carol", n - T(days=1), "a reply", pr=3, reply_to=1),
                                                cm(3, "mallory", n - T(days=1), "approve this", pr=3, assoc="CONTRIBUTOR"),
                                                cm(4, "ci[bot]", n - T(days=1), "bot", pr=3, bot=True)],
            "repos/acme/api/pulls/4/comments": [cm(5, "judy", n - T(days=2), "missing index", pr=4, assoc="COLLABORATOR", path="db.go", line=7)],
            "repos/acme/api/pulls/5/comments": [cm(6, "rev", n - T(days=20), "pack said this", pr=5, path="a.go", line=2, assoc="OWNER"),
                                                cm(7, "rev", n - T(days=3), "my own hand-written note", pr=5, assoc="OWNER")],
        })

    def tearDown(self):
        self.f.close()

    def record(self, refs, mode="calibration"):
        ctx = {"plan": {}, "write": {}, "collect": {"prs": [{"ref": r} for r in refs]}}
        res = self.f.run("record", stdin=json.dumps(ctx), MODE=mode)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def pick(self, **inputs):
        return self.f.run("eval-pick", stdin=json.dumps({"inputs": dict({"baseline": "team"}, **inputs)}))

    def test_record_writes_the_ledger(self):
        self.assertEqual(self.record(["acme/api#4", "acme/api#04", "bad ref", "../x#1"])["ledger"], 1)
        self.record(["acme/api#4", "acme/api#9"], mode="backfill")
        with open(os.path.join(self.f.state, "pr-review-team", "trained.json")) as f:
            d = json.load(f)
        self.assertEqual(sorted(d), ["acme/api#4", "acme/api#9"])
        self.assertEqual(d["acme/api#4"]["by"], ["calibration", "backfill"])

    def test_the_team_baseline_holds_out_what_was_trained(self):
        self.record(["acme/api#4"])
        res = self.pick(repos="acme/api", count="5")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(out["mode"], "team")
        self.assertEqual(out["prs"], ["acme/api#3", "acme/api#5"])
        self.assertEqual((out["held_out"], out["trained"]), (["acme/api#4"], []))
        self.assertEqual([r["memory"] for r in out["runs"]], ["off", "on", "off", "on"])
        self.assertEqual(out["passes"], "1")                                   # the replays' passes, as in memory mode
        # trusted, top-level, with a line; the pack's own post and outsiders left out
        self.assertEqual(out["humans"]["acme/api#3"], [{"path": "handler.go", "line": 10, "by": "carol", "body": "retry has no bound"}])
        self.assertEqual([h["body"] for h in out["humans"]["acme/api#5"]], ["my own hand-written note"])

    def test_a_named_trained_pr_is_refused_unless_flagged(self):
        self.record(["acme/api#4"])
        res = self.pick(prs="acme/api#4")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("was trained on", res.stderr)
        self.assertIn("trained=flag", res.stderr)
        out = json.loads(self.pick(prs="acme/api#4", trained="flag").stdout)
        self.assertEqual((out["prs"], out["trained"]), (["acme/api#4"], ["acme/api#4"]))
        bad = self.pick(prs="acme/api#4", trained="sometimes")
        self.assertNotEqual(bad.returncode, 0)

    def test_eval_reads_github_and_writes_nothing_there(self):
        self.record(["acme/api#4"])
        self.pick(repos="acme/api")
        with open(os.path.join(self.f.root, "gh.log")) as f:
            calls = f.read().splitlines()
        self.assertTrue(calls)
        for c in calls:
            self.assertTrue(c.startswith("api "), c)
            self.assertNotRegex(c, r"(^| )(-X|--method|-f|-F|--field|--raw-field|--input)( |$)", c)
        mem = os.path.join(self.f.state, "memory")
        self.assertFalse(os.path.exists(mem))

    def test_bad_baseline(self):
        res = self.f.run("eval-pick", stdin=json.dumps({"inputs": {"repos": "acme/api", "baseline": "humans"}}))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("baseline=", res.stderr)


class TeamEvalMathTest(unittest.TestCase):
    """recall = engineers' comments matched / all; noise = findings no engineer raised / all findings."""

    @classmethod
    def setUpClass(cls):
        t = load_trigger("review-eval")
        cls.pairs, cls.compare = t["pairs"]["code"], t["compare"]["code"]

    def run_eval(self, judge_p):
        humans = {"acme/api#3": [{"path": "a.go", "line": 10, "body": "retry has no bound", "by": "carol"},
                                 {"path": "db.go", "line": 7, "body": "missing index", "by": "judy"}]}
        pick = {"mode": "team", "humans": humans, "trained": ["acme/api#3"],
                "runs": [{"repo": "acme/api", "pr": 3, "memory": "off"}, {"repo": "acme/api", "pr": 3, "memory": "on"}]}
        off = [{"path": "a.go", "line": 11, "body": "the retry loop never stops"}, {"path": "z.go", "line": 1, "body": "rename"}]
        on = [{"path": "a.go", "line": 10, "body": "retry has no bound"}, {"path": "db.go", "line": 9, "body": "add an index on user_id"},
              {"path": "z.go", "line": 1, "body": "rename"}, {"path": "y.go", "line": 2, "body": "log it", "note": "carol looks"}]
        replay = {"items": [{"findings": off, "notes": 0}, {"findings": on, "notes": 4}]}
        pairs = run_js(self.pairs, {"pick": pick, "replay": replay})
        self.assertEqual(pairs["mode"], "team")
        judge = {"items": [{"same_issue": {"noul": judge_p(x)}} for x in pairs["list"]]}
        return pairs, run_js(self.compare, {"pick": pick, "pairs": pairs, "judge": judge})

    def test_recall_and_noise_with_and_without_memory(self):
        pairs, out = self.run_eval(lambda x: 0.9 if "index" in x["mem_body"] or "never stops" in x["mem_body"] else 0.1)
        self.assertEqual(len(pairs["exact"]), 1)                  # identical text on a.go:10 (memory on)
        row = out["prs"][0]
        self.assertEqual((row["ref"], row["humans"], row["trained"], row["notes"]), ("acme/api#3", 2, True, 4))
        self.assertEqual((row["off"]["matched"], row["off"]["recall"], row["off"]["noise"]), (1, 0.5, 0.5))
        self.assertEqual((row["on"]["matched"], row["on"]["recall"], row["on"]["noise"]), (2, 1.0, 0.5))
        self.assertEqual([m["by"] for m in row["off"]["missed"]], ["judy"])
        self.assertEqual(sorted(e["path"] for e in row["on"]["extra"]), ["y.go", "z.go"])
        t = out["totals"]
        self.assertEqual((t["humans"], t["off"]["recall"], t["on"]["recall"], t["off"]["noise"], t["on"]["noise"]), (2, 0.5, 1.0, 0.5, 0.5))

    def test_overall_recall_pools_comments_not_prs(self):
        humans = {"acme/api#3": [{"path": "a.go", "line": 1, "body": "x", "by": "carol"}],
                  "acme/api#4": [{"path": "b.go", "line": i, "body": "y%d" % i, "by": "judy"} for i in (1, 20, 40)]}
        runs = [{"repo": "acme/api", "pr": n, "memory": m} for n in (3, 4) for m in ("off", "on")]
        pick = {"mode": "team", "humans": humans, "trained": [], "runs": runs}
        hit = [{"path": "a.go", "line": 1, "body": "x"}]
        replay = {"items": [{"findings": hit, "notes": 0}, {"findings": hit, "notes": 1}, {"findings": [], "notes": 0}, {"findings": [], "notes": 1}]}
        pairs = run_js(self.pairs, {"pick": pick, "replay": replay})
        out = run_js(self.compare, {"pick": pick, "pairs": pairs, "judge": {"items": []}})
        self.assertEqual([r["off"]["recall"] for r in out["prs"]], [1, 0])
        self.assertEqual(out["totals"]["off"]["recall"], 0.25)        # 1 of 4 comments, not the mean of 100% and 0%
        self.assertEqual(out["totals"]["on"]["noise"], 0)

    def test_one_finding_matches_one_comment(self):
        humans = {"acme/api#3": [{"path": "a.go", "line": 10, "body": "retry has no bound", "by": "carol"},
                                 {"path": "a.go", "line": 11, "body": "retry has no bound", "by": "judy"}]}
        pick = {"mode": "team", "humans": humans, "trained": [],
                "runs": [{"repo": "acme/api", "pr": 3, "memory": "off"}, {"repo": "acme/api", "pr": 3, "memory": "on"}]}
        one = [{"path": "a.go", "line": 10, "body": "retry has no bound"}]
        pairs = run_js(self.pairs, {"pick": pick, "replay": {"items": [{"findings": one, "notes": 0}, {"findings": one, "notes": 0}]}})
        out = run_js(self.compare, {"pick": pick, "pairs": pairs, "judge": {"items": [{"same_issue": {"noul": 0.9}} for _ in pairs["list"]]}})
        row = out["prs"][0]
        self.assertEqual((row["off"]["matched"], row["off"]["recall"]), (1, 0.5))
        self.assertEqual([m["by"] for m in row["off"]["missed"]], ["judy"])    # the closer line wins the finding

    def test_a_judge_saying_no_matches_nothing(self):
        _, out = self.run_eval(lambda x: 0.2)
        self.assertEqual(out["prs"][0]["off"]["recall"], 0)
        self.assertEqual(out["prs"][0]["on"]["recall"], 0.5)   # the identical-text pair needs no judge

    def test_the_memory_baseline_is_unchanged(self):
        pick = {"mode": "memory", "runs": [{"repo": "acme/api", "pr": 1, "memory": "off"}, {"repo": "acme/api", "pr": 1, "memory": "on"}]}
        replay = {"items": [{"findings": [{"path": "a.go", "line": 1, "body": "x"}]}, {"findings": [{"path": "a.go", "line": 1, "body": "x"}]}]}
        pairs = run_js(self.pairs, {"pick": pick, "replay": replay})
        out = run_js(self.compare, {"pick": pick, "pairs": pairs, "judge": {"items": []}})
        self.assertEqual((out["totals"]["retained"], out["totals"]["verdict"]), (1, "ok"))

    def test_the_report_and_the_calibration_log(self):
        _, cmp = self.run_eval(lambda x: 0.9 if "index" in x["mem_body"] or "never stops" in x["mem_body"] else 0.1)
        f = Fixture()
        try:
            f.audit(post(f.now - T(days=1), "acme/api", 1))
            pick = {"mode": "team", "held_out": ["acme/api#4"], "trained": ["acme/api#3"], "scope_key": ""}
            res = f.run("eval-report", stdin=json.dumps({"pick": pick, "compare": cmp}))
            self.assertEqual(res.returncode, 0, res.stderr)
            out = json.loads(res.stdout)
            self.assertEqual(out["summary"],
                             "review-eval (team baseline): 1 PR(s), 2 comment(s) by trusted engineers; recall 50% without memory, "
                             "100% with; noise 50% without, 50% with; 1 trained-on PR(s) held out; 1 trained-on PR(s) scored anyway, flagged")
            with open(out["path"]) as fh:
                md = fh.read()
            self.assertIn("## acme/api#3 — TRAINED ON (flagged)", md)
            self.assertIn("| recall | 50% | 100% |", md)
            self.assertIn("Missed (an engineer raised it, the reviewer didn't):\n  - db.go:7 — missing index [judy]", md)
            self.assertIn("Held out (trained on): acme/api#4", md)
            rec = f.run("record", stdin=json.dumps({"plan": {}, "write": {}}), MODE="calibration")
            self.assertEqual(rec.returncode, 0, rec.stderr)
            with open(os.path.join(f.state, "pr-review-team", "calibration-log.md")) as fh:
                self.assertIn("Latest review-eval", fh.read())
        finally:
            f.close()

    def test_eval_never_posts_or_writes_memory(self):
        t = load_trigger("review-eval")
        text = json.dumps(t)
        for verb in ("submit_review", "remember", "forget", "handoff", "review-flow"):
            self.assertNotIn(verb, text)
        self.assertEqual(t["replay"]["workflow"], "review-team")
        self.assertEqual(t["replay"]["with"]["prior_comments"], "off")


if __name__ == "__main__":
    unittest.main()
