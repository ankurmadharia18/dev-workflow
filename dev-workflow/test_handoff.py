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
import tempfile  # noqa: E402


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


def _init_repo(path):
    """A real git repo — ancestry tests need real commits."""
    handoff._git(["init", "--quiet", "-b", "main", path])
    handoff._git(["config", "user.email", "t@example.com"], cwd=path)
    handoff._git(["config", "user.name", "T"], cwd=path)
    return path


def _commit(path, name):
    with open(os.path.join(path, name), "w") as fh:
        fh.write(name)
    handoff._git(["add", "-A"], cwd=path)
    handoff._git(["commit", "--quiet", "-m", name], cwd=path)
    return handoff._git(["rev-parse", "HEAD"], cwd=path).stdout.strip()


class TrailerTests(unittest.TestCase):
    def test_trailer_is_an_html_comment_with_a_full_oid(self):
        oid = "d" * 40
        line = handoff.make_trailer(oid, 3, 2, "2026-09-12T15:20Z")
        self.assertTrue(line.startswith(handoff.TRAILER_PREFIX))
        self.assertIn("HEAD=" + oid, line)
        self.assertIn("dirty=3", line)
        self.assertIn("untracked=2", line)
        self.assertTrue(line.endswith("-->"))

    def test_append_preserves_existing_prose(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "note.md")
            with open(path, "w") as fh:
                fh.write("# note\n\nsome prose\n")
            handoff.append_line(path, "APPENDED")
            with open(path) as fh:
                body = fh.read()
            self.assertIn("some prose", body)
            self.assertTrue(body.endswith("APPENDED\n"))

    def test_append_adds_a_newline_when_the_file_lacks_one(self):
        # Without this, the trailer welds onto the last sentence and the
        # "^<!-- dw-checkpoint " anchor never matches it again.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "note.md")
            with open(path, "w") as fh:
                fh.write("no trailing newline")
            handoff.append_line(path, "TRAILER")
            with open(path) as fh:
                lines = fh.read().splitlines()
            self.assertEqual(lines[-1], "TRAILER")
            self.assertEqual(lines[-2], "no trailing newline")

    def test_append_does_not_double_the_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "note.md")
            with open(path, "w") as fh:
                fh.write("ends with newline\n")
            handoff.append_line(path, "TRAILER")
            with open(path) as fh:
                body = fh.read()
            self.assertNotIn("\n\nTRAILER", body)

    def test_tree_counts_separates_dirty_from_untracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_repo(tmp)
            _commit(tmp, "tracked.txt")
            with open(os.path.join(tmp, "tracked.txt"), "w") as fh:
                fh.write("changed")
            with open(os.path.join(tmp, "new.txt"), "w") as fh:
                fh.write("new")
            dirty, untracked = handoff.tree_counts(tmp)
            self.assertEqual(dirty, 1)
            self.assertEqual(untracked, 1)


if __name__ == "__main__":
    unittest.main()
