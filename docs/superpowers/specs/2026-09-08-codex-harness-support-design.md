# Codex CLI harness support (v1 session skills) — design

**Date:** 2026-09-08
**Status:** approved design, pre-implementation
**Scope:** v1 session skills only. v2 and v3 stay Claude Code only.
**Files touched:** all 7 `skills/*/SKILL.md`, `hooks/hooks.json`,
`hooks/session-start.sh`, new `.codex-plugin/`, `scripts/bump-version.sh`,
`README.md`, `AGENTS.md`

## Summary

Make the dev-workflow plugin install and run on the Codex CLI as well as Claude
Code. A Codex user installs the same plugin from the same repo. The user then
runs `/setup`, `/worktree`, `/standup`, `/cleanup`, `/release` and
`/blog-from-session` the same way.

The autonomous tiers stay on Claude Code. `cron-run.sh` calls `claude -p`.
`usage-parse.py` reads the Claude JSON envelope. The Docker image installs the
`claude` CLI. This design does not change them.

## Verified facts

The team verified these facts on Codex CLI 0.151.0. The test repo was
`~/repos/aws/pubx-app`, which has a `dev-workflow.yml`.

| Fact | Result |
|---|---|
| Codex reads `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` | Yes. All 7 skills loaded as `dev-workflow:<name>`. |
| Codex copies the whole repo into its plugin cache | Yes — `~/.codex/plugins/cache/dev-workflow/dev-workflow/<version>/`. |
| Framework scripts keep the same relative path | Yes — `<root>/dev-workflow/validate.py` next to `<root>/skills/`. |
| `CLAUDE_PLUGIN_ROOT` or `PLUGIN_ROOT` in the shell | No. Both are unset. |
| Codex gives the model the absolute SKILL.md path | Yes — `<root>/skills/setup/SKILL.md`. |
| Codex fires the SessionStart hook | No. The manifest declares no hook path. |
| Codex hook event names | The binary carries `SessionStart`, `PreToolUse`, `PostToolUse`, `SessionEnd`, `SubagentStart`, `SubagentStop`, `UserPromptSubmit`, `PreCompact`, `PostCompact` and `Interrupt`. |

A local marketplace takes a snapshot of the clone. An edit to the clone does not
reach Codex until the user runs `codex plugin marketplace upgrade` and installs
again.

## Problem 1 — the skills cannot find the framework

Every skill locates its scripts through `${CLAUDE_PLUGIN_ROOT}`. Codex never
sets that variable. Each such command fails on Codex.

### The fix

Each SKILL.md gets one "locate the framework" step. The step runs before the
first script call. The step reads:

> Set `DW` to `$CLAUDE_PLUGIN_ROOT` when that variable is set. Otherwise set `DW`
> to the directory two levels above this SKILL.md file. Use `$DW/dev-workflow/`
> for every framework script.

Claude Code sets the variable, so Claude Code behavior does not change. Codex
falls through to the path anchor. The design adds no marker file and no new
state.

The 7 files are `skills/setup/`, `skills/worktree/`, `skills/standup/`,
`skills/cleanup/`, `skills/release/`, `skills/blog-from-session/` and
`skills/ticket-loop/` SKILL.md. The `ticket-loop` files get the same edit for
consistency. Their runner stays Claude Code only.

## Problem 2 — the SessionStart hook does not fire

Claude Code finds `hooks/hooks.json` on its own. Codex needs an explicit path in
the manifest.

### The fix

Add `.codex-plugin/plugin.json` with these keys:

- `name`, `version`, `description`, `author`, `license`, `repository`, `homepage`
- `"skills": "./skills/"`
- `"hooks": "./hooks/hooks.json"`
- an `interface` block — display name, short description, category

Add `.codex-plugin/marketplace.json` that mirrors the Claude marketplace file.

Keep the logic in `hooks/session-start.sh` as it is. The hook wire schema matches
Claude's schema. Change only the comment that says "Claude Code only".

**Risk — the hook may still not fire.** The team has not tested a Codex hook from
this plugin. The `${PLUGIN_ROOT}` token in `hooks.json` may need a different
spelling on Codex, or Codex may not run plugin hooks in `codex exec` at all. If
the hook does not fire after the work, record that fact and tell Codex users to
start a session with `/standup`. The brief is a convenience. No skill depends on
it.

## Problem 3 — version drift between two manifests

Two manifests now carry the version. `scripts/bump-version.sh` writes one.

### The fix

Extend `.version-bump.json` so the bump writes both manifests. Extend
`scripts/bump-version.sh --check` to fail when the two versions differ.

## Problem 4 — the docs describe one harness

### The fix

In `README.md`:

- Change the prerequisite line to "Claude Code or Codex CLI".
- Add the Codex install block next to the Claude block:
  `codex plugin marketplace add singlas/dev-workflow` then
  `codex plugin add dev-workflow`.
- Add a note that a local marketplace snapshots the clone. Name the refresh
  command.
- State that v2 and v3 run on Claude Code only. Give the reason in one line.

In `AGENTS.md`: replace the stale copy of `CLAUDE.md`. Write a short Codex-side
file that points at the same conventions.

In `skills/setup/SKILL.md`: accept either the `claude` binary or the `codex`
binary in the prereq check.

Also warn in `README.md` and in `/setup`: Codex reads `AGENTS.md`, not
`CLAUDE.md`. A target repo with only a `CLAUDE.md` gives a Codex user the skills
but none of the repo's own instructions. The test repo `pubx-app` has this gap
today.

## Out of scope

- `skills/ticket-loop/cron-run.sh` and the `claude -p` call
- `skills/ticket-loop/usage-parse.py` and the Claude JSON envelope
- `skills/ticket-loop/orchestrator/`
- `skills/ticket-loop/docker/` and `loop-mcp.json`

## Test plan

1. Install the plugin from this clone into Claude Code. Run `/standup` and the
   read-only path of `/worktree` against `pubx-app`. Both must behave as before.
2. Install the plugin from this clone into Codex. Run the same two skills.
3. Confirm that a Codex skill resolves `$DW` and runs `validate.py`.
4. Confirm whether the SessionStart hook fires on Codex. Record the result.
5. Run `scripts/bump-version.sh --check`. It must pass.
6. Change one manifest version by hand. Run `--check` again. It must fail.
