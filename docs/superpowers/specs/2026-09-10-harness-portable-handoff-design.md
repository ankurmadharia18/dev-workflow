# Harness-portable handoff — design

**Date:** 2026-09-10. Revision 3, 2026-09-12.
**Status:** approved design, pre-implementation
**Prerequisite:** PR #10 (Codex support + `AGENTS.md` canonical). Land it first.
**Files touched:** new `dev-workflow/handoff.py`, new `dev-workflow/test_handoff.py`,
`AGENTS.md`, `hooks/session-start.sh`, `skills/worktree/SKILL.md`,
`skills/standup/SKILL.md`, `skills/cleanup/SKILL.md`,
`dev-process/scripts/worktree-reset.sh`

## Summary

Let a developer stop work in one AI agent and continue it in another on the same
machine. The trigger is a session limit or a token limit.

One small Python helper owns an append-only file per workspace. The file records
the intent, the decisions, the next steps and the blockers. Git, the tracker and
— in one direction — Codex's own `/import` already carry everything else.

## Revision history

Revision 1 was rejected: two false premises and nine design flaws. Revision 2 was
rejected: six of its fixes were incomplete or wrong, and it added four new
defects. Both rejections came from a Codex review, and every finding was verified
by hand before being accepted.

**The root cause of revision 2's failure was structural, not detail.** It
specified the file format in prose and expected four separate surfaces — the
`AGENTS.md` rule, `/worktree`, `/cleanup` and `/standup` — each to implement
identical key encoding, id generation, escaping and parsing from English. They
would drift. Four of the six findings were symptoms of that single choice.

Revision 3 therefore puts one tested helper in charge of the format, and the
skills call it. This matches what the repo already does with `dw-config.py`,
`dw-board.py` and `validate.py`. The full finding-to-fix table is at the end.

## Verified facts

Tested on Claude Code 2.1.263 and Codex CLI 0.151.0.

| Fact | Evidence |
|---|---|
| Codex fires the plugin's SessionStart hook | `codex exec` in `pubx-app` printed `hook: SessionStart Completed` and the brief. |
| The hook exits silently unless `dev-workflow.yml` is in the session's CWD | `hooks/session-start.sh:12`. |
| Codex `/import` pulls Claude Code sessions, not only config | A run imported 48 chat sessions alongside skills, commands, plugins and MCP config. |
| An imported session becomes a Codex thread with `cwd` preserved | The ledger maps `…/99968417-….jsonl` → thread `01a0905e-5613-7cf2-955a-acb8b3d108e3`. |
| `claude import codex` carries config only | `claude import codex --dry-run`: 1 mappable item, 6 unmapped, no session type. |
| `CLAUDE_PLUGIN_ROOT` is set only during skill execution | Unset in a plain Claude shell; a skill preamble resolved it to the plugin root. |
| Codex exports no plugin-root variable to a skill's shell | `env` under `env -i` shows `CODEX_*` only. |
| `.local/` is git-ignored | `.gitignore:6`; `git check-ignore` confirms `.local/handoff`. |
| `.local` is shared across worktree slots only when the manifest includes it | `dev-process/scripts/worktree-reset.sh:104`. Conditional, not guaranteed. |
| `feature%2Fx` is a legal branch name | `git check-ref-format --branch 'feature%2Fx'` succeeds. This is why naive `/`→`%2F` encoding collides. |

**One claim from revision 2 is withdrawn.** It asserted that re-running `/import`
would re-import everything. The ledger stores `content_sha256` per source, which
suggests deduplication. The behaviour is untested, so this design makes no claim
about it.

## The asymmetry that shapes this design

| Direction | Native support |
|---|---|
| Claude Code → Codex | **Full.** `/import` carries the transcript into a Codex thread. |
| Codex → Claude Code | **None.** Config only. |

`/import` is still the wrong tool for a per-switch handoff: it is a bulk TUI
migration that pulled 48 sessions across 9 repos in one run. So this design does
not move conversation. It moves the small durable part, identically in both
directions, and points at `/import` as an optional bulk tool.

## Goal

A developer hits a limit in one agent, opens the other **on the same machine, in
the same checkout**, and learns where the work stands without retyping it.

## Non-goals

- No conversation replay, and no transcript parsing.
- No support beyond Claude Code and Codex. The file is plain Markdown, so other
  agents can read it; the project does not test that.
