# Harness-Portable Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a developer stop work in one AI agent and continue it in another on the same machine, by leaving a prose note plus one machine-written line of git state.

**Architecture:** One helper, `dev-workflow/handoff.py`, with three verbs — `path`, `checkpoint`, `show`. The note itself is free-form Markdown the agent writes with its own file tools; nothing parses the prose. The only structured element is an HTML-comment trailer carrying HEAD, dirty and untracked counts, which the helper writes and reads. `show` prints the note plus one factual comparison line, and the agent judges what it means.

**Tech Stack:** Python 3 standard library only. No third-party imports. `unittest`, run as `python3 dev-workflow/test_handoff.py`.

**Spec:** `docs/superpowers/specs/2026-09-10-harness-portable-handoff-design.md` (revision 4)

## Global Constraints

- **Standard library only.** `dw-config.py` needs PyYAML; `handoff.py` must not. No PEP 723 dependency header.
- **Three verbs only:** `path`, `checkpoint`, `show`. Do not add `append`, `init`, `list`, `key`, `status` or `archive`. The agent writes prose with its own tools.
- **Nothing parses the prose.** The only pattern ever matched is a line beginning `<!-- dw-checkpoint `.
- **Keys carry one of three fixed prefixes** the helper always prepends: `bq-`, `bh-`, `dh-`. These keep the forms disjoint and are not optional.
- **The 200-character limit applies to the whole key**, prefix included.
- **Hashes are full sha256 hex**, never truncated.
- **`checkpoint` computes git values itself.** It never accepts a SHA from a caller.
- **Full 40-character OIDs** in the trailer, never abbreviations.
- **Do NOT modify `hooks/session-start.sh`.** It is dependency-free bash and is deliberately out of scope.
- **Do NOT modify `dev-process/scripts/worktree-reset.sh`.** There is no archiving.
- Match the house style of `dev-workflow/dw-config.py`: shebang, module docstring with usage lines, `def main(argv)`, `sys.exit(main(sys.argv))`.
- Match the house test style of `dev-workflow/test_validate.py`: `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))`, then import the sibling module, `unittest`, `unittest.main()`.
- New Python must pass `python3 -m py_compile`.

---

### Task 1: Workspace keys and the `path` verb

**Files:**
- Create: `dev-workflow/handoff.py`
- Create: `dev-workflow/test_handoff.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `workspace_key(branch, worktree) -> str`, `handoff_dir(worktree) -> str`, `handoff_path(branch, worktree) -> str`, `current_branch() -> str|None`, `worktree_root() -> str`, `main(argv) -> int`. Tasks 2 and 3 build on all of these.

- [ ] **Step 1: Write the failing test**

Create `dev-workflow/test_handoff.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 dev-workflow/test_handoff.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'handoff'`.

- [ ] **Step 3: Write the minimal implementation**

Create `dev-workflow/handoff.py`:

```python
#!/usr/bin/env python3
"""Leave a note for the next AI agent working this branch, and read it back.

    handoff.py path                     # print the note's path, creating its directory
    handoff.py checkpoint               # append the git-state trailer
    handoff.py show                     # print the note, then one comparison line

Run it however the framework was installed:

    uv run "$CLAUDE_PLUGIN_ROOT/dev-workflow/handoff.py" show   # plugin install
    python3 dev-workflow/handoff.py show                        # framework checkout

The note itself is free-form Markdown that the agent writes with its own tools.
Nothing here parses the prose. The only structured element is a trailer line
holding the git state an agent cannot reliably self-report.

Standard library only, on purpose: this runs from a plugin cache where no
dependency is guaranteed.
"""
import hashlib
import os
import subprocess
import sys
from urllib.parse import quote

KEY_MAX = 200


def _git(args, cwd=None):
    return subprocess.run(
        ["git"] + args, capture_output=True, text=True, cwd=cwd
    )


