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

    def test_key_of_exactly_200_stays_bq(self):
        # Pins the boundary itself: "> KEY_MAX" and ">= KEY_MAX" both hash a
        # 201-character key, but only ">=" wrongly hashes a 200-character
        # one. "z" is untouched by quote(safe=""), so 197 of them plus the
        # 3-character "bq-" prefix land the key at exactly 200.
        branch = "z" * 197
        key = handoff.workspace_key(branch, "/repo")
        self.assertEqual(len("bq-" + quote(branch, safe="")), 200)
        self.assertTrue(key.startswith("bq-"))

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

    def test_tree_counts_does_not_swap_dirty_and_untracked(self):
        # Symmetric 1/1 above cannot catch a swapped return tuple. Two
        # modified tracked files plus one untracked file pins each count to
        # its own place.
        with tempfile.TemporaryDirectory() as tmp:
            _init_repo(tmp)
            _commit(tmp, "a.txt")
            _commit(tmp, "b.txt")
            with open(os.path.join(tmp, "a.txt"), "w") as fh:
                fh.write("changed a")
            with open(os.path.join(tmp, "b.txt"), "w") as fh:
                fh.write("changed b")
            with open(os.path.join(tmp, "new.txt"), "w") as fh:
                fh.write("new")
            dirty, untracked = handoff.tree_counts(tmp)
            self.assertEqual(dirty, 2)
            self.assertEqual(untracked, 1)


