#!/usr/bin/env python3
"""Leave a note for the next AI agent working this branch, and read it back.

    handoff.py path                     # print the note's path, creating its directory
    handoff.py checkpoint               # append the git-state trailer
    handoff.py show                     # print the note, then one comparison line

Run it however the framework was installed:

    python3 "$CLAUDE_PLUGIN_ROOT/dev-workflow/handoff.py" show   # plugin install
    python3 dev-workflow/handoff.py show                         # framework checkout

The note itself is free-form Markdown that the agent writes with its own tools.
Nothing here parses the prose. The only structured element is a trailer line
holding the git state an agent cannot reliably self-report.

Standard library only, on purpose: this runs from a plugin cache where no
dependency is guaranteed.
"""
import datetime
import hashlib
import os
import subprocess
import sys
from urllib.parse import quote

KEY_MAX = 200


def _git(args, cwd=None):
    """Run git, turning "git is missing" into a normal non-zero result.

    Callers only ever read `.returncode` and `.stdout`, never raise on this
    call, so a plain `OSError` (its `FileNotFoundError` subclass included,
    raised when the `git` executable itself cannot be found) is folded into
    the same shape instead of escaping as an uncaught exception.
    """
    try:
        return subprocess.run(
            ["git"] + args, capture_output=True, text=True, cwd=cwd
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            args=["git"] + args, returncode=1, stdout="", stderr=str(exc)
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


def _parse_field_int(value, default=0):
    """A trailer field as an int, or `default` when it will not parse.

    The trailer is hand-editable (the agent edits this file with its own
    tools), so `dirty=lots` is plausible. Falling back to `default` rather
    than raising keeps `show` informative instead of crashing on one bad
    field; the missing/malformed count then reads as unchanged from now,
    which undersells a real change but never lies in the other direction.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
    if not oid:
        # An empty HEAD field (a malformed trailer) reads the same as a
        # missing commit, but without the doubled space that "commit %s is"
        # leaves behind when %s is the empty string.
        return "checkpoint commit is missing from this repo"
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


OTHER_HANDOFFS_LIMIT = 5


def _other_handoffs(worktree, current):
    """Up to OTHER_HANDOFFS_LIMIT other notes, newest first.

    A shared `.local` (per `worktree-reset.sh`) can accumulate one note per
    ticket a slot has ever held. Newest-first, capped, keeps the line useful
    instead of dumping the slot's whole history at every session start.
    """
    directory = handoff_dir(worktree)
    if not os.path.isdir(directory):
        return []
    current_name = os.path.basename(current)
    candidates = []
    for name in os.listdir(directory):
        if not name.endswith(".md") or name == current_name:
            continue
        try:
            mtime = os.path.getmtime(os.path.join(directory, name))
        except OSError:
            mtime = 0
        candidates.append((mtime, name))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [name for _, name in candidates[:OTHER_HANDOFFS_LIMIT]]


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
    if head is None:
        # Same case cmd_checkpoint already guards: an unborn HEAD (e.g. right
        # after `git checkout --orphan`). Without this, `compare` reaches
        # `_is_ancestor`, which hands `None` to subprocess and raises.
        sys.stderr.write("ERROR: this repo has no commits yet\n")
        return 1
    print(compare(fields.get("HEAD", ""), head))
    now_dirty, now_untracked = tree_counts()
    changed = dirty_line(
        _parse_field_int(fields.get("dirty")),
        _parse_field_int(fields.get("untracked")),
        now_dirty,
        now_untracked,
    )
    if changed:
        print(changed)
    return 0


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
    if verb in ("--help", "-h"):
        sys.stdout.write(__doc__)
        return 0
    if verb == "path":
        return cmd_path(argv)
    if verb == "checkpoint":
        return cmd_checkpoint(argv)
    if verb == "show":
        return cmd_show(argv)
    sys.stderr.write("ERROR: unknown verb %r\n" % verb)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
