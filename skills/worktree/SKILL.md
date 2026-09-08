---
name: worktree
description: >-
  Set up and reset git worktrees for the dev-workflow branch model. Use when
  someone says "reset my worktree", "fresh worktree", "new branch for this
  ticket", "start a fresh slot", "set up worktrees", "parallel worktrees",
  "worktree slots", or "/worktree". Two modes: BOOTSTRAP (first run) offers the
  canonical checkout + 2-4 parallel slot layout and creates them; RESET (the
  default, run from inside a slot) mints a fresh auto-numbered feature branch off
  the latest integration trunk, relinks shared per-machine state, sweeps finished
  branches, and installs deps. It also teaches the branch opinions every developer
  absorbs — feature branch → PR into the base branch (merge does NOT deploy) →
  release promotes base→prod (the human's merge deploys) — so even a single-worktree
  linear developer never sits on the trunk. Its only outward action is deleting
  REMOTE branches already merged into the trunk (skippable). NOT for shipping a PR
  (use cleanup), promoting to prod (use release), or board orientation (use standup).
---

# worktree

Get a developer a clean place to start the next ticket, and teach the branch
opinions while doing it. This skill wraps `dev-process/scripts/worktree-reset.sh`
— the mature, safety-critical worktree tool — and adds the judgment around it:
which mode you're in, what the script is about to do, and *why the branch model
is shaped this way.*

It is **branch/worktree surgery, nothing more.** The only action that leaves
your machine is the script's sweep of REMOTE branches already merged into the
trunk (and that's skippable). This skill never pushes your work, never opens a
PR, never touches the tracker — that's `cleanup` and `release`.

## Per-repo configuration (`dev-workflow.yml`)

**Run this preamble ONCE at the start** to resolve the config reader and load the
keys this skill uses. No `dev-workflow.yml` → the preamble says so and the script's
own defaults (trunk `dev`, prod `main`) take over.

**Set `DW_ROOT` first, but only when `CLAUDE_PLUGIN_ROOT` is unset.** Claude Code
sets `CLAUDE_PLUGIN_ROOT` for you; other harnesses (Codex) do not. When it is
unset and this SKILL.md sits inside a plugin cache, export `DW_ROOT` as the
absolute directory **two levels above this SKILL.md file** — write the path out
in full, quoted, from the location your harness showed you. Example:
`export DW_ROOT="$HOME/.codex/plugins/cache/dev-workflow/dev-workflow/0.6.10"`.
Leave `DW_ROOT` unset when you are working from a framework checkout.

```bash
if command -v dw-config >/dev/null 2>&1 && dw-config 2>&1 | grep -q -- '--batch'; then DW="dw-config"   # hardened install (PATH), only if --batch-capable
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then DW="uv run ${CLAUDE_PLUGIN_ROOT}/dev-workflow/dw-config.py" # plugin install (Claude Code)
elif [ -n "${DW_ROOT:-}" ]; then DW="uv run ${DW_ROOT}/dev-workflow/dw-config.py"                       # plugin install (other harness)
else DW="uv run dev-workflow/dw-config.py"; fi                                                          # framework checkout
[ -f dev-workflow.yml ] \
  && $DW dev-workflow.yml --batch repo.base_branch repo.prod_branch quality.bootstrap \
  || echo "no dev-workflow.yml — the script falls back to its dev/main defaults"
```

- `repo.base_branch` — the integration trunk; feature branches start here and PR
  back here. Exported to the script as **`WORKTREE_TRUNK`**.
- `repo.prod_branch` — the prod mirror; only a base→prod PR (or a hotfix PR)
  advances it. Exported as **`WORKTREE_PROD`**.
- `quality.bootstrap` — optional per-worktree dep-install command. Exported as
  **`WORKTREE_DEPS_CMD`**, which the script runs after a reset.

**Always invoke the script with those env vars set from the config**, so the
whole branch model follows this repo's names (never assume `dev`/`main`):

```bash
# script path: Claude plugin root, then DW_ROOT (other harness), then checkout
WT_ROOT="${CLAUDE_PLUGIN_ROOT:-${DW_ROOT:-}}"
WT="${WT_ROOT:+$WT_ROOT/}dev-process/scripts/worktree-reset.sh"
[ -f "$WT" ] || WT="dev-process/scripts/worktree-reset.sh"
WORKTREE_TRUNK="<repo.base_branch>" WORKTREE_PROD="<repo.prod_branch>" \
  WORKTREE_DEPS_CMD="<quality.bootstrap>" bash "$WT" [args]
```

(Omit an env var whose config key is absent — the script defaults it.)

## The branch model — state this up front, every time

This is the teaching surface. Whether the developer runs 4 parallel slots or one
lonely checkout, the opinions are the same:

| Branch | Role | Merging it… |
|---|---|---|
| feature `<slot>-N` | Short-lived. One per ticket, off the latest trunk. | opens a PR into the trunk |
| `base_branch` (trunk, e.g. `dev`) | Integration trunk. Feature PRs land here. | does **NOT** deploy |
| `prod_branch` (prod, e.g. `main`) | Prod mirror. Advances only via a release PR. | **DEPLOYS** (the human's click) |

```
feature-a-7 ──/cleanup──► PR into <trunk>  ──merge──►  <trunk>   (no deploy)
                                                          │
                                              /release: <trunk>→<prod> PR
                                                          │
                                                   human merges ──► DEPLOY
```

The rules, stated plainly:

- **Never work on the trunk or the prod branch directly** — not even with a
  single worktree. You branch *off* them; you don't commit *on* them. The script
  refuses to put a worktree on a long-lived branch for exactly this reason.
- A feature branch **PRs into the base branch via `/cleanup`** — and merging that
  PR **does not deploy.** Trunk merges are cheap and frequent.
- **`/release` promotes base→prod**, and the human's merge of that PR is what
  deploys. Deploys are always deliberate.
- **Hotfix:** an urgent prod fix branches off prod (`--hotfix`), PRs straight to
  prod (that deploys), then back-merges prod→trunk so the next release doesn't
  revert it.

The full narrative behind these opinions is
[`dev-process/README.md`](../../dev-process/README.md) — read it if you need the
CI shape or the one-time GitHub setup.

## Which mode? Detect first

```bash
git worktree list        # one line = just the canonical checkout; several = slots exist
git rev-parse --show-toplevel
git rev-parse --git-common-dir
```

- The repo has **only the canonical checkout** (one `git worktree list` line), or
  the developer says "set up worktrees" → **Bootstrap mode**.
- The developer is **inside a slot** and wants a fresh branch ("reset", "new
  branch for this ticket") → **Reset mode** (the default).

## Bootstrap mode — first run / "set up worktrees"

The developer has no slots yet. Offer the layout the playbook documents (read
[`dev-process/README.md`](../../dev-process/README.md) §3 and follow ITS slot
naming): one canonical checkout that stays on the trunk, plus a handful of fixed
**worktree slots** as sibling directories.

1. **Recommend 2-4 slots** (`feature-a` … `feature-d`) — enough to run parallel
   tickets without collisions, few enough to keep straight. The slot dir
   basenames **must match `feature-[a-z]`**: the script's garbage collection
   exempts exactly that pattern, so a differently-named dir would get swept as a
   dead worktree. Use the sibling-directory layout from the playbook (e.g.
   `../<repo>-worktrees/feature-a`).
2. **Create each slot** with `git worktree add` (start it detached or on a
   throwaway ref — the reset gives it a real branch), then **link shared state**
   by running the script with `--link` from inside each:

   ```bash
   git worktree add --detach ../<repo>-worktrees/feature-a
   ( cd ../<repo>-worktrees/feature-a && WORKTREE_TRUNK=… WORKTREE_PROD=… bash "$WT" --link )
   ```

   `--link` only (re)creates the shared symlinks (`.env`, local scratch, agent
   settings, driven by the repo's `.worktree-shared` manifest) — it does not
   branch or install deps. Do a full reset per slot when they're ready to work.
3. **Single-worktree developer may decline slots.** That's fine — the opinions
   still bind. Teach them: they branch for each ticket via the script (from
   inside their checkout) or a plain `git switch -c <name> origin/<trunk>`, and
   **never sit on the trunk.** They just skip the parallel-slot layout.

## Reset mode — the default (run from inside a slot)

Starting the next ticket in a slot. This is `worktree-reset.sh` with no mode flag.

1. **Confirm no uncommitted work.** The script refuses on a dirty tree anyway
   (tracked changes → it stops and tells you to commit / stash / ship first), but
   check `git status --short` first and, if there's unshipped work, point the
   developer at `/cleanup` before resetting.
2. **Run the reset** (env vars from the preamble):

   ```bash
   WORKTREE_TRUNK=… WORKTREE_PROD=… WORKTREE_DEPS_CMD=… bash "$WT"
   ```

3. **Explain what happened** in plain terms:
   - A **fresh `<slot>-N`** branch was minted at the latest `origin/<trunk>`
     (auto-numbered — never a reused name, so it always starts from the current
     trunk and lets the previous ticket's PR stay open).
   - **Finished branches were swept:** dead worktrees, and every branch (local +
     remote) already merged into the trunk. Merged branches lose no commits.
   - Shared per-machine state was relinked; this slot's own deps were installed
     (if `quality.bootstrap` is set).

**Surface the useful flags** when they fit:

- **`--hotfix`** — urgent prod fix: bases the fresh branch off `origin/<prod>`
  instead of the trunk. PR it straight to prod, then back-merge prod→trunk.
- **`--gc`** — sweep only (dead worktrees + merged local/remote branches), runs
  from anywhere including the canonical repo. Good hygiene between tickets.
- An explicit name (`bash "$WT" feat-x`) instead of the auto-numbered `<slot>-N`,
  and `--force` to discard unmerged commits on a reused name.

## Safety

- The **only outward action** this skill can cause is the script's sweep of
  REMOTE branches already merged into the trunk (`git push origin --delete`).
  Pass **`--keep-remote`** to skip it (e.g. if GitHub already auto-deletes merged
  head branches, or you want a purely local reset).
- This skill **never pushes your work, never opens a PR, never moves a ticket.**
  Shipping is `/cleanup`; promoting to prod is `/release`.
- The script **refuses** to put a worktree on a long-lived branch (trunk/prod/
  `master`) and refuses on a dirty tree — don't work around either; they protect
  the shared `.git`.

## Never

- Put a worktree on, or commit directly to, the trunk or prod branch — the whole
  point of the model is that work happens on short-lived feature branches.
- Push, open a PR, or move a ticket — this skill only does branch/worktree setup.
- Name a slot directory anything but `feature-[a-z]` — GC would sweep it.
- Reset a slot with unshipped, uncommitted work — send them to `/cleanup` first.
