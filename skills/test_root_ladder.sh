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
extracted="$(awk '/^if uv run python3 -c pass/{f=1} f{print; if (/^else DW=/) exit}' "$CANON")"

# An extraction that comes back empty, or missing a rung, must FAIL loudly -
# never resolve to an empty $DW and pass by accident.
rung_count="$(printf '%s\n' "$extracted" | grep -c '.')"
if [ "$rung_count" = "5" ] \
  && printf '%s' "$extracted" | grep -q 'dw-config"' \
  && printf '%s' "$extracted" | grep -q 'CLAUDE_PLUGIN_ROOT:-' \
  && printf '%s' "$extracted" | grep -q 'DW_ROOT:-' \
  && printf '%s' "$extracted" | grep -q 'else DW="$PY dev-workflow/dw-config.py"; fi' \
  && printf '%s' "$extracted" | grep -q 'if uv run python3 -c pass'; then
  pass "extracted the expected probe + four-rung ladder from $CANON"
else
  fail "extraction from $CANON did not yield the probe plus four rungs (got $rung_count lines): $extracted"
fi

# Run the extracted (real) code under a clean environment plus the case's
# vars, then read back $DW - this exercises the actual chain in the file,
# not a stand-in.
#
# PATH is neutralised to just the directory that holds `bash` itself, not the
# caller's real $PATH. Keeping the real $PATH here is a false-failure risk: on
# any machine that happens to have a --batch-capable `dw-config` installed on
# PATH, rung 1 would win for every case below and all three would fail loudly
# even though the ladder in the file is correct. A bare directory keeps `bash`
# resolvable while keeping rungs 2-4 deterministic regardless of the host.
SAFE_PATH="$(dirname "$(command -v bash)")"
run_extracted() {
  local code="$1"
  shift
  env -i PATH="$SAFE_PATH" "$@" bash -c "$code"$'\nprintf "%s" "$DW"' 2>/dev/null
}

# rung 2 wins over rung 3 (Claude Code behaviour must not change)
got="$(run_extracted "$extracted" CLAUDE_PLUGIN_ROOT=/claude DW_ROOT=/codex)"
case "$got" in
  *" /claude/dev-workflow/dw-config.py") pass "CLAUDE_PLUGIN_ROOT wins over DW_ROOT (real chain)" ;;
  *) fail "CLAUDE_PLUGIN_ROOT should win, got: $got" ;;
esac

# rung 3 fires when only DW_ROOT is set (the Codex case)
got="$(run_extracted "$extracted" DW_ROOT=/codex)"
case "$got" in
  *" /codex/dev-workflow/dw-config.py") pass "DW_ROOT resolves when CLAUDE_PLUGIN_ROOT is unset (real chain)" ;;
  *) fail "DW_ROOT should resolve, got: $got" ;;
esac

# rung 4 still fires when neither is set (framework checkout)
got="$(run_extracted "$extracted")"
case "$got" in
  *" dev-workflow/dw-config.py") pass "framework-checkout fallback intact (real chain)" ;;
  *) fail "checkout fallback broken, got: $got" ;;
esac

# the runner itself must be one the probe can actually pick
case "$got" in
  "uv run "*|"python3 "*) pass "runner is uv run or python3, chosen by probe" ;;
  *) fail "unexpected runner in: $got" ;;
esac

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

# Defining WT_ROOT proves nothing on its own - a reverted `WT=` line (e.g. back
# to `WT="${CLAUDE_PLUGIN_ROOT:+…}"`) would leave WT_ROOT unused and Codex would
# silently fall back to a repo-relative path. Pin that WT= actually consumes it.
grep -q 'WT="${WT_ROOT:+$WT_ROOT/}dev-process/scripts/worktree-reset.sh"' "$ROOT/skills/worktree/SKILL.md" \
  && pass "worktree WT= consumes WT_ROOT" \
  || fail "worktree WT= does not reference WT_ROOT"

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

