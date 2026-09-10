---
name: setup
description: >-
  First-run onboarding for the dev-workflow plugin in a repo. Checks prereqs
  (git, uv, gh, the tracker API key), and if the repo has no dev-workflow.yml,
  interviews you for the handful of required values (tracker team + ticket
  prefix, base/prod branch, test + lint commands) and writes one — starting from
  the bundled example — then validates it and points you at the daily worktree
  workflow. Use when someone says "set up dev-workflow", "/setup", "onboard this
  repo", "get me started with dev-workflow", or opens the plugin in a repo that
  has no config yet. It writes ONLY dev-workflow.yml, locally; it never commits,
  pushes, or moves a ticket. NOT for daily work (use standup/cleanup/release) and
  NOT for turning on the autonomous agent unless you explicitly ask about v2.
---

# setup

The plugin's front door. A developer just installed `dev-workflow` and pointed it
at a repo — this skill gets them from "nothing configured" to "`/standup` works"
in one pass: check the tools are present, write a valid `dev-workflow.yml`, and
hand off to the daily workflow.

It is **onboarding, not operation.** It writes exactly one file —
`dev-workflow.yml` at the repo root — and never commits, pushes, moves a ticket,
or touches anything else. Configuration is the human's to own.

## Resolving the bundled framework files

This skill reads two files that ship with the plugin. Resolve them with
`${CLAUDE_PLUGIN_ROOT}` when Claude Code sets it, else with `${DW_ROOT}` (set
below), else — from a framework checkout — drop the prefix and use the
repo-relative path.

**Set `DW_ROOT` first, but only when `CLAUDE_PLUGIN_ROOT` is unset.** Claude Code
sets `CLAUDE_PLUGIN_ROOT` for you; other harnesses (Codex) do not. When it is
unset and this SKILL.md sits inside a plugin cache, export `DW_ROOT` as the
absolute directory **two levels above this SKILL.md file** — write the path out
in full, quoted, from the location your harness showed you. Example:
`export DW_ROOT="$HOME/.codex/plugins/cache/dev-workflow/dev-workflow/<version>"`.
Leave `DW_ROOT` unset when you are working from a framework checkout.

Then set `ROOT` once:

`ROOT="${CLAUDE_PLUGIN_ROOT:-${DW_ROOT:-.}}"`

Then use `${ROOT}` for both bundled files:

- example config — `${ROOT}/dev-workflow/dev-workflow.example.yml`
- validator — `uv run "${ROOT}/dev-workflow/validate.py" dev-workflow.yml`

## 1. Check prerequisites (report, don't fail hard)

Run these and report a tidy checklist — a missing tool is a warning with the fix,
not a stop (the config can still be written):

- **A git repo** — `git rev-parse --is-inside-work-tree`. If this is not a git
  repo, say so and stop: the whole workflow is branch/PR-based.
- **`uv`** — `command -v uv`. Used to run the validator and the config reader with
  PyYAML supplied from cache. If absent, note that `python3` is the fallback and
  point at https://docs.astral.sh/uv/ .
- **`gh`** — `command -v gh` then `gh auth status`. Needed by `cleanup`/`release`
  to open PRs. If unauthenticated, note `gh auth login` as the fix.
- **Tracker API key** — is `LINEAR_API_KEY` present in the environment?
  (`[ -n "$LINEAR_API_KEY" ]` — never print its value.) The board tools and the
  headless loop read it from the environment only, never from config. If absent,
  note that interactive skills using Linear's MCP OAuth still work, but
  `dw-board`/the loop need this key exported.
- **`AGENTS.md`.** When the repo has a `CLAUDE.md` but no `AGENTS.md`, report it
  as a warning on every harness, not just Codex: Codex and most other agents read
  `AGENTS.md`, so the repo's own conventions load nowhere but Claude Code. The fix
  is to make `AGENTS.md` canonical and leave `CLAUDE.md` as a one-line import —
  move the content across, then `echo '@AGENTS.md' > CLAUDE.md`. Claude Code
  expands that import at session start, so nothing is lost and the two files
  cannot drift. This is a warning, not a stop — and never move or write either
  file without asking.

## 2. Write dev-workflow.yml — only if it's missing

**If `dev-workflow.yml` already exists at the repo root:** do NOT overwrite it.
Load its current values in one call and report them, then validate (step 3); offer
to walk through any missing keys the validator flags, editing in place with the
human's confirmation.

`DW_ROOT` is already set (see "Resolving the bundled framework files" above).

