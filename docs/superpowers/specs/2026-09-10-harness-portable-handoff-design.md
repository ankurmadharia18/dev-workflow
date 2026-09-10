# Harness-portable handoff — design

**Date:** 2026-09-10
**Status:** approved design, pre-implementation
**Prerequisite:** the `AGENTS.md` inversion (see "Prerequisite" below). Land it first.
**Files touched:** `AGENTS.md`, `hooks/session-start.sh`, `skills/worktree/SKILL.md`,
`skills/standup/SKILL.md`, `skills/cleanup/SKILL.md`, new `skills/test_handoff.sh`

## Summary

Let a developer stop work in one AI agent and continue it in another without
repeating the context. The trigger is a session limit or a token limit. The
developer switches from Claude Code to the Codex CLI, or back, and keeps the
progress.

The design adds one file per branch. The file records the intent, the decisions,
the next steps, and the blockers. Git and the tracker already record everything
else.

## Goal

A developer switches harness mid-task. The new session tells them where the work
stands, without the developer typing the history again.

## Non-goals

- **No conversation replay.** The two harnesses store different transcript
  formats and expose different tools. A replayed transcript misleads more than it
  helps.
- **No transcript parsing.** Both harnesses write readable JSONL, but a parser
  for each is two vendor-specific readers that break when either schema changes.
- **No support beyond Claude Code and the Codex CLI.** The file is plain
  Markdown, so another agent can read it. The project does not test that.
- **No new tracker traffic.** The handoff stays local.

## Verified facts

| Fact | Evidence |
|---|---|
| Claude Code stores sessions as `~/.claude/projects/<slug>/<uuid>.jsonl` | Listed the directory. |
| Codex stores sessions as `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` plus sqlite | Listed the directory; read a `session_meta` record. |
| The two formats differ, and each resume command takes only its own id | `codex resume --help` accepts a Codex `SESSION_ID`. |
| Codex fires no SessionStart hook | Codex 0.151 rejects a `hooks` key in a plugin manifest. See the 2026-09-08 spec. |
| Claude Code fires a SessionStart hook | `hooks/hooks.json` ships one today. |
| `.local/` is git-ignored | `.gitignore:6`. |
| `.local` is a symlink SHARED by every worktree slot | `dev-process/scripts/worktree-reset.sh:104` — `DEFAULT_SHARED=( .env .local .claude/settings.local.json )`. |
| `CLAUDE.md` can import another file with `@AGENTS.md` | Tested: a `CLAUDE.md` holding only `@AGENTS.md` made `claude -p` report a codeword defined in `AGENTS.md`. |
| A missing import target fails silently | Tested: `CLAUDE.md` holding `@NOPE.md` still ran, with no error. |

Because `.local` is shared across slots, one `.local/handoff.md` would let a slot
on one ticket overwrite the notes of a slot on another. The handoff must be
per-branch.

## Prerequisite — `AGENTS.md` becomes canonical

This design needs one file that every harness reads at session start. Today
`CLAUDE.md` holds the content and `AGENTS.md` points at it, which is the wrong
way around: Codex, Cursor and others read `AGENTS.md`.

Invert it. `AGENTS.md` holds the content. `CLAUDE.md` becomes the single line
`@AGENTS.md`. Claude Code expands the import at session start, so the content
lives in one place and the two files cannot drift.

`AGENTS.md` then carries the handoff rule, and every harness picks it up.

## What the handoff records, and what it does not

**It records only what git and the tracker cannot answer:**

- the current intent — what this branch is trying to do
- decisions, each with the reason
- the next steps
- blockers
- open questions

**It does not record** the commits, the branch name, the diff, or the ticket
state. `git log`, `git status` and the board answer those already, and they
answer them without help from the agent. A reader combines the three sources.

This rule removes a `post-commit` git hook from the design. A hook would need
`core.hooksPath`, which overrides every other hook in the target repo and breaks
an existing husky or pre-commit install.

## The file

**Path:** `.local/handoff/<branch-slug>.md`

`<branch-slug>` is the branch name with each `/` replaced by `-`. The branch
`feature/codex-harness-support` gives
`.local/handoff/feature-codex-harness-support.md`.

**Format:** a header, then append-only entries.

