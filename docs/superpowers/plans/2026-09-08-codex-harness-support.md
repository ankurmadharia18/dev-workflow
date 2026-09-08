# Codex CLI Harness Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the 6 v1 session skills work on the Codex CLI, and make the 2 autonomous-loop skills refuse there instead of failing halfway.

**Architecture:** Every skill already resolves the framework root with a three-rung ladder (PATH install → `$CLAUDE_PLUGIN_ROOT` → framework checkout). Codex sets no plugin variable, so the ladder falls through to a path that does not exist. This plan adds a fourth rung, `DW_ROOT`, that the agent fills in from the absolute SKILL.md path its harness shows it. It adds a `CODEX_THREAD_ID` guard to the two loop skills. It adds no new manifest, no hook, and no build-script change.

**Tech Stack:** Markdown skill files, POSIX shell snippets, `bash` test scripts. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-08-codex-harness-support-design.md`

## Global Constraints

- Do **not** create a `.codex-plugin/` directory. Codex loads the skills from the existing `.claude-plugin/`.
- Do **not** add a `hooks` key to any plugin manifest. Codex 0.151 rejects it.
- Do **not** change `scripts/bump-version.sh` or `.version-bump.json`. They already work.
- Do **not** add a harness-binary check to `/setup`. Out of scope.
- Do **not** touch `skills/ticket-loop/cron-run.sh`, `usage-parse.py`, `orchestrator/` or `docker/`.
- Do **not** reassign the shell variable `DW`. It holds the config-reader **command** in all 8 skills. The new directory variable is `DW_ROOT`.
- `hooks/hooks.json` line 9 keeps `${CLAUDE_PLUGIN_ROOT}`. The hook is Claude Code only by design.
- Claude Code behavior must not change. `CLAUDE_PLUGIN_ROOT` stays the second rung and still wins over `DW_ROOT`.
- New shell tests follow the repo idiom: a `test_*.sh` file run as `bash <file>`, exit 0 on pass.
- To re-test in Codex after an edit: `codex plugin add dev-workflow@dev-workflow`, then start a new thread.

---

### Task 1: The fourth rung on the root ladder

The 8 skills carry a byte-identical 3-line ladder. This task makes it 4 lines everywhere, and adds a test that keeps the 8 copies in sync.

**Files:**
- Create: `skills/test_root_ladder.sh`
- Modify: `skills/blog-from-session/SKILL.md:32-34`
- Modify: `skills/cleanup/SKILL.md:35-37`
- Modify: `skills/standup/SKILL.md:35-37`
- Modify: `skills/setup/SKILL.md:62-64`
- Modify: `skills/worktree/SKILL.md:39-41`
- Modify: `skills/release/SKILL.md:58-60`
- Modify: `skills/ticket-loop/SKILL.md:35-37`
- Modify: `skills/ticket-loop-parent/SKILL.md:73-75`

**Interfaces:**
- Consumes: nothing.
- Produces: the shell variable `DW_ROOT` (an absolute directory, or empty) and the 4-rung ladder text. Task 2 reuses `DW_ROOT` for non-`dev-workflow/` paths. Task 3's test file follows the same layout as `skills/test_root_ladder.sh`.

- [ ] **Step 1: Write the failing test**

Create `skills/test_root_ladder.sh`:

```bash
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `bash skills/test_root_ladder.sh`
Expected: FAIL — 8 lines of `FAIL: DW_ROOT rung missing in skills/<name>/SKILL.md`, plus `FAIL: DW_ROOT rung has drifted into 0 variants`. The three `ladder()` assertions pass, because the test defines that function itself.

- [ ] **Step 3: Add the rung to all 8 SKILL.md files**

In each of the 8 files, find this line:

```bash
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then DW="uv run ${CLAUDE_PLUGIN_ROOT}/dev-workflow/dw-config.py" # plugin install
```

Insert one line directly after it, and change the trailing comment on the line above:

```bash
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then DW="uv run ${CLAUDE_PLUGIN_ROOT}/dev-workflow/dw-config.py" # plugin install (Claude Code)
elif [ -n "${DW_ROOT:-}" ]; then DW="uv run ${DW_ROOT}/dev-workflow/dw-config.py"                       # plugin install (other harness)
```

The inserted line must be byte-identical in all 8 files, or the drift assertion fails.

- [ ] **Step 4: Add the DW_ROOT instruction above each preamble**