def current_branch(cwd=None):
    """The checked-out branch, or None when HEAD is detached."""
    result = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=cwd)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def worktree_root(cwd=None):
    """Absolute path of this worktree's root."""
    result = _git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def workspace_key(branch, worktree):
    """A filesystem-safe key that no other workspace can collide with.

    Three disjoint forms, each carrying a prefix WE prepend:
      bq-<quoted branch>   a branch, when the whole key fits
      bh-<sha256 branch>   a branch, when it does not
      dh-<sha256 path>     detached HEAD

    The prefixes are what keep the forms apart. Without them a branch named
    `dh-<some hash>` could name the same file as a detached worktree whose
    path hashes to that value — no cryptography required, just a `git branch`.
    """
    if branch is None:
        return "dh-" + hashlib.sha256(worktree.encode()).hexdigest()
    key = "bq-" + quote(branch, safe="")
    if len(key) > KEY_MAX:
        return "bh-" + hashlib.sha256(branch.encode()).hexdigest()
    return key


def handoff_dir(worktree):
    return os.path.join(worktree, ".local", "handoff")


def handoff_path(branch, worktree):
    return os.path.join(handoff_dir(worktree), workspace_key(branch, worktree) + ".md")


def _resolve(cwd=None):
    """(branch, worktree, path) for the current repo, or None when not in one."""
    worktree = worktree_root(cwd)
    if worktree is None:
        return None
    branch = current_branch(cwd)
    return branch, worktree, handoff_path(branch, worktree)


def cmd_path(argv):
    resolved = _resolve()
    if resolved is None:
        sys.stderr.write("ERROR: not inside a git work tree\n")
        return 1
    _, worktree, path = resolved
    os.makedirs(handoff_dir(worktree), exist_ok=True)
    print(path)
    return 0


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    verb = argv[1]
    if verb == "path":
        return cmd_path(argv)
    sys.stderr.write("ERROR: unknown verb %r\n" % verb)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 dev-workflow/test_handoff.py`
Expected: PASS — 7 tests, `OK`.

- [ ] **Step 5: Prove the impersonation assertion can fail**

Temporarily change `workspace_key` so the branch form has no prefix:

```python
    key = quote(branch, safe="")          # TEMPORARY — no "bq-"
```

Run: `python3 dev-workflow/test_handoff.py`
Expected: FAIL on `test_a_branch_cannot_impersonate_a_detached_key` and several others.
Then restore the `"bq-" +` prefix and confirm the suite passes again.

- [ ] **Step 6: Confirm the directory is git-ignored and the module compiles**

```bash
python3 -m py_compile dev-workflow/handoff.py
python3 dev-workflow/handoff.py path
git check-ignore -q .local/handoff && echo "ignored"
```
Expected: the path prints, `ignored` prints, and `py_compile` is silent.

- [ ] **Step 7: Commit**

```bash
git add dev-workflow/handoff.py dev-workflow/test_handoff.py
git commit -m "feat(handoff): workspace keys and the path verb

Three disjoint key forms, each carrying a prefix the helper prepends: bq- for a
quoted branch, bh- for a hashed long branch, dh- for a detached worktree. The
prefixes are load-bearing -- without them a branch named dh-<hash> could name
the same file as a detached worktree whose path hashes to that value.

quote(safe='') encodes every unsafe byte including '%', so feature/x and the
legal branch feature%2Fx cannot collide."
```

---

### Task 2: The `checkpoint` verb

**Files:**
- Modify: `dev-workflow/handoff.py`
- Modify: `dev-workflow/test_handoff.py`

**Interfaces:**
- Consumes: `handoff_path`, `handoff_dir`, `_resolve`, `_git` from Task 1.
- Produces: `TRAILER_PREFIX` (str), `make_trailer(oid, dirty, untracked, when) -> str`, `append_line(path, text) -> None`, `tree_counts(cwd) -> (int, int)`, `head_oid(cwd) -> str|None`. Task 3 reads trailers written here.

- [ ] **Step 1: Write the failing test**

Append to `dev-workflow/test_handoff.py`, before the `if __name__` block:

```python
import tempfile  # noqa: E402


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 dev-workflow/test_handoff.py`
Expected: FAIL — `AttributeError: module 'handoff' has no attribute 'make_trailer'`.

- [ ] **Step 3: Write the minimal implementation**

Add `import datetime` to the import block at the top of the file (keep the
imports alphabetical, so it goes before `import hashlib`). Then add the rest
above `cmd_path`:

```python
TRAILER_PREFIX = "<!-- dw-checkpoint "


def make_trailer(oid, dirty, untracked, when):
    return "%sHEAD=%s dirty=%d untracked=%d at=%s -->" % (
        TRAILER_PREFIX,
        oid,
        dirty,
        untracked,
        when,
    )


