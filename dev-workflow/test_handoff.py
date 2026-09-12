#!/usr/bin/env python3
"""Unit tests for handoff.py.

The dev-workflow/ directory has a hyphen, so it is not an importable
package — we insert this file's own directory on sys.path and import the
sibling `handoff` module directly. Run with:

    python3 dev-workflow/test_handoff.py
"""
import hashlib
import os
import sys
import unittest
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import handoff  # noqa: E402


class KeyTests(unittest.TestCase):
    def test_slash_and_literal_percent_2f_differ(self):
        # `feature%2Fx` is a LEGAL git branch name. Encoding `/` without also
        # encoding `%` would collide these two.
        a = handoff.workspace_key("feature/x", "/repo")
        b = handoff.workspace_key("feature%2Fx", "/repo")
        self.assertNotEqual(a, b)
        self.assertEqual(a, "bq-feature%2Fx")
        self.assertEqual(b, "bq-feature%252Fx")

    def test_branch_key_round_trips(self):
        from urllib.parse import unquote
        key = handoff.workspace_key("feature/a-b", "/repo")
        self.assertEqual(unquote(key[len("bq-"):]), "feature/a-b")

    def test_detached_worktrees_sharing_a_basename_differ(self):
        a = handoff.workspace_key(None, "/tmp/a/slot")
        b = handoff.workspace_key(None, "/tmp/b/slot")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("dh-"))

    def test_long_branch_falls_back_to_hash(self):
        branch = "x" * 500
        key = handoff.workspace_key(branch, "/repo")
        self.assertEqual(key, "bh-" + hashlib.sha256(branch.encode()).hexdigest())

    def test_limit_is_measured_on_the_whole_key(self):
        # 198 chars of branch + "bq-" = 201 > 200, so it must hash.
        branch = "y" * 198
        self.assertEqual(len("bq-" + quote(branch, safe="")), 201)
        self.assertTrue(handoff.workspace_key(branch, "/repo").startswith("bh-"))

    def test_a_branch_cannot_impersonate_a_detached_key(self):
        # Compute a real detached key, then make a branch literally named that.
        detached = handoff.workspace_key(None, "/tmp/a/slot")
        impostor = handoff.workspace_key(detached, "/repo")
        self.assertNotEqual(detached, impostor)
        self.assertTrue(impostor.startswith("bq-"))

    def test_path_is_under_local_handoff(self):
        p = handoff.handoff_path("main", "/repo")
        self.assertEqual(p, os.path.join("/repo", ".local", "handoff", "bq-main.md"))


if __name__ == "__main__":
    unittest.main()