# --- setup's ROOT= definition must precede its own uses --------------------
# A SKILL.md is read top to bottom by an agent, so `${ROOT}` in the two
# bundled-file bullets must appear AFTER the `ROOT=...` line that defines it,
# not before. Presence-only grep cannot catch a defined-after-use ordering -
# this pins position, the same way the rung-order check above does.
SETUP="$ROOT/skills/setup/SKILL.md"
section_start="$(grep -n '^## Resolving the bundled framework files' "$SETUP" | head -1 | cut -d: -f1)"
section_end="$(awk -v s="$section_start" 'NR>s && /^## /{print NR; exit}' "$SETUP")"
if [ -n "$section_start" ] && [ -n "$section_end" ]; then
  section="$(sed -n "${section_start},${section_end}p" "$SETUP")"
  def_line="$(printf '%s\n' "$section" | grep -n 'ROOT="${CLAUDE_PLUGIN_ROOT:-${DW_ROOT:-.}}"' | head -1 | cut -d: -f1)"
  example_use_line="$(printf '%s\n' "$section" | grep -n '${ROOT}/dev-workflow/dev-workflow.example.yml' | head -1 | cut -d: -f1)"
  validator_use_line="$(printf '%s\n' "$section" | grep -n '${ROOT}/dev-workflow/validate.py' | head -1 | cut -d: -f1)"
  if [ -n "$def_line" ] && [ -n "$example_use_line" ] && [ -n "$validator_use_line" ] \
    && [ "$def_line" -lt "$example_use_line" ] && [ "$def_line" -lt "$validator_use_line" ]; then
    pass "setup defines ROOT before using it in the bundled-files section"
  else
    fail "setup's ROOT= definition does not precede its uses (def_line=$def_line example_use_line=$example_use_line validator_use_line=$validator_use_line)"
  fi
else
  fail "could not locate the 'Resolving the bundled framework files' section bounds in $SETUP"
fi

# --- setup's ROOT= definition must be preceded by the DW_ROOT paragraph, ----
# whole-file, not just within the section above. The within-section check
# above cannot catch a bug where the DW_ROOT-setting paragraph sits in a LATER
# section of the file: an agent reading top to bottom would still hit
# `ROOT=...` with `DW_ROOT` unset. Compare whole-file line numbers, the way
# test_harness_guard.sh's guard_position_ok already does for the harness guard.
dwroot_para_line="$(grep -n '\*\*Set `DW_ROOT` first' "$SETUP" | head -1 | cut -d: -f1)"
root_def_line="$(grep -n 'ROOT="${CLAUDE_PLUGIN_ROOT:-${DW_ROOT:-.}}"' "$SETUP" | head -1 | cut -d: -f1)"
if [ -z "$dwroot_para_line" ] || [ -z "$root_def_line" ]; then
  fail "whole-file DW_ROOT-order lookup returned empty in $SETUP (dwroot_para_line=$dwroot_para_line root_def_line=$root_def_line)"
elif [ "$dwroot_para_line" -lt "$root_def_line" ]; then
  pass "setup's DW_ROOT paragraph precedes ROOT= whole-file"
else
  fail "setup's DW_ROOT paragraph does not precede ROOT= whole-file (dwroot_para_line=$dwroot_para_line root_def_line=$root_def_line)"
fi

# --- the invocation must survive zsh, which does NOT word-split -------------
# Codex runs commands through /bin/zsh. A bare `$DW args` works in bash and
# fails in zsh with "no such file or directory: uv run ...", so every skill
# wraps the call in eval. This asserts that, and proves it behaviourally.
for f in "$ROOT"/skills/*/SKILL.md; do
  if grep -qE '^\s*(&&\s*)?\$DW dev-workflow\.yml' "$f"; then
    fail "bare \$DW invocation in ${f#$ROOT/} breaks under zsh — wrap it in eval"
  else
    pass "invocation is eval-wrapped in ${f#$ROOT/}"
  fi
done

if command -v zsh >/dev/null 2>&1; then
  probe='DW="/bin/echo ok"; eval "$DW split-correctly"'
  z="$(zsh -c "$probe" 2>&1)"
  b="$(bash -c "$probe" 2>&1)"
  [ "$z" = "ok split-correctly" ] && [ "$b" = "$z" ] \
    && pass "eval form word-splits identically in zsh and bash" \
    || fail "eval form differs between shells (zsh=$z bash=$b)"
else
  pass "zsh absent — cross-shell check skipped"
fi

# --- a config that references CLAUDE_PLUGIN_ROOT needs a DW_ROOT rung -------
# Same gap one layer down: on Codex the Claude variable is unset, so a config
# command without a DW_ROOT rung falls through to a checkout-relative path that
# does not exist in a target repo.
EX="$ROOT/dev-workflow/dev-workflow.example.yml"
if grep -q 'CLAUDE_PLUGIN_ROOT' "$EX"; then
  grep -q 'DW_ROOT' "$EX" \
    && pass "example config offers a DW_ROOT rung beside CLAUDE_PLUGIN_ROOT" \
    || fail "example config uses CLAUDE_PLUGIN_ROOT with no DW_ROOT rung — breaks on Codex"
else
  pass "example config does not reference CLAUDE_PLUGIN_ROOT"
fi

exit "$FAIL"
