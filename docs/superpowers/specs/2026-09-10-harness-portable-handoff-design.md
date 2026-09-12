# Harness-portable handoff — design

**Date:** 2026-09-10. Revision 4, 2026-09-12. Rewritten small.
**Status:** approved design, pre-implementation
**Prerequisite:** PR #10 (Codex support + `AGENTS.md` canonical). Land it first.
**Files touched:** new `dev-workflow/handoff.py`, new `dev-workflow/test_handoff.py`,
`AGENTS.md`, `skills/standup/SKILL.md`, `skills/cleanup/SKILL.md`

## Summary

Let a developer stop work in one AI agent and continue it in another on the same
machine, when a session or token limit forces the switch.

The handoff is **a prose note plus one machine-written line of git state.** A
small helper writes the git line and prints the note. Nothing parses the prose.

## Why this revision is small

Revisions 1, 2 and 3 were rejected. Every finding across all three — colliding
keys, colliding ids, an ambiguous parser, undefined state precedence, an archive
verb that could not know what to archive — came from one decision: treating the
file as a machine-parsed data structure with ids, entry kinds and a state
machine.

The consumer is an LLM. It reads prose natively. The apparatus existed for a
reader that never needed it.

So this revision deletes the apparatus rather than fixing it. No entry grammar,
no ids, no `resolve`, no eight-state classifier, no archive verb. What remains is
the part that was never in question across three reviews: leave a note, read it
next session.

The reviewer's verdict on revision 3 named the risk this avoids: a design that
promises collision-free atomic parseable records, but can deliver partial or
ambiguous state, "may confidently report wrong context, worse than 'not found'."
A prose note makes no such promise.

## Goal

A developer hits a limit in one agent, opens the other **on the same machine, in
the same checkout**, and learns where the work stands without retyping it.

## Non-goals

- No conversation replay, and no transcript parsing. Codex `/import` already
  moves Claude sessions natively in one direction; nothing does the reverse, and
  this design does not try.
- No cross-machine support. The file is git-ignored and local.
- No support beyond Claude Code and Codex.
- **No machine-readable record format.** This is the point of the revision.

## The file

**Path:** `.local/handoff/<key>.md`, git-ignored (`.gitignore:6`).

**Key:** one of three forms, each carrying a fixed prefix the helper adds:

| Form | When |
|---|---|
| `bq-<quote(branch, safe="")>` | on a branch, and the whole key is ≤ 200 characters |
| `bh-<full sha256 hex of the branch name>` | on a branch, when `bq-…` would exceed 200 characters |
| `dh-<full sha256 hex of the absolute worktree path>` | detached HEAD |

Per-branch, because `dev-process/scripts/worktree-reset.sh:104` shares `.local`
across worktree slots, so one file would let parallel slots overwrite each other.

`quote(safe="")` encodes every unsafe byte including `%`, which is what makes it
injective — `feature/x` gives `feature%2Fx` and the legal branch `feature%2Fx`
gives `feature%252Fx`.

**The three prefixes are what keep the forms disjoint, and they are not
optional.** An earlier draft used a bare `detached-` and `long-` prefix, which a
branch could impersonate: someone computes the hash of a worktree path, creates a
branch literally named `detached-<that hash>`, and both resolve to the same file.
No cryptography is needed to do it. Because the helper prepends `bq-` to every
branch-derived key, a branch named `dh-abc` becomes `bq-dh-abc` and cannot reach
the `dh-` namespace. The 200-character limit applies to the **whole** key,
prefix included.

The hashes are full sha256, not truncated. Truncation buys shorter filenames and
costs collision resistance, and nothing here needs short filenames.

**Content:** free-form Markdown, written by the agent. Whatever it judges the
next session needs — what it is doing, what it decided and why, what is next,
what is blocked. No required structure, and nothing parses it.

**The one structured element** is a trailer the helper appends:

```
<!-- dw-checkpoint HEAD=d3b017e4f1a9c2b8e5d7061a3f4c9b2e8d05c6a1 dirty=0 untracked=0 at=2026-09-12T15:20Z -->
```

An HTML comment, so it stays invisible in rendered Markdown. The helper reads the
**last** line in the file matching `^<!-- dw-checkpoint `. Prose containing that
exact prefix at the start of a line is the only way to confuse it, which is
acceptable for a scratch file the agent itself writes.

**The trailer must begin on its own line.** An agent that appends prose without a
terminal newline would otherwise leave the trailer welded to the end of a
sentence, where the `^` anchor never matches it and the checkpoint silently
disappears. `checkpoint` therefore writes a leading newline whenever the file is
non-empty and does not already end in one.

The helper records the git state because that is the one thing an agent cannot
reliably self-report. Everything else it can simply write down.

## The helper — `dev-workflow/handoff.py`

Three verbs. Standard library only, `python3 -m py_compile` clean, a
`test_handoff.py` beside it, matching `dw-config.py` and `validate.py`.

| Verb | Does |
|---|---|
| `path` | Print the handoff path for the current worktree, creating the directory |
| `checkpoint` | Append the trailer, computing HEAD, dirty and untracked itself |
| `show` | Print the note, then one line comparing the last checkpoint to now |

The agent appends prose with its own file tools. It does not need a verb for
that, and adding one would only invite a format to grow back.

`checkpoint` computes the git values rather than accepting them, so no caller can
record a wrong SHA. It stores the full 40-character OID.

### What `show` reports

The note, then exactly one comparison line:

