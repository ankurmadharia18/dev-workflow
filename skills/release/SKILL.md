---
name: release
description: >-
  The base→prod promotion that DEPLOYS. Run from the canonical repo on the base
  branch (not a worktree) when a batch is ready for prod. It confirms you're on
  the base branch in the main checkout, absorbs any hotfixes (merges prod back
  into base), runs the full test gate, bumps the version file per the configured
  scheme, regenerates the changelog, commits + tags, then opens the base→prod PR
  via gh — merging which triggers the prod deploy. CI runs the full suite on the
  PR (the authoritative gate); pass `--full` to also run it locally first. It
  STOPS after opening the PR — the human merges (merging is what deploys). After
  the merge, announces the release to the team channel. Use when asked to
  "release", "promote to prod", "cut a release", "ship to prod", "deploy to
  production", or "/release". The heavyweight counterpart to cleanup (the everyday
  feature→base PR). NOT for feature work (use cleanup) or board orientation (use
  standup). Repo/dev skill.
---

# release

The deliberate promotion of accumulated base-branch work to prod — the merge that
deploys. Everyday feature work lands on the base branch via `cleanup` and does
**not** deploy; `release` is the gate where a batch becomes a production deploy.
Treat it with care: **the base→prod PR you open here ships the moment it's merged.**

## ⚠️ Safety — this skill can trigger a production deploy

This is the one skill in the framework that leads to prod. Two hard rules bind it:

1. **It REFUSES to run unless `repo.prod_branch` AND `deploy.trigger` are both set
   in `dev-workflow.yml`.** A mis-set deploy trigger is a bad prod push — so the
   skill never guesses what deploys or where. If either is absent, STOP immediately
   and tell the human to configure them (and validate with
   `uv run dev-workflow/validate.py dev-workflow.yml`) before releasing.
2. **It STOPS after opening the base→prod PR. The human merges.** Merging is what
   deploys, and that stays the human's explicit click — never merge it yourself.

**The one deliberate exception to "never push the base or prod branch directly":**
this skill pushes the single version-bump commit to `repo.base_branch` (Step 5) —
an explicit, scoped exception that still reaches prod only via the PR merge. Nothing
else here pushes a long-lived branch.

**BASELINE (framework-side, non-overridable):** never push the base or prod branch
directly beyond the version-bump commit this skill makes on the base branch (which
still lands on prod only via the PR merge); no force-push; deploys only via the
repo's CI-gated promotion; never read secrets (`.env*`, `*.key`, `*.pem`,
`credentials.json`, `~/.claude/**`, `.claude/settings*`); never edit the framework
or `.github/workflows/**`.

## Per-repo configuration (`dev-workflow.yml`)

**Run this preamble ONCE at the start** to resolve the config reader and load every
key this skill uses; the list below explains each. (`release` still refuses unless
`repo.prod_branch` and `deploy.trigger` both come back set — see the safety rules
above.)

**Set `DW_ROOT` before the preamble — on EVERY harness, Claude Code included.**
Neither Claude Code nor Codex exports a plugin-root variable into a skill's
shell, so without this the preamble cannot find the framework and falls through
to a relative path that does not exist in a target repo. Export `DW_ROOT` as the
absolute directory **two levels above this SKILL.md file** — write the path out
in full, quoted, from the location your harness showed you. Examples:
`export DW_ROOT="$HOME/.claude/plugins/cache/dev-workflow/dev-workflow/<version>"`
or `export DW_ROOT="$HOME/.codex/plugins/cache/dev-workflow/dev-workflow/<version>"`.
Skip it ONLY when you are working from a framework checkout, where the
relative fallback is the correct answer.

