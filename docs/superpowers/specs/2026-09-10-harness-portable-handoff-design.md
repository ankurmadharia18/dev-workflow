# Harness-portable handoff — design

**Date:** 2026-09-10. Rewritten 2026-09-11 after a Codex review rejected revision 1.
**Status:** approved design, pre-implementation
**Prerequisite:** PR #10 (Codex support + `AGENTS.md` canonical). Land it first.
**Files touched:** `AGENTS.md`, `hooks/session-start.sh`, `skills/worktree/SKILL.md`,
`skills/standup/SKILL.md`, `skills/cleanup/SKILL.md`, new `skills/test_handoff.sh`

## Summary

Let a developer stop work in one AI agent and continue it in another on the same
machine. The trigger is a session limit or a token limit.

The design adds one append-only file per workspace. The file records the intent,
the decisions, the next steps and the blockers. Git, the tracker and — in one
direction — Codex's own `/import` already carry everything else.

## Why revision 1 was rejected

A Codex review found two false premises and nine design flaws. Revision 1 claimed
Codex fires no SessionStart hook, and it did not know that Codex ships an
`/import` command. Both claims were tested on 2026-09-11 and both were wrong. The
nine design flaws are listed in "What changed" at the end of this document, each
against the fix.

## Verified facts

Tested on Claude Code 2.1.263 and Codex CLI 0.151.0.

| Fact | Evidence |
|---|---|
| Codex DOES fire the plugin's SessionStart hook | `codex exec` in `pubx-app` printed `hook: SessionStart Completed` and the brief. |
| The hook fires only when `dev-workflow.yml` sits in the session's CWD | `hooks/session-start.sh:12` exits otherwise. |
| Codex `/import` pulls Claude Code **sessions**, not just config | A real run imported 195 items: 96 skills, 48 chat sessions, 42 commands, 6 plugins, 2 MCP servers, 1 AGENTS.md. |
| An imported session becomes a resumable Codex thread | The ledger maps `…/99968417-….jsonl` → thread `01a0905e-5613-7cf2-955a-acb8b3d108e3`, `cwd` preserved. |
| `claude import codex` carries **config only**, never sessions | `claude import codex --dry-run`: 1 mappable item, 6 unmapped. No session type exists. |
| `CLAUDE_PLUGIN_ROOT` is set only during skill execution | Unset in a plain Claude shell; a skill preamble resolved it to the plugin root. |
| Codex exports no plugin-root variable to a skill's shell | `env` under `env -i` shows `CODEX_*` only. |
| `.local/` is git-ignored | `.gitignore:6`. |
| `.local` is a symlink SHARED across worktree slots when the manifest includes it | `dev-process/scripts/worktree-reset.sh:104`. Conditional, not guaranteed: linking is skipped when the canonical source is absent. |

## The asymmetry that shapes this design

| Direction | Native support |
|---|---|
| Claude Code → Codex | **Full.** `/import` carries the transcript into a resumable thread. |
| Codex → Claude Code | **None.** Config only. |

`/import` is nonetheless the wrong tool for a per-switch handoff. It is a bulk,
one-time migration: it imported all 48 sessions across 9 repos, plus 96 skills and
42 slash commands, from a TUI picker. Running it at each limit would re-import
everything and churn config.

So this design does not try to move conversation. It moves the small durable part,
the same way in both directions, and points at `/import` as an optional bulk tool.

## Goal

A developer hits a limit in one agent, opens the other **on the same machine, in
the same checkout**, and learns where the work stands without retyping it.

## Non-goals

- **No conversation replay.** `/import` does this better one way; nothing does it
  the other way.
- **No transcript parsing.** Two vendor-specific readers would break on schema
  changes.
- **No support beyond Claude Code and Codex.** The file is plain Markdown, so
  another agent can read it. The project does not test that.
- **No tracker traffic.** The handoff stays local.
- **No cross-machine support.** The file is git-ignored and local. A developer who
  changes machine falls back on git and the board. This is a deliberate narrowing,
  and it is why the goal says "same machine".

## What the handoff records

Only what git and the tracker cannot answer: intent, decisions with reasons, next
steps, blockers, open questions, and whether the tree was clean at each
checkpoint.

It does not record the commits or the diff. `git log` answers those without help.
This is why the design needs no `post-commit` hook — and a hook would need
`core.hooksPath`, which overrides every other hook in a target repo.

## The file

**Path:** `.local/handoff/<key>.md`

**`<key>` is derived from the workspace, and the derivation is injective:**

- On a branch: the branch name with `/` replaced by `%2F`. `feature/a-b` gives
  `feature%2Fa-b`; `feature-a/b` gives `feature-a%2Fb`. Different branches always
  give different keys, which a naive slash-to-dash slug does not.
