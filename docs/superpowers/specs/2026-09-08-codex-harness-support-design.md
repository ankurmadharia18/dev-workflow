# Codex CLI harness support (v1 session skills) — design

**Date:** 2026-09-08
**Status:** approved design, pre-implementation. Revision 2, after a Codex review.
**Scope:** v1 session skills only. v2 and v3 stay Claude Code only.
**Files touched:** 8 `skills/*/SKILL.md`, `README.md`, `AGENTS.md`

## Summary

Make the dev-workflow plugin work on the Codex CLI as well as Claude Code. A
Codex user installs the same plugin from the same repo, then runs `/setup`,
`/worktree`, `/standup`, `/cleanup`, `/release` and `/blog-from-session`.

The autonomous tiers stay on Claude Code. `cron-run.sh` calls `claude -p`.
`usage-parse.py` reads the Claude JSON envelope. The Docker image installs the
`claude` CLI. The `ticket-loop` and `ticket-loop-parent` skills use Claude
subagents. This design does not port them. It makes them refuse on Codex.

The work is two changes: one new rung on the root-resolution ladder that every
skill already has, and a harness guard on the two loop skills. Plus docs.

## Verified facts

The team verified these facts on Codex CLI 0.151.0. The test repo was
`~/repos/aws/pubx-app`, which has a `dev-workflow.yml`.

| Fact | Result |
|---|---|
| Codex reads `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` | Yes. All 8 skills loaded as `dev-workflow:<name>`. |
| Codex copies the whole repo into its plugin cache | Yes — `~/.codex/plugins/cache/dev-workflow/dev-workflow/<version>/`. |
| Framework files keep the same relative path | Yes — `<root>/dev-workflow/`, `<root>/dev-process/` and `<root>/skills/` all present. |
| `CLAUDE_PLUGIN_ROOT` or `PLUGIN_ROOT` in the shell | No. Both are unset. |
| Codex gives the model the absolute SKILL.md path | Yes — `<root>/skills/setup/SKILL.md`. |
| Codex fires the SessionStart hook | **Yes.** Corrected 2026-09-10 — see the note below. |
| A Codex plugin manifest accepts a `hooks` key | No. The validator's `allowed_keys` omits `hooks`. The bundled plugin guidance says to omit it. |
| Codex exports a harness marker | Yes — `CODEX_THREAD_ID`, `CODEX_SESSION_ID`, `CODEX_SANDBOX`. |
| Codex sets any `CLAUDE_*` variable | No. A probe from a clean environment shows none. |
| `codex plugin marketplace upgrade` refreshes a local marketplace | No. It reports "No configured Git marketplaces to upgrade." |
| `codex plugin add <plugin>@<marketplace>` re-copies an edited clone | Yes, at the same version. No version change is needed. |

**How the team checked the marker.** A first probe ran from a Claude Code shell.
That Codex process inherited `CLAUDECODE=1` and the other `CLAUDE_*` variables
from its parent. A second probe ran under `env -i` with only `HOME`, `PATH`,
`TERM` and `SHELL`. It showed the `CODEX_*` variables and no `CLAUDE_*` variable.
So `CLAUDECODE` belongs to Claude Code, and `CODEX_THREAD_ID` belongs to Codex.

## Decisions taken

1. **No `.codex-plugin/` manifest.** Codex already loads the skills from
   `.claude-plugin/`. Without a `hooks` key, a second manifest buys only display
   metadata, and it costs five version-coupling points: `.version-bump.json`,
   `dev-workflow.yml`, `scripts/changelog.sh`, `scripts/howto-broadcast.sh` and
   the `/release` staging path. Not worth it.
2. ~~**No SessionStart hook on Codex.**~~ **Withdrawn 2026-09-10.** Codex does
   fire the hook. See the correction note below. The decision that survives is
   the narrower one: the manifest still carries no `hooks` key, because the
   validator rejects it — Codex finds `hooks/hooks.json` by path instead.
3. **The loop skills refuse on Codex.** `ticket-loop` and `ticket-loop-parent`
   stop with a clear message rather than fail halfway.

## Change 1 — a fourth rung on the root ladder

Every skill already resolves the framework root with the same three-rung ladder:

```sh
if command -v dw-config …; then DW="dw-config"                                    # PATH install
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then DW="uv run ${CLAUDE_PLUGIN_ROOT}/…"   # plugin install
else DW="uv run dev-workflow/dw-config.py"; fi                                    # framework checkout
```

`DW` holds a **command**, not a directory. Do not reassign it.

On Codex the first rung misses unless the user hardened the install, the second
rung misses because the variable is unset, and the third rung misses because the
cwd is the target repo, not the framework checkout. The ladder falls through to a
path that does not exist.

### The fix

Add a rung between the second and the third. The rung uses the absolute SKILL.md
path that the harness shows the agent. The agent interpolates that path; no shell
variable holds it. Introduce a new variable named `DW_ROOT` for the directory, so
nothing collides with `DW`.

> Set `DW_ROOT` as follows. Use `$CLAUDE_PLUGIN_ROOT` when that variable is set.
> Otherwise, when this SKILL.md sits under a plugin cache, use the directory two
> levels above this file, quoted, written out in full. Otherwise leave `DW_ROOT`
> empty and use paths relative to the framework checkout.

Claude Code sets `CLAUDE_PLUGIN_ROOT`, so its behavior does not change.

### Every site to change

16 references across 9 files.

