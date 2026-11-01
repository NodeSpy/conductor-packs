"""Scope keys through the binding the pack calls — core's ctx.memory.key
(memory.RenderScopeKey) — on a real conductor build: core's own vectors, the
extra cases, and the pack's `scopekeys` step itself.

    CONDUCTOR_BIN=/path/to/conductor [CONDUCTOR_JS_ENGINE=/path/to/conductor-js] \\
        python3 -m unittest tests.test_scope_key_binding

Skipped without CONDUCTOR_BIN. It runs a throwaway daemon with its own HOME and
XDG directories (nothing of yours is read or written) and fires one manual
trigger. `conductor init` fetches the js engine; CONDUCTOR_JS_ENGINE swaps in a
local engine build (one with ctx.memory.key, until a release has it). Run it
inside a sandbox of your own (e.g. `unshare --user --map-current-user --pid
--fork --kill-child --mount --mount-proc`) if you want the daemon fenced off
further.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "..", "conductor-pack.yaml")
BIN = os.environ.get("CONDUCTOR_BIN", "")
ENGINE = os.environ.get("CONDUCTOR_JS_ENGINE", "")


def js_literal(v):
    # `${` in a config is an env reference; the vectors carry the ${repo}
    # aliases, so every `$` goes in as a js escape
    return json.dumps(v).replace("$", "\\u0024")


def vectors(name):
    with open(os.path.join(HERE, "testdata", name)) as f:
        return json.load(f)


def scopekeys_code(template):
    with open(MANIFEST) as f:
        code = yaml.safe_load(f)["x-templates"]["scopekeys"]["code"]
    return code.replace("${settings.scope_key}", template)


@unittest.skipUnless(BIN, "set CONDUCTOR_BIN to a conductor build with ctx.memory.key")
class BindingTest(unittest.TestCase):
    """One daemon run renders everything; each test checks its part."""

    PACK_PRS = [
        {"ref": "acme/api#1", "facts": {"repo": "acme/api", "owner": "acme", "name": "api", "number": 1, "pr": 1, "issue": 0,
                                        "kind": "review_requested", "inputs": {"repo": "acme/api", "pr": 1}, "labels": ["billing"]}},
        {"ref": "acme/web#2", "facts": {"repo": "acme/web", "owner": "acme", "name": "web", "number": 2, "pr": 2, "issue": 0,
                                        "kind": "review_requested", "inputs": {"repo": "acme/web", "pr": 2}, "labels": []}},
        {"ref": "other/svc#0", "facts": {"repo": "other/svc", "owner": "other", "name": "svc", "number": 0, "pr": 0, "issue": 0,
                                         "kind": "review_requested", "inputs": {"repo": "other/svc", "pr": 0}, "labels": [""]}},
    ]

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="pack-binding-")
        r = cls.root
        env = {"PATH": "/usr/bin:/bin", "HOME": r + "/home", "XDG_STATE_HOME": r + "/state", "XDG_CONFIG_HOME": r + "/cfg",
               "XDG_CACHE_HOME": r + "/cache", "TMPDIR": r + "/tmp", "GOCACHE": r + "/tmp/gocache"}
        for d in ("home", "state", "cfg", "cache", "tmp"):
            os.makedirs(os.path.join(r, d))
        cls.env = env
        cases = {name: [{"t": c["template"], "f": v["facts"]} for c in v["cases"]]
                 for name, v in (("core", vectors("scope_key_vectors.json")), ("extra", vectors("scope_key_vectors_extra.json")))}
        steps = [
            {"id": "core", "run": "js", "code": "const V = %s;\nreturn { got: V.map(function (c) { return ctx.memory.key(c.t, c.f); }) };" % js_literal(cases["core"])},
            {"id": "extra", "run": "js", "code": "const V = %s;\nreturn { got: V.map(function (c) { return ctx.memory.key(c.t, c.f); }) };" % js_literal(cases["extra"])},
            {"id": "collect", "run": "js", "code": "return { prs: %s };" % js_literal(cls.PACK_PRS)},
            {"id": "keys_owner", "run": "js", "code": scopekeys_code("org:{{.owner}}")},
            {"id": "keys_label", "run": "js", "code": scopekeys_code("area:{{index .labels 0}}")},
            {"id": "keys_repo", "run": "js", "code": scopekeys_code("{{.repo}}")},
        ]
        cfg = {"triggers": [{"name": "binding", "on": "manual", "steps": steps},
                            {"name": "review", "on": "manual", "steps": [
                                {"id": "prmeta", "run": "js", "code": "return { labels: [{ name: \"payments\" }], base: \"release-2\" };"},
                                {"id": "key_repo", "run": "js", "code": scopekeys_code("{{.repo}}@{{.base}}")},
                                {"id": "key_area", "run": "js", "code": scopekeys_code("area:{{index .labels 0}}")}]}]}
        cls.config = os.path.join(r, "config.yaml")
        with open(cls.config, "w") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)
        cls.out = {}
        cls.daemon = None
        try:
            cls._run()
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def _c(cls, *args, **kw):
        return subprocess.run([BIN, *args, "--config", cls.config], env=cls.env, capture_output=True, text=True, timeout=300, **kw)

    @classmethod
    def _run(cls):
        res = cls._c("init")
        if res.returncode != 0:
            raise AssertionError("conductor init: " + res.stdout + res.stderr)
        if ENGINE:
            inst = os.path.join(cls.root, "state", "conductor", "plugins", "installed.yaml")
            with open(inst) as f:
                state = yaml.safe_load(f)
            for p in state["plugins"]:
                if p.get("key") == "engines/js":
                    shutil.copyfile(ENGINE, p["path"])
                    with open(p["path"], "rb") as f:
                        p["sha256"] = hashlib.sha256(f.read()).hexdigest()
            with open(inst, "w") as f:
                yaml.safe_dump(state, f, sort_keys=False)
        with open(os.path.join(cls.root, "daemon.log"), "w") as log:
            cls.daemon = subprocess.Popen([BIN, "run", "--config", cls.config], env=cls.env, stdout=log, stderr=subprocess.STDOUT)
        audit = os.path.join(cls.root, "state", "conductor", "audit.jsonl")
        want = {"core", "extra", "collect", "keys_owner", "keys_label", "keys_repo", "prmeta", "key_repo", "key_area"}
        fired = False
        for _ in range(120):
            time.sleep(1)
            if not fired:
                a = cls._c("run", "binding")
                b = cls._c("run", "review", "--input", "repo=acme/api", "--input", "pr=7")
                fired = a.returncode == 0 and b.returncode == 0
                continue
            if os.path.exists(audit):
                with open(audit) as f:
                    for line in f:
                        e = json.loads(line)
                        if e.get("step") in want and (e.get("outputs") is not None or e.get("error")):
                            cls.out[e["step"]] = e.get("outputs") if e.get("outputs") is not None else {"error": e.get("error")}
            if want <= set(cls.out):
                return
        raise AssertionError("the daemon did not run both triggers; got %s\n%s" % (
            sorted(cls.out), open(os.path.join(cls.root, "daemon.log")).read()[-3000:]))

    @classmethod
    def tearDownClass(cls):
        if cls.daemon is not None:
            cls.daemon.terminate()
            try:
                cls.daemon.wait(timeout=20)
            except subprocess.TimeoutExpired:
                cls.daemon.kill()
        shutil.rmtree(cls.root, ignore_errors=True)

    def check(self, name, step, n):
        v = vectors(name)
        got = self.out[step]["got"]
        self.assertEqual(len(got), n)
        for c, g in zip(v["cases"], got):
            if c.get("drop"):
                self.assertEqual((g["ok"], g["key"]), (False, ""), c["template"])
                self.assertTrue(g["reason"], c["template"])
            else:
                self.assertEqual((g["ok"], g["key"]), (True, c["want"]), c["template"])

    def test_cores_vectors(self):
        self.check("scope_key_vectors.json", "core", 66)

    def test_extra_cases(self):
        self.check("scope_key_vectors_extra.json", "extra", 48)

    def test_the_pack_maps_prs_to_keys_through_core(self):
        owner = self.out["keys_owner"]
        self.assertEqual(owner["keys"], {"acme/api#1": "org:acme", "acme/web#2": "org:acme", "other/svc#0": "org:other"})
        self.assertEqual(owner["key_list"], ["org:acme", "org:other"])
        label = self.out["keys_label"]
        # no label, and a blank label: core says the key doesn't apply
        self.assertEqual((label["keys"], label["unkeyed"]), ({"acme/api#1": "area:billing"}, ["acme/web#2", "other/svc#0"]))
        repo = self.out["keys_repo"]
        self.assertEqual(repo["keys"], {"acme/api#1": "acme/api", "acme/web#2": "acme/web", "other/svc#0": "other/svc"})

    def test_a_review_renders_its_key_through_core(self):
        self.assertEqual(self.out["key_repo"], {"key": "acme/api@release-2", "applies": True})
        self.assertEqual(self.out["key_area"], {"key": "area:payments", "applies": True})


if __name__ == "__main__":
    unittest.main()