```bash
if uv run python3 -c pass >/dev/null 2>&1; then PY="uv run"; else PY="python3"; fi                  # uv is unusable where its cache is unwritable (a Codex sandbox)
if command -v dw-config >/dev/null 2>&1 && dw-config 2>&1 | grep -q -- '--batch'; then DW="dw-config"   # hardened install (PATH), only if --batch-capable
elif [ -f "${CLAUDE_PLUGIN_ROOT}/dev-workflow/dw-config.py" ]; then DW="$PY ${CLAUDE_PLUGIN_ROOT}/dev-workflow/dw-config.py"  # plugin install — test the FILE: the harness substitutes the path but not the guard
elif [ -f "${DW_ROOT}/dev-workflow/dw-config.py" ]; then DW="$PY ${DW_ROOT}/dev-workflow/dw-config.py"                        # plugin install (other harness)
else DW="$PY dev-workflow/dw-config.py"; fi                                                          # framework checkout
if [ -f dev-workflow.yml ]; then
  eval "$DW dev-workflow.yml --batch repo.base_branch repo.prod_branch deploy.trigger deploy.announce version.file version.scheme version.changelog quality.test" \
    || echo "ERROR: dev-workflow.yml exists but could not be read — STOP. Do NOT fall back to defaults; a wrong base branch can open a PR into prod."
else
  echo "no dev-workflow.yml — using the skill's missing-config fallbacks"
fi
```

- `repo.base_branch` — the trunk you release *from*. `repo.prod_branch` — the prod
  mirror you release *to* (**required**; refuse if unset).
- `deploy.trigger` — how a merge to prod actually deploys, e.g. `push-main-gha`
  (**required**; refuse if unset). `deploy.announce` — where to announce the
  release (e.g. `telegram`).
- `version.file` — the file holding the version string (e.g. `VERSION`).
  `version.scheme` — how to bump it (see Step 3). `version.changelog` — the command
  that regenerates the changelog view from commits.
- `quality.test` — the full-suite test command (run bare, no `{pkgs}`, for the
  `--full` local gate).

Ticket keys in examples use `ABC-123`. **Ticket state is NOT handled here** —
`cleanup` owns it at the feature-PR stage. `release` doesn't touch the board.

## Modes

Default is lean: CI is the authoritative test gate — it runs the full suite on the
base→prod PR (and again on the prod push), so `release` does **not** re-run the
full suite locally by default. Pass **`--full`** to add the local full-suite
pre-flight (Step 2) when you want to catch a failure before the CI round-trip.

## Preflight — you must be in the right place

0. **Config gate (do this first).** Read `repo.prod_branch` and `deploy.trigger`.
   If either is missing, STOP — do not release (see Safety rule 1).
1. Confirm this is the **canonical repo, not a linked worktree**:
   `git rev-parse --show-toplevel` must equal the `dirname` of
   `git rev-parse --git-common-dir`. If it's a worktree, STOP and tell the human to
   run `release` from the main checkout.
2. Confirm the branch is **`repo.base_branch`** (`git branch --show-current`). If
   not, STOP — don't switch branches for them.
3. `git fetch origin --prune`, then bring the base current:
   `git pull --ff-only origin <repo.base_branch>`. Not fast-forwardable → STOP and
   surface why (someone pushed; reconcile first).
4. Confirm a **clean working tree** (`git status --short` must be empty). If dirty,
   STOP and tell the human exactly what's uncommitted — don't stash it and don't
   commit it for them; a release commits only the version bump, so any stray change
   is theirs to resolve first.

## Step 1 — absorb hotfixes (keep prod an ancestor of the base branch)

Urgent fixes can land on `repo.prod_branch` directly (the hotfix path). If prod has
commits the base doesn't, promoting base→prod would *revert* them. Merge prod into
the base first:

```bash
git merge origin/<repo.prod_branch> --no-edit
```

Clean merge with nothing to absorb → fine. Conflicts → resolve the simple ones,
STOP on anything non-trivial.

## Step 2 — tests: CI is the gate (local run is opt-in via `--full`)

CI runs the **full** suite on the base→prod PR and again on the prod push — that's
the authoritative gate, so the default `release` does **not** re-run it locally.

**Only if invoked with `--full`**, run the local pre-flight to catch a failure
before the round-trip — the bare `quality.test` (no `{pkgs}` → the full suite):

```bash
dw-config dev-workflow.yml quality.test   # -> the command
# run it with no {pkgs} substitution for the full suite
```

If anything fails, STOP and fix before promoting — never open a release PR on a red
suite. Without `--full`: skip this and note "CI is the test gate; no local full
run" in the summary. If the batch is risky (wide migration, config change), suggest
`--full`.