```
# handoff — feature/codex-harness-support → dev · PXT-123
created: 2026-09-10T09:14Z
last-seen HEAD: d3b017e

[2026-09-10T14:22Z] decision: dropped the .codex-plugin manifest — Codex rejects
  `hooks`, so it buys only display metadata and costs 5 version-coupling points
[2026-09-10T14:40Z] next: fold the AGENTS.md warning into /setup
[2026-09-10T15:01Z] blocked: rajpubx has pull-only on singlas/dev-workflow
```

**Header fields:** branch, base branch, ticket id (or `none`), `created`,
`last-seen HEAD`.

**Entry kinds — use these five and no others:** `decision`, `next`, `blocked`,
`question`, `note`.

**The header is mutable. The entries are append-only.** A writer rewrites
`last-seen HEAD` in the header at each checkpoint, and appends its entry at the
end. No writer ever edits or deletes an earlier entry. A process killed mid-write
loses one entry, not the file. The decision history is itself the value.

**Timestamps** use ISO 8601 in UTC, to the minute.

**If the file does not exist, the first writer creates it,** header included.
`/worktree` is the usual creator, but a session that starts on an existing branch
without `/worktree` must create the file rather than skip the checkpoint.

## Who writes

1. **The agent, at each decision point.** `AGENTS.md` carries the rule, so every
   harness that reads its instruction file applies it.
2. **`/worktree`**, when it mints a branch: write the header.
3. **`/cleanup`**, when it opens the PR: append a final entry, then archive the
   file (see below).

## Who reads

Three paths. At least one always fires.

| Harness | Path |
|---|---|
| Both | The `AGENTS.md` rule: read the handoff for this branch before acting. |
| Claude Code | `hooks/session-start.sh` surfaces it with the existing brief. |
| Both | `/standup` reports it beside the board. |

Codex needs the first and the third, because it fires no hook.

## Staleness

A handoff for another branch, or one far behind HEAD, is noise. The reader
applies these rules in order:

1. The header's branch does not match the current branch — report the handoff as
   **foreign** and do not use it.
2. `last-seen HEAD` is not an ancestor of the current HEAD — report the handoff
   as **diverged** and treat its next-steps as suspect.
3. `last-seen HEAD` is an ancestor of HEAD, and HEAD is ahead by one or more
   commits — report the handoff as **behind**, and say how many commits landed
   after the last entry.
4. `last-seen HEAD` equals HEAD — the handoff is **current**.

Use `git merge-base --is-ancestor` for rules 2 and 3.

## Archival

`/cleanup` moves the file to
`.local/handoff/archive/<branch-slug>-<YYYY-MM-DD>.md` after it opens the PR. A
finished branch never greets the next session with stale next-steps. The archive
keeps the decision history for the PR discussion.

## Risks

- **An agent can be killed between a decision and its checkpoint.** The developer
  loses the reasoning since the last entry. They never lose work, because work is
  committed. This is not removable without transcript parsing, which is a
  non-goal.
- **An agent can forget the rule.** The rule lives in `AGENTS.md`, which every
  session reads, and the skills checkpoint at their own boundaries. The risk
  stays above zero.
- **The handoff does not survive a machine change**, because `.local` is local
  and git-ignored. A developer who changes machine falls back on git and the
  board. Sending the handoff to the tracker was considered and rejected: it puts
  working scratch on the team board and costs a network call per checkpoint.

## Test plan

New file `skills/test_handoff.sh`, in the repo idiom (`bash <file>`, exit 0 on
pass). It asserts:

1. Two branch names produce two different paths, so parallel slots cannot
   collide.
2. An append preserves every earlier entry.
3. A header whose branch differs from the current branch reads as **foreign**.
4. A `last-seen HEAD` that is not an ancestor of HEAD reads as **diverged**.
5. A `last-seen HEAD` that is an ancestor reports the correct commit count.
6. `.local/handoff/` is git-ignored — `git check-ignore` confirms it.
7. `AGENTS.md` carries the handoff rule.

Each assertion must be shown to fail against the defect it targets. An assertion
that has not been seen failing is not verified.

**One manual end-to-end check, which no script can make:** write a handoff in a
Claude Code session, exhaust nothing, switch to `codex exec` in the same
checkout, and confirm the Codex session reports the work state without the
developer repeating it. Record the result.
