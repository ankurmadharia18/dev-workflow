#!/usr/bin/env bash
# SessionStart hook for the dev-workflow plugin. Fires on BOTH Claude Code and
# Codex — Codex discovers hooks/hooks.json by path, with no manifest hooks key.
# Exits silently unless dev-workflow.yml is in the session's CWD.
#
# Purpose: when a session opens in a repo that USES dev-workflow (a dev-workflow.yml
# sits in the cwd), inject a ~5-line orientation so the session knows the skills are
# here. In every OTHER repo it must be SILENT — no output, no noise. That silence is
# the whole contract: the plugin is installed globally, but only speaks up where the
# repo has opted in with a config file.
#
# It never blocks session start: any internal hiccup exits 0 with no output.

# The one gate: no config in the cwd → stay completely silent.
[ -f "./dev-workflow.yml" ] || exit 0

# Cheap, dependency-free read of agent.enabled (the v2 local-agent opt-in). We do NOT
# shell out to uv/dw-config here — the hook must be fast and must not hard-depend on a
# Python toolchain. A tiny awk block-scan over the flat YAML is enough; anything we
# can't read confidently is reported as "unknown".
agent_line="$(awk '
  /^agent:[[:space:]]*$/ { inblk = 1; next }
  /^[^[:space:]#]/       { inblk = 0 }
  inblk && /^[[:space:]]+enabled:/ {
    v = $0
    sub(/^[[:space:]]+enabled:[[:space:]]*/, "", v)
    sub(/[[:space:]]*#.*$/, "", v)
    gsub(/[[:space:]]/, "", v)
    print tolower(v)
    exit
  }
' ./dev-workflow.yml 2>/dev/null || true)"

case "$agent_line" in
  true)         agent_state="ENABLED" ;;
  false|"")     agent_state="off (opt-in via agent.enabled)" ;;
  *)            agent_state="unknown" ;;
esac

# Escape a string for embedding inside a JSON string literal (bash parameter passes;
# same technique as the superpowers hook — a few single C-level passes).
escape_for_json() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/\\r}"
  s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}

context="This repo uses dev-workflow (CI, but for ticket work) — a dev-workflow.yml is present at the repo root.
Session skills: /standup to orient on the board, /worktree to start a fresh slot, /cleanup to ship a PR into the base branch, /release to promote to prod, /setup to (re)configure or check prereqs.
Everything repo-specific (tracker team/roles, branch model, test + lint commands, tightened guardrails) is read from dev-workflow.yml — never hardcode it.
Local autonomous agent tier (v2, the /ticket-loop skill): ${agent_state}.
Daily loop: /standup to start, /worktree for a fresh branch, work, /cleanup to ship. The worktree branch model lives in dev-process/README.md."

escaped="$(escape_for_json "$context")"

# SessionStart context-injection shape. Claude Code defined it; Codex accepts the
# same wire format. printf (not heredoc) to dodge the
# bash 5.3+ heredoc hang seen in the superpowers hook.
printf '{\n  "hookSpecificOutput": {\n    "hookEventName": "SessionStart",\n    "additionalContext": "%s"\n  }\n}\n' "$escaped"

exit 0