def append_line(path, text):
    """Append one line, guaranteeing it starts on a line of its own.

    An agent that writes prose without a terminal newline would otherwise
    leave the trailer welded to the end of a sentence, where the anchored
    match in `show` never finds it and the checkpoint silently disappears.
    """
    needs_newline = False
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            needs_newline = fh.read(1) != b"\n"
    with open(path, "a") as fh:
        fh.write(("\n" if needs_newline else "") + text + "\n")


def head_oid(cwd=None):
    result = _git(["rev-parse", "HEAD"], cwd=cwd)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def tree_counts(cwd=None):
    """(modified tracked files, untracked files)."""
    result = _git(["status", "--porcelain", "-uall"], cwd=cwd)
    dirty = untracked = 0
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        if line.startswith("??"):
            untracked += 1
        else:
            dirty += 1
    return dirty, untracked


def cmd_checkpoint(argv):
    resolved = _resolve()
    if resolved is None:
        sys.stderr.write("ERROR: not inside a git work tree\n")
        return 1
    _, worktree, path = resolved
    oid = head_oid()
    if oid is None:
        sys.stderr.write("ERROR: this repo has no commits yet\n")
        return 1
    dirty, untracked = tree_counts()
    when = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    os.makedirs(handoff_dir(worktree), exist_ok=True)
    append_line(path, make_trailer(oid, dirty, untracked, when))
    print(path)
    return 0
```

Then add the verb to `main`, directly after the `path` branch:

```python
    if verb == "checkpoint":
        return cmd_checkpoint(argv)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 dev-workflow/test_handoff.py`
Expected: PASS — 12 tests, `OK`.

- [ ] **Step 5: Prove the newline assertion can fail**

Temporarily simplify `append_line` to ignore the missing newline:

```python
    with open(path, "a") as fh:           # TEMPORARY
        fh.write(text + "\n")
```

Run: `python3 dev-workflow/test_handoff.py`
Expected: FAIL on `test_append_adds_a_newline_when_the_file_lacks_one`.
Restore the real implementation and confirm the suite passes.

- [ ] **Step 6: Commit**

```bash
git add dev-workflow/handoff.py dev-workflow/test_handoff.py
git commit -m "feat(handoff): the checkpoint verb

Appends one HTML-comment trailer carrying the full 40-char HEAD oid plus dirty
and untracked counts -- the facts an agent cannot reliably self-report. It
computes them itself rather than accepting them, so no caller can record a
wrong SHA.

append_line guarantees the trailer starts its own line. Without that, prose
written without a terminal newline welds the trailer onto a sentence, where the
anchored match never finds it and the checkpoint silently vanishes."
```

---

### Task 3: The `show` verb

**Files:**
- Modify: `dev-workflow/handoff.py`
- Modify: `dev-workflow/test_handoff.py`

**Interfaces:**
- Consumes: everything from Tasks 1 and 2.
- Produces: `last_trailer(text) -> str|None`, `parse_trailer(line) -> dict`, `is_commit(oid, cwd) -> bool`, `compare(oid, head, cwd) -> str`, `dirty_line(was_dirty, was_untracked, now_dirty, now_untracked) -> str`. Task 4 calls the verb, not these.

- [ ] **Step 1: Write the failing test**

Append to `dev-workflow/test_handoff.py`, before the `if __name__` block:

```python
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


