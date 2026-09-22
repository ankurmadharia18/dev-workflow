# Tracker adapters — the provider seam

The skills and the autonomous loop never talk to a tracker directly. They
talk in **canonical verbs**, and a thin per-provider adapter maps each verb
onto that tracker's API. Linear has the full skill implementation via its MCP
server. GitHub Issues implements the local build path used by both the manual
command and the Docker orchestrator: actionable selection, queue counting,
reversible ownership, clarification comments/labels, PR lookup and status.

**One hard rule: state and label names always come from `tracker.roles` in
`dev-workflow.yml`.** A skill must never hardcode `agent`, `Todo`, `Done`,
`agent-blocked`, etc. It resolves the *role* (`queue`, `blocked`, `exclude`,
`done`) to the repo's own names at runtime — read them with
`dw-config.py`, e.g. `uv run dev-workflow/dw-config.py dev-workflow.yml
tracker.roles.queue.label`. That is what lets the same skill drive two repos
whose boards use different words.

## Canonical verbs

| Verb | Inputs | Semantics |
|---|---|---|
| `list_actionable` | `roles.queue` (label + states), `roles.exclude.labels` | The work queue: tickets carrying the queue label, in one of the queue states, that carry none of the excluded labels. This is what a loop pass iterates. |
| `get_ticket` | ticket key (`ABC-123`) | Full ticket — title, description, current state, labels, comments, linked PRs. |
| `create_ticket` | title, body, `[labels]`, `[state]` | Open a new ticket (e.g. a follow-up the agent spun off). |
| `comment` | ticket key, markdown body | Append a comment — clarifying questions, answers recorded back, status notes. |
| `move` (state-role) | ticket key, **state role** (`done`, or a queue state) | Advance lifecycle by *role*, resolved to the board's state name. Never pass a literal state. |
| `label` / `unlabel` (label-role) | ticket key, **label role** (`queue`, `blocked`, an `exclude` label) | Add/remove a role's label, resolved to the board's label name. E.g. set `blocked` when a ticket needs a human; clear `queue` when done. |
| `link_pr` | ticket key, PR URL | Attach the opened PR to the ticket so the two are cross-referenced. |
| `get_blockers` | ticket key (`ABC-123`) | The tickets this one is **blocked by** — its upstream dependencies. Returns each blocker's key and current state, so a loop can check whether every blocker has reached `roles.done.state` before building the dependent ticket. Read-only; used at triage to sequence dependent work. |
| `queue_count` | `roles.queue` (label + states), `roles.exclude.labels` | Read-only count of `list_actionable`'s result set — the orchestrator's cheap pre-check. MUST share `list_actionable`'s eligibility definition (same roles, same exclude filter) so the pre-check can never silently drift from what a pass would pick up. |

## Linear mapping (the implementation today)

Linear is driven through its MCP tools (`mcp__linear__*`). Pass the **team
name** from `tracker.team` (Linear's `list_*` tools want the human team name,
not the key prefix). Names below in `roles.*` are read from `dev-workflow.yml`.

**`tracker.project` (optional) — one repo's slice of a shared team.** When
several repos share one Linear team (e.g. a multi-repo product, or a personal
team spanning hobby repos), set `tracker.project: <Linear Project name>` in each
repo's `dev-workflow.yml`. Every read/create verb then additionally scopes to
that Project, so a repo only ever sees and touches its own issues on the shared
board. Ticket **identifiers stay team-scoped** (all repos share the team's key
prefix — the Project field, not the key, distinguishes the repo). Omit
`tracker.project` and every verb is team-only, exactly as before.