| Situation | Line |
|---|---|
| No handoff file | `no handoff for <key>` — then the names of any other handoffs present, without adopting one |
| No trailer yet | `handoff has no checkpoint yet` |
| Recorded OID is not a commit here | `checkpoint commit <oid> is missing from this repo` |
| Same commit | `checkpoint is current (<oid>)` |
| Recorded is an ancestor of HEAD | `<n> commits landed since the checkpoint` |
| HEAD is an ancestor of recorded | `the checkout is behind the checkpoint by <n> commits` |
| Neither is an ancestor | `history diverged since the checkpoint — a rebase or amend looks like this too, check before trusting the note` |

Dirty counts are reported alongside, never folded in: `tree was clean, now 7
modified` or `tree was 3 modified, now clean`.

Commits are verified with `git rev-parse --verify <oid>^{commit}`, because an OID
can name a tree or a blob. The agent reads these lines and judges what they mean.
That is what agents are good at, and it is why there is no state machine.

## Who writes, who reads

**Writes.** The agent, at decision points and before it stops: append prose, then
run `checkpoint`. The rule lives in `AGENTS.md`, which every harness reads at
session start. `/cleanup` runs `checkpoint` when it opens the PR.

**Reads.** `AGENTS.md` tells the agent to run `handoff show` at session start.
`/standup` runs it and reports beside the board.

**The SessionStart hook is not touched at all.** It is dependency-free bash by
design (`hooks/session-start.sh:15`), so it cannot call the helper. An earlier
draft had it do a cheap `[ -f ]` test instead — but knowing *which* file to test
means reimplementing percent-encoding, sha256 and the detached-HEAD rule in bash,
which is the duplication this whole revision exists to avoid. The `AGENTS.md`
rule and `/standup` both already run `show`, so the hint bought nothing.

`hooks/session-start.sh` therefore leaves the files-touched list.

**Locating the helper** uses the same ladder the skills already use for
`dw-config.py`, with `handoff.py` substituted for it: `$CLAUDE_PLUGIN_ROOT`, then
`$DW_ROOT` (added by PR #10), then the framework checkout. There is no
`handoff` on PATH, so that rung does not apply.

## Staleness, deletion, orphans

**No archive verb.** The file is git-ignored scratch. A stale note is a stale
note, not corruption, and `show` always says how far the checkpoint is from HEAD.

**A new branch means a new key**, so a fresh branch in a slot starts with no
handoff rather than inheriting the last ticket's. That is the behaviour we want,
and it falls out of the key rather than needing a sweep.

**Orphans after a rename** are listed by `show`, never adopted. Adoption is the
developer renaming the file, which needs no code.

## Risks

- **An agent can be killed before it writes anything.** Then there is no note.
  Work is not lost, because work is committed and the board holds ticket state.
  Closing this fully would need transcript parsing, a non-goal.
- **An agent can write a poor note.** Nothing validates prose quality. The
  mitigation is the rule in `AGENTS.md` saying what to include, not a schema.
- **`.local` is shared across slots only when the worktree manifest includes it.**
  Where it is not, a note written in one slot is invisible in another. Per-key
  naming makes that "not found", never the wrong file.

## Test plan

`dev-workflow/test_handoff.py`, run as `python3 dev-workflow/test_handoff.py`.

1. `feature/x` and the legal branch `feature%2Fx` produce different keys, and a
   `bq-` key round-trips to its branch name.
2. Two detached worktrees sharing a basename produce different keys.
3. A branch whose key would exceed 200 characters falls back to `bh-`, and the
   limit is measured on the whole key including its prefix.
4. A branch literally named `dh-<hash>` or `bh-<hash>` cannot collide with a
   detached or hashed key — it lands in the `bq-` namespace.
5. `checkpoint` appends without disturbing existing prose, and `show` reads the
   **last** trailer when several exist.
6. `checkpoint` writes a leading newline when the file does not end in one, so
   the trailer always starts its own line and stays matchable.
7. Each comparison line is produced by its situation: current, commits landed,
   checkout behind, diverged, missing commit, no trailer, no file.
8. An OID naming a tree reports as missing, not as diverged.
9. Dirty counts are reported when the commit is unchanged.
10. `.local/handoff/` is git-ignored.

Ten assertions, against revision 3's twenty-two. The drop is not a weakening —
the removed assertions tested id collisions, parser grammar and state precedence,
none of which now exist.

Each assertion must be seen failing against the defect it targets.

**One manual check no script can make:** write a note in Claude Code, switch to
Codex in the same checkout, confirm it continues without the developer repeating
themselves. Then the reverse — the direction with no native support, and the
reason this exists.

## What was deleted from revision 3, and why it is safe

| Deleted | Why it is not needed |
|---|---|
| Entry ids | Nothing references an entry; prose refers to things by name |
| `resolve` and open-state derivation | The agent reads the note and sees what is done |
| Eight staleness states | `show` prints one factual line; the agent judges |
| The entry grammar and parser | Nothing parses prose, so no grammar can be ambiguous |
| `append`, `init`, `list`, `archive`, `key`, `status` verbs | Three verbs remain; the agent writes prose directly |
| `O_CREAT\|O_EXCL` creation protocol | No header to create; the first append creates the file |
| The archive sweep in `worktree-reset.sh` | No archiving, so that file leaves the blast radius |
| The SessionStart hook `[ -f ]` hint | Finding which file to test means reimplementing the key rules in bash |