In each of the 8 files, immediately before the ```` ```bash ```` fence that opens the preamble, insert this paragraph verbatim:

```markdown
**Set `DW_ROOT` first, but only when `CLAUDE_PLUGIN_ROOT` is unset.** Claude Code
sets `CLAUDE_PLUGIN_ROOT` for you; other harnesses (Codex) do not. When it is
unset and this SKILL.md sits inside a plugin cache, export `DW_ROOT` as the
absolute directory **two levels above this SKILL.md file** — write the path out
in full, quoted, from the location your harness showed you. Example:
`export DW_ROOT="$HOME/.codex/plugins/cache/dev-workflow/dev-workflow/0.6.10"`.
Leave `DW_ROOT` unset when you are working from a framework checkout.
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `bash skills/test_root_ladder.sh`
Expected: PASS — every line starts `ok:`, exit code 0.

- [ ] **Step 6: Confirm no Claude-side regression**

Run: `grep -c 'CLAUDE_PLUGIN_ROOT' skills/*/SKILL.md hooks/hooks.json`
Expected: the same counts as before the change — 8 ladder lines untouched, `setup` still 6, `worktree` still 2, `release` still 2, `hooks.json` still 1.

- [ ] **Step 7: Commit**

```bash
git add skills/test_root_ladder.sh skills/*/SKILL.md
git commit -m "feat(skills): add DW_ROOT rung so the root ladder resolves off-Claude

Codex sets neither CLAUDE_PLUGIN_ROOT nor PLUGIN_ROOT, so the three-rung
ladder fell through to a checkout-relative path that does not exist in a
target repo. The new rung takes an absolute DW_ROOT the agent fills in from
the SKILL.md path its harness shows it. CLAUDE_PLUGIN_ROOT still wins, so
Claude Code behaviour is unchanged.

test_root_ladder.sh pins the rung order and keeps the 8 copies identical."
```

---

### Task 2: The paths that do not live under `dev-workflow/`

Three skills reach outside `dev-workflow/` for bundled files. The Task 1 rung does not cover them.

**Files:**
- Modify: `skills/setup/SKILL.md:28-34` (prose + example config + validator), `:95`, `:99`
- Modify: `skills/worktree/SKILL.md:58-60` (`WT=`)
- Modify: `skills/release/SKILL.md:206` (telegram.py comment)
- Modify: `skills/test_root_ladder.sh` (add assertions)

**Interfaces:**
- Consumes: `DW_ROOT` from Task 1 — an absolute directory, or empty.
- Produces: nothing new. Later tasks do not depend on this one.

- [ ] **Step 1: Add the failing assertions**

Append to `skills/test_root_ladder.sh`, before the final `exit "$FAIL"`:

```bash
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
```

- [ ] **Step 2: Run the test to verify the new assertions fail**

Run: `bash skills/test_root_ladder.sh`
Expected: the Task 1 assertions still print `ok:`; the four new ones print `FAIL:`. Exit code 1.

- [ ] **Step 3: Fix `skills/setup/SKILL.md`**

Replace the block at lines 28-34:

```markdown
This skill reads two files that ship with the plugin. Resolve them with
`${CLAUDE_PLUGIN_ROOT}` (Claude Code sets it for plugin skills); from a framework
checkout, drop the prefix and use the repo-relative path:

- example config — `${CLAUDE_PLUGIN_ROOT}/dev-workflow/dev-workflow.example.yml`
- validator — `uv run "${CLAUDE_PLUGIN_ROOT}/dev-workflow/validate.py" dev-workflow.yml`
```

with:

```markdown
This skill reads two files that ship with the plugin. Resolve them with
`${CLAUDE_PLUGIN_ROOT}` when Claude Code sets it, else with `${DW_ROOT}` (see the
preamble above), else — from a framework checkout — drop the prefix and use the
repo-relative path. Write `${ROOT}` below for whichever of the two applies:

- example config — `${ROOT}/dev-workflow/dev-workflow.example.yml`
- validator — `uv run "${ROOT}/dev-workflow/validate.py" dev-workflow.yml`

Set it once: `ROOT="${CLAUDE_PLUGIN_ROOT:-${DW_ROOT:-.}}"`
```