- No tracker traffic.
- No cross-machine support. The file is git-ignored and local. This is a
  deliberate scope cut, and it is why the goal says "same machine".

## Component 1 — `dev-workflow/handoff.py`

One helper owns the format. Nothing else parses or writes the file.

**Why a helper and not prose:** key encoding, id generation and staleness
classification each have a correct answer and several plausible wrong ones. Four
prose surfaces produced four chances to get each wrong. One helper with one test
file produces one chance, and a test that pins it.

It follows the conventions of its neighbours: standard library only, runs under
`uv run` or `python3`, `python3 -m py_compile` clean, and a `test_handoff.py`
beside it.

### Verbs

| Verb | Does |
|---|---|
| `key` | Print the workspace key for the current worktree |
| `path` | Print the handoff file path |
| `init` | Create the file with its header, atomically; no-op if it exists |
| `append <kind> <text>` | Append one entry, generating its id |
| `checkpoint` | Compute HEAD, dirty and untracked counts itself, and append them |
| `resolve <id> [text]` | Append a `resolve` entry naming an earlier id |
| `status` | Parse, classify staleness, list open items |
| `list` | List every handoff with workspace, ticket and last entry time |
| `archive` | Move the file to the archive directory |

A caller never formats a line itself. `checkpoint` reads git directly rather than
accepting values, so no caller can record a stale or wrong SHA.

### The workspace key

**On a branch:** `urllib.parse.quote(branch, safe="")`.

This percent-encodes **every** unsafe byte, including `%` itself. That is what
makes it injective, and it is precisely what revision 2 got wrong: encoding `/`
to `%2F` while leaving `%` alone collides `feature/x` with the legal branch name
`feature%2Fx`. Encoding `%` first, or encoding every byte, both fix it; `quote`
does the latter and is standard library.

**Detached HEAD:** `detached..<quoted basename>..<first 8 hex of sha256 of the
absolute worktree path>`.

The `..` separator is illegal in a git ref name, so no branch can encode into the
detached namespace. Verified: `git check-ref-format --branch 'detached..slot'`
fails.

**That protection comes from git's ref rules alone, not from the encoding.**
`quote` does not percent-encode `.`, so `quote("detached..slot", safe="")` returns
`detached..slot` unchanged. An implementer who swaps the separator for something
`quote` also leaves alone, or who stops relying on git's rule, reopens the
collision.

The path hash is what makes the key unique: revision 2 used the basename alone,
so `/a/slot` and `/b/slot` collided. The basename is kept ahead of the hash for
human readability.

### Entry ids

**8 random hex characters**, from `secrets.token_hex(4)`.

Revision 2 used sequential `e<n>` ids, then admitted two writers could pick the
same one and proposed resolving to the earliest match — which left the later
duplicate permanently unresolvable. Random ids remove the coordination problem
instead of managing it, and need no lock.

### Writing

`init` uses `O_CREAT | O_EXCL`, so two agents racing to create the same file
cannot both write a header; the loser sees the winner's file and proceeds.

Every entry is written with a single `write()` of one complete logical entry,
continuation lines included, opened in append mode. A short append is atomic on
POSIX, so concurrent appends interleave cleanly and no entry is lost or torn.

## Component 2 — the file

**Path:** `.local/handoff/<key>.md`

**Append-only. Nothing is ever rewritten.** The header is written once and holds
only immutable facts. Mutable state lives in `checkpoint` entries, so there is no
header to rewrite and no lock to take.

```
# handoff
key: feature%2Fcodex-harness-support
branch: feature/codex-harness-support
base: dev
ticket: PXT-123
worktree: /Users/rajverma/repos/aws/dev-workflow
created: 2026-09-12T09:14Z

[a3f9c1d2 2026-09-12T09:14Z] intent: make the plugin run on Codex without a second manifest
[7b2e4f60 2026-09-12T14:22Z] decision: no .codex-plugin — Codex reads .claude-plugin
  already, and a second manifest adds 5 version-coupling points
[c81d05aa 2026-09-12T14:40Z] next: fold the AGENTS.md warning into /setup
[1f6a93bc 2026-09-12T15:20Z] checkpoint: HEAD=d3b017e4f1a9c2b8e5d7061a3f4c9b2e8d05c6a1 dirty=0 untracked=0
[9e04b7d3 2026-09-12T16:05Z] resolve: c81d05aa — done in a7a6dd8
```

