"""One fetch per review, however many reviewers: the lens panel runs
lenses × passes sessions in parallel, and each used to check the PR out
itself — that many fetches of the same head at once, which a forge answers
by timing ssh connections out. review-team now clones the PR ONCE (the
`tree` step, the github connector's pr_clone into a temp directory), stores the
tree as an artifact (`snapshot`, blob.put of the directory), discards the
clone, and every lens starts from its own copy (`workspace.from`).

    python3 -m unittest discover -s pr-review-team/tests

Needs PyYAML.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_manifest_js import load  # noqa: E402

CHECKOUTS = ("checkout-pr", "branch-off")
FETCHING_VERBS = ("github.pr_clone",)


class SharedCheckoutTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.man = load()
        cls.steps = cls.man["workflows"]["review-team"]["steps"]
        cls.by_id = {s.get("id"): s for s in cls.steps}

    def fetching(self):
        """The steps of review-team that fetch the PR: a checkout or a
        pr_clone."""
        return [s for s in self.steps
                if s.get("checkout") in CHECKOUTS or s.get("checkout_pr") or s.get("uses") in FETCHING_VERBS]

    def test_the_review_fetches_exactly_once_and_it_is_not_fanned_out(self):
        fetching = self.fetching()
        self.assertEqual([s.get("id") for s in fetching], ["tree"],
                         "review-team must fetch the PR once, in the tree step")
        tree = fetching[0]
        # one fetch however many lenses and passes: no for_each, no parallel
        self.assertNotIn("for_each", tree)
        self.assertNotIn("parallel", tree)

    def test_the_tree_is_the_github_connectors_clone_of_the_pr(self):
        tree = self.by_id["tree"]
        self.assertEqual(tree["uses"], "github.pr_clone")
        # the PR being reviewed (or replayed): this workflow's inputs
        self.assertEqual(tree["options"], {"repo": "{{.inputs.repo}}", "pr": "{{.inputs.pr}}"})
        # no second connector and no clone setting: the github connector's
        # own auth, and it picks the depth
        connectors = self.man["pack"]["requires"]["connectors"]
        self.assertNotIn("git", connectors)
        self.assertNotIn("tree_depth", self.man["settings"])
        self.assertEqual(connectors["github"], ">=2.5.0")

    def test_the_tree_is_stored_as_one_artifact_and_the_clone_discarded(self):
        snap = self.by_id["snapshot"]
        self.assertEqual(snap["uses"], "blob.put")
        self.assertEqual(snap["options"]["path"], "{{.tree.dir}}")
        ids = [s.get("id") for s in self.steps]
        self.assertLess(ids.index("tree"), ids.index("snapshot"))
        self.assertLess(ids.index("snapshot"), ids.index("review"))
        self.assertIn("blob", self.man["pack"]["requires"]["connectors"])
        # the clone goes whether the store worked or not
        hooks = {h["at"]: h for h in snap["hooks"]}
        for at in ("done", "fail"):
            self.assertEqual(hooks[at]["uses"], "github.pr_clone_discard", at)
            self.assertEqual(hooks[at]["options"], {"dir": "{{.tree.dir}}"}, at)

    def test_every_lens_starts_from_its_own_copy_of_the_snapshot(self):
        lens = self.by_id["review"]
        # N reviewers: the panel fans out…
        self.assertEqual(lens.get("for_each"), "{{.lenses.runs}}")
        self.assertTrue(lens.get("parallel"))
        # …from the one stored tree, with no checkout of its own
        self.assertEqual(lens.get("workspace"), {"from": "{{.snapshot.blob}}"})
        self.assertNotIn(lens.get("checkout"), CHECKOUTS)
        self.assertNotIn("checkout_pr", lens)

    def test_no_other_fanned_out_step_fetches(self):
        for s in self.steps:
            if s.get("for_each") or s.get("parallel"):
                self.assertNotIn(s.get("checkout"), CHECKOUTS, s.get("id"))
                self.assertNotIn(s.get("uses"), FETCHING_VERBS, s.get("id"))


if __name__ == "__main__":
    unittest.main()