```bash
if command -v dw-config >/dev/null 2>&1 && dw-config 2>&1 | grep -q -- '--batch'; then DW="dw-config"   # hardened install (PATH), only if --batch-capable
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then DW="uv run ${CLAUDE_PLUGIN_ROOT}/dev-workflow/dw-config.py" # plugin install (Claude Code)
elif [ -n "${DW_ROOT:-}" ]; then DW="uv run ${DW_ROOT}/dev-workflow/dw-config.py"                       # plugin install (other harness)
else DW="uv run dev-workflow/dw-config.py"; fi                                                          # framework checkout
$DW dev-workflow.yml --batch repo.base_branch repo.prod_branch tracker.provider tracker.team \
  tracker.ticket_prefix quality.test quality.lint agent.enabled=false
```

**If it's absent:** interview the user for the required values, then write the
file. Start from the bundled example so the annotations and optional sections
come along, and fill in these — ask, don't guess (infer a sensible default and
confirm it rather than inventing a value):

- **`repo.base_branch`** — the integration trunk feature PRs land on (often `dev`
  or `main`). Peek at the repo's branches to suggest one.
- **`repo.prod_branch`** — the branch a release promotes to and that deploys
  (often `main`). May equal nothing sensible if there's no prod mirror — ask.
- **`tracker.provider`** — `linear` (the only adapter today).
- **`tracker.team`** — the Linear team/workspace name.
- **`tracker.ticket_prefix`** — the key shape, e.g. `ABC` for `ABC-123`.
- **`quality.test`** — the test command (use `{pkgs}` where a narrow run is
  possible). Look for the repo's test script to suggest one.
- **`quality.lint`** — the linter command.

Leave the optional sections (roles, board, guardrails, schedule, blog, agent)
as the example's commented/annotated defaults unless the user wants to tune them
now. Write the result to `dev-workflow.yml` at the repo root — **this one local
file, nothing else.**

## 3. Validate

Always validate what's on disk before declaring success:

```bash
ROOT="${CLAUDE_PLUGIN_ROOT:-${DW_ROOT:-.}}"
uv run "${ROOT}/dev-workflow/validate.py" dev-workflow.yml
# -> OK: dev-workflow.yml    (or one ERROR: line per violation)
```

If `uv` is absent, fall back to `python3 "${ROOT}/dev-workflow/validate.py"
dev-workflow.yml` (PyYAML required for the validator). Fix any reported errors
with the user before moving on — a config that doesn't validate will trip every
skill.

## 4. Hand off to the daily workflow (v1)

Point the user at how they'll actually work day to day — don't duplicate it here:

- **The immediate next step is `/worktree`** — it sets up the worktree slots (or,
  for a single-checkout developer, just the first fresh feature branch) and, in
  one pass, teaches the branch opinions this workflow runs on. Send them there
  now: "Run `/worktree` to set up your slots and get a fresh branch."
- **Open a session** with `/standup` (board orientation), **close it** with
  `/cleanup` (commit → push → PR into the base branch), **promote to prod** with
  `/release`.
- The branch model is **worktree-based**: one canonical checkout on the base
  branch plus fixed worktree slots, a fresh auto-numbered branch per slot. The
  `/worktree` skill drives it; the narrative + ready-to-copy scripts live in the
  **dev-process playbook** (`dev-process/README.md`, and
  `dev-process/scripts/worktree-reset.sh`).

That's the whole v1 loop: configure once, then `/worktree` → standup → work →
cleanup.

## 5. The autonomous agent (v2) — mention, don't enable

Tell the user, in one line, that a **local autonomous agent tier exists** (the
`ticket-loop` skill + a launchd/cron installer — it works the queue on their
machine and opens one PR per ticket) and that it's **off by default**.

**Only walk through enabling it if the user explicitly asks.** If they do:

1. Add to `dev-workflow.yml`:

   ```yaml
   agent:
     enabled: true
   ```

   (Re-validate.) Without this, both `/ticket-loop` and the installer refuse.
2. Supply the loop's environment — see `skills/ticket-loop/env.example`
   (`TELEGRAM_BOT_TOKEN`, `AGENT_TELEGRAM_CHAT_ID`, `GH_TOKEN`, `LINEAR_API_KEY`).
3. Install the scheduled job with `skills/ticket-loop/install-cron.sh`
   (`--work-tree <this repo>`); see that script's header for the flags.

The **v3 remote Docker runner / multi-project orchestrator** is a separate,
repo-level track — not part of plugin setup. Point interested users at
`skills/ticket-loop/docker/README.md` and the orchestrator runbook; don't set it
up from here.

## Never

- Overwrite an existing `dev-workflow.yml`, or write any file other than it.
- Commit, push, open a PR, or move a ticket — setup only writes the local config.
- Enable the v2 agent (`agent.enabled: true`) unless the user explicitly asks.
- Print or echo the value of `LINEAR_API_KEY` or any other secret.
- Guess a config value silently — infer, then confirm.
