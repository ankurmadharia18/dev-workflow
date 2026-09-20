---
name: cleanup
description: >-
  End-of-session ship (feature→base branch). Fast by default: assess the working
  tree, commit anything outstanding, sync + push the feature branch, open/update
  a real PR into the repo's base branch (the integration trunk — merging it does
  NOT deploy), and move the session's completed tickets to the tracker's done
  state. Trusts the session's commit-as-you-go + earlier green tests; CI runs the
  full suite on the PR. Pass `--full` for the thorough dance (hygiene scan +
  re-run the diff's tests regardless). The changelog is generated from commit
  messages, not hand-edited. The everyday counterpart to release (the base→prod
  promotion that deploys). Use when asked to "clean up", "tidy up", "push my
  changes", "wrap up this session", "cleanup and push", or "open a PR" — or when
  the user says they're done and want to push, or there are uncommitted changes
  at end of session. Repo/dev skill.
---

# cleanup

End-of-session tidy-and-ship: commit anything outstanding, push the branch, open
a PR **into the base branch**, and close out the tickets the session completed.

This is the everyday feature→base-branch path. `release` is the heavyweight
base→prod promotion that deploys. `cleanup` is deliberately light, and **its job
is to *finish* the session's work, not redo it.** Invoking it is the human's
explicit authorization to push and open a PR.

## Per-repo configuration (`dev-workflow.yml`)

**Run this preamble ONCE at the start** to resolve the config reader and load every
key this skill uses; the list below explains each. No `dev-workflow.yml` → the
preamble says so and the missing-config fallbacks in the procedure take over.

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
  eval "$DW dev-workflow.yml --batch repo.base_branch repo.prod_branch quality.lint quality.test quality.bootstrap version.changelog tracker.team tracker.roles.done.state tracker.roles.exclude.labels blog.skill blog.posts_dir=docs/blog" \
    || echo "ERROR: dev-workflow.yml exists but could not be read — STOP. Do NOT fall back to defaults; a wrong base branch can open a PR into prod."
else
  echo "no dev-workflow.yml — using the skill's missing-config fallbacks"
fi
```

- `repo.base_branch` — the integration trunk every feature PR targets. Merging a
  PR into it does **not** deploy.
- `repo.prod_branch` — the prod mirror (a hotfix PR targets this instead; see the
  hotfix note below).
- `quality.lint` — the linter (Step 2). `quality.test` — the test command, with
  `{pkgs}` substituted for the changed packages (Step 5). `quality.bootstrap` —
  optional dep-install to run once before testing.
- `version.changelog` — optional command that regenerates the changelog view from
  commits (Step 3).
- `tracker.roles.done.state` — the state completed tickets move to.
  `tracker.roles.exclude.labels` — never auto-close a ticket carrying one.
- `blog` (optional) — when this section is present, the blog offer (Step 8) is
  live: `blog.skill` names the skill to invoke (fallback: the bundled
  `blog-from-session`). Absent → skip Step 8 entirely.

Tracker access is through the canonical verbs (`get_ticket`, `move`, `comment`,
`link_pr`, …) in `dev-workflow/tracker-adapters.md`. Ticket keys below use
`ABC-123`. State/label names always resolve from `tracker.roles`, never a literal.

## Modes — default is FAST; `--full` does the whole dance

**Default (fast).** Trust what the session already did and only do what's
outstanding. By the time you reach cleanup you've usually been committing as you
go and running tests on what you changed, so the common path collapses to:

> **assess → commit what's left → sync-push → open/refresh PR → close tickets → handoff**

Skip the hygiene scan and skip re-running tests that already went green this
session with nothing changed since. CI runs the **full** suite on the base-branch
PR — that's the authoritative gate.

**`--full`.** Run the complete dance regardless: the hygiene scan (Step 2) on
every changed source file, and re-run the diff's relevant tests (Step 5) even if
the session already ran them. Use when the session was long/messy or you weren't
committing as you go.

**Hotfix exception:** an urgent prod fix branches off `repo.prod_branch`; target
the PR at `repo.prod_branch` instead of `repo.base_branch` in Steps 6-7.

## Step 1: Assess the working tree

Inspect the state and decide whether there's anything to ship:

```bash
git branch --show-current                       # refuse if it's the base or prod branch
git status --short                              # dirty?
git diff <base>..HEAD --stat                    # what's changed vs the base branch
git log <base>..HEAD --oneline                  # unpushed commits
```

(`<base>` = `origin/<repo.base_branch>`.) If the branch is long-lived
(`repo.base_branch` / `repo.prod_branch`), STOP — cleanup ships a feature branch,
not the trunk. If the tree is clean AND there's nothing ahead of the base, say
"nothing to ship" and stop.

## Step 2: Hygiene pass — `--full` only (default: skip)

**Default: skip.** Also skip in `--full` if the diff is docs / markdown only.
Otherwise (code changed) a fast mechanical pass on the changed source files —
**not** a code review:

- **Auto-fix mechanically** (silent): run `quality.lint` with its auto-fix flag on
  the changed files, strip trailing whitespace, trim to one trailing newline,
  remove obvious debug-print leftovers. Fold changes into the relevant commit or a
  single `chore: cleanup` commit — not one commit per fix.
- **Flag, don't fix** (one short list, only if present): leftover `TODO`/`FIXME`,
  anything that looks like a hardcoded secret/key, an obviously-dead function in
  the diff. If non-trivial, ask "fix these or push as-is?"; otherwise continue.

CI lint and code review are the real gates — this only sweeps mechanical leftovers.

## Step 3: Changelog — generated from commits, don't hand-edit

The changelog is generated from git commit messages, not a hand-edited file — so
the real work here is making sure each commit is a good changelog line: a
conventional `type(scope): subject` (`feat`/`fix`/`refactor`/`chore`/`docs`/…)
with a body explaining the *why*. `feat` → Added, `fix` → Fixed, everything else →
Changed. If a commit subject is vague, reword it now (amend a not-yet-pushed
commit). If `version.changelog` is configured, optionally regenerate the view:

```bash
dw-config dev-workflow.yml version.changelog   # -> the command (if any)
# run it to eyeball this branch's entries; the generated view is throwaway
```

Never stage/commit a generated changelog view — it's regenerated from commits.

## Step 4: Commit remaining changes

If the tree is still dirty after Steps 2-3: stage the relevant files (never
`.env`/secrets), write a clear conventional commit message based on the diff, and
commit. If everything's already committed, skip.

## Step 5: Tests — default trusts the session; `--full` re-runs the diff's scope

CI runs the **full** suite on the base-branch PR — that's the authoritative gate.
This local step only exists to catch something before the round-trip:

- **Default + already validated this session** (you ran the relevant tests green
  and nothing changed since) → **skip, and say so** ("tests already green this
  session; CI re-runs the full suite"). The common case.
- **Docs / markdown only** → skip.
- **`--full`, or unvalidated runtime changes** (something changed since your last
  green run, or you never ran them) → run the **narrowest** relevant scope via
  `quality.test`, substituting `{pkgs}` with the changed packages — not the whole
  suite. Reserve a full local run for a genuinely cross-cutting change (shared
  config, a base template, a wide migration).

If `quality.bootstrap` is set and dependencies look stale (or `quality.test` fails
with import/dependency errors), run `quality.bootstrap` once before the test command,
then re-run.

**Never push code you have reason to believe is broken** — if you run tests and
any fail, STOP and fix first. Note in the summary whether you ran tests or relied
on the session's green run + CI.

## Step 6: Sync + push + open the PR

The deterministic git dance: bring the base branch in, then push the feature
branch (never the base or prod branch):

```bash
git fetch origin --prune
git merge origin/<repo.base_branch> --no-edit    # STOP on non-trivial conflicts; resolve simple ones
git push origin "$(git branch --show-current)"
```

If merging the base hits conflicts, resolve the simple ones and STOP on anything
non-trivial, then re-run. Then open or update the PR against `repo.base_branch`
(idempotent — never a second PR for the same branch):

```bash
gh pr view --json url,state -q .url    # already have one?
```

**Always create a real PR with `gh pr create` — never just print a `…/compare`
link.** Confirm it returned a real `…/pull/<number>` URL before reporting success.

- **No open PR** → create one. Title: a concise summary of the branch's work.
  Body: a `## Summary` (group `git log origin/<base>..HEAD --oneline` by theme), a
  `## Test scope` line (which tests ran + result, or "relied on session green +
  CI"), and a `## Test plan` checklist.
  ```bash
  gh pr create --base <repo.base_branch> --head "$(git branch --show-current)" \
    --title "<summary>" --body "<body>"
  ```
- **Open PR exists** → `gh pr edit --body "<refreshed body>"`.

Record a checkpoint (`handoff.py checkpoint`) so a session resuming on this
branch knows where the PR got to:

```bash
HANDOFF=""
for C in "${CLAUDE_PLUGIN_ROOT:-/nonexistent}/dev-workflow/handoff.py" \
         "${DW_ROOT:-/nonexistent}/dev-workflow/handoff.py" \
         "dev-workflow/handoff.py"; do
  [ -f "$C" ] && { HANDOFF="$C"; break; }
done
[ -n "$HANDOFF" ] && python3 "$HANDOFF" checkpoint
```

Test the FILE, not the variable: a set-but-wrong root (the container sets
`CLAUDE_PLUGIN_ROOT` to a plugin copy that carries no `dev-workflow/`) must fall
through to the next rung, not resolve to a path that does not exist. If no rung
matches, skip the checkpoint — never fail the ship over it.

Append one line of prose to the note first if anything is still outstanding —
review comments expected, CI still running, a follow-up already known.

(Hotfix: swap `repo.base_branch` → `repo.prod_branch` so the PR targets prod.)

## Step 7: Close out completed tickets

The tracker is the source of truth for ticket state, and **ticket state lives here,
not in `release`.** Now that the work is pushed and in a PR, move the tickets this
branch completed to `tracker.roles.done.state`.

1. **Gather candidate keys** (`ABC-\d+`) from the branch name and commits:
   ```bash
   git branch --show-current
   git log origin/<repo.base_branch>..HEAD --format='%s%n%b'
   ```
   Add any keys from the commit messages / PR body. Deduplicate.
2. **Check each** via `get_ticket`. Skip any already in the done/canceled state.
3. **Decide which actually shipped in this branch — read the commits, don't trust
   the branch name alone.** Completed by this diff → close. Partially done → leave
   it in progress, say what's left. **Never auto-close a ticket carrying a
   `tracker.roles.exclude.labels` entry** — list those for the human instead.
4. **`move` the completed ones** to `tracker.roles.done.state`.
5. If no keys surface anywhere, say "No tickets referenced — nothing to close."

## Step 8: Blog offer — optional, default SKIP

**Default: skip, and say nothing.** This step exists ONLY when the repo's
`dev-workflow.yml` has a `blog:` section AND the session produced an obviously
sharp, non-obvious learning — a technique that worked, a gotcha with a real fix, a
decision worth explaining. Routine work, a plain bug fix, or "shipped a thing" is
**not** it.

When both hold: name the learning in **one line** and ask —

> "This looks like a post about `<X>` — want me to write it?"

On **yes**, invoke the skill named in `blog.skill` (fallback: the bundled
`blog-from-session`), which writes ONE local draft and stops. On **no** (or no
answer), continue to handoff. **Never manufacture an angle** just to have
something to offer, and **never auto-write** — the offer is the whole action here;
the human's yes is what triggers the draft.

## Step 9: Session handoff (last output)

End with a compact **session handoff** so a fresh session can pick up cold:

**Shipped** — 2-4 bullets of what actually changed (the "what" + why it mattered),
not a commit dump. Fold in: test scope + result, the PR URL, tickets moved to the
done state (and any left open, with why).

**Carry into next session** — the non-obvious context *not* already in the
diff/PR/`git log`: decisions + rationale, deliberate deferrals, a gotcha hit along
the way. If nothing, say "nothing beyond the PR."

**Natural next steps** — 2-3 concrete follow-ons (real ticket keys where one
exists). Suggestions, not commitments — don't start them.

Close with the reminder that merging the PR lands the work on `repo.base_branch`
and does **not** deploy — prod ships only when the base branch is promoted to
`repo.prod_branch` via `release`.
