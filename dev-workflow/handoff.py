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