| Verb | Linear MCP call | Notes |
|---|---|---|
| `list_actionable` | `mcp__linear__list_issues` | Filter by `team = tracker.team`, `label = roles.queue.label`, `state ∈ roles.queue.states`, **and `project = tracker.project` when set**; then drop any issue carrying a `roles.exclude.labels` entry (filter client-side — combine as needed). |
| `get_ticket` | `mcp__linear__get_issue` | By issue id / key. |
| `create_ticket` | `mcp__linear__save_issue` | Omit the id to create; set `team`, `title`, `description`, `labels`, `state`, **and `project = tracker.project` when set** (so a new ticket lands in this repo's slice of the shared board). |
| `comment` | `mcp__linear__save_comment` | New comment on the issue. |
| `move` (state-role) | `mcp__linear__save_issue` | Update the existing issue's `state` to the name resolved from the role (e.g. `roles.done.state`). |
| `label` / `unlabel` (label-role) | `mcp__linear__save_issue` | Update the issue's `labels` set — add/remove the name resolved from the role. |
| `link_pr` | `mcp__linear__create_attachment` | Attach the PR URL to the issue. |
| `get_blockers` | `mcp__linear__get_issue` (relations) | If the MCP returns the issue's `blocked-by` relations, use them directly. **Fallback convention** (relations not cheaply readable): parse a `Blocked by: ABC-###` line — comma-separated keys allowed — from the ticket description **at triage**; zero adapter work. Either path, `get_ticket` each blocker key to read its current state. |
| `queue_count` | `dev-workflow/queue-count.py` (GraphQL over urllib, keyed by `LINEAR_API_KEY`) | Same filter as `list_actionable` (team + optional `tracker.project` + queue label + queue states, exclude labels dropped client-side); returns only the count. Not an MCP call — it must run without a Claude session. |

**Linear MCP has no delete.** The strongest teardown is moving an issue to a
`Canceled` state — never assume a hard delete exists. Anything the loop
"removes" is a state/label change, not a deletion.

## The board CLI — a read-only second seam

`dw-board` (the framework board tool) is a *second* way the framework touches the
tracker, alongside the canonical verbs. It is **read-only except for the
config-gated `prune`**: `dw-board snapshot` renders throwaway board views (what
`standup` reads), and `dw-board prune` in its default report-only mode
(`board.prune.allow_delete: false`) prints finished/stale candidates without
mutating — exactly the input the loop's weekly `🧹 Board hygiene` digest consumes.
Every *mutation* the loop performs (labeling `dep_blocked`, commenting, moving
state) still goes through the canonical verbs above, never the board CLI.

## Adding a provider

To wire a second tracker, add its name to `KNOWN_TRACKERS` in `validate.py`
and write a mapping table like the one above — implement each canonical verb,
resolving every state/label from `tracker.roles`, never hardcoding names.

## GitHub Issues mapping (Phase 1: local build execution)

The implemented Phase 1 adapter is `github_issues.py`. It shells out to the
authenticated `gh` CLI. `list_actionable` returns open issues with the
configured queue label, minus issues with an excluded label, oldest first.
`queue_count` calls that same function, so the upstream orchestrator's cheap
pre-check cannot disagree with a real pass. Planning is read-only. A real run
swaps ready for claimed before Claude starts and restores ready if Claude exits
unsuccessfully. The same run can ask a Telegram clarification, add/remove the
configured blocked label, inspect the conventional issue PR and open a draft PR.

The provider dispatch lives in the upstream runner: `cron-run.sh` invokes the
canonical `/ticket-loop` skill for Linear and the GitHub-aware
`dw_ticket_loop.py` executor for `tracker.provider: github`. Both emit the same
`outcome.json` contract, so scheduling, locking, Telegram ingress, retries,
backoff and supervision remain provider-neutral.

The remaining full-loop parity for a later phase is:

- **Labels → labels.** The `queue` / `blocked` / `exclude` roles map straight
  onto GitHub issue labels (`roles.queue.label` = e.g. `agent`).
- **States → open/closed + labels.** GitHub issues have only `open`/`closed`,
  so multi-step lifecycle rides on labels: the queue states become label
  presence on an open issue; `roles.done.state` maps to closing the issue
  (optionally with a `done` label). `list_actionable` = open issues with the
  queue label and without any exclude label.
- **Verbs → API.** `get_ticket` = `gh issue view`; `comment` = `gh issue
  comment`; `create_ticket` = `gh issue create`; `move`/`label` = `gh issue
  edit` (labels) or `gh issue close`; `link_pr` = a comment or the PR body's
  `Closes #N`. `get_blockers` = read the issue's task-list / tracked-by
  references or the same `Blocked by: #N` description convention, then `gh issue
  view` each blocker for its open/closed state.

Keep the shape identical to the Linear table so a skill written to the verbs
runs unchanged against either provider.