## Step 3 — pick the new version

Read the current `version.file`, then review what's shipping since prod
(`git log origin/<repo.prod_branch>..<repo.base_branch> --oneline`, grouped).
Propose the next version from the change shape and `version.scheme`:

- **`conservative-patch`** — feature *batches* bump the patch segment
  (`0.7.0` → `0.7.1`); a hotfix rides a fourth segment on the next patch. Keeps
  version churn low for a fast-moving solo/small-team trunk.
- **`semver`** — standard semantic versioning: breaking → major, feature → minor,
  fix → patch.

If `version.scheme` is unset or you're unsure which segment applies, **ask the
human** rather than guessing. **Show the proposed version and let them
confirm/override** before writing it to `version.file`.

## Step 4 — regenerate the changelog (generated from commits)

The changelog is generated from git history, not hand-edited. After writing the new
version, regenerate the view so it reflects the bump:

```bash
dw-config dev-workflow.yml version.changelog   # -> the command
# run it; the generated view is not committed
```

Nothing to hand-edit — only `version.file` is committed.

## Step 5 — commit, push the base branch, tag, open the base→prod PR

1. Commit the release bookkeeping on the base branch (the version file only):
   ```bash
   git add <version.file>
   git commit -m "release: version <new> — <one-line theme of the batch>"
   ```
2. Push the base branch: `git push origin <repo.base_branch>`. (This does **not**
   deploy — only prod does.)
3. **Tag the release** — an annotated `v<new>` tag on the bump commit, so prod
   state maps to a tag:
   ```bash
   git tag -a v<new> <bump-sha> -m "Release <new> — <one-line theme>"
   git push origin v<new>
   ```
   (`<bump-sha>` = `git rev-parse HEAD` right after committing.)
4. Open the **base→prod** PR — a real PR via `gh`, never a compare link:
   ```bash
   gh pr create --base <repo.prod_branch> --head <repo.base_branch> \
     --title "Release <new>" --body "<body>"
   ```
   Body: a `## Summary` of the batch (grouped from
   `git log origin/<repo.prod_branch>..<repo.base_branch> --oneline`), the new
   version, a `## Tests` line (the local `--full` result, or "CI is the gate — full
   suite runs on this PR"), and a `## Deploy` note that **merging this PR deploys to
   prod** via `deploy.trigger` (gated on CI).

**STOP here.** Merging the base→prod PR is what deploys — that stays the human's
explicit action. Report the new version, the changelog regen, the test posture, and
the PR URL, and wait.

## Step 6 — announce the release (after the human merges)

Merging is the human's call. Once it's merged — they tell you, or you confirm with
`gh pr view <num> --json state,mergedAt` — announce to `deploy.announce`. When it's
`telegram`, use the bundled bridge (same one the loop uses) — installed on PATH as
`dw-telegram` by the hardened installs, else the plugin-root or framework copy:

```bash
dw-telegram send "🚀 Released v<new> — <one-line theme>. <3-6 highlight bullets>"
# fallbacks when the symlink isn't installed:
#   python3 "${CLAUDE_PLUGIN_ROOT}/skills/ticket-loop/telegram.py" send "…"   (plugin install, Claude Code)
#   python3 "${DW_ROOT}/skills/ticket-loop/telegram.py" send "…"              (plugin install, other harness)
#   python3 /opt/dev-workflow/bin/telegram.py send "…"                        (hardened /opt, or the framework clone's copy)
```

Write the highlights for the people reading the channel — what changed *for them*,
distilled from the changelog, not raw commit subjects. If the session ends before
the merge, say in the summary that the announcement is pending and should be sent
when the PR merges.

## Never

- **Run when `repo.prod_branch` or `deploy.trigger` is unset** — refuse and ask the
  human to configure them (no guessing on anything that deploys).
- Run from a feature worktree, or on any branch but `repo.base_branch`.
- Open a release PR on a failing suite (if you ran `--full` and it's red, STOP).
- **Merge the base→prod PR yourself** — merging deploys to prod; that's the human's
  call.
- Hand-edit a changelog file — it's generated from commits.
- Touch the board — ticket state is `cleanup`'s job.