Replace line 95:

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/dev-workflow/validate.py" dev-workflow.yml
```

with:

```bash
ROOT="${CLAUDE_PLUGIN_ROOT:-${DW_ROOT:-.}}"
uv run "${ROOT}/dev-workflow/validate.py" dev-workflow.yml
```

Replace the `uv`-absent sentence at line 99 so it reads:

```markdown
If `uv` is absent, fall back to `python3 "${ROOT}/dev-workflow/validate.py"
dev-workflow.yml` (PyYAML required for the validator).
```

- [ ] **Step 4: Fix `skills/worktree/SKILL.md`**

Replace lines 58-60:

```bash
# script path: plugin install first, framework-checkout fallback second
WT="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/}dev-process/scripts/worktree-reset.sh"
[ -f "$WT" ] || WT="dev-process/scripts/worktree-reset.sh"
```

with:

```bash
# script path: Claude plugin root, then DW_ROOT (other harness), then checkout
WT_ROOT="${CLAUDE_PLUGIN_ROOT:-${DW_ROOT:-}}"
WT="${WT_ROOT:+$WT_ROOT/}dev-process/scripts/worktree-reset.sh"
[ -f "$WT" ] || WT="dev-process/scripts/worktree-reset.sh"
```

- [ ] **Step 5: Fix `skills/release/SKILL.md`**

Replace line 206:

```bash
#   python3 "${CLAUDE_PLUGIN_ROOT}/skills/ticket-loop/telegram.py" send "…"   (plugin install)
```

with:

```bash
#   python3 "${CLAUDE_PLUGIN_ROOT}/skills/ticket-loop/telegram.py" send "…"   (plugin install, Claude Code)
#   python3 "${DW_ROOT}/skills/ticket-loop/telegram.py" send "…"              (plugin install, other harness)
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `bash skills/test_root_ladder.sh`
Expected: PASS — all `ok:`, exit code 0.

- [ ] **Step 7: Commit**

```bash
git add skills/test_root_ladder.sh skills/setup/SKILL.md skills/worktree/SKILL.md skills/release/SKILL.md
git commit -m "feat(skills): honour DW_ROOT for bundled paths outside dev-workflow/

setup reaches for dev-workflow.example.yml and validate.py, worktree for
dev-process/scripts/worktree-reset.sh, release for skills/ticket-loop/
telegram.py. The DW_ROOT rung only covered dev-workflow/, so these three
still broke off-Claude."
```

---

### Task 3: The harness guard on the two loop skills

Codex advertises all 8 skills. `ticket-loop` and `ticket-loop-parent` call `claude -p` and dispatch Claude subagents, so they must refuse rather than fail partway.

**Files:**
- Create: `skills/test_harness_guard.sh`
- Modify: `skills/ticket-loop/SKILL.md` (insert guard before the preamble)
- Modify: `skills/ticket-loop-parent/SKILL.md` (insert guard before the preamble)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: the guard snippet. No later task depends on it.

- [ ] **Step 1: Write the failing test**

Create `skills/test_harness_guard.sh`:

```bash
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `bash skills/test_harness_guard.sh`
Expected: the four `guard()` assertions pass; two lines read `FAIL: guard missing in skills/ticket-loop/SKILL.md` and `FAIL: guard missing in skills/ticket-loop-parent/SKILL.md`. Exit code 1.

- [ ] **Step 3: Add the guard to both loop skills**

In `skills/ticket-loop/SKILL.md` and `skills/ticket-loop-parent/SKILL.md`, insert this section immediately before the `DW_ROOT` paragraph added in Task 1:

````markdown
## 0. Harness check (run before anything else)

This skill runs on Claude Code only. It shells out to `claude -p` and dispatches
Claude subagents; neither exists on another harness.

```bash
if [ -n "${CODEX_THREAD_ID:-}" ] || [ -z "${CLAUDECODE:-}" ]; then
  echo "ticket-loop runs on Claude Code only."
  echo "On this harness use the session skills instead: /standup, /worktree, /cleanup, /release."
  exit 1
fi
```

Stop here when the guard fires. Report the message to the user and do nothing
else. Do not try to emulate the loop by hand.
````

In `skills/ticket-loop-parent/SKILL.md`, change the two echoed lines to say
`ticket-loop-parent` instead of `ticket-loop`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `bash skills/test_harness_guard.sh`
Expected: PASS — all `ok:`, exit code 0.

- [ ] **Step 5: Run the Task 1 test to check for regressions**

Run: `bash skills/test_root_ladder.sh`
Expected: PASS, exit code 0. The guard sits above the preamble and must not have disturbed the ladder.

- [ ] **Step 6: Commit**

```bash
git add skills/test_harness_guard.sh skills/ticket-loop/SKILL.md skills/ticket-loop-parent/SKILL.md
git commit -m "feat(skills): refuse the autonomous loops on non-Claude harnesses