- Detached HEAD: `detached..<basename of the worktree directory>`. Worktree
  bootstrap creates detached slots deliberately
  (`skills/worktree/SKILL.md:129`), and the directory is stable while the SHA is
  not. The separator is `..` because git forbids it in a ref name, so no real
  branch can ever encode to a detached key. A single character such as `/` would
  not be safe here: a branch genuinely named `detached/slot-a` would collide.

**The file is append-only. Nothing in it is ever rewritten.** The header is
written once at creation and holds only immutable facts. Mutable state — HEAD,
dirty count — arrives as `checkpoint` entries, so there is no header to rewrite,
no truncate-and-rewrite window, and no lock. A POSIX append of a short line is
atomic, so two sessions appending cannot lose each other's entries.

```
# handoff
workspace: feature/codex-harness-support
base: dev
ticket: PXT-123
worktree: /Users/rajverma/repos/aws/dev-workflow
created: 2026-09-11T09:14Z

[e1 2026-09-11T09:14Z] intent: make the plugin run on Codex without a second manifest
[e2 2026-09-11T14:22Z] decision: no .codex-plugin — Codex reads .claude-plugin already,
  and a second manifest adds 5 version-coupling points
[e3 2026-09-11T14:40Z] next: fold the AGENTS.md warning into /setup
[e4 2026-09-11T15:01Z] blocked: rajpubx has pull-only on singlas/dev-workflow
[e5 2026-09-11T15:20Z] checkpoint: HEAD=d3b017e dirty=0 untracked=0
[e6 2026-09-11T16:05Z] resolve: e3 — done in a7a6dd8
```

**Entry ids** are `e<n>`. A writer reads the highest existing id and adds one.

Two writers appending in the same instant can therefore pick the same id. The
append itself is still safe — no entry is lost — but a `resolve` naming a
duplicated id is ambiguous. The reader resolves that by taking the **earliest**
entry with the named id, and by reporting the duplicate so a human can see it.
Ids are a reference aid, not a uniqueness guarantee, and the design does not add
a lock to make them one: two agents writing the same workspace in the same minute
is not the case this serves.

**Entry kinds — these eight, and no others:**

| Kind | Meaning | Closable |
|---|---|---|
| `intent` | What this workspace is trying to achieve | no |
| `decision` | A choice, with its reason | no |
| `next` | A step to take | yes |
| `blocked` | Something preventing progress | yes |
| `question` | Unresolved, needs an answer | yes |
| `note` | Anything else worth carrying | no |
| `checkpoint` | `HEAD=<sha> dirty=<n> untracked=<n>` — the mutable state the header deliberately does not hold | no |
| `resolve` | References an earlier entry id and closes it | n/a |

**Open state is derivable.** A `next`, `blocked` or `question` is OPEN unless a
later `resolve` names its id. This is what makes an append-only file usable: a
reader lists the open items without needing to rewrite history. Revision 1 had no
`resolve`, so a reader could not tell a finished step from a pending one.

**Timestamps** use ISO 8601 in UTC, to the minute.

**The first writer creates the file**, header included. `/worktree` is the usual
creator, but a session that starts on an existing branch without `/worktree` must
create it rather than skip the checkpoint.

## Who writes

1. **The agent, at each decision point and before it stops.** `AGENTS.md` carries
   the rule, so every harness that reads its instruction file applies it.
2. **`/worktree`**, when it mints a branch: write the header and an `intent`.
3. **`/cleanup`**, when it opens the PR: append a `note` and a final `checkpoint`.
   It does **not** archive — see below.

## Who reads

Three paths. Each states its own condition; none is claimed to be universal.

| Path | Harness | Fires when |
|---|---|---|
| The `AGENTS.md` rule | both | the repo has an `AGENTS.md` and the agent reads it at session start |
| `hooks/session-start.sh` | both | `dev-workflow.yml` is in the session's CWD |
| `/standup` | both | the developer runs it |

Revision 1 claimed one of these always fires. That was false: the hook is
CWD-conditional and `/standup` is manual. The honest statement is that the first
two are automatic under stated conditions, and `/standup` is the fallback a
developer can always reach for.

## Staleness

The reader compares the newest `checkpoint` entry against the working tree, and
reports one state. Prose freshness — how old the last entry is — is reported
separately from commit ancestry, because they answer different questions.

| State | Test |
|---|---|
| `foreign` | The header's `workspace` does not match the current key. Do not use it. |
| `invalid` | The recorded HEAD is not a commit in this repo (garbage-collected, or a bad record). |
| `current` | The recorded HEAD equals the current HEAD. |
| `behind` | The recorded HEAD is an ancestor of HEAD. Report the commit count. |
| `diverged` | The recorded HEAD is a valid commit but not an ancestor. |

Use `git cat-file -e` for `invalid` and `git merge-base --is-ancestor` for the
rest.

