"""scripts/calibration.py against a temp state dir, a local git "forge" and a
fake `gh` on PATH — no network, nothing outside a temp dir.

    python3 -m unittest discover -s pr-review-team/tests
"""
import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "calibration.py")

FAKE_GH = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    a = sys.argv[1:]
    with open(os.environ["FAKE_GH_LOG"], "a") as log:
        log.write(" ".join(a) + "\\n")
    if a[:2] == ["api", "user"]:
        print("rev"); sys.exit(0)
    if a[:1] == ["api"]:
        path, _, q = a[1].partition("?")
        page = ([p for p in q.split("&") if p.startswith("page=")] or ["page=1"])[0]
        if path.endswith("/pulls/comments"):
            print(open(os.environ["FAKE_COMMENTS"]).read() if page == "page=1" else "[]"); sys.exit(0)
        api = json.load(open(os.environ["FAKE_API"]))
        parts = path.split("/")
        if len(parts) == 6 and parts[3] == "commits" and parts[5] == "pulls" and path not in api:
            path = "/".join(parts[:4] + ["*", "pulls"])   # one answer for every commit
        if len(parts) == 5 and parts[3] == "commits" and path not in api:
            path = "/".join(parts[:4] + ["*"])
        if path in api:
            print(json.dumps(api[path]) if page == "page=1" else "[]"); sys.exit(0)
    sys.exit(1)
