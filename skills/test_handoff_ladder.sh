#!/usr/bin/env bash
# Tests the handoff-helper resolution ladder carried by standup + cleanup, and
# that the runner image actually ships handoff.py where that ladder looks.
#
# Regression guarded here: v0.6.11 shipped the ladder as
#   HANDOFF="${CLAUDE_PLUGIN_ROOT:-${DW_ROOT:-.}}/dev-workflow/handoff.py"
# which tests the VARIABLE, not the FILE. In the runner container
# CLAUDE_PLUGIN_ROOT is set (/opt/dev-workflow/plugin) but carries no
# dev-workflow/, so it resolved to a missing path and python3 exited 2.
#
# Run: bash skills/test_handoff_ladder.sh
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FAIL=0

fail() { printf 'FAIL: %s\n' "$1"; FAIL=1; }
pass() { printf 'ok: %s\n' "$1"; }

# --- extract the real ladder from the shipped files, not a hand copy --------
extract() {  # extract <skill-file> -> the HANDOFF ladder as it ships
  awk '/^HANDOFF=""/{f=1} f{print; if (/^done$/) exit}' "$1"
}

for skill in cleanup standup; do
  f="$ROOT/skills/$skill/SKILL.md"
  code="$(extract "$f")"
  lines="$(printf '%s\n' "$code" | grep -c '.')"
  if [ "$lines" -ge 6 ] \
    && printf '%s' "$code" | grep -q 'CLAUDE_PLUGIN_ROOT:-/nonexistent}/dev-workflow/handoff.py' \
    && printf '%s' "$code" | grep -q 'DW_ROOT:-/nonexistent}/dev-workflow/handoff.py' \
    && printf '%s' "$code" | grep -q '"dev-workflow/handoff.py"' \
    && printf '%s' "$code" | grep -q '\[ -f "\$C" \]'; then
    pass "extracted a file-guarded three-rung ladder from skills/$skill/SKILL.md"
  else
    fail "skills/$skill/SKILL.md does not carry a file-guarded ladder (got $lines lines): $code"
  fi
done

# The ladder must never test the bare variable — that is the v0.6.11 bug.
for skill in cleanup standup; do
  if grep -q 'CLAUDE_PLUGIN_ROOT:-${DW_ROOT:-\.}}' "$ROOT/skills/$skill/SKILL.md"; then
    fail "skills/$skill/SKILL.md still resolves \$HANDOFF from the variable, not the file"
  else
    pass "skills/$skill/SKILL.md does not resolve \$HANDOFF from the bare variable"
  fi
done

# --- run the extracted (real) code under controlled roots ------------------
CODE="$(extract "$ROOT/skills/cleanup/SKILL.md")"
SAFE_PATH="$(dirname "$(command -v bash)")"

run_case() {  # run_case <plugin-root> <dw-root> <cwd> -> prints resolved $HANDOFF
  env -i PATH="$SAFE_PATH" HOME="$HOME" \
    CLAUDE_PLUGIN_ROOT="$1" DW_ROOT="$2" \
    bash -c "cd '$3' && $CODE"$'\n''printf "%s" "$HANDOFF"'
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/good-plugin/dev-workflow" "$TMP/bad-plugin" "$TMP/good-dwroot/dev-workflow" "$TMP/empty"
: > "$TMP/good-plugin/dev-workflow/handoff.py"
: > "$TMP/good-dwroot/dev-workflow/handoff.py"

got="$(run_case "$TMP/good-plugin" "$TMP/good-dwroot" "$TMP/empty")"
[ "$got" = "$TMP/good-plugin/dev-workflow/handoff.py" ] \
  && pass "rung 1 wins when CLAUDE_PLUGIN_ROOT really carries handoff.py" \
  || fail "rung 1: expected the plugin-root copy, got '$got'"

# THE REGRESSION: plugin root set, but no dev-workflow/ under it.
got="$(run_case "$TMP/bad-plugin" "$TMP/good-dwroot" "$TMP/empty")"
[ "$got" = "$TMP/good-dwroot/dev-workflow/handoff.py" ] \
  && pass "a set-but-wrong CLAUDE_PLUGIN_ROOT falls through to DW_ROOT" \
  || fail "set-but-wrong plugin root did not fall through, got '$got'"

# Framework checkout: both roots wrong, cwd has dev-workflow/handoff.py.
got="$(run_case "$TMP/bad-plugin" "$TMP/bad-plugin" "$ROOT")"
[ "$got" = "dev-workflow/handoff.py" ] \
  && pass "rung 3 resolves relative from a framework checkout" \
  || fail "rung 3: expected the relative path, got '$got'"

# Nothing matches: empty, and no crash.
got="$(run_case "$TMP/bad-plugin" "$TMP/bad-plugin" "$TMP/empty")"
[ -z "$got" ] \
  && pass "no rung matches -> \$HANDOFF empty, caller skips the handoff" \
  || fail "expected an empty \$HANDOFF when nothing matches, got '$got'"

# --- the image must ship handoff.py where rung 1 looks ---------------------
DOCKERFILE="$ROOT/skills/ticket-loop/docker/Dockerfile"
if grep -q '^COPY dev-workflow/handoff.py /opt/dev-workflow/plugin/dev-workflow/' "$DOCKERFILE"; then
  pass "Dockerfile copies handoff.py under the plugin root (rung 1 resolves in-container)"
else
  fail "Dockerfile does not copy handoff.py to /opt/dev-workflow/plugin/dev-workflow/"
fi

[ "$FAIL" = 0 ] && { echo "PASS"; exit 0; } || { echo "FAILED"; exit 1; }
