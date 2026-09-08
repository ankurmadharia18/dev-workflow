# AGENTS.md

Codex reads this file. Claude Code reads `CLAUDE.md`. The two must agree.

The project overview, repository structure, content guidelines and conventions
live in [CLAUDE.md](CLAUDE.md). Read that file — this one adds only what differs
on Codex.

## What differs on Codex

- **No session brief.** Codex 0.151 does not accept a `hooks` key in a plugin
  manifest, so the SessionStart hook never fires. Start a session with `/standup`.
- **The autonomous tiers refuse.** `/ticket-loop` and `/ticket-loop-parent` need
  `claude -p` and Claude subagents. They stop with a message on Codex. Use the
  session skills: `/setup`, `/worktree`, `/standup`, `/cleanup`, `/release`,
  `/blog-from-session`.
- **`DW_ROOT`.** Codex sets no plugin-root variable. Each skill's preamble tells
  you to set `DW_ROOT` to the directory two levels above its SKILL.md. Do it
  before running the preamble.
- **Reinstall to pick up edits.** `codex plugin add dev-workflow@dev-workflow`,
  then start a new thread.

## Tests

Same idiom as the rest of the repo — run the file directly:

- `python3 dev-workflow/test_validate.py`
- `python3 dev-workflow/test_queue_count.py`
- `python3 skills/ticket-loop/orchestrator/test_orch.py`
- `bash skills/test_root_ladder.sh`
- `bash skills/test_harness_guard.sh`