The header carries **both** `key` and `branch`. `key` is what staleness compares;
`branch` is for humans. Revision 2 stored only the raw branch and compared it
against the encoded key, so every branch containing a slash read as foreign.

`checkpoint` records the **full 40-character OID**, not an abbreviation, so
verification cannot be ambiguous.

### Entry kinds — these eight, and no others

| Kind | Meaning | Closable |
|---|---|---|
| `intent` | What this workspace is trying to achieve | no |
| `decision` | A choice, with its reason | no |
| `next` | A step to take | yes |
| `blocked` | Something preventing progress | yes |
| `question` | Unresolved, needs an answer | yes |
| `note` | Anything else worth carrying | no |
| `checkpoint` | `HEAD=<oid> dirty=<n> untracked=<n>` | no |
| `resolve` | Names an earlier id and closes it | n/a |

**Open state is derivable.** A `next`, `blocked` or `question` is open unless a
later `resolve` names its id. This is what makes an append-only file usable.

## Component 3 — staleness

`status` reports one state. It verifies commits with
`git rev-parse --verify <oid>^{commit}` — the `^{commit}` matters, since an OID
can name a tree or a blob.

| State | Test |
|---|---|
| `foreign` | The header's `key` does not match the current key |
| `malformed` | The header or an entry does not parse |
| `no-checkpoint` | The file has no `checkpoint` entry yet |
| `invalid` | The recorded OID does not resolve to a commit here |
| `current` | The recorded OID equals the current HEAD |
| `behind` | The recorded OID is an ancestor of HEAD; report the count |
| `ahead-of-checkpoint` | The current HEAD is an ancestor of the recorded OID — the checkout moved backwards |
| `diverged` | Both are commits, neither is an ancestor of the other |

`ahead-of-checkpoint` is new in revision 3, on the reviewer's suggestion. It is
cheap and it distinguishes "you checked out something older" from real
divergence.

**Dirty state is reported alongside, never folded into, the commit state.** The
recorded counts are compared with the current ones, so `current` with
`dirty 0 → 7` still tells the developer the tree moved. Revision 2 recorded the
counts but never compared them.

**`diverged` deliberately does not distinguish a rebase from real divergence.** A
rebase or amend produces exactly this reading for an otherwise relevant handoff.
Patch-id matching is fragile and would manufacture false certainty. The message
must name rebase as a likely cause and tell the developer to check. The reviewer
endorsed this.

## Component 4 — the skills

Each calls the helper. None parses the file.

| Surface | Calls |
|---|---|
| The `AGENTS.md` rule | `status` at session start; `append` at each decision point and before stopping |
| `hooks/session-start.sh` | `status`, appended to the existing brief |
| `/worktree` | `init` plus an `intent` when it mints a branch |
| `/standup` | `status`, reported beside the board |
| `/cleanup` | `checkpoint` and a `note` when it opens the PR |
| `dev-process/scripts/worktree-reset.sh` | `archive` when it sweeps a merged branch |

Resolving the helper's path uses the four-rung ladder PR #10 establishes:
`dw-config` on PATH, then `$CLAUDE_PLUGIN_ROOT`, then `$DW_ROOT`, then
checkout-relative.

### Read paths and their conditions

None is universal, and the design does not claim one is.

| Path | Fires when |
|---|---|
| The `AGENTS.md` rule | the repo has an `AGENTS.md` that the agent reads at session start |
| `hooks/session-start.sh` | `dev-workflow.yml` is in the session's CWD |
| `/standup` | the developer runs it |

## Component 5 — archival and orphans

**Archive when the branch is gone, not when the PR opens.** A branch stays alive
through CI failures, review and conflict repair. `worktree-reset.sh` already
sweeps merged branches, so it calls `handoff archive` in the same pass.

**Archive path:** `.local/handoff/archive/<key>-<YYYYMMDDTHHMMSSZ>.md`. Revision 2
used a date alone, which collides when a branch name is reused twice in one day.

**Orphans.** A rename or a new worktree leaves a handoff no key matches. `status`
reports that no handoff matches and prints what `list` would show. It never
adopts an orphan silently. Adoption is a deliberate act: the developer moves or
copies the file, and the helper does not guess.

## Risks

- **An agent can be killed between a decision and its checkpoint.** The developer
  loses reasoning since the last entry, never committed work. Not removable
  without transcript parsing, which is a non-goal.
- **An agent can forget to append.** The rule lives in `AGENTS.md` and the skills
  checkpoint at their boundaries. The risk stays above zero. This design reduces
  it; it does not eliminate it.
