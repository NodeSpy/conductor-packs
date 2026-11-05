"""One fetch per review, however many reviewers: the lens panel runs
lenses × passes sessions in parallel, and each used to check the PR out
itself — that many fetches of the same head at once, which a forge answers
by timing ssh connections out. review-team now fetches the PR ONCE (the
`snapshot` step, the github connector's pr_snapshot, which returns the tree
as an artifact), and every lens starts from its own copy (`workspace.from`).

    python3 -m unittest discover -s pr-review-team/tests

Needs PyYAML.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_manifest_js import load  # noqa: E402

CHECKOUTS = ("checkout-pr", "branch-off")
FETCHING_VERBS = ("github.pr_snapshot",)


class SharedCheckoutTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.man = load()
        cls.steps = cls.man["workflows"]["review-team"]["steps"]
        cls.by_id = {s.get("id"): s for s in cls.steps}

    def fetching(self):
        """The steps of review-team that fetch the PR: a checkout or a
        pr_snapshot."""
        return [s for s in self.steps
                if s.get("checkout") in CHECKOUTS or s.get("checkout_pr") or s.get("uses") in FETCHING_VERBS]

    def test_the_review_fetches_exactly_once_and_it_is_not_fanned_out(self):
        fetching = self.fetching()
        self.assertEqual([s.get("id") for s in fetching], ["snapshot"],
                         "review-team must fetch the PR once, in the snapshot step")
        snap = fetching[0]
        # one fetch however many lenses and passes: no for_each, no parallel
        self.assertNotIn("for_each", snap)
        self.assertNotIn("parallel", snap)

    def test_the_snapshot_is_the_github_connectors_archive_of_the_pr(self):
        snap = self.by_id["snapshot"]
        self.assertEqual(snap["uses"], "github.pr_snapshot")
        # the PR being reviewed (or replayed): this workflow's inputs
        self.assertEqual(snap["options"], {"repo": "{{.inputs.repo}}", "pr": "{{.inputs.pr}}"})
        # no second connector and no clone setting: the github connector's
        # own auth, and it picks the depth
        connectors = self.man["pack"]["requires"]["connectors"]
        self.assertNotIn("git", connectors)
        self.assertNotIn("tree_depth", self.man["settings"])
        self.assertEqual(connectors["github"], ">=2.5.0")

    def test_nothing_but_the_snapshot_holds_the_tree(self):
        # one verb returns the tree as an artifact: no clone step to store,
        # nothing to discard afterwards
        ids = [s.get("id") for s in self.steps]
        self.assertNotIn("tree", ids)
        self.assertLess(ids.index("snapshot"), ids.index("review"))
        for s in self.steps:
            self.assertNotIn(s.get("uses"), ("blob.put", "github.pr_clone", "github.pr_clone_discard"), s.get("id"))
            self.assertFalse(s.get("hooks"), s.get("id"))

    def test_every_lens_starts_from_its_own_copy_of_the_snapshot(self):
        lens = self.by_id["review"]
        # N reviewers: the panel fans out…
        self.assertEqual(lens.get("for_each"), "{{.lenses.runs}}")
        self.assertTrue(lens.get("parallel"))
        # …from the one stored tree, with no checkout of its own
        self.assertEqual(lens.get("workspace"), {"from": "{{.snapshot.archive}}"})
        self.assertNotIn(lens.get("checkout"), CHECKOUTS)
        self.assertNotIn("checkout_pr", lens)

    def test_no_other_fanned_out_step_fetches(self):
        for s in self.steps:
            if s.get("for_each") or s.get("parallel"):
                self.assertNotIn(s.get("checkout"), CHECKOUTS, s.get("id"))
                self.assertNotIn(s.get("uses"), FETCHING_VERBS, s.get("id"))


if __name__ == "__main__":
    unittest.main()
