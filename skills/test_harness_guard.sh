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
  grep -q 'CODEX_THREAD_ID' "$ROOT/skills/$name/SKILL.md" \
    && fail "v1 skill skills/$name/SKILL.md must NOT carry the guard" \
    || pass "no guard in skills/$name/SKILL.md"
done

exit "$FAIL"