Codex advertises all 8 skills, but ticket-loop and ticket-loop-parent call
claude -p and dispatch Claude subagents. Guard on CODEX_THREAD_ID first,
then on CLAUDECODE being unset -- a Codex child of a Claude shell inherits
CLAUDECODE=1, so order matters. Same refuse-don't-half-run posture as the
existing agent.enabled gate."
```

---

### Task 4: Documentation

**Files:**
- Modify: `README.md` (§1 Quickstart prerequisites, the install block, the skills table intro)
- Modify: `AGENTS.md` (replace the stale copy of `CLAUDE.md`)

**Interfaces:**
- Consumes: the behavior from Tasks 1-3.
- Produces: nothing code-facing.

- [ ] **Step 1: Update the README prerequisite line**

In `README.md` §1, replace:

```markdown
**You need:** [Claude Code](https://docs.anthropic.com/en/docs/claude-code) with
your tracker connected
```

with:

```markdown
**You need:** [Claude Code](https://docs.anthropic.com/en/docs/claude-code) or the
[Codex CLI](https://developers.openai.com/codex/cli) (0.151+), with your tracker
connected
```

- [ ] **Step 2: Add the Codex install block**

In `README.md` §1, directly after the existing `claude --plugin-dir` block, insert:

````markdown
   On the Codex CLI, the same repo installs as a Codex plugin:

   ```
   codex plugin marketplace add singlas/dev-workflow
   codex plugin add dev-workflow@dev-workflow
   ```

   Two differences on Codex. There is **no session brief** — Codex 0.151 does not
   accept a hook from a plugin manifest, so start a session with `/standup`
   instead. And the autonomous tiers (`/ticket-loop`, `/ticket-loop-parent`)
   refuse to run: they need `claude -p` and Claude subagents. The six session
   skills work the same on both.

   Developing against a **local** marketplace: `codex plugin marketplace upgrade`
   refreshes Git marketplaces only. To pick up an edit to a local clone, re-run
   `codex plugin add dev-workflow@dev-workflow` and start a new thread.
````

- [ ] **Step 3: Add the AGENTS.md warning to the README**

In `README.md` §1, after the `/setup` step, insert:

```markdown
   **Codex users:** Codex reads `AGENTS.md`, not `CLAUDE.md`. A repo that has only
   a `CLAUDE.md` gives you the skills but none of its own conventions. Copy or
   symlink it: `ln -s CLAUDE.md AGENTS.md`.
```

- [ ] **Step 4: Rewrite AGENTS.md**

Replace the whole of `AGENTS.md` with:

```markdown
# AGENTS.md

Codex reads this file. Claude Code reads `CLAUDE.md`. The two must agree.

The project overview, repository structure, content guidelines and conventions
live in [CLAUDE.md](CLAUDE.md). Read that file — this one adds only what differs
on Codex.

## What differs on Codex

- **No session brief.** Codex 0.151 does not accept a `hooks` key in a plugin
  manifest, so the SessionStart hook never fires. Start a session with `/standup`.
- **The autonomous tiers refuse.** `/ticket-loop` and `/ticket-loop-parent` need
  `claude -p` and Claude subagents. They stop with a message on Codex. Use the
  session skills: `/setup`, `/worktree`, `/standup`, `/cleanup`, `/release`,
  `/blog-from-session`.
- **`DW_ROOT`.** Codex sets no plugin-root variable. Each skill's preamble tells
  you to set `DW_ROOT` to the directory two levels above its SKILL.md. Do it
  before running the preamble.
- **Reinstall to pick up edits.** `codex plugin add dev-workflow@dev-workflow`,
  then start a new thread.

## Tests

Same idiom as the rest of the repo — run the file directly:

- `python3 dev-workflow/test_validate.py`
- `python3 dev-workflow/test_queue_count.py`
- `python3 skills/ticket-loop/orchestrator/test_orch.py`
- `bash skills/test_root_ladder.sh`
- `bash skills/test_harness_guard.sh`
```

- [ ] **Step 5: Add the AGENTS.md check to `/setup`**

`/setup` reports a prereq checklist. In `skills/setup/SKILL.md` §1, add one more
reported item after the existing checks:

```markdown
- **`AGENTS.md` (Codex only).** When `CODEX_THREAD_ID` is set and the repo has a
  `CLAUDE.md` but no `AGENTS.md`, report it as a warning: Codex reads `AGENTS.md`,
  so this repo's own conventions will not load. Offer `ln -s CLAUDE.md AGENTS.md`.
  This is a warning, not a stop — and never write the file without asking.
```

This reports a condition. It does not add a harness-binary prereq, which stays
out of scope.

- [ ] **Step 6: Verify the README links resolve**

Run: `grep -n 'developers.openai.com/codex' README.md`
Expected: one hit, in the prerequisite line.

- [ ] **Step 7: Confirm the guard test still passes**

Run: `bash skills/test_harness_guard.sh`
Expected: PASS. Step 5 adds `CODEX_THREAD_ID` to `skills/setup/SKILL.md`, and the
guard test asserts that no v1 skill carries it. Update that assertion to match:
the check must look for the guard's `exit 1` block, not the bare variable name.
Change the v1 loop's test to `grep -q 'runs on Claude Code only' "$ROOT/skills/$name/SKILL.md"`.

- [ ] **Step 8: Commit**

```bash
git add README.md AGENTS.md skills/setup/SKILL.md skills/test_harness_guard.sh
git commit -m "docs: document Codex CLI support and its three differences

Codex gets the six session skills, no session brief, and a refusal on the
autonomous tiers. Also records the local-marketplace reinstall step and the
AGENTS.md-vs-CLAUDE.md gap that leaves a Codex user without a target repo's
own conventions.

AGENTS.md stops being a stale sed-copy of CLAUDE.md and points at it."
```

---

### Task 5: End-to-end verification on both harnesses

The first four tasks are verified by shell tests and by reading. This task runs the skills for real. It changes no files.

**Files:** none.

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces: a recorded result. If a step fails, stop and report — do not patch around it.

- [ ] **Step 1: Run the whole test suite**

```bash
python3 dev-workflow/test_validate.py
python3 dev-workflow/test_queue_count.py
python3 skills/ticket-loop/orchestrator/test_orch.py
bash skills/test_root_ladder.sh
bash skills/test_harness_guard.sh
```

Expected: all exit 0.

- [ ] **Step 2: Claude Code — confirm no regression**

In a Claude Code session in `~/repos/aws/pubx-app`, run `/standup`.
Expected: the board brief renders exactly as it did before this branch. `DW_ROOT` is never set, because `CLAUDE_PLUGIN_ROOT` wins.

- [ ] **Step 3: Claude Code — confirm the guard stays quiet**

In the same session, invoke `/ticket-loop`.
Expected: it does **not** refuse. It proceeds to its existing `agent.enabled` check.

- [ ] **Step 4: Install into Codex**

```bash
codex plugin add dev-workflow@dev-workflow
```

Expected: `Added plugin ...` and an `Installed plugin root:` line.

- [ ] **Step 5: Codex — the config reader resolves**

```bash
cd ~/repos/aws/pubx-app
codex exec --sandbox read-only "Run the dev-workflow:standup skill preamble only. \
Set DW_ROOT as its instructions say, then print the resolved \$DW and the value of \
repo.base_branch. Do not move any ticket." < /dev/null
```

Expected: `$DW` reads `uv run <codex cache path>/dev-workflow/dw-config.py`, and `repo.base_branch` prints pubx-app's trunk name.

- [ ] **Step 6: Codex — the non-`dev-workflow/` paths resolve**

```bash
codex exec --sandbox read-only "Using the dev-workflow:worktree skill's WT_ROOT logic, \
print the absolute path it resolves for worktree-reset.sh and whether that file exists. \
Do not create or delete any branch or worktree." < /dev/null
```

Expected: an absolute path under the Codex plugin cache, and `exists: yes`.

- [ ] **Step 7: Codex — the guard fires**

```bash
codex exec --sandbox read-only "Invoke the dev-workflow:ticket-loop skill." < /dev/null
```

Expected: it refuses and prints the Claude-Code-only message. It must not attempt a pass.

- [ ] **Step 8: Record the results and commit nothing**

Report each step's outcome. If steps 5-7 all pass, the branch is ready for `/cleanup`.

---

## Notes for the executor

- The 8 ladder copies must stay byte-identical. `skills/test_root_ladder.sh` fails when they drift, and that is the point — do not "fix" the test by loosening it.
- `DW` is a command, `DW_ROOT` is a directory. Confusing them makes `$DW dev-workflow.yml …` try to execute a directory.
- Step 5 and 6 of Task 5 need the plugin reinstalled after every edit. `codex plugin add dev-workflow@dev-workflow`, then a new thread.
- If Codex turns out to fill `DW_ROOT` in unreliably — say it guesses a path instead of using the one it was shown — stop and report. The fallback is a `dw-config` symlink on PATH (the ladder's first rung), which sidesteps root resolution entirely, but that is a `/setup` change and a different design.
