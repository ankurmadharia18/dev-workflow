#!/usr/bin/env bash
# Tests the 4-rung framework-root ladder that every SKILL.md carries.
# Run: bash skills/test_root_ladder.sh
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FAIL=0

fail() { printf 'FAIL: %s\n' "$1"; FAIL=1; }
pass() { printf 'ok: %s\n' "$1"; }

# --- extract the real ladder from the canonical file, not a hand copy ------
# skills/cleanup/SKILL.md is the canonical copy. Extraction runs the actual
# `if/elif/elif/else/fi` lines that ship in the file, so these checks fail if
# the real chain ever breaks - not just a copy of it living in this test.
CANON="$ROOT/skills/cleanup/SKILL.md"
extracted="$(awk '/^if command -v dw-config/{f=1} f{print; if (/^else DW=/) exit}' "$CANON")"

# An extraction that comes back empty, or missing a rung, must FAIL loudly -
# never resolve to an empty $DW and pass by accident.
rung_count="$(printf '%s\n' "$extracted" | grep -c '.')"
if [ "$rung_count" = "4" ] \
  && printf '%s' "$extracted" | grep -q 'dw-config"' \
  && printf '%s' "$extracted" | grep -q 'CLAUDE_PLUGIN_ROOT:-' \
  && printf '%s' "$extracted" | grep -q 'DW_ROOT:-' \
  && printf '%s' "$extracted" | grep -q 'else DW="uv run dev-workflow/dw-config.py"; fi'; then
  pass "extracted the expected four-rung ladder from $CANON"
else
  fail "extraction from $CANON did not yield the expected four rungs (got $rung_count lines): $extracted"
fi

# Run the extracted (real) code under a clean environment plus the case's
# vars, then read back $DW - this exercises the actual chain in the file,
# not a stand-in.
run_extracted() {
  local code="$1"
  shift
  env -i PATH="$PATH" "$@" bash -c "$code"$'\nprintf "%s" "$DW"' 2>/dev/null
}

# rung 2 wins over rung 3 (Claude Code behaviour must not change)
got="$(run_extracted "$extracted" CLAUDE_PLUGIN_ROOT=/claude DW_ROOT=/codex)"
[ "$got" = "uv run /claude/dev-workflow/dw-config.py" ] \
  && pass "CLAUDE_PLUGIN_ROOT wins over DW_ROOT (real chain)" \
  || fail "CLAUDE_PLUGIN_ROOT should win, got: $got"

# rung 3 fires when only DW_ROOT is set (the Codex case)
got="$(run_extracted "$extracted" DW_ROOT=/codex)"
[ "$got" = "uv run /codex/dev-workflow/dw-config.py" ] \
  && pass "DW_ROOT resolves when CLAUDE_PLUGIN_ROOT is unset (real chain)" \
  || fail "DW_ROOT should resolve, got: $got"

# rung 4 still fires when neither is set (framework checkout)
got="$(run_extracted "$extracted")"
[ "$got" = "uv run dev-workflow/dw-config.py" ] \
  && pass "framework-checkout fallback intact (real chain)" \
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

# --- DW_ROOT rung must come AFTER the CLAUDE_PLUGIN_ROOT rung, per file ----
# Byte-identical text does not guarantee correct order - this pins position
# so CLAUDE_PLUGIN_ROOT keeps winning in every file, not only in the canon.
for f in "$ROOT"/skills/*/SKILL.md; do
  claude_line="$(grep -n 'elif \[ -n "${CLAUDE_PLUGIN_ROOT:-}" \]' "$f" | head -1 | cut -d: -f1)"
  dwroot_line="$(grep -n 'elif \[ -n "${DW_ROOT:-}" \]' "$f" | head -1 | cut -d: -f1)"
  if [ -n "$claude_line" ] && [ -n "$dwroot_line" ] && [ "$dwroot_line" -gt "$claude_line" ]; then
    pass "DW_ROOT rung sits after CLAUDE_PLUGIN_ROOT rung in ${f#$ROOT/}"
  else
    fail "DW_ROOT rung is not positioned after CLAUDE_PLUGIN_ROOT rung in ${f#$ROOT/} (claude_line=$claude_line dwroot_line=$dwroot_line)"
  fi
done

# --- bundled paths outside dev-workflow/ must honour DW_ROOT ---------------
grep -q 'WT_ROOT="${CLAUDE_PLUGIN_ROOT:-${DW_ROOT:-}}"' "$ROOT/skills/worktree/SKILL.md" \
  && pass "worktree WT_ROOT honours DW_ROOT" \
  || fail "worktree WT_ROOT does not honour DW_ROOT"

# setup routes both bundled paths through one ROOT= line
grep -q 'ROOT="${CLAUDE_PLUGIN_ROOT:-${DW_ROOT:-.}}"' "$ROOT/skills/setup/SKILL.md" \
  && pass "setup defines ROOT from CLAUDE_PLUGIN_ROOT then DW_ROOT" \
  || fail "setup does not define ROOT from DW_ROOT"

grep -q '${ROOT}/dev-workflow/dev-workflow.example.yml' "$ROOT/skills/setup/SKILL.md" \
  && pass "setup example-config path uses ROOT" \
  || fail "setup example-config path does not use ROOT"

grep -q '${ROOT}/dev-workflow/validate.py' "$ROOT/skills/setup/SKILL.md" \
  && pass "setup validator path uses ROOT" \
  || fail "setup validator path does not use ROOT"

# setup must no longer hardcode the Claude variable in those two paths
grep -q '${CLAUDE_PLUGIN_ROOT}/dev-workflow/validate.py' "$ROOT/skills/setup/SKILL.md" \
  && fail "setup still hardcodes CLAUDE_PLUGIN_ROOT for the validator" \
  || pass "setup no longer hardcodes CLAUDE_PLUGIN_ROOT for the validator"

grep -q 'DW_ROOT}/skills/ticket-loop/telegram.py' "$ROOT/skills/release/SKILL.md" \
  && pass "release telegram fallback honours DW_ROOT" \
  || fail "release telegram fallback does not honour DW_ROOT"

exit "$FAIL"
