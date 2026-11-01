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
        page = [p for p in q.split("&") if p.startswith("page=")][0]
        if path.endswith("/pulls/comments"):
            print(open(os.environ["FAKE_COMMENTS"]).read() if page == "page=1" else "[]"); sys.exit(0)
        api = json.load(open(os.environ["FAKE_API"]))
        if path in api:
            print(json.dumps(api[path]) if page == "page=1" else "[]"); sys.exit(0)
    sys.exit(1)
''')


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


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
        self.assertIn("DROPPED a.go:3 nit: rename", out["reviews"])
        self.assertEqual(out["team_count"], 1)                # bots and the reviewer excluded
        self.assertIn("TEAM bob handler.go", out["team"])
        self.assertIn("[ALSO REVIEWED BY THE BOT", out["team"])
        cache = os.path.join(self.f.state, "pr-review-team")
        self.assertTrue(os.path.isdir(os.path.join(cache, "clones", "acme", "api", ".git")))

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
        self.assertEqual((out["owner_count"], out["owner_prs"]), (3, 1))
        owner = out["owner"]
        self.assertIn("acme/api#1 (reviewed", owner)                         # the pack's review, for context
        self.assertIn("OWNER REVIEW CHANGES_REQUESTED", owner)
        self.assertIn("this breaks the deploy job", owner)
        self.assertRegex(owner, r"OWNER INLINE handler.go:3 \(reply to the pack's comment: pack inline note\) "
                                r"\(\d{4}-\d\d-\d\d\): the nil check here is wrong")
        self.assertIn("OWNER COMMENT", owner)
        self.assertIn("missed the migration", owner)
        self.assertNotIn("OWNER REVIEW APPROVED", owner)                    # the pack's own approval
        self.assertNotIn("OWNER INLINE handler.go:3: pack inline note", owner)   # the pack's own inline comment
        self.assertNotIn("bob's own review", owner)

    def test_before_the_pack_reviewed_is_not_a_catch(self):
        t0, d = self.t0, dt.timedelta
        self.api(extra_reviews=[rv(12, "rev", t0 - d(hours=5), "COMMENTED", "an early note of mine")],
                 inline=[cm(102, "rev", t0 - d(hours=5), "before the review", review=12)],
                 conversation=[{"user": {"login": "rev"}, "created_at": iso(t0 - d(hours=6)), "body": "pre-review chat"}])
        out = self.collect()
        self.assertEqual(out["owner_count"], 0)
        self.assertNotIn("early note", out["owner"])

    def test_a_pack_review_is_not_mistaken_for_the_owner_when_only_time_matches(self):
        # same account, a minute after the pack posted, but a different body: the owner's
        t0, d = self.t0, dt.timedelta
        self.api(extra_reviews=[rv(12, "rev", t0 + d(minutes=1), "COMMENTED", "wait, one more thing")])
        self.assertIn("wait, one more thing", self.collect()["owner"])

    def test_a_dismissed_pack_review_is_reported_once(self):
        self.api(pack_state="DISMISSED")
        out = self.collect()
        self.assertIn("OWNER DISMISSED the pack's review", out["owner"])
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
             "path": "b.go", "body": body, "pull_request_url": "x/pulls/4"},
            {"created_at": iso(n - dt.timedelta(days=41)), "user": {"login": "bob", "type": "User"}, "in_reply_to_id": None,
             "path": "b.go", "body": "lgtm", "pull_request_url": "x/pulls/4"},
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
        self.assertIn("MINE a.go", text)
        self.assertIn("TEAM bob b.go", text)
        self.assertNotIn("posted by the pack", text)
        self.assertIn("my own hand-written note after the pack started", text)

    def test_days_all_reads_the_whole_history(self):
        n = self.f.now
        old = "this is a comment from years ago about the deploy job"
        self.f.set_comments([{"created_at": iso(n - dt.timedelta(days=3000)), "user": {"login": "bob", "type": "User"},
                              "in_reply_to_id": None, "path": "b.go", "body": old, "pull_request_url": "x/pulls/2"}])
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