**A `diverged` result does not prove the handoff is stale.** A rebase or an amend
rewrites history and produces exactly this reading for an otherwise perfectly
relevant handoff. The design does not try to distinguish them — patch-id matching
is fragile and expensive. The reader must say so in those words, so a developer
checks rather than discards.

**Dirty state is reported too.** Each `checkpoint` records the count of modified
tracked files and untracked files. A clean HEAD match with `dirty=7` means the
tree moved even though the commit did not. Revision 1 claimed a developer "never
loses work, because work is committed"; `/cleanup` exists precisely because work
sits uncommitted, so the handoff records that instead of assuming it away.

## When no handoff matches

If no file matches the current key, the reader lists the other handoffs in
`.local/handoff/` with their workspace, ticket and last entry time, and stops.
A branch rename or a new worktree orphans the old file, and guessing which orphan
belongs to the current work is not something the reader should do silently.

## Archival

Archive when the **branch is gone**, not when the PR opens. A branch stays alive
through CI failures, review changes and conflict repair, all of which need the
handoff. `/worktree` already sweeps merged branches; it moves the matching
handoff to `.local/handoff/archive/<key>-<YYYY-MM-DD>.md` in the same pass.

## Relationship to `/import`

`AGENTS.md` and the README should tell a developer moving **Claude → Codex** that
`codex` `/import` exists and carries the actual conversation, as a bulk one-time
migration. It is complementary, never required, and there is no equivalent going
the other way.

## Risks

- **An agent can be killed between a decision and its checkpoint.** The developer
  loses reasoning since the last entry, never committed work. Not removable
  without transcript parsing, which is a non-goal.
- **An agent can forget the rule.** It lives in `AGENTS.md`, which every session
  reads, and the skills checkpoint at their boundaries. The risk stays above zero.
- **`.local` is shared across slots only when the worktree manifest includes it.**
  Where it is not shared, a handoff written in one slot is invisible in another.
  The per-key file naming means this degrades to "not found", never to a wrong
  file being read.

## Test plan

New `skills/test_handoff.sh`, in the repo idiom (`bash <file>`, exit 0 on pass):

1. `feature/a-b` and `feature-a/b` produce different keys.
2. A detached HEAD produces a key from the worktree directory, and a branch
   literally named `detached/<x>` does not collide with it.
3. An append preserves every earlier entry.
4. `resolve` closes the referenced entry, and an unreferenced `next` stays open.
5. Entry ids increment. Two writers picking the same id is tolerated: the reader
   resolves to the earliest entry with that id and reports the duplicate.
6. A header whose `workspace` differs reads as `foreign`.
7. A recorded HEAD that is not a commit reads as `invalid`.
8. An ancestor reads as `behind` with the correct count.
9. A non-ancestor reads as `diverged`, and the message names rebase as a cause.
10. A dirty tree is reported even when HEAD matches.
11. No matching key lists the other handoffs instead of guessing.
12. `.local/handoff/` is git-ignored — `git check-ignore` confirms it.
13. `AGENTS.md` carries the handoff rule.

Each assertion must be shown to fail against the defect it targets. An assertion
that has not been seen failing is not verified.

**Two manual checks no script can make:**

- Write a handoff in Claude Code, switch to `codex` in the same checkout, and
  confirm the Codex session reports the work state without the developer
  repeating it. Then the reverse — the direction with no native support, and the
  one this design exists for.
- Confirm the same flow in a repo where `.local` is **not** shared across slots,
  and confirm it degrades to "not found" rather than reading the wrong file.

## What changed from revision 1

| Review finding | Fix |
|---|---|
| Codex does fire the hook | Premise corrected; the hook is now a read path on both harnesses, with its CWD condition stated |
| `/import` exists and carries sessions | Documented as a complementary bulk tool; the asymmetry now drives the design |
| Slash-to-dash slug is not injective | `%2F` encoding, which is reversible |
| Detached HEAD has no path | Key from the worktree directory |
| Header rewrite is neither append-only nor crash-safe | The header is immutable; HEAD and dirty state moved into `checkpoint` entries |
| Archiving at PR-open is premature | Archive when the branch is swept |
| `intent` promised but not an entry kind | `intent` is now a kind |
| Append-only entries can never be resolved | `resolve` entries close earlier ids; open state is derivable |
| "At least one path always fires" is false | Each path now states its condition; no universality claimed |
| "Work is always committed" is false | `checkpoint` records dirty and untracked counts |
| Rebase reads as diverged with no recovery rule | `diverged` explicitly names rebase as a cause and tells the reader to check |
| Branch rename orphans the file | The reader lists orphans instead of guessing |
| Cross-machine loss called fatal | The goal is explicitly narrowed to same machine, same checkout |
