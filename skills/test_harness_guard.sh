#!/usr/bin/env bash
# Tests the Claude-Code-only guard on the two autonomous-loop skills.
# Run: bash skills/test_harness_guard.sh
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FAIL=0

fail() { printf 'FAIL: %s\n' "$1"; FAIL=1; }
pass() { printf 'ok: %s\n' "$1"; }

# --- the guard under test, kept identical to the one in both SKILL.md ------
guard() {
  if [ -n "${CODEX_THREAD_ID:-}" ] || [ -z "${CLAUDECODE:-}" ]; then
    printf 'refused'
  else
    printf 'allowed'
  fi
}

got="$(CODEX_THREAD_ID= CLAUDECODE=1 guard)"
[ "$got" = "allowed" ] && pass "runs under Claude Code" \
  || fail "should run under Claude Code, got: $got"

got="$(CODEX_THREAD_ID=abc123 CLAUDECODE= guard)"
[ "$got" = "refused" ] && pass "refuses under Codex" \
  || fail "should refuse under Codex, got: $got"

# a Codex process launched from a Claude shell inherits CLAUDECODE=1;
# CODEX_THREAD_ID must still win
got="$(CODEX_THREAD_ID=abc123 CLAUDECODE=1 guard)"
[ "$got" = "refused" ] && pass "CODEX_THREAD_ID beats inherited CLAUDECODE" \
  || fail "inherited CLAUDECODE leaked through, got: $got"

got="$(CODEX_THREAD_ID= CLAUDECODE= guard)"
[ "$got" = "refused" ] && pass "refuses on an unknown harness" \
  || fail "should refuse on unknown harness, got: $got"

# --- both loop skills must carry the guard; no v1 skill may carry it -------
for name in ticket-loop ticket-loop-parent; do
  grep -q 'CODEX_THREAD_ID' "$ROOT/skills/$name/SKILL.md" \
    && pass "guard present in skills/$name/SKILL.md" \
    || fail "guard missing in skills/$name/SKILL.md"
done

for name in setup worktree standup cleanup release blog-from-session; do
  grep -q 'runs on Claude Code only' "$ROOT/skills/$name/SKILL.md" \
    && fail "v1 skill skills/$name/SKILL.md must NOT carry the guard" \
    || pass "no guard in skills/$name/SKILL.md"
done

# --- each loop skill's guard message must name that skill, not the other one ---
grep -q 'ticket-loop-parent runs on Claude Code only\.' "$ROOT/skills/ticket-loop-parent/SKILL.md" \
  && pass "ticket-loop-parent guard message names ticket-loop-parent" \
  || fail "ticket-loop-parent guard message missing or misworded"

if grep -q 'ticket-loop runs on Claude Code only\.' "$ROOT/skills/ticket-loop/SKILL.md" \
  && ! grep -q 'ticket-loop-parent runs on Claude Code only\.' "$ROOT/skills/ticket-loop/SKILL.md"; then
  pass "ticket-loop guard message names ticket-loop (not ticket-loop-parent)"
else
  fail "ticket-loop guard message missing, misworded, or matching ticket-loop-parent's text"
fi

# --- the guard must come FIRST: before its file's config section and ------
# before the DW_ROOT paragraph, not just present somewhere in the file.
# "run before anything else" means the guard's own line number must be the
# smallest of the three - an empty lookup fails loudly, never compares blank.
guard_position_ok() {
  local file="$1" config_heading="$2"
  local guard_line config_line dwroot_line
  guard_line="$(grep -n '^## 0. Harness check' "$file" | head -1 | cut -d: -f1)"
  config_line="$(grep -n "$config_heading" "$file" | head -1 | cut -d: -f1)"
  dwroot_line="$(grep -n '\*\*Set `DW_ROOT` first' "$file" | head -1 | cut -d: -f1)"
  if [ -z "$guard_line" ] || [ -z "$config_line" ] || [ -z "$dwroot_line" ]; then
    fail "position lookup returned empty in ${file#$ROOT/} (guard_line=$guard_line config_line=$config_line dwroot_line=$dwroot_line)"
    return
  fi
  if [ "$guard_line" -lt "$config_line" ] && [ "$guard_line" -lt "$dwroot_line" ]; then
    pass "guard sits before the config section and the DW_ROOT paragraph in ${file#$ROOT/}"
  else
    fail "guard does not precede the config section/DW_ROOT paragraph in ${file#$ROOT/} (guard_line=$guard_line config_line=$config_line dwroot_line=$dwroot_line)"
  fi
}

guard_position_ok "$ROOT/skills/ticket-loop/SKILL.md" '^## Per-repo configuration'
guard_position_ok "$ROOT/skills/ticket-loop-parent/SKILL.md" '^## Parent configuration'

exit "$FAIL"
