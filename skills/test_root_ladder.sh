#!/usr/bin/env bash
# Tests the 4-rung framework-root ladder that every SKILL.md carries.
# Run: bash skills/test_root_ladder.sh
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FAIL=0

fail() { printf 'FAIL: %s\n' "$1"; FAIL=1; }
pass() { printf 'ok: %s\n' "$1"; }

# --- the ladder under test, kept identical to the one in every SKILL.md -----
ladder() {
  if command -v dw-config >/dev/null 2>&1 && dw-config 2>&1 | grep -q -- '--batch'; then DW="dw-config"
  elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then DW="uv run ${CLAUDE_PLUGIN_ROOT}/dev-workflow/dw-config.py"
  elif [ -n "${DW_ROOT:-}" ]; then DW="uv run ${DW_ROOT}/dev-workflow/dw-config.py"
  else DW="uv run dev-workflow/dw-config.py"; fi
  printf '%s' "$DW"
}

# rung 2 wins over rung 3 (Claude Code behaviour must not change)
got="$(CLAUDE_PLUGIN_ROOT=/claude DW_ROOT=/codex ladder)"
[ "$got" = "uv run /claude/dev-workflow/dw-config.py" ] \
  && pass "CLAUDE_PLUGIN_ROOT wins over DW_ROOT" \
  || fail "CLAUDE_PLUGIN_ROOT should win, got: $got"

# rung 3 fires when only DW_ROOT is set (the Codex case)
got="$(CLAUDE_PLUGIN_ROOT= DW_ROOT=/codex ladder)"
[ "$got" = "uv run /codex/dev-workflow/dw-config.py" ] \
  && pass "DW_ROOT resolves when CLAUDE_PLUGIN_ROOT is unset" \
  || fail "DW_ROOT should resolve, got: $got"

# rung 4 still fires when neither is set (framework checkout)
got="$(CLAUDE_PLUGIN_ROOT= DW_ROOT= ladder)"
[ "$got" = "uv run dev-workflow/dw-config.py" ] \
  && pass "framework-checkout fallback intact" \
  || fail "checkout fallback broken, got: $got"

# --- every SKILL.md must carry the DW_ROOT rung ----------------------------
for f in "$ROOT"/skills/*/SKILL.md; do
  grep -q 'DW_ROOT:-' "$f" \
    && pass "DW_ROOT rung present in ${f#$ROOT/}" \
    || fail "DW_ROOT rung missing in ${f#$ROOT/}"
done

# --- the 8 copies must stay identical --------------------------------------
copies="$(grep -h 'elif \[ -n "${DW_ROOT:-}" \]' "$ROOT"/skills/*/SKILL.md | sort -u | wc -l | tr -d ' ')"
[ "$copies" = "1" ] \
  && pass "all DW_ROOT rungs are byte-identical" \
  || fail "DW_ROOT rung has drifted into $copies variants"

exit "$FAIL"