class ShowTests(unittest.TestCase):
    def test_last_trailer_wins(self):
        text = (
            "prose\n"
            + handoff.make_trailer("a" * 40, 0, 0, "2026-09-12T10:00Z")
            + "\nmore prose\n"
            + handoff.make_trailer("b" * 40, 1, 2, "2026-09-12T11:00Z")
            + "\n"
        )
        parsed = handoff.parse_trailer(handoff.last_trailer(text))
        self.assertEqual(parsed["HEAD"], "b" * 40)
        self.assertEqual(parsed["dirty"], "1")
        self.assertEqual(parsed["untracked"], "2")

    def test_no_trailer_returns_none(self):
        self.assertIsNone(handoff.last_trailer("just prose\nand more\n"))

    def test_prose_mentioning_checkpoint_is_not_a_trailer(self):
        self.assertIsNone(handoff.last_trailer("I ran checkpoint: it worked\n"))

    def test_trailer_prefix_mid_sentence_is_not_a_trailer(self):
        # The existing "prose mentioning checkpoint" sample never contains
        # TRAILER_PREFIX itself, so a `startswith` -> `in` regression would
        # slip past it. This line carries the exact prefix, but not at the
        # start of the line, so only an anchored match rejects it.
        text = "Note: " + handoff.TRAILER_PREFIX + "HEAD=" + "a" * 40 + " -->\n"
        self.assertIsNone(handoff.last_trailer(text))

    def test_compare_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_repo(tmp)
            first = _commit(tmp, "a.txt")
            self.assertIn("current", handoff.compare(first, first, cwd=tmp))

    def test_compare_commits_landed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_repo(tmp)
            first = _commit(tmp, "a.txt")
            _commit(tmp, "b.txt")
            head = _commit(tmp, "c.txt")
            line = handoff.compare(first, head, cwd=tmp)
            self.assertIn("2 commits landed", line)

    def test_compare_checkout_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_repo(tmp)
            first = _commit(tmp, "a.txt")
            later = _commit(tmp, "b.txt")
            line = handoff.compare(later, first, cwd=tmp)
            self.assertIn("behind the checkpoint by 1", line)

    def test_compare_diverged_names_rebase(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_repo(tmp)
            base = _commit(tmp, "a.txt")
            handoff._git(["checkout", "--quiet", "-b", "other", base], cwd=tmp)
            side = _commit(tmp, "side.txt")
            handoff._git(["checkout", "--quiet", "main"], cwd=tmp)
            head = _commit(tmp, "main2.txt")
            line = handoff.compare(side, head, cwd=tmp)
            self.assertIn("diverged", line)
            self.assertIn("rebase", line)

    def test_an_oid_naming_a_tree_reports_missing_not_diverged(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_repo(tmp)
            head = _commit(tmp, "a.txt")
            tree = handoff._git(
                ["rev-parse", "HEAD^{tree}"], cwd=tmp
            ).stdout.strip()
            line = handoff.compare(tree, head, cwd=tmp)
            self.assertIn("missing", line)
            self.assertNotIn("diverged", line)

    def test_dirty_line_reports_a_change_even_when_the_commit_matches(self):
        line = handoff.dirty_line(0, 0, 7, 0)
        self.assertIn("was clean", line)
        self.assertIn("7", line)

    def test_dirty_line_is_quiet_when_nothing_changed(self):
        self.assertEqual(handoff.dirty_line(0, 0, 0, 0), "")

    def test_describe_counts_full_string_dirty_only(self):
        # assertIn("7", ...) alone would still pass with the modified/
        # untracked labels swapped. Pin the exact string.
        self.assertEqual(handoff._describe_counts(7, 0), "7 modified")

    def test_describe_counts_full_string_untracked_only(self):
        self.assertEqual(handoff._describe_counts(0, 7), "7 untracked")

    def test_dirty_line_reverse_direction_tree_now_clean(self):
        # The spec names both directions explicitly; only testing "clean ->
        # dirty" leaves "dirty -> clean" uncovered.
        line = handoff.dirty_line(3, 0, 0, 0)
        self.assertEqual(line, "tree was 3 modified, now clean")


class ShowVerbTests(unittest.TestCase):
    """The two cmd_show paths that produce no comparison line at all."""

    def _run_show(self, cwd):
        import io
        import contextlib
        buf = io.StringIO()
        saved = os.getcwd()
        os.chdir(cwd)
        try:
            with contextlib.redirect_stdout(buf):
                handoff.cmd_show(["handoff.py", "show"])
        finally:
            os.chdir(saved)
        return buf.getvalue()

    def test_missing_note_says_so_and_lists_others_without_adopting(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_repo(tmp)
            _commit(tmp, "a.txt")
            os.makedirs(handoff.handoff_dir(tmp))
            with open(os.path.join(handoff.handoff_dir(tmp), "bq-other.md"), "w") as fh:
                fh.write("someone else's note\n")
            out = self._run_show(tmp)
            self.assertIn("no handoff for bq-main", out)
            self.assertIn("bq-other.md", out)
            # Listing is not adopting: the other note's body must not appear.
            self.assertNotIn("someone else's note", out)

    def test_a_note_without_a_trailer_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_repo(tmp)
            _commit(tmp, "a.txt")
            os.makedirs(handoff.handoff_dir(tmp))
            with open(handoff.handoff_path("main", tmp), "w") as fh:
                fh.write("# handoff\n\nprose only.\n")
            out = self._run_show(tmp)
            self.assertIn("prose only.", out)
            self.assertIn("no checkpoint yet", out)

    def test_show_with_a_checkpoint_prints_the_exact_output_lines(self):
        # No existing test drives cmd_show against a note WITH a checkpoint,
        # so the field mapping (HEAD/dirty/untracked -> compare/dirty_line)
        # rested only on manual inspection. This pins the exact lines,
        # including the swapped-count and mid-sentence-prefix mutations.
        with tempfile.TemporaryDirectory() as tmp:
            _init_repo(tmp)
            first = _commit(tmp, "a.txt")
            os.makedirs(handoff.handoff_dir(tmp))
            with open(handoff.handoff_path("main", tmp), "w") as fh:
                fh.write(
                    "# handoff\n\ndoing the thing.\n"
                    + handoff.make_trailer(first, 3, 0, "2026-09-12T10:00Z")
                    + "\n"
                )
            with open(os.path.join(tmp, "new.txt"), "w") as fh:
                fh.write("untracked now")
            out = self._run_show(tmp)
            lines = out.splitlines()
            self.assertEqual(lines[0], "# handoff")
            self.assertEqual(lines[2], "doing the thing.")
            self.assertEqual(lines[4], "checkpoint is current (%s)" % first)
            # 2 untracked, not 1: this fixture (unlike the real repo) has no
            # .gitignore, so the handoff note itself and new.txt both count.
            self.assertEqual(lines[5], "tree was 3 modified, now 2 untracked")


class IgnoreTests(unittest.TestCase):
    def test_local_handoff_is_git_ignored(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = handoff._git(["check-ignore", "-q", ".local/handoff"], cwd=root)
        self.assertEqual(result.returncode, 0, ".local/handoff must be git-ignored")


class WiringTests(unittest.TestCase):
    def _repo_file(self, name):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, name)) as fh:
            return fh.read()

    def test_agents_md_carries_the_handoff_rule(self):
        body = self._repo_file("AGENTS.md")
        self.assertIn("handoff.py show", body)
        self.assertIn("handoff.py checkpoint", body)
        # The mentions above pass even if the executable lines are deleted.
        # AGENTS.md is prose (no shell HANDOFF= assignment), so its
        # "executable wiring" is the literal invocation lines plus the
        # ladder-resolution paragraph that tells the reader how to fill in
        # $HANDOFF -- assert both survive, not just the parenthetical gloss.
        self.assertIn('    python3 "$HANDOFF" show', body)
        self.assertIn('    python3 "$HANDOFF" checkpoint', body)
        self.assertIn(
            "Resolve `$HANDOFF` the way the skills resolve `dw-config.py` — try\n"
            "`$CLAUDE_PLUGIN_ROOT/dev-workflow/handoff.py`, then\n"
            "`$DW_ROOT/dev-workflow/handoff.py`, then `dev-workflow/handoff.py` from a\n"
            "framework checkout.",
            body,
        )

    def test_standup_reads_the_handoff(self):
        body = self._repo_file("skills/standup/SKILL.md")
        self.assertIn("handoff.py show", body)
        # Prove the wiring is executable, not just mentioned in prose: the
        # ladder assignment and the actual invocation must both survive.
        self.assertIn(
            'HANDOFF="${CLAUDE_PLUGIN_ROOT:-${DW_ROOT:-.}}/dev-workflow/handoff.py"',
            body,
        )
        self.assertIn('python3 "$HANDOFF" show', body)

    def test_cleanup_writes_a_checkpoint(self):
        body = self._repo_file("skills/cleanup/SKILL.md")
        self.assertIn("handoff.py checkpoint", body)
        # Same executable-wiring proof as standup, for the checkpoint verb.
        self.assertIn(
            'HANDOFF="${CLAUDE_PLUGIN_ROOT:-${DW_ROOT:-.}}/dev-workflow/handoff.py"',
            body,
        )
        self.assertIn('python3 "$HANDOFF" checkpoint', body)


if __name__ == "__main__":
    unittest.main()