class IgnoreTests(unittest.TestCase):
    def test_local_handoff_is_git_ignored(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = handoff._git(["check-ignore", "-q", ".local/handoff"], cwd=root)
        self.assertEqual(result.returncode, 0, ".local/handoff must be git-ignored")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 dev-workflow/test_handoff.py`
Expected: FAIL — `AttributeError: module 'handoff' has no attribute 'last_trailer'`.

- [ ] **Step 3: Write the minimal implementation**

Add to `dev-workflow/handoff.py`, above `cmd_path`:

```python
def last_trailer(text):
    """The last line that starts with the trailer prefix, or None.

    Anchored at line start on purpose: prose that merely mentions the word
    checkpoint is not a trailer.
    """
    found = None
    for line in text.splitlines():
        if line.startswith(TRAILER_PREFIX):
            found = line
    return found


def parse_trailer(line):
    body = line[len(TRAILER_PREFIX):]
    if body.endswith("-->"):
        body = body[:-3]
    fields = {}
    for token in body.split():
        if "=" in token:
            name, value = token.split("=", 1)
            fields[name] = value
    return fields


def is_commit(oid, cwd=None):
    # ^{commit} matters: an oid can name a tree or a blob.
    return _git(["rev-parse", "--verify", "--quiet", oid + "^{commit}"], cwd=cwd).returncode == 0


def _is_ancestor(older, newer, cwd=None):
    return _git(["merge-base", "--is-ancestor", older, newer], cwd=cwd).returncode == 0


def _count_between(older, newer, cwd=None):
    result = _git(["rev-list", "--count", "%s..%s" % (older, newer)], cwd=cwd)
    return result.stdout.strip() or "0"


def compare(oid, head, cwd=None):
    """One factual line. The agent decides what it means."""
    if not is_commit(oid, cwd=cwd):
        return "checkpoint commit %s is missing from this repo" % oid
    if oid == head:
        return "checkpoint is current (%s)" % oid
    if _is_ancestor(oid, head, cwd=cwd):
        return "%s commits landed since the checkpoint" % _count_between(oid, head, cwd=cwd)
    if _is_ancestor(head, oid, cwd=cwd):
        return "the checkout is behind the checkpoint by %s commits" % _count_between(
            head, oid, cwd=cwd
        )
    return (
        "history diverged since the checkpoint — a rebase or amend looks like "
        "this too, check before trusting the note"
    )


def _describe_counts(dirty, untracked):
    if dirty == 0 and untracked == 0:
        return "clean"
    parts = []
    if dirty:
        parts.append("%d modified" % dirty)
    if untracked:
        parts.append("%d untracked" % untracked)
    return ", ".join(parts)


def dirty_line(was_dirty, was_untracked, now_dirty, now_untracked):
    """Reported alongside the commit state, never folded into it."""
    if (was_dirty, was_untracked) == (now_dirty, now_untracked):
        return ""
    return "tree was %s, now %s" % (
        _describe_counts(was_dirty, was_untracked),
        _describe_counts(now_dirty, now_untracked),
    )


def _other_handoffs(worktree, current):
    directory = handoff_dir(worktree)
    if not os.path.isdir(directory):
        return []
    return sorted(
        name for name in os.listdir(directory)
        if name.endswith(".md") and name != os.path.basename(current)
    )


def cmd_show(argv):
    resolved = _resolve()
    if resolved is None:
        sys.stderr.write("ERROR: not inside a git work tree\n")
        return 1
    branch, worktree, path = resolved
    key = workspace_key(branch, worktree)

    if not os.path.exists(path):
        print("no handoff for %s" % key)
        others = _other_handoffs(worktree, path)
        if others:
            # List, never adopt. A rename leaves an orphan and guessing which
            # orphan belongs to this work is not the helper's call.
            print("other handoffs present: %s" % ", ".join(others))
        return 0

    with open(path) as fh:
        body = fh.read()
    sys.stdout.write(body)
    if not body.endswith("\n"):
        print()

    trailer = last_trailer(body)
    if trailer is None:
        print("handoff has no checkpoint yet")
        return 0

    fields = parse_trailer(trailer)
    head = head_oid()
    print(compare(fields.get("HEAD", ""), head))
    now_dirty, now_untracked = tree_counts()
    changed = dirty_line(
        int(fields.get("dirty", 0)),
        int(fields.get("untracked", 0)),
        now_dirty,
        now_untracked,
    )
    if changed:
        print(changed)
    return 0
```

Then add the verb to `main`, after the `checkpoint` branch:

```python
    if verb == "show":
        return cmd_show(argv)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 dev-workflow/test_handoff.py`
Expected: PASS — 25 tests, `OK`.

- [ ] **Step 5: Prove the tree-vs-commit assertion can fail**

Temporarily drop the `^{commit}` suffix:

```python
    return _git(["rev-parse", "--verify", "--quiet", oid], cwd=cwd).returncode == 0
```

Run: `python3 dev-workflow/test_handoff.py`
Expected: FAIL on `test_an_oid_naming_a_tree_reports_missing_not_diverged` — the tree oid verifies, so it falls through to `diverged`.
Restore `^{commit}` and confirm the suite passes.

- [ ] **Step 6: Exercise the whole helper by hand**

```bash
python3 dev-workflow/handoff.py show           # -> "no handoff for bq-..."
printf '# handoff\n\ntrying the helper.\n' > "$(python3 dev-workflow/handoff.py path)"
python3 dev-workflow/handoff.py checkpoint
python3 dev-workflow/handoff.py show           # -> the note, then "checkpoint is current (...)"
```
Expected: exactly that sequence. Then delete the scratch note — it is git-ignored, so it will not appear in `git status`.

- [ ] **Step 7: Commit**

```bash
git add dev-workflow/handoff.py dev-workflow/test_handoff.py
git commit -m "feat(handoff): the show verb

Prints the note, then ONE factual comparison line -- current, commits landed,
checkout behind, diverged, or missing commit -- plus a dirty-state line when the
tree moved even though the commit did not. The agent judges what it means, which
is why there is no state machine.

Commits are verified with ^{commit}, because an oid can name a tree or a blob
and a tree would otherwise fall through and report as diverged.

A missing note lists the other handoffs present but never adopts one: after a
rename, guessing which orphan belongs to this work is not the helper's call."
```

---

### Task 4: Wire the skills and `AGENTS.md`

**Files:**
- Modify: `AGENTS.md`
- Modify: `skills/standup/SKILL.md`
- Modify: `skills/cleanup/SKILL.md`
- Modify: `dev-workflow/test_handoff.py`

**Interfaces:**
- Consumes: the three verbs from Tasks 1-3.
- Produces: nothing code-facing.

**Note on the prerequisite:** this task assumes PR #10 has landed, which makes `AGENTS.md` canonical (with `CLAUDE.md` reduced to `@AGENTS.md`) and adds the `$DW_ROOT` rung. If `CLAUDE.md` is still a full copy when you start, stop and say so rather than editing both files.

- [ ] **Step 1: Write the failing test**

Append to `dev-workflow/test_handoff.py`, before the `if __name__` block:

```python
class WiringTests(unittest.TestCase):
    def _repo_file(self, name):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, name)) as fh:
            return fh.read()

    def test_agents_md_carries_the_handoff_rule(self):
        body = self._repo_file("AGENTS.md")
        self.assertIn("handoff.py show", body)
        self.assertIn("handoff.py checkpoint", body)

    def test_standup_reads_the_handoff(self):
        self.assertIn("handoff.py show", self._repo_file("skills/standup/SKILL.md"))

    def test_cleanup_writes_a_checkpoint(self):
        self.assertIn(
            "handoff.py checkpoint", self._repo_file("skills/cleanup/SKILL.md")
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 dev-workflow/test_handoff.py`
Expected: FAIL on all three `WiringTests` — the strings are absent.

- [ ] **Step 3: Add the rule to `AGENTS.md`**

Add this section to `AGENTS.md`, immediately before its `## Tests` section:

```markdown
## Handing off between agents

When a session limit forces a switch between Claude Code and Codex, the next
agent needs the reasoning this one has. Git and the tracker already carry the
commits and the ticket state; they do not carry why.

**At the start of a session**, run the helper and read what it prints:

    python3 "$HANDOFF" show

**Before you stop, and at each real decision**, append plain Markdown to the
note — what you are doing, what you decided and why, what is next, what is
blocked — then record the git state:

    python3 "$HANDOFF" checkpoint

Write the note for a reader with none of your context. Prose, not a format:
nothing parses it.

Resolve `$HANDOFF` the way the skills resolve `dw-config.py` — try
`$CLAUDE_PLUGIN_ROOT/dev-workflow/handoff.py`, then
`$DW_ROOT/dev-workflow/handoff.py`, then `dev-workflow/handoff.py` from a
framework checkout. `python3 "$HANDOFF" path` prints the note's location.
```

- [ ] **Step 4: Wire `/standup`**

In `skills/standup/SKILL.md`, add this immediately before the section that reports the board brief:

```markdown
### Read the handoff first

Before the board, check whether the previous session left a note:

```bash
HANDOFF="${CLAUDE_PLUGIN_ROOT:-${DW_ROOT:-.}}/dev-workflow/handoff.py"
python3 "$HANDOFF" show
```

If it prints a note, summarise it in one or two lines at the top of the brief —
what was in flight and what is next — and say plainly how far the checkpoint sits
from HEAD. If it prints `no handoff for …`, say nothing about it.
```

- [ ] **Step 5: Wire `/cleanup`**

In `skills/cleanup/SKILL.md`, add this to the step that opens or updates the PR, after the PR is opened:

```markdown
Record a checkpoint so a session resuming on this branch knows where the PR got to:

```bash
HANDOFF="${CLAUDE_PLUGIN_ROOT:-${DW_ROOT:-.}}/dev-workflow/handoff.py"
python3 "$HANDOFF" checkpoint
```

Append one line of prose to the note first if anything is still outstanding —
review comments expected, CI still running, a follow-up already known.
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `python3 dev-workflow/test_handoff.py`
Expected: PASS — 28 tests, `OK`.

- [ ] **Step 7: Run the whole repo suite**

```bash
python3 dev-workflow/test_validate.py
python3 dev-workflow/test_queue_count.py
python3 dev-workflow/test_dw_config.py
python3 skills/ticket-loop/orchestrator/test_orch.py
python3 dev-workflow/test_handoff.py
bash skills/test_root_ladder.sh
bash skills/test_harness_guard.sh
```
Expected: every one exits 0.

- [ ] **Step 8: Commit**

```bash
git add AGENTS.md skills/standup/SKILL.md skills/cleanup/SKILL.md dev-workflow/test_handoff.py
git commit -m "feat(handoff): wire the rule into AGENTS.md, standup and cleanup

AGENTS.md carries the rule, so every harness that reads its instruction file
picks it up -- show at session start, checkpoint before stopping. /standup reads
the note beside the board. /cleanup records a checkpoint when it opens the PR.

The SessionStart hook is deliberately untouched: it is dependency-free bash, and
knowing which note file to test would mean reimplementing percent-encoding,
sha256 and the detached-HEAD rule in shell."
```

---

### Task 5: End-to-end verification across both harnesses

This task changes no files. It is the check no unit test can make.

**Files:** none.

**Interfaces:**
- Consumes: everything above.
- Produces: a recorded result. If a step fails, report it — do not patch around it.

- [ ] **Step 1: Write a note in Claude Code**

In a Claude Code session in this repo, append two or three lines of real prose to the note and run `checkpoint`. Confirm `show` prints the note and `checkpoint is current (...)`.

- [ ] **Step 2: Read it from Codex — the direction with no native support**

```bash
codex plugin add dev-workflow@dev-workflow
cd /Users/rajverma/repos/aws/dev-workflow
codex exec --sandbox read-only "Run: python3 dev-workflow/handoff.py show — then tell me in two lines what the previous session was doing and what is next. Do not edit anything." < /dev/null
```
Expected: Codex reports the work state without you re-explaining it. **This is the whole point of the feature** — `claude import codex` carries config only, so nothing else moves state this way.

- [ ] **Step 3: Confirm the checkpoint reacts to real movement**

Make a commit, then run `show` again.
Expected: `1 commits landed since the checkpoint`.

- [ ] **Step 4: Confirm the dirty line appears**

Modify a tracked file without committing, then run `show`.
Expected: the commit line still reads current, and a second line reports the tree changed.

- [ ] **Step 5: Confirm a fresh branch starts clean**

```bash
git checkout -b scratch/handoff-check
python3 dev-workflow/handoff.py show
git checkout -   && git branch -D scratch/handoff-check
```
Expected: `no handoff for bq-scratch%2Fhandoff-check`, plus the other handoff listed — not the previous branch's note silently adopted.

- [ ] **Step 6: Report**

Record each step's outcome. Delete any scratch notes — they are git-ignored, so the tree stays clean either way.

---

## Notes for the executor

- `DW` in the existing skills is a config-reader **command**; `$HANDOFF` here is a **file path**. Do not merge them.
- Do not add verbs. Three is the design, and `append` in particular would invite the record format to grow back — that is what sank three earlier revisions.
- If a test seems to demand a parser for the prose, re-read the spec: nothing parses prose. The only pattern ever matched is a line starting `<!-- dw-checkpoint `.
- Task 4 depends on PR #10 having landed. Check `CLAUDE.md` is the single line `@AGENTS.md` before editing instruction files.
