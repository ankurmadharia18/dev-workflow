---
name: standup
description: >-
  Run at the START of a dev session to orient on the issue-tracker board and
  decide what to work on. Use whenever someone opens a session and says things
  like "standup", "start my session", "what should I work on", "what's on the
  board", "where do I start", "what's next", "board review", or "/standup" —
  even without naming a specific ticket. It regenerates the live board snapshot
  (the repo's `board.snapshot` command), reads the generated `board.views`, and
  hands back a tight session brief: what's still In Progress to resume, the
  board's shape by project and milestone, and 2-4 recommended starting points
  each with a one-line why. Read-only orientation — it never starts work or
  moves ticket states until the human picks one and asks. NOT for end-of-session
  wrap-up (use cleanup), or the dev→prod promotion (use release). Repo/dev skill.
---

# standup

A session-opener. You sit down, you don't remember exactly where the board is,
and you want one screen that says *here's where you left off, here's what's worth
starting, here's the shape of the work.* This skill produces that brief from the
**live** board — not from stale memory.

It is **orientation, not action.** Don't start coding, don't move tickets to In
Progress, don't open worktrees. Surface, recommend, then let the human pick.

## Per-repo configuration (`dev-workflow.yml`)

Everything repo-specific comes from `dev-workflow.yml` at the target-repo root.
**Run this preamble ONCE at the start** to resolve the config reader and load every
key this skill uses; the list below explains each. No `dev-workflow.yml` → the
preamble says so and the missing-config fallbacks in the procedure take over.

**Set `DW_ROOT` first, but only when `CLAUDE_PLUGIN_ROOT` is unset.** Claude Code
sets `CLAUDE_PLUGIN_ROOT` for you; other harnesses (Codex) do not. When it is
unset and this SKILL.md sits inside a plugin cache, export `DW_ROOT` as the
absolute directory **two levels above this SKILL.md file** — write the path out
in full, quoted, from the location your harness showed you. Example:
`export DW_ROOT="$HOME/.codex/plugins/cache/dev-workflow/dev-workflow/<version>"`.
Leave `DW_ROOT` unset when you are working from a framework checkout.

```bash
if command -v dw-config >/dev/null 2>&1 && dw-config 2>&1 | grep -q -- '--batch'; then DW="dw-config"   # hardened install (PATH), only if --batch-capable
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then DW="uv run ${CLAUDE_PLUGIN_ROOT}/dev-workflow/dw-config.py" # plugin install (Claude Code)
elif [ -n "${DW_ROOT:-}" ]; then DW="uv run ${DW_ROOT}/dev-workflow/dw-config.py"                       # plugin install (other harness)
else DW="uv run dev-workflow/dw-config.py"; fi                                                          # framework checkout
[ -f dev-workflow.yml ] \
  && $DW dev-workflow.yml --batch board.snapshot board.views tracker.team tracker.project= tracker.ticket_prefix \
       tracker.roles.exclude.labels tracker.roles.queue.states \
  || echo "no dev-workflow.yml — using the skill's missing-config fallbacks"
```

Never hardcode a team, label, state, or command:

- `board.snapshot` — the command that regenerates the board views (e.g. a
  tracker-export script). `board.views` — the directory it writes them to.
- `tracker.team` — the team/workspace to query. `tracker.ticket_prefix` — the
  key shape (`ABC-123`).
- `tracker.roles.exclude.labels` — never recommend a ticket carrying one of
  these as a "start now" task.
- `tracker.roles.queue.states` — the states the board treats as actionable.

Tracker access is through the canonical verbs (`list_actionable`, `get_ticket`,
…) in `dev-workflow/tracker-adapters.md`; the adapter maps them onto the
provider (Linear today). Ticket keys below use `ABC-123` as a stand-in.

## 1. Fetch the live board (always, first)

Regenerate the snapshot so the views are current — never reason off a stale
`board.views`:

Run the `board.snapshot` command loaded by the preamble (the value of
`board.snapshot`).

Then read the generated views under `board.views` — at minimum the
In-Progress + Todo view (the primary input), plus whatever gate/milestone and
backlog views the repo generates.

**If `board.snapshot`/`board.views` isn't configured** (or the snapshot fails):
say so and fall back to the tracker adapter directly — `list_actionable` over
`tracker.team` (scoped to `tracker.project` when set, so a shared-team repo's
brief doesn't surface sibling repos' tickets), dropping
`tracker.roles.exclude.labels`. If `tracker.team` / `tracker.ticket_prefix` are
also missing, ask the human for the team name and key prefix once, then degrade
to adapter-only for the rest of the brief.

## 2. Orient — where did I leave off?

- **In Progress tickets** are the strongest start candidates: finishing a
  started thread beats opening a new front. Lead the brief with these.
- Skim recent commits for in-flight context: `git log --oneline -15` (this
  branch). Check sibling worktrees only if it clarifies what's mid-air.

## 3. Surface the broad themes

Give a compact read of the board's *shape*, not a list dump:

- **By project/area:** rough counts across the tracker's projects — where the
  board's weight sits right now.
- **Active milestones** with open work — the forward initiatives in motion.
  Name them; don't enumerate every ticket.
- Call out anything blocking downstream work — a `decision`-style ticket left
  open (no code until resolved) is its own theme.

## 4. Recommend starting points (the judgment)

Pick **2-4** tickets to start, each with a one-line *why*. Rank by:

1. **Resume** — In Progress work; finish what's started.
2. **Unblocks the current gate/milestone.**
3. **High priority + unblocked** — highest tracker priority in a queue state,
   carrying none of `tracker.roles.exclude.labels`.

Within that ranking, order by tracker priority, then oldest. Exclude from "start
now" anything labeled with a `tracker.roles.exclude.labels` entry (gated/decision
work — the trigger hasn't fired, or a decision must land first).

Offer a **mix of effort** when the board allows — one quick win and one meatier
task — so the human can match the task to the time they have. If they've
signalled a focus ("ops today", "I've got 90 minutes"), filter to it; otherwise
give the spread and let them choose.

Before recommending a specific ticket, read its body with `get_ticket` so the
*why* is real (acceptance criteria, the blocker) — not just the title from the
snapshot.

## Output — a tight brief, not a wall

Roughly this shape, scannable in ten seconds:

- **Left off:** In Progress tickets (resume candidates) + a one-liner of context.
- **Start here:** 2-4 tickets as `ABC-123 — title · why (resume / unblocks gate /
  quick win)`.
- **Board shape:** themes from step 3 in 2-3 lines.
- Close with the offer: *want me to scope one, or move it to In Progress and start?*

## Never

- Start coding, edit files, or open a worktree — this skill only orients.
- Move a ticket's state, comment, or otherwise mutate the board until the human
  picks a ticket and asks you to start it.
- Recommend a ticket carrying a `tracker.roles.exclude.labels` entry as a "start
  now" task.
- Reason off a stale `board.views` — always regenerate the snapshot first.
