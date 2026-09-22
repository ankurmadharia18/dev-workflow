# dev-workflow

**CI, but for ticket work.** A generic runner + a Claude Code plugin read one
per-repo `dev-workflow.yml` and work your board — picking up tickets, asking
clarifying questions in team chat, and opening one reviewable PR per ticket —
inside guardrails a repo can *tighten but never loosen*. Same shape as CI:
generic runner, per-repo config file, non-escapable guardrails. Point it at any
codebase.

Three tiers, adopt as far as you want to go:

- **v1 — Local developer (default, every install).** Install the plugin, run
  `/setup` to write a `dev-workflow.yml`, then work locally with the session
  skills — `/worktree`, `/standup`, `/cleanup`, `/release` (plus
  `/blog-from-session`). They run on [the opinionated workflow](#2-the-opinionated-workflow)
  below. This is what you get out of the box.
- **v2 — Local agent (opt-in, OFF by default).** A local autonomous agent (the
  `ticket-loop` skill + a launchd/cron installer) that works your queue on your
  machine and opens one PR per ticket. Gated by `agent.enabled: true` in
  `dev-workflow.yml` — absent/false and both the skill and installer refuse.
- **v3 — Remote runner (repo-level, separate).** The Docker runner + multi-project
  orchestrator — an AI teammate you manage from a Telegram group, running
  unattended on a server. Not part of plugin install; it has its own runbook
  track (§ *Autonomous ticket-loop* below).

**Works today with [Linear](https://linear.app) as the full tracker and
[Telegram](https://telegram.org) as the team chat.** GitHub Issues has a
manual local runner: `dw-ticket-loop plan` previews eligible work and
`dw-ticket-loop run` claims the selected issues and starts Claude locally.
There is no schedule or unattended trigger in this phase. Both sit behind an
adapter seam ([tracker-adapters.md](dev-workflow/tracker-adapters.md)).

Secrets are injected at runtime; the framework is baked **read-only** so the
agent physically cannot edit its own leash. The narrative behind the skills is
the [dev-process playbook](dev-process/README.md).

> This repo was previously a collection of standalone AI prompts. Those still
> ship here — demoted to **[`extras/`](extras/README.md)** — but the framework
> is the front door.

## 1. Quickstart

**You need:** [Claude Code](https://docs.anthropic.com/en/docs/claude-code) or the
[Codex CLI](https://developers.openai.com/codex/cli) (0.151+), with your tracker
connected (Linear's MCP, or a `LINEAR_API_KEY` for the headless
loop). Only for the autonomous loop and release announcements: a Telegram bot
token + group chat id, and a GitHub token for PRs — all injected via env vars,
enumerated in [`skills/ticket-loop/env.example`](skills/ticket-loop/env.example).

### Interactive skills

1. **Install the plugin.** Add this repo as a plugin marketplace once, then
   install from it:

   ```
   claude plugin marketplace add singlas/dev-workflow
   claude plugin install dev-workflow
   ```

   Or skip the marketplace and point Claude Code at a clone of this repo:

   ```
   claude --plugin-dir <path-to-this-clone>
   ```

   On the Codex CLI, the same repo installs as a Codex plugin:

   ```
   codex plugin marketplace add singlas/dev-workflow
   codex plugin add dev-workflow@dev-workflow
   ```

   Two differences on Codex. **You invoke a skill by asking for it, not with a
   slash command** — Codex's `/` namespace holds its own built-ins, so `/standup`
   is not recognised there. Say `run the dev-workflow:standup skill` instead. And
   the autonomous tiers (`ticket-loop`, `ticket-loop-parent`) refuse to run —
   they need `claude -p` and Claude subagents. Everything else, the session brief
   included, behaves the same on both.

   Developing against a **local** marketplace: `codex plugin marketplace upgrade`
   refreshes Git marketplaces only. To pick up an edit to a local clone, re-run
   `codex plugin add dev-workflow@dev-workflow` and start a new thread.

   It provides `/setup`, `/worktree`, `/standup`, `/cleanup`, `/release`,
   `/ticket-loop`, and `/blog-from-session`. Opening a session in a repo that
   already has a `dev-workflow.yml` auto-orients you on **both** harnesses — a
   SessionStart hook injects a short brief, and it stays silent in every repo
   without one. Codex finds `hooks/hooks.json` by path, without a `hooks` key in
   the plugin manifest (its validator rejects that key), and expands
   `${CLAUDE_PLUGIN_ROOT}` inside the hook command even though it never exports
   that variable to a skill's shell.

2. **Add a config.** Run `/setup` — it checks prereqs and interviews you for the
   required values, writing a validated `dev-workflow.yml`. Or copy
   [`dev-workflow.example.yml`](dev-workflow/dev-workflow.example.yml) to your
   repo root by hand and edit the values (branch model, tracker team/roles,
   test/lint commands, tightened guardrails).

   **Make `AGENTS.md` your repo's canonical instruction file.** Codex and most
   other agents read `AGENTS.md`; Claude Code reads `CLAUDE.md`. Keep the content
   in `AGENTS.md` and make `CLAUDE.md` a one-line import, so the two can never
   drift:

   ```
   echo '@AGENTS.md' > CLAUDE.md
   ```

   A repo that has only a `CLAUDE.md` gives a Codex user the skills but none of
   its own conventions. This repo does exactly what it recommends — see its
   [`AGENTS.md`](AGENTS.md) and [`CLAUDE.md`](CLAUDE.md).

3. **Validate it.** `/setup` already validates what it writes. If you edited the
   config by hand, run the validator yourself — from your repo root, pointing at
   wherever the framework lives (the plugin cache for a marketplace install, or
   your clone):

   ```
   uv run "$CLAUDE_PLUGIN_ROOT/dev-workflow/validate.py" dev-workflow.yml  # inside a session
   uv run <path-to-clone>/dev-workflow/validate.py dev-workflow.yml       # from a clone
   # -> OK: dev-workflow.yml
   ```

   The validator rejects unknown keys and any config that tries to *loosen* a
   baseline (see the boundary rules below).

4. **Work the loop.** Get a fresh branch with `/worktree`, orient with
   `/standup`, build (committing as you go), ship a PR with `/cleanup`, and
   promote a ready batch with `/release`. The opinions behind that loop are the
   next section.

### Manual GitHub issue execution (Phase 1)

For a repo configured with `tracker.provider: github`, preview up to three
eligible issues locally:

```
dw-ticket-loop plan <repo-path-or-name> --max 3
```

`plan` is read-only. After reviewing its selection, manually start the local
Claude coordinator with:

```
dw-ticket-loop run <repo-path-or-name> --max 3
```

Choose the coordinator harness explicitly when needed:

```
dw-ticket-loop run <repo-path-or-name> --engine claude --max 3
dw-ticket-loop run <repo-path-or-name> --engine codex --max 3
```

The Codex profile reads its coordinator/implementer model and reasoning settings
from `build.codex_*`. For example, `gpt-6-astra` with `low` reasoning can
coordinate fresh `gpt-5.6-sol` workers at `medium` reasoning. If the Codex
coordinator cannot delegate to that exact worker profile, it must stop before
editing rather than silently implement with the wrong model.

`run` requires `agent.enabled: true`, moves selected issues from the ready
label to the claimed label, and restores them if Claude fails. The generated
instructions allow only feature-branch pushes and draft PR creation for human
review; they prohibit direct trunk pushes, merging, deployment, issue comments,
and cloud changes. No cron or launchd job is installed.

### The skills at a glance

Each skill reads `dev-workflow.yml` for your branch names, tracker roles, and
commands — and each sits one step further up a deliberate safety gradient:

| Skill | What it does | Blast radius |
|---|---|---|
| `/setup` | First-run onboarding: checks prereqs (git/uv/gh/tracker key), interviews you, writes a validated `dev-workflow.yml`, points at the daily workflow | Writes one local file (`dev-workflow.yml`); never commits or pushes |
| `/worktree` | Sets up 2-4 parallel worktree slots (or one linear branch), resets a slot to a fresh branch off the latest trunk, and teaches the branch model | Local branch/worktree surgery; one outward action — deletes REMOTE branches already merged into the trunk (skippable with `--keep-remote`) |
| `/standup` | Opens a session: board snapshot, what's In Progress to resume, 2-4 recommended starting tickets with one-line whys | **Read-only** — never moves a ticket |
| `/cleanup` | Closes a session: commit what's left → sync with the base branch → push → open the PR → move the session's finished tickets to Done | Pushes *your feature branch*; merging its PR lands on the base branch and **never deploys** |
| `/release` | The promotion: absorb hotfixes, test gate, bump `version.file`, tag, open the base→prod PR — then **stops. A human merges; the merge deploys.** Refuses to run at all if `repo.prod_branch` / `deploy.trigger` aren't configured | The **only** skill that touches prod, and it's human-gated at the merge |
| `/ticket-loop` | One pass of the autonomous agent (v2): daily digest → drain the Telegram group (answers, new tickets, approvals) → babysit open PRs → build the next actionable tickets, one PR each. Opt-in: refuses interactively unless `agent.enabled: true` | Same baseline guardrails as the containerized runner |
| `/blog-from-session` | Optional: turns a sharp session learning (or a topic you hand it) into one practitioner field-note draft — proposes angles, then writes the pick. Enabled by a `blog:` config section; `/cleanup` can offer it | Writes one draft file locally; never publishes or commits |

### Autonomous ticket-loop (v2 local agent · v3 remote runner)

The same loop runs unattended two ways. **v2** is a local agent you turn on per
repo; **v3** is a separate, repo-level deployment on a server. Both run the
identical runner + skill — the difference is where it lives and how it's gated.

**v2 — Local agent (opt-in).** An autonomous agent on your own machine, scheduled
via macOS launchd (adapt to cron/systemd on Linux). Turn it on in three steps:

1. **Opt in.** Set `agent.enabled: true` in the repo's `dev-workflow.yml` (the
   `/setup` skill can do this). Without it, `/ticket-loop` and the installer both
   refuse.
2. **Supply the loop's env** — Telegram bot token + group chat id, `GH_TOKEN`,
   `LINEAR_API_KEY` — see [`skills/ticket-loop/env.example`](skills/ticket-loop/env.example).
3. **Install the schedule** with
   [`skills/ticket-loop/install-cron.sh`](skills/ticket-loop/install-cron.sh)
   (`--work-tree <your repo>`). What the agent does and how you manage it from a
   group chat: [`skills/ticket-loop/README.md`](skills/ticket-loop/README.md).

**v3 — Remote runner (repo-level, separate — NOT part of plugin install).** The
Docker image + multi-project orchestrator, running on a server against mounted
work trees. It is intentionally *not* gated on `agent.enabled` (a live deployment
predates that key). Follow the docs in order:

1. **Build + run one repo** — build the image, mount a work tree, set the timer:
   [`skills/ticket-loop/docker/README.md`](skills/ticket-loop/docker/README.md).
2. **Scale to many repos** — the round-robin orchestrator (roster, pre-check,
   backoff) over the same image:
   [`skills/ticket-loop/orchestrator/README.md`](skills/ticket-loop/orchestrator/README.md).

## 2. The opinionated workflow

The skills aren't neutral — they encode a branch model where **deploys are
deliberate**. Two long-lived branches, and all real work on short-lived feature
branches:

- **`repo.base_branch`** (the integration trunk, default `dev`) — feature branches
  start here and PR back here. **Merging into it never deploys.**
- **`repo.prod_branch`** (the prod mirror, default `main`) — it advances **only**
  via the `/release` promotion PR, and **that** merge deploys.
- **You never commit directly on the trunk or prod** — not even as a solo
  developer with a single checkout. Every change rides a feature branch.
- **Hotfixes** branch off prod, PR straight to prod (deploys), then back-merge
  prod→trunk so the next release doesn't revert them.

The ideal shape is a canonical checkout on the trunk plus **2-4 worktree slots**
for parallel tickets (one linear checkout works too — same rules). The daily loop:

```
per ticket:  /worktree ─► /standup ─► work + commit ─► /cleanup ─► PR into trunk (no deploy)
                (fresh      (pick the                    (open the        │
                 branch off  ticket)                      PR)             └─ repeat
                 latest trunk)
when a batch is ready:  /release ─► trunk→prod PR ─► human merges = DEPLOY
```

The full narrative behind these opinions — the CI shape, the one-time GitHub
setup, worktree mechanics — is the [dev-process playbook](dev-process/README.md).

## 3. How it's put together

Everything splits into **three zones** — the framework is generic and shared,
the target repo owns only its config, and secrets live nowhere in git. Full
detail (baseline guardrails, tracker seam, distribution) is the deep-dive in
[`dev-workflow/README.md`](dev-workflow/README.md).

| Zone | Owns | Lives in |
|---|---|---|
| **Framework** (generic) | Plugin (skills), runner scripts, Docker image, validator. Identical across every repo. | This repo — `.claude-plugin/` + `skills/` + `dev-workflow/`; baked root-owned at `/opt/dev-workflow` in the container. |
| **Target-repo config** | One `dev-workflow.yml` + the repo's own `CLAUDE.md`. Branch model, tracker team/roles, commands, tightened guardrails. | The target repo root. |
| **Injected** | Secrets (`agent.env`), Claude auth (`~/.claude`), the loop's `state.json`. Never in git. | A mounted volume / the runtime environment. |

Two boundary rules make it safe to point at any repo:

1. **Config can only tighten, never loosen.** Baseline guardrails are
   framework-side constants. A `dev-workflow.yml` may *add* protected paths or
   *lower* a diff budget, but can never switch a baseline off or raise a ceiling.
   `validate.py` enforces the ceilings; the runner enforces the baseline.
2. **The runner lives outside the mounted work tree.** In the container the
   runner + plugin are baked root-owned at `/opt/dev-workflow`; the target repo
   is the mounted volume the build subagent edits as a non-root user — so it
   *physically cannot* edit the framework driving it.

The framework files:

| Piece | What It Does |
|-------|-------------|
| [dev-workflow/README.md](dev-workflow/README.md) | Framework overview: three zones, two boundary rules, baseline guardrails, distribution (Docker runner + Claude Code plugin) |
| [dev-workflow/dev-workflow.example.yml](dev-workflow/dev-workflow.example.yml) | Annotated full config — branch model, tracker team/roles, test/lint commands, tightened guardrails, schedule |
| [dev-workflow/validate.py](dev-workflow/validate.py) | Schema + tighten-only validator — rejects unknown keys and any config that raises a ceiling |
| [dev-workflow/dw-config.py](dev-workflow/dw-config.py) | Dotted-path config reader shell scripts use (`dw-config.py dev-workflow.yml tracker.team`) |
| [dev-workflow/github_issues.py](dev-workflow/github_issues.py) | GitHub Issues Phase 1 adapter: read-only selection plus reversible ready/claimed ownership |
| [dev-workflow/dw_ticket_loop.py](dev-workflow/dw_ticket_loop.py) | Manual `dw-ticket-loop plan/run` CLI; starts local Claude execution but never schedules itself |
| [dev-workflow/dw-board.py](dev-workflow/dw-board.py) | Framework board tool — `dw-board snapshot` renders the board views from Linear, `dw-board prune` reports (config-gated) old Done/Canceled tickets, `dw-board import` bulk-creates issues from a JSON holding file (dry-run unless `--yes`). Team + gates + prune policy from config; `LINEAR_API_KEY` from the env only |
| [dev-workflow/tracker-adapters.md](dev-workflow/tracker-adapters.md) | The provider seam — canonical verbs (`list_actionable`, `move`, `label`, …) mapped onto a tracker (Linear today; GitHub Issues sketch) |
| [skills/worktree/](skills/worktree/) · [skills/standup/](skills/standup/) · [skills/cleanup/](skills/cleanup/) · [skills/release/](skills/release/) | The session skills — fresh worktree/branch, open a session, close it into a PR, promote to prod. Driven entirely by `dev-workflow.yml` |
| [skills/ticket-loop/](skills/ticket-loop/) | The autonomous agent + its [`docker/`](skills/ticket-loop/docker/) runner packaging |
| [dev-process/](dev-process/) | The narrative playbook the skills grew out of — two-branch model, worktree slots, daily loop — plus ready-to-copy scripts ([worktree-reset.sh](dev-process/scripts/worktree-reset.sh), [ship-preflight.sh](dev-process/scripts/ship-preflight.sh)) |

## 4. Repo map

```
dev-workflow/
├── dev-workflow/            # The framework: config contract + validator + tracker seam
├── skills/                  # Claude Code plugin skills — worktree, standup, cleanup, release, ticket-loop
├── dev-process/             # The narrative playbook behind the skills (branches, worktrees, loop)
├── hooks/                   # Plugin SessionStart hook — auto-orients sessions in configured repos
├── scripts/                 # Repo maintenance — bump-version.sh (release version bump + drift check)
├── .claude-plugin/          # Plugin manifest + marketplace (plugin name: dev-workflow)
├── extras/                  # The legacy copy-paste prompt collections (see extras/README.md)
└── site/                    # HTML guide page + assets
```

## 5. Extras: the prompt collections

Before the framework, this repo was a curated set of standalone copy-paste AI
prompts — context-file generators, a multi-repo audit pipeline, web-performance
prompts, a project-handover checklist. They still work, no install required,
and now live in **[`extras/`](extras/README.md)** with their own index.

## 6. Contributing

This repo is a living collection. Contributions are welcome.

- **Improve the framework** — sharper guardrails, a new tracker adapter, a
  cleaner runner.
- **Improve the [extras](extras/README.md)** — clearer prompt sections, new tool
  generators (Windsurf, Cline, Codex, Copilot, Zed), or great (or terrible)
  generated output as examples for others.

To contribute: fork, branch (`git checkout -b my-change`), edit, and open a PR
with a brief description of what changed and why.

**Releasing** — the plugin version lives in one declared place
(`.version-bump.json` → `.claude-plugin/plugin.json`). Bump it with
`scripts/bump-version.sh <new-version>`; verify no drift with
`scripts/bump-version.sh --check` (or `--audit` to also catch stray version
strings that should be declared).

## License

MIT