''')


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


FENCED = r"<<<(ev[0-9a-f]{16}) (.*?) \1>>>"


def unfence(text):
    """The evidence with its fences taken off (each item's text, in place)."""
    import re
    return re.sub(FENCED, r"\2", text)


class Fixture:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="calib-test-")
        self.state = os.path.join(self.root, "state")
        self.bin = os.path.join(self.root, "bin")
        os.makedirs(self.state)
        os.makedirs(self.bin)
        gh = os.path.join(self.bin, "gh")
        with open(gh, "w") as f:
            f.write(FAKE_GH)
        os.chmod(gh, 0o755)
        self.comments = os.path.join(self.root, "comments.json")
        self.set_comments([])
        self.api = os.path.join(self.root, "api.json")
        self.set_api({})
        self.now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    def set_comments(self, cs):
        with open(self.comments, "w") as f:
            json.dump(cs, f)

    def set_api(self, paths):
        # a follow-up commit with no PR was pushed straight to the branch by the
        # reviewer's own account (trusted), unless a test says otherwise
        paths = dict({"repos/acme/api/commits/*/pulls": [], "repos/acme/api/commits/*": {"author": {"login": "rev"}}}, **paths)
        with open(self.api, "w") as f:
            json.dump(paths, f)

    def audit(self, *events):
        with open(os.path.join(self.state, "audit.jsonl"), "a") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def forge(self, commits):
        """A bare acme/api.git under <root>/forge; commits = [(message, {path: text}, when)]."""
        work = os.path.join(self.root, "work")
        os.makedirs(work)
        g = lambda *a, env=None: subprocess.run(["git", "-C", work, *a], check=True, capture_output=True, env=env)
        g("init", "-q", "-b", "main")
        for msg, files, when in commits:
            for p, text in files.items():
                with open(os.path.join(work, p), "w") as f:
                    f.write(text)
            g("add", "-A")
            env = dict(os.environ, GIT_AUTHOR_DATE=iso(when), GIT_COMMITTER_DATE=iso(when))
            g("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", msg, env=env)
        bare = os.path.join(self.root, "forge", "acme", "api.git")
        os.makedirs(os.path.dirname(bare))
        subprocess.run(["git", "clone", "-q", "--bare", work, bare], check=True, capture_output=True)

    def run(self, cmd, stdin="{}", **env):
        e = {"PATH": self.bin + ":" + os.environ["PATH"], "HOME": self.root, "STATE_DIR": self.state,
             "GIT_BASE": "file://" + os.path.join(self.root, "forge"), "FAKE_COMMENTS": self.comments, "FAKE_API": self.api,
             "FAKE_GH_LOG": os.path.join(self.root, "gh.log")}
        e.update(env)
        return subprocess.run(["python3", SCRIPT, cmd], input=stdin, capture_output=True, text=True, env=e)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def post(ts, repo, pr, event="APPROVE", comments=()):
    return {"event": "verb", "outcome": "ok", "verb": "submit_review", "ts": iso(ts), "repo": repo, "number": pr,
            "options": {"repo": repo, "pr": pr, "event": event, "body": "looks good", "comments": list(comments)}}


HANDLER = "\n".join(["func handle() error {", "    value, err := store.Load(key)",
                     "    if err != nil { return nil }", "    return process(value)", "}"]) + "\n"
FIXED = "\n".join(["func handle() error {", "    value, loadErr := store.Load(key)",
                   "    if loadErr != nil { return loadErr }", "    return processValue(value)", "}"]) + "\n"


class CollectCalibrationTest(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        n = self.f.now
        self.f.forge([
            ("init", {"README.md": "api\n"}, n - dt.timedelta(days=10)),
            ("add handler (#1)", {"handler.go": HANDLER}, n - dt.timedelta(days=2)),
            ("handler: return the load error", {"handler.go": FIXED}, n - dt.timedelta(days=1)),
        ])
        draft = {"event": "APPROVE", "body": "ok", "comments": [{"path": "a.go", "line": 3, "body": "nit: rename"},
                                                                {"path": "a.go", "line": 9, "body": "check the nil"}]}
        self.f.audit(
            post(n - dt.timedelta(days=3), "acme/api", 1),
            {"event": "step", "step": "review", "ts": iso(n - dt.timedelta(hours=30)), "repo": "acme/api", "number": 2,
             "outputs": {"review": json.dumps(draft)}},
            {"event": "step", "step": "gate", "ts": iso(n - dt.timedelta(hours=30)), "repo": "acme/api", "number": 2,
             "outputs": {"handoff": True}},
            # the human posted it with one comment dropped, via the hand-off agent's skill grant
            dict(post(n - dt.timedelta(hours=29), "acme/api", 2, comments=[draft["comments"][1]]),
                 verb=None, uses="gh.submit_review"),
        )
        self.f.set_comments([
            {"created_at": iso(n - dt.timedelta(hours=5)), "user": {"login": "bob", "type": "User"}, "in_reply_to_id": None,
             "author_association": "MEMBER",
             "path": "handler.go", "body": "the store error is swallowed here", "pull_request_url": "x/pulls/1"},
            {"created_at": iso(n - dt.timedelta(hours=5)), "user": {"login": "ci[bot]", "type": "Bot"}, "in_reply_to_id": None,
             "path": "x", "body": "bot noise", "pull_request_url": "x/pulls/1"},
            {"created_at": iso(n - dt.timedelta(hours=5)), "user": {"login": "rev", "type": "User"}, "in_reply_to_id": None,
             "path": "x", "body": "the reviewer's own account", "pull_request_url": "x/pulls/1"},
        ])

    def tearDown(self):
        self.f.close()

    def test_collects_reviews_followups_handoffs_and_team(self):
        res = self.f.run("collect-calibration")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(out["repos"], ["acme/api"])          # derived from the audit log
        self.assertEqual(out["reviewer"], "rev")              # from `gh api user`
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["flagged"], 1)
        self.assertIn("FOLLOW-UP", out["reviews"])
        self.assertIn("handler: return the load error", out["reviews"])
        self.assertIn("loadErr", out["reviews"])              # the follow-up's patch is inline
        self.assertEqual(out["handoffs"], 1)
        self.assertIn("EDITED before posting", out["reviews"])
        self.assertRegex(out["reviews"], r"DROPPED <<<(ev[0-9a-f]{16}) a.go:3 nit: rename \1>>>")
        self.assertEqual(out["team_count"], 1)                # bots and the reviewer excluded
        self.assertRegex(out["team"], r"TEAM bob <<<(ev[0-9a-f]{16}) handler.go: the store error is swallowed here \1>>>")
        self.assertIn("[ALSO REVIEWED BY THE BOT", out["team"])
        cache = os.path.join(self.f.state, "pr-review-team")
        self.assertTrue(os.path.isdir(os.path.join(cache, "clones", "acme", "api", ".git")))

    def test_every_pr_shown_comes_with_its_facts(self):
        out = json.loads(self.f.run("collect-calibration").stdout)
        refs = [p["ref"] for p in out["prs"]]
        self.assertEqual(refs, ["acme/api#1", "acme/api#2"])   # the reviewed PR (+ its team comment) and the hand-off
        self.assertEqual(out["prs"][0]["facts"], {"repo": "acme/api", "owner": "acme", "name": "api", "number": 1, "pr": 1,
                                                  "issue": 0, "kind": "review_requested",
                                                  "inputs": {"repo": "acme/api", "pr": 1}})
        with open(os.path.join(self.f.root, "gh.log")) as f:
            self.assertNotIn("pulls/1 ", f.read() + " ")         # no PR fetched unless the key needs it

    def test_pr_metadata_is_fetched_when_the_key_reads_it(self):
        self.f.set_api({"repos/acme/api/pulls/1": {"title": "Add handler", "labels": [{"name": "billing"}],
                                                   "base": {"ref": "main"}, "html_url": "https://example.test/acme/api/pull/1"}})
        out = json.loads(self.f.run("collect-calibration", NEED_META="true").stdout)
        f1 = out["prs"][0]["facts"]
        self.assertEqual((f1["title"], f1["labels"], f1["base"], f1["url"]),
                         ("Add handler", ["billing"], "main", "https://example.test/acme/api/pull/1"))
        self.assertNotIn("title", out["prs"][1]["facts"])        # its fetch failed: those facts stay absent

    def test_state_advances_only_when_record_promotes_it(self):
        self.assertEqual(json.loads(self.f.run("collect-calibration").stdout)["count"], 2)
        # no record yet (say the agent failed): the next run sees the same window
        self.assertEqual(json.loads(self.f.run("collect-calibration").stdout)["count"], 2)
        rec = self.f.run("record", stdin=json.dumps({"plan": {"writes": [{"label": "acme/api", "text": "t"}]},
                                                     "write": {"items": [{"id": "m1"}]}}), MODE="calibration")
        self.assertEqual(rec.returncode, 0, rec.stderr)
        self.assertTrue(json.loads(rec.stdout)["promoted"])
        self.f.set_comments([])
        again = json.loads(self.f.run("collect-calibration").stdout)
        self.assertEqual((again["count"], again["handoffs"], again["team_count"]), (0, 0, 0))
        with open(os.path.join(self.f.state, "pr-review-team", "calibration-log.md")) as f:
            log = f.read()
        self.assertIn("- m1 [acme/api] t", log)

    def test_a_malformed_repo_never_reaches_git_gh_or_a_path(self):
        n = self.f.now
        bad = ["../../escape", "acme/..", "./x", "acme/api/extra", "acme/api;rm", "acme api", "https://evil/x"]
        self.f.audit(*[post(n - dt.timedelta(days=1), r, 3) for r in bad])
        self.f.audit({"event": "step", "step": "gate", "ts": iso(n - dt.timedelta(hours=2)), "repo": "../../escape",
                      "number": 4, "outputs": {"handoff": True}})
        cache = os.path.join(self.f.state, "pr-review-team")
        os.makedirs(cache, exist_ok=True)
        with open(os.path.join(cache, "calibration.json"), "w") as f:   # a tampered state file
            json.dump({"last_run": iso(n - dt.timedelta(days=5)),
                       "tracked": {"x#1": {"repo": "../../escape2", "pr": 1, "at": iso(n)[:10]}},
                       "handoffs": {"../../escape3#2": {"at": iso(n - dt.timedelta(days=30))}}}, f)
        res = self.f.run("collect-calibration")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(out["repos"], ["acme/api"])
        for r in bad[:3] + ["../../escape2"]:
            self.assertIn(r, out["invalid_repos"])
        self.assertEqual(sorted(os.listdir(os.path.join(cache, "clones"))), ["acme"])
        self.assertEqual(sorted(os.listdir(self.f.root)), sorted(["state", "bin", "comments.json", "api.json", "work", "forge", "gh.log"]))
        with open(os.path.join(self.f.root, "gh.log")) as f:
            calls = f.read()
        with open(os.path.join(cache, "calibration.next.json")) as f:
            nxt = f.read()
        for text in (calls, nxt, out["reviews"], out["team"]):
            self.assertNotIn("escape", text)
            self.assertNotIn("evil", text)

    def test_without_the_audit_log_it_names_the_grant(self):
        os.remove(os.path.join(self.f.state, "audit.jsonl"))
        res = self.f.run("collect-calibration")
        self.assertEqual(res.returncode, 3)
        self.assertIn("trust: full", res.stderr)


def rv(id_, login, when, state, body):
    return {"id": id_, "user": {"login": login}, "submitted_at": iso(when), "state": state, "body": body}


def cm(id_, login, when, body, review=None, reply_to=None, path="handler.go", line=3):
    return {"id": id_, "user": {"login": login}, "created_at": iso(when), "body": body, "path": path, "line": line,
            "pull_request_review_id": review, "in_reply_to_id": reply_to}


class OwnerCatchTest(unittest.TestCase):
    """Reviews post AS the reviewer's account. What that account wrote by hand on a PR the
    pack reviewed — told apart from the pack's posts by the audit log, not by author — is
    the owner correcting the pack."""

    def setUp(self):
        self.f = Fixture()
        n = self.f.now
        self.t0 = n - dt.timedelta(days=3)
        self.f.forge([
            ("init", {"README.md": "api\n"}, n - dt.timedelta(days=10)),
            ("add handler (#1)", {"handler.go": HANDLER}, n - dt.timedelta(days=2)),
        ])
        self.f.audit(post(self.t0, "acme/api", 1, comments=[{"path": "handler.go", "line": 3, "body": "pack inline note"}]))

    def tearDown(self):
        self.f.close()

    def api(self, pack_state="APPROVED", extra_reviews=(), inline=(), conversation=()):
        t0, d = self.t0, dt.timedelta
        self.f.set_api({
            "repos/acme/api/pulls/1/reviews": [rv(11, "rev", t0 + d(seconds=40), pack_state, "looks good"), *extra_reviews],
            "repos/acme/api/pulls/1/comments": [cm(100, "rev", t0 + d(seconds=40), "pack inline note", review=11), *inline],
            "repos/acme/api/issues/1/comments": list(conversation),
        })

    def collect(self):
        res = self.f.run("collect-calibration")
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_owner_after_the_pack_is_a_catch_and_the_pack_itself_is_not(self):
        t0, d = self.t0, dt.timedelta
        self.api(
            extra_reviews=[rv(12, "rev", t0 + d(days=1), "CHANGES_REQUESTED", "this breaks the deploy job"),
                           rv(13, "bob", t0 + d(days=1), "COMMENTED", "bob's own review"),
                           rv(14, "rev", t0 + d(days=1), "COMMENTED", "")],
            inline=[cm(101, "rev", t0 + d(days=1), "the nil check here is wrong", review=14, reply_to=100)],
            conversation=[{"user": {"login": "rev"}, "created_at": iso(t0 + d(days=2)), "body": "missed the migration"}])
        out = self.collect()
        self.assertEqual((out["owner_count"], out["catch_prs"]), (3, 1))
        owner = out["catches"]
        self.assertIn("acme/api#1 (reviewed", owner)                         # the pack's review, for context
        self.assertRegex(owner, r"CATCH by rev \(owner\) \(\d{4}-\d\d-\d\d\): <<<(ev[0-9a-f]{16}) REVIEW CHANGES_REQUESTED: this breaks the deploy job \1>>>")
        self.assertRegex(owner, r"CATCH by rev \(owner\) \(\d{4}-\d\d-\d\d\): <<<(ev[0-9a-f]{16}) INLINE handler.go:3 "
                                r"\(reply to the pack's comment: pack inline note\): the nil check here is wrong \1>>>")
        self.assertIn("COMMENT: missed the migration", unfence(owner))
        self.assertNotIn("REVIEW APPROVED", owner)                    # the pack's own approval
        self.assertNotIn("INLINE handler.go:3: pack inline note", owner)   # the pack's own inline comment
        self.assertNotIn("bob's own review", owner)

    def test_before_the_pack_reviewed_is_not_a_catch(self):
        t0, d = self.t0, dt.timedelta
        self.api(extra_reviews=[rv(12, "rev", t0 - d(hours=5), "COMMENTED", "an early note of mine")],
                 inline=[cm(102, "rev", t0 - d(hours=5), "before the review", review=12)],
                 conversation=[{"user": {"login": "rev"}, "created_at": iso(t0 - d(hours=6)), "body": "pre-review chat"}])
        out = self.collect()
        self.assertEqual(out["owner_count"], 0)
        self.assertNotIn("early note", out["catches"])

    def test_a_pack_review_is_not_mistaken_for_the_owner_when_only_time_matches(self):
        # same account, a minute after the pack posted, but a different body: the owner's
        t0, d = self.t0, dt.timedelta
        self.api(extra_reviews=[rv(12, "rev", t0 + d(minutes=1), "COMMENTED", "wait, one more thing")])
        self.assertIn("wait, one more thing", self.collect()["catches"])

    def test_the_owner_repeating_the_pack_s_words_later_is_a_catch(self):
        # same account and the same body as the pack's review, but a day later: not the post
        t0, d = self.t0, dt.timedelta
        self.api(extra_reviews=[rv(12, "rev", t0 + d(days=1), "CHANGES_REQUESTED", "looks good")])
        out = self.collect()
        self.assertEqual(out["owner_count"], 1)
        self.assertIn("REVIEW CHANGES_REQUESTED: looks good", unfence(out["catches"]))

    def test_pack_inline_comments_are_known_by_review_or_by_body(self):
        # one carries the pack review's id (its body since edited), one the audited body
        # (its review id lost): neither is the owner's
        t0, d = self.t0, dt.timedelta
        self.api(inline=[cm(101, "rev", t0 + d(seconds=40), "pack inline note, edited", review=11),
                         cm(102, "rev", t0 + d(seconds=40), "pack inline note", review=None, line=4)])
        out = self.collect()
        self.assertEqual(out["owner_count"], 0)
        self.assertNotIn("INLINE", out["catches"])

    def test_a_dismissed_pack_review_is_reported_once(self):
        self.api(pack_state="DISMISSED")
        out = self.collect()
        self.assertIn("CATCH: DISMISSED the pack's review", unfence(out["catches"]))
        rec = self.f.run("record", stdin=json.dumps({"plan": {}, "write": {}}), MODE="calibration")
        self.assertEqual(rec.returncode, 0, rec.stderr)
        self.assertEqual(self.collect()["owner_count"], 0)

    def test_after_the_follow_up_window_is_not_a_catch(self):
        t0, d = self.t0, dt.timedelta
        late = self.f.now + d(days=1)   # merged n-2d; with a 0-day window it closed then
        self.api(conversation=[{"user": {"login": "rev"}, "created_at": iso(late), "body": "much later"}])
        res = self.f.run("collect-calibration", WINDOW_DAYS="0")
        self.assertEqual(json.loads(res.stdout)["owner_count"], 0)

    def test_the_audit_log_rotated_away_means_no_guessing(self):
        # tracked from an earlier run, but no audit record of the pack's post any more
        os.remove(os.path.join(self.f.state, "audit.jsonl"))
        self.f.audit({"event": "step", "step": "x", "ts": iso(self.f.now)})
        cache = os.path.join(self.f.state, "pr-review-team")
        os.makedirs(cache, exist_ok=True)
        with open(os.path.join(cache, "calibration.json"), "w") as f:
            json.dump({"last_run": iso(self.t0 - dt.timedelta(days=1)),
                       "tracked": {"acme/api#1": {"repo": "acme/api", "pr": 1, "at": iso(self.t0)[:10]}}}, f)
        self.api(extra_reviews=[rv(12, "rev", self.t0 + dt.timedelta(days=1), "COMMENTED", "x")])
        self.assertEqual(self.collect()["owner_count"], 0)


class EvalScriptTest(unittest.TestCase):
    """review-eval's deterministic half: which PRs it replays, and the report it writes."""

    def setUp(self):
        self.f = Fixture()
        n = self.f.now
        self.f.audit(post(n - dt.timedelta(days=5), "acme/api", 1), post(n - dt.timedelta(days=4), "acme/api", 2),
                     post(n - dt.timedelta(days=3), "acme/web", 7), post(n - dt.timedelta(days=2), "acme/api", 3),
                     post(n - dt.timedelta(days=1), "other/svc", 9))

    def tearDown(self):
        self.f.close()

    def pick(self, **inputs):
        return self.f.run("eval-pick", stdin=json.dumps({"inputs": inputs}))

    def test_the_most_recent_reviewed_prs_in_the_repos(self):
        res = self.pick(repos="acme/api,acme/web", count="3")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(out["prs"], ["acme/api#3", "acme/web#7", "acme/api#2"])
        self.assertEqual(out["runs"][:3], [{"repo": "acme/api", "pr": 3, "memory": "off", "arm": "A"},
                                           {"repo": "acme/api", "pr": 3, "memory": "off", "arm": "B"},
                                           {"repo": "acme/api", "pr": 3, "memory": "on", "arm": "M"}])
        self.assertEqual((len(out["runs"]), out["scope_key"], out["control"]), (9, "", True))
        self.assertEqual(json.loads(self.pick(repos="acme/api").stdout)["count"], 3)   # default 5, only 3 exist

    def test_named_prs_and_a_scope_key(self):
        out = json.loads(self.pick(prs="acme/api#12, acme/web#3 acme/api#12", scope_key=" org:{{.owner}} ").stdout)
        self.assertEqual((out["prs"], out["scope_key"]), (["acme/api#12", "acme/web#3"], "org:{{.owner}}"))

    def test_bad_inputs_fail_plainly(self):
        for inputs, says in (({}, "name the repos"), ({"repos": "acme/api", "count": "0"}, "count="),
                             ({"repos": "acme/api", "count": "many"}, "count="),
                             ({"prs": "acme/api#x"}, "owner/name#N"), ({"prs": "../x#1"}, "owner/name#N"),
                             ({"repos": "acme/api", "prs": "acme/web#1"}, "is not in"),
                             ({"repos": "nobody/here"}, "names no review")):
            res = self.pick(**inputs)
            self.assertNotEqual(res.returncode, 0, inputs)
            self.assertIn(says, res.stderr, inputs)

    def compare(self):
        return {"totals": {"prs": 1, "notes": 4, "baseline": 4, "with_memory": 4, "retained": 3, "dropped": 1, "added": 1,
                           "added_citing_note": 1, "retention": 0.75, "verdict": "watch"},
                "prs": [{"ref": "acme/api#3", "notes": 4, "baseline": 4, "with_memory": 4, "retention": 0.75,
                         "retained": [{"base": "a.go:1", "memory": "a.go:2", "p": 0.9, "body": "kept"}] * 3,
                         "dropped": [{"path": "b.go", "line": 4, "body": "the dropped one"}],
                         "added": [{"path": "c.go", "line": 9, "body": "the added one", "note": "Deploy job needs"}]}]}

    def test_the_report(self):
        res = self.f.run("eval-report", stdin=json.dumps({"compare": self.compare(), "pick": {"scope_key": ""}}))
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        d = os.path.join(self.f.state, "pr-review-team", "eval")
        self.assertEqual(out["path"], os.path.join(d, self.f.now.strftime("%Y-%m-%d") + ".md"))
        text = open(out["path"]).read()
        for must in ("# review-eval", "## Overall", "| retained | 3 (75%) |", "| added with memory | 1 (1 citing a note) |",
                     "## acme/api#3", "Dropped with memory", "b.go:4 — the dropped one",
                     "c.go:9 — the added one [note: Deploy job needs]", "Target: memory-on retains nearly all baseline findings"):
            self.assertIn(must, text)
        self.assertIn("3 retained with memory (75%)", out["summary"])
        self.assertIn("watch", out["summary"])
        self.assertIn(out["summary"], res.stderr)                       # printed
        latest = json.load(open(os.path.join(d, "latest.json")))
        self.assertEqual((latest["summary"], latest["path"]), (out["summary"], out["path"]))
        again = json.loads(self.f.run("eval-report", stdin=json.dumps({"compare": self.compare()})).stdout)
        self.assertTrue(again["path"].endswith("-2.md"))                # a second run the same day keeps the first

    def test_calibration_logs_an_eval_run_since_its_last_run(self):
        cache = os.path.join(self.f.state, "pr-review-team")
        os.makedirs(cache, exist_ok=True)
        log = os.path.join(cache, "calibration-log.md")
        def calibrate(last_run):
            with open(os.path.join(cache, "calibration.json"), "w") as f:
                json.dump({"last_run": iso(last_run)}, f)
            res = self.f.run("record", stdin=json.dumps({"plan": {}, "write": {}}), MODE="calibration")
            self.assertEqual(res.returncode, 0, res.stderr)
            return open(log).read().split("## ")[-1]
        self.assertNotIn("review-eval", calibrate(self.f.now - dt.timedelta(days=1)))   # no eval yet
        self.f.run("eval-report", stdin=json.dumps({"compare": self.compare()}))
        self.assertIn("Latest review-eval", calibrate(self.f.now - dt.timedelta(days=1)))
        self.assertNotIn("review-eval", calibrate(self.f.now + dt.timedelta(minutes=5)))  # nothing new since


class RepoGuardTest(unittest.TestCase):
    """The innermost guards, called directly: whatever a caller hands them, a
    malformed repo is neither cloned nor sent to gh."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("calibration", SCRIPT)
        self.c = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.c)
        self.root = tempfile.mkdtemp(prefix="calib-guard-")
        self.state = os.path.join(self.root, "a", "b", "state")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_clone_refuses(self):
        with mock.patch.dict(os.environ, {"STATE_DIR": self.state, "GIT_BASE": "file://" + self.root}), \
                mock.patch.object(self.c.subprocess, "run", side_effect=AssertionError("ran a command")):
            for r in ("../../x", "acme/..", "a/b/c", "a b/c"):
                self.assertIsNone(self.c.clone(r, {}))
        self.assertFalse(os.path.exists(os.path.join(self.state, "pr-review-team", "clones")))

    def test_gh_paths_refuse(self):
        with mock.patch.object(self.c, "gh_json", side_effect=AssertionError("called gh")):
            for r in ("../x", "acme/..", "a/b/c"):
                self.assertEqual(list(self.c.human_comments(r, self.c.now(), 3)), [])

    def test_valid_repo(self):
        for r in ("acme/api", "a-b.c/d_e.f", "A/B"):
            self.assertTrue(self.c.valid_repo(r), r)
        for r in ("", "acme", "../x", "acme/..", "./x", "a/b/c", "a b/c", "a/b#1", None, 3):
            self.assertFalse(self.c.valid_repo(r), r)


class CollectBackfillTest(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()

    def tearDown(self):
        self.f.close()

    def test_months_mine_and_team(self):
        n = self.f.now
        body = "please return the error instead of logging it here"
        pack_note = "posted by the pack under the reviewer's name " + body
        self.f.audit(post(n - dt.timedelta(days=10), "acme/api", 9,                   # the pack's first post
                          comments=[{"path": "a.go", "line": 1, "body": pack_note}]))
        self.f.set_comments([
            {"created_at": iso(n - dt.timedelta(days=2)), "user": {"login": "rev", "type": "User"}, "in_reply_to_id": None,
             "path": "a.go", "body": pack_note, "pull_request_url": "x/pulls/9"},
            {"created_at": iso(n - dt.timedelta(days=1)), "user": {"login": "rev", "type": "User"}, "in_reply_to_id": None,
             "path": "a.go", "body": "my own hand-written note after the pack started " + body, "pull_request_url": "x/pulls/9"},
            {"created_at": iso(n - dt.timedelta(days=40)), "user": {"login": "rev", "type": "User"}, "in_reply_to_id": None,
             "path": "a.go", "body": body, "pull_request_url": "x/pulls/3"},
            {"created_at": iso(n - dt.timedelta(days=41)), "user": {"login": "bob", "type": "User"}, "in_reply_to_id": None,
             "author_association": "MEMBER", "path": "b.go", "body": body, "pull_request_url": "x/pulls/4"},
            {"created_at": iso(n - dt.timedelta(days=41)), "user": {"login": "bob", "type": "User"}, "in_reply_to_id": None,
             "author_association": "MEMBER", "path": "b.go", "body": "lgtm", "pull_request_url": "x/pulls/4"},
        ])
        res = self.f.run("collect-backfill", stdin=json.dumps({"inputs": {"repos": "acme/api, bad repo, ../acme, acme/.., acme/api"}}),
                         BACKFILL_DAYS="90")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(out["repos"], ["acme/api"])
        self.assertEqual((out["mine"], out["team"]), (2, 1))   # the pack's own and short comments dropped
        self.assertEqual(out["pack_dropped"], 1)
        self.assertEqual(out["truncated"], [])
        self.assertEqual(out["month_count"], len(out["months"]))
        text = "".join(m["text"] for m in out["months"])
        self.assertIn("MINE a.go", unfence(text))
        self.assertIn("TEAM bob b.go", unfence(text))
        self.assertNotIn("posted by the pack", text)
        self.assertIn("my own hand-written note after the pack started", text)

    def test_pack_era_comments_before_the_audit_log_are_dropped(self):
        # the pack started before the oldest audit record: own comments from then can't be
        # told apart from the pack's, so they are dropped rather than taught as yours
        n = self.f.now
        self.f.audit(post(n - dt.timedelta(days=10), "acme/api", 9))
        mine = {"user": {"login": "rev", "type": "User"}, "in_reply_to_id": None, "path": "a.go"}
        self.f.set_comments([
            dict(mine, created_at=iso(n - dt.timedelta(days=20)), pull_request_url="x/pulls/5",
                 body="written in the pack era before the audit log began, can't be told"),
            dict(mine, created_at=iso(n - dt.timedelta(days=60)), pull_request_url="x/pulls/3",
                 body="written before the pack started, so this one is mine for sure"),
        ])
        res = self.f.run("collect-backfill", stdin=json.dumps({"inputs": {"repos": "acme/api"}}),
                         BACKFILL_DAYS="90", REVIEWER_SINCE=(n - dt.timedelta(days=30)).strftime("%Y-%m-%d"))
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual((out["mine"], out["pack_dropped"]), (1, 1))
        text = "".join(m["text"] for m in out["months"])
        self.assertIn("mine for sure", text)
        self.assertNotIn("can't be told", text)

    def test_days_all_reads_the_whole_history(self):
        n = self.f.now
        old = "this is a comment from years ago about the deploy job"
        self.f.set_comments([{"created_at": iso(n - dt.timedelta(days=3000)), "user": {"login": "bob", "type": "User"},
                              "in_reply_to_id": None, "author_association": "COLLABORATOR", "path": "b.go", "body": old, "pull_request_url": "x/pulls/2"}])
        inp = json.dumps({"inputs": {"repos": "acme/api"}})
        self.assertEqual(json.loads(self.f.run("collect-backfill", stdin=inp, BACKFILL_DAYS="365").stdout)["team"], 0)
        out = json.loads(self.f.run("collect-backfill", stdin=inp, BACKFILL_DAYS="all").stdout)
        self.assertEqual((out["team"], out["since"]), (1, "all"))
        bad = self.f.run("collect-backfill", stdin=inp, BACKFILL_DAYS="forever")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("`all`", bad.stderr)

    def test_the_page_cap_reports_a_truncated_repo(self):
        n = self.f.now
        self.f.set_comments([{"created_at": iso(n - dt.timedelta(hours=i + 1)), "user": {"login": "bob", "type": "User"},
                              "in_reply_to_id": None, "path": "b.go", "body": "comment number %d is long enough" % i,
                              "pull_request_url": "x/pulls/2"} for i in range(100)])
        inp = json.dumps({"inputs": {"repos": "acme/api"}})
        res = self.f.run("collect-backfill", stdin=inp, BACKFILL_DAYS="all", MAX_PAGES="1")
        out = json.loads(res.stdout)
        self.assertEqual(out["truncated"], ["acme/api"])
        self.assertIn("Raise the backfill_max_pages setting", res.stderr)
        # a full page and then an empty one is the end of the history, not truncation
        self.assertEqual(json.loads(self.f.run("collect-backfill", stdin=inp, BACKFILL_DAYS="all", MAX_PAGES="2").stdout)["truncated"], [])

    def test_needs_repos(self):
        res = self.f.run("collect-backfill", stdin=json.dumps({"inputs": {}}))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--input repos=", res.stderr)


if __name__ == "__main__":
    unittest.main()