| File | Sites |
|---|---|
| `skills/blog-from-session/SKILL.md` | ladder (line 33) |
| `skills/cleanup/SKILL.md` | ladder (36) |
| `skills/standup/SKILL.md` | ladder (36) |
| `skills/setup/SKILL.md` | ladder (63), prose (30), example config (33), validator (34, 95, 99) |
| `skills/worktree/SKILL.md` | ladder (40), `WT=` → `dev-process/scripts/worktree-reset.sh` (59) |
| `skills/release/SKILL.md` | ladder (59), `telegram.py` comment (206) |
| `skills/ticket-loop/SKILL.md` | ladder (36) |
| `skills/ticket-loop-parent/SKILL.md` | ladder (74) |
| `hooks/hooks.json` | line 9 — leave as is; the hook is Claude Code only |

Note the paths outside `dev-workflow/`: `worktree` reaches into `dev-process/`,
`release` reaches into `skills/ticket-loop/`. `DW_ROOT` must serve all of them.

## Change 2 — a harness guard on the loop skills

Codex advertises all 8 skills. `ticket-loop` and `ticket-loop-parent` cannot run
there: they call `claude -p` and they dispatch Claude subagents.

Add a first step to both SKILL.md files:

```sh
if [ -n "${CODEX_THREAD_ID:-}" ] || [ -z "${CLAUDECODE:-}" ]; then
  echo "ticket-loop runs on Claude Code only. Use /standup, /worktree, /cleanup."
  exit 1
fi
```

Stop there. Say that the autonomous tiers are Claude Code only, and point the
user at the v1 session skills. This matches how `agent.enabled` already gates
these skills — refuse, do not half-run.

The test carries both halves on purpose. `CODEX_THREAD_ID` catches Codex.
`CLAUDECODE` unset catches any other harness. A Codex process launched from a
Claude Code shell inherits `CLAUDECODE=1`, so the first half must come first.

## Change 3 — docs

In `README.md`:

- Change the prerequisite line to "Claude Code or Codex CLI".
- Add the Codex install block beside the Claude block.
- State that v2 and v3 run on Claude Code only, with the one-line reason.
- ~~State that Codex gets no session brief.~~ Withdrawn — the brief works on both.
- Describe how to refresh a local marketplace after an edit. `codex plugin
  marketplace upgrade` refreshes Git marketplaces only. For a local marketplace,
  run `codex plugin add <plugin>@<marketplace>` again. It re-copies the clone at
  the same version. Start a new thread to pick up the change.

In `AGENTS.md`: replace the stale copy of `CLAUDE.md` with a short Codex-side
file that points at the same conventions.

Warn in `README.md` and in `skills/setup/SKILL.md`: Codex reads `AGENTS.md`, not
`CLAUDE.md`. A target repo with only a `CLAUDE.md` gives a Codex user the skills
but none of the repo's own instructions. The test repo `pubx-app` has this gap
today.

`/setup` has no harness-binary check today. Adding one is new work, not a change
to an existing check. Keep it out of this scope.

## Out of scope

- `skills/ticket-loop/cron-run.sh`, `usage-parse.py`, `orchestrator/`, `docker/`
- Any `.codex-plugin/` manifest
- A harness-binary prereq check in `/setup`

## Test plan

1. Install from this clone into Claude Code. Run `/standup` and the read-only
   path of `/worktree` against `pubx-app`. Behavior must not change.
2. In Claude Code, run `/setup` against a scratch repo and `/cleanup --help`-level
   inspection. Confirm the validator and example-config paths still resolve.
3. Install from this clone into Codex. Run `/standup` against `pubx-app`.
4. In Codex, confirm a skill resolves `DW_ROOT` and runs `validate.py`.
5. In Codex, confirm `/worktree` resolves `dev-process/scripts/worktree-reset.sh`
   and `/release` resolves `skills/ticket-loop/telegram.py`.
6. In Codex, invoke `/ticket-loop` and `/ticket-loop-parent`. Both must refuse
   with the guard message.
7. In Claude Code, invoke `/ticket-loop`. The guard must not fire.

Reinstall the plugin into Codex between edits. Run
`codex plugin add dev-workflow@dev-workflow`, then start a new thread.

## Correction — 2026-09-10

**The claim that Codex never fires the SessionStart hook was wrong, and this
branch shipped it.** Two facts in this document were verified correctly and the
inference between them was not:

- Codex's plugin validator does reject a `hooks` key in the manifest. Still true.
- An early probe reported no session brief. Observed, but not reproducible.

Codex discovers `hooks/hooks.json` **by path** under the plugin root, with no
manifest key involved, and expands `${CLAUDE_PLUGIN_ROOT}` inside the hook
command — a template token, distinct from the shell variable a skill never
receives. Re-tested in `~/repos/aws/pubx-app`:

```
$ codex exec --sandbox read-only "...report any SessionStart context..."
hook: SessionStart Completed
This repo uses dev-workflow (CI, but for ticket work) — a dev-workflow.yml is present...
```

Why the first probe showed no brief is unknown; hook trust had possibly not been
granted yet. It is not restated here as fact.

What changes: the session brief works on both harnesses, and the docs no longer
tell Codex users to compensate with `/standup`. What does not change: no
`.codex-plugin/` manifest, no `hooks` key, and the `DW_ROOT` rung — Codex still
exports no plugin-root variable to a skill's shell, which is the finding this
branch actually rests on.
