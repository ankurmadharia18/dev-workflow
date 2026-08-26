#!/bin/bash
# In-container per-pass driver — the image CMD (/opt/dev-workflow/bin/run-pass.sh),
# invoked by the host systemd oneshot once per scheduled tick. Extra args pass
# through to the skill (e.g. --report, --dry-run).
#
# Runs INSIDE the container as the non-root `agent` user, with /home/agent on a
# persistent volume (the work tree + ~/.claude auth + agent.env + state.json). It
# wires up auth/env from the mounted volume, then hands off to cron-run.sh — which
# does the fetch→reset-to-base, singleton lock, digest step-0, and the pass.
#
# Boundary rule 2: this runner is baked root-owned at /opt/dev-workflow; the ONLY
# writable surface is the mounted volume. Secrets stay OUT of the image — they live
# in agent.env on the volume and are sourced here, never baked or passed as
# unit-level -e (so they never show up in `docker inspect`).
set -euo pipefail

# A scheduler (launchd/systemd) hands us almost no environment — make sure the
# usual tool homes (uv, claude, gh, brew) are reachable in native mode too. In the
# container the image PATH already covers these; prepending is harmless there.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# Where this runner + its siblings (cron-run.sh, dw-config.py) are baked.
DW_ROOT="${DW_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

# ── secrets (600, never read by Claude — consumed here + by the MCP header) ──
# LINEAR_API_KEY (tracker MCP bearer), GH_TOKEN (gh + git push), TELEGRAM_BOT_TOKEN,
# AGENT_TELEGRAM_CHAT_ID, and optionally CLAUDE_CODE_OAUTH_TOKEN. The volume provides
# the file; the unit points us at it via DW_ENV_FILE (default /home/agent/agent.env).
DW_ENV_FILE="${DW_ENV_FILE:-/home/agent/agent.env}"
# TICKET_LOOP_INGRESS is the ORCHESTRATOR's call (it knows whether the Telegram
# ingress daemon is serving this tenant); a stale copy in the tenant's env file
# must not override it and start a second getUpdates consumer.
_dw_ingress="${TICKET_LOOP_INGRESS:-}"
if [ -f "$DW_ENV_FILE" ]; then
  set -a; . "$DW_ENV_FILE"; set +a
  [ -n "$_dw_ingress" ] && export TICKET_LOOP_INGRESS="$_dw_ingress"
else
  echo "WARN: env file $DW_ENV_FILE not found — auth/secrets may be missing" >&2
fi

# The target-repo checkout to drive (the systemd unit sets this; REQUIRED).
: "${DW_WORK_TREE:?DW_WORK_TREE must be set (the target-repo checkout, e.g. /home/agent/<repo>)}"

# Per-repo config (base branch, dependency bootstrap) — read via the baked dw-config.py.
# Preferred runner: `uv run` (dw-config.py carries PEP 723 metadata, uv supplies
# PyYAML). Fallbacks: DW_PYTHON, then a bare python3 — dw-config.py has a stdlib-only
# YAML fallback, so it runs without PyYAML. Same dance as cron-run.sh.
CFG="$DW_WORK_TREE/dev-workflow.yml"
DW_RUN=""
if [ -n "${DW_PYTHON:-}" ]; then
  DW_RUN="$DW_PYTHON"
elif command -v uv >/dev/null 2>&1; then
  DW_RUN="uv run --quiet --no-project"
elif command -v python3 >/dev/null 2>&1; then
  DW_RUN="python3"
fi
cfg() {  # cfg <dotted.path> [default]
  if [ -f "$CFG" ] && [ -f "$DW_ROOT/dw-config.py" ] && [ -n "$DW_RUN" ]; then
    $DW_RUN "$DW_ROOT/dw-config.py" "$CFG" "$@" 2>/dev/null && return 0
  fi
  if [ "$#" -ge 2 ]; then printf '%s\n' "$2"; return 0; fi
  return 1
}
BASE_BRANCH="$(cfg repo.base_branch dev)"

# Manager/parent mode (roster `manager:` → DW_MANAGER, else agent.manager): the
# work tree is a PARENT checkout holding child clones + docs + PM state, not a
# disposable single-repo tree — never reset it (children reset per-child).
MANAGER="${DW_MANAGER:-$(cfg agent.manager false || true)}"
# lowercase first: dw-config prints a YAML boolean as Python True/False (capital).
case "$(printf '%s' "$MANAGER" | tr 'A-Z' 'a-z')" in 1|true|yes|on) MANAGER=1 ;; *) MANAGER=0 ;; esac

# ── bring the work tree current with origin/<base> ──
cd "$DW_WORK_TREE"
if [ "$MANAGER" = 1 ]; then
  echo "manager mode — parent work tree, skipping git reset" >&2
else
  git fetch --quiet origin "$BASE_BRANCH" || echo "WARN: git fetch failed — using current checkout" >&2
  git reset --hard "origin/$BASE_BRANCH" || echo "WARN: git reset failed — using current checkout" >&2
fi

# ── optional dependency bootstrap (e.g. `uv sync`) so the build subagents' quality
# gate can run in-container; a failure only warns (the pass still triages/reports). ──
BOOTSTRAP="$(cfg quality.bootstrap '' || true)"
if [ -n "$BOOTSTRAP" ]; then
  echo "bootstrap: $BOOTSTRAP" >&2
  bash -c "$BOOTSTRAP" || echo "WARN: bootstrap failed — tests may not run" >&2
fi

# gh uses $GH_TOKEN automatically; make git push over HTTPS use it too (idempotent).
if [ -n "${GH_TOKEN:-}" ]; then gh auth setup-git 2>/dev/null || true; fi

# The loop uses the keyed tracker MCP (static bearer, no OAuth) + the baked plugin.
export TICKET_LOOP_MCP_CONFIG="${TICKET_LOOP_MCP_CONFIG:-/opt/dev-workflow/loop-mcp.json}"
export DW_PLUGIN_DIR="${DW_PLUGIN_DIR:-/opt/dev-workflow/plugin}"

exec "$DW_ROOT/cron-run.sh" "$@"