- **`.local` is shared across slots only when the manifest includes it.** Where it
  is not, a handoff written in one slot is invisible in another. Per-key naming
  means this degrades to "not found", never to reading the wrong file.

## Test plan

`dev-workflow/test_handoff.py`, run as `python3 dev-workflow/test_handoff.py`,
matching `test_validate.py`.

**Keys**

1. `feature/x` and the legal branch `feature%2Fx` produce different keys.
2. Round-tripping a key recovers the branch name exactly.
3. Two detached worktrees sharing a basename (`/a/slot`, `/b/slot`) produce
   different keys.
4. No branch name can produce a key in the `detached..` namespace.

**Entries**

5. An append preserves every earlier entry.
6. Ids are unique across many appends.
7. `resolve` closes the named entry; an unreferenced `next` stays open.
8. A multi-line entry round-trips, and its continuation lines are not parsed as
   separate entries.
9. Two concurrent appends both land, and neither is torn.
10. Two concurrent `init` calls produce one header, not two.

**Staleness**

11. A mismatched key reads `foreign`.
12. A file with no checkpoint reads `no-checkpoint`.
13. A corrupt header reads `malformed`.
14. An OID that is not a commit reads `invalid`, including an OID that names a
    tree.
15. An ancestor reads `behind` with the correct count.
16. A reversed relationship reads `ahead-of-checkpoint`.
17. Unrelated commits read `diverged`, and the message names rebase as a cause.
18. A dirty tree is reported even when HEAD matches.

**Files**

19. No matching key lists the other handoffs instead of guessing.
20. Two archives of the same key on the same day do not collide.
21. `.local/handoff/` is git-ignored.
22. `AGENTS.md` carries the handoff rule.

Each assertion must be shown to fail against the defect it targets. An assertion
that has not been seen failing is not verified.

**Two manual checks no script can make:**

- Write a handoff in Claude Code, switch to Codex in the same checkout, and
  confirm it reports the work state without the developer repeating it. Then the
  reverse — the direction with no native support, and the reason this exists.
- Repeat where `.local` is **not** shared across slots, and confirm it degrades to
  "not found" rather than reading the wrong file.

## Finding-to-fix table

**From the revision 1 review — two false premises and nine design flaws, eleven rows:**

| Finding | Fix |
|---|---|
| Codex does fire the hook | Premise corrected; it is a read path on both harnesses |
| `/import` exists and carries sessions | Documented; the asymmetry now drives the design |
| Slash-to-dash slug not injective | Full percent-encoding via `quote(safe="")` |
| Detached HEAD has no path | Key from the worktree, with a path hash |
| Header rewrite is not crash-safe | Header immutable; state moved to `checkpoint` entries |
| Archiving at PR-open is premature | Archive when the branch is swept |
| `intent` promised but not a kind | `intent` is a kind |
| Append-only entries can never be resolved | `resolve` closes ids |
| "At least one path always fires" is false | Each path states its condition |
| "Work is always committed" is false | `checkpoint` records dirty and untracked counts |
| Rename orphans the file | `status` lists orphans, never adopts one |

**From the revision 2 review — six incomplete fixes and four new defects, thirteen rows:**

| Finding | Fix |
|---|---|
| `%2F` alone is not injective — `feature%2Fx` is a legal branch | `quote(safe="")` encodes `%` too |
| Detached basename is not unique | Add a hash of the absolute worktree path |
| Sequential ids break `resolve` under concurrency | 8 random hex characters |
| Header stored a raw branch, staleness compared an encoded key | Header stores both `key` and `branch`; comparison uses `key` |
| Concurrent first creation, and partial headers | `O_CREAT|O_EXCL`; one `write()` per entry |
| Archive sweep lives in `worktree-reset.sh`, which was out of scope | Added to the files touched |
| Dirty state recorded but never compared | `status` compares recorded with current |
| Staleness missing several states, and used short SHAs | Adds `malformed`, `no-checkpoint`, `ahead-of-checkpoint`; verifies `^{commit}`; stores full OIDs |
| Archive filenames collide on same-day reuse | Timestamp to the second |
| No shared writer or parser | `dev-workflow/handoff.py` — the structural fix |
| Test plan gaps | 22 assertions, including every collision case named above |
| "Nine flaws" miscounted against eleven rows | Both tables are now explicitly counted |
| `/import` re-import claim unsupported | Withdrawn |
