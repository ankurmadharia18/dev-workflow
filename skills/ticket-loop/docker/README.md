# Containerized ticket-loop

Run each scheduled ticket-loop pass in a pinned Docker container on a Linux host,
on a systemd timer, or run the upstream always-on orchestrator through Docker
Desktop on macOS. The image bakes the **dev-workflow framework** (runner +
plugin) root-owned at `/opt/dev-workflow`; your repo checkout, auth, and secrets
live in a mounted volume. This is the unattended deployment — the interactive
`/loop /ticket-loop` and the laptop launchd variant (`../install-cron.sh`) are the
other two shapes.

> **Prereqs:** Docker on the host, a login user for the volume, and the framework
> checked out on the host to use as the build context. Everything below is one
> host at a time; secrets never enter the image or `docker inspect`.

## The three zones (boundary rule 2)

| Zone | What | Where |
|---|---|---|
| **Framework** (baked, root-owned) | runner (`cron-run.sh`, `run-pass.sh`, `loop-lock.sh`, `telegram.py`, `dw-config.py`), the plugin, `loop-mcp.json` | `/opt/dev-workflow` in the image |
| **Work tree + config** | your repo checkout (a `base_branch` clone) with its `dev-workflow.yml` at the root | the volume, at `@WORK_TREE@` (e.g. `/home/agent/<repo>`) |
| **Injected secrets/auth** | `agent.env`, `~/.claude` (auth), `state.json` | the volume, `@ENV_FILE@` + `/home/agent/.claude` |

The container runs as non-root `agent`; `/opt/dev-workflow` is root-owned, so a
build subagent physically cannot edit the framework driving it. A `git pull` of the
work tree can never change how the loop runs — only a rebuild can.

## 1. Build the image (pin the claude version)

Build context is the **framework repo root**, and the claude pin is required:

```
docker build -f skills/ticket-loop/docker/Dockerfile \
  --build-arg CLAUDE_CODE_VERSION=<pin> \
  -t <image>:<pin> <repo-root>
docker tag <image>:<pin> <image>:current
```

Verify `<pin>` against whatever `claude` your team runs — the autoupdater is off in
the image, so it stays put. Extend the Dockerfile's apt list to match your test
suite's system deps (it ships `git gh curl jq python3 python3-yaml postgresql-client`
+ `uv`).

## 2. Seed the volume (a base-branch clone of YOUR repo)

Create the volume and clone your repo's **base branch** into it at `@WORK_TREE@`,
owned by uid `10001` (the image's `agent` user):

```
docker volume create <volume>
docker run --rm -v <volume>:/home/agent node:22-bookworm-slim \
  bash -lc 'apt-get update && apt-get install -y git && \
    git clone --branch <base_branch> <your-repo-url> /home/agent/<repo>'
docker run --rm -v <volume>:/home/agent node:22-bookworm-slim \
  chown -R 10001:10001 /home/agent
```

`dev-workflow.yml` must exist at the checkout root (validate it first with
`uv run dev-workflow/validate.py dev-workflow.yml`).

## 3. Write `agent.env` on the volume (secrets — human only)

`agent.env` is **never read by Claude** — only the runner and the tracker MCP header
use it. Write it into the volume at `@ENV_FILE@` (mode `600`, owned by uid `10001`):

| Field | What |
|---|---|
| `LINEAR_API_KEY` | Tracker personal API key — the Linear MCP `Authorization: Bearer` value |
| `GH_TOKEN` | A PAT with contents + PR read/write on your repo (gh + git push) |
| `TELEGRAM_BOT_TOKEN` | The bot token (same bot as any laptop loop) |
| `AGENT_TELEGRAM_CHAT_ID` | The dedicated group's chat id |
| `CLAUDE_CODE_OAUTH_TOKEN` | From `claude setup-token` — auth without a browser (or `docker run -it … claude` once to `/login`; it persists in the volume) |
| `PGHOST` / `PGPORT` / `PGUSER` / `PGPASSWORD` / `PGDATABASE` | Optional — only if your test suite needs a DB |

```
docker run --rm -i -v <volume>:/home/agent node:22-bookworm-slim \
  bash -lc 'cat > /home/agent/agent.env && chmod 600 /home/agent/agent.env && \
    chown 10001:10001 /home/agent/agent.env' < ./agent.env.local
```

## 4. Render + install the units

The `agent.service.template` + `agent.timer.template` carry `@PLACEHOLDER@` tokens.
`sed` your names in and install to `/etc/systemd/system/`:

```
sed -e 's|@IMAGE@|<image>:current|' -e 's|@VOLUME@|<volume>|' \
    -e 's|@CONTAINER@|<name>-pass|' -e 's|@WORK_TREE@|/home/agent/<repo>|' \
    -e 's|@ENV_FILE@|/home/agent/agent.env|' -e 's|@ONCALENDAR@|*-*-* 09..20:00/30|' \
    skills/ticket-loop/docker/agent.service.template | sudo tee /etc/systemd/system/<name>.service
sed -e 's|@ONCALENDAR@|*-*-* 09..20:00/30|' \
    skills/ticket-loop/docker/agent.timer.template | sudo tee /etc/systemd/system/<name>.timer
sudo systemctl daemon-reload
sudo systemctl enable --now <name>.timer
```

The service passes `-e DW_WORK_TREE=@WORK_TREE@` and `-e DW_ENV_FILE=@ENV_FILE@`
only — the runner sources the secrets from the volume itself, so **no secret is
ever passed as unit-level env** (boundary rule 2: nothing sensitive in
`docker inspect`). Set the host timezone to your team's zone so `@ONCALENDAR@`
times match your working window.

## 5. Smoke-test (dry run)

`run-pass.sh` is the image CMD, but it's plain `CMD` (not `ENTRYPOINT`), so you can
override the command for a no-side-effects pass — no sends, no builds:

```
docker run --rm --network host \
  -e DW_WORK_TREE=/home/agent/<repo> \
  -v <volume>:/home/agent \
  <image>:current /opt/dev-workflow/bin/run-pass.sh --dry-run
```

Confirm the tracker MCP answered (static key works), `gh auth` is OK, and the
Telegram poll succeeded before enabling the timer.

## Try one pass locally first

Before you touch a server, validate the WHOLE loop on your laptop — same image,
same volume shape, same runner, just local Docker. (No Docker? The native launchd
mode in the skill [README](../README.md) trials the same runner directly.)
[`local-run.sh`](local-run.sh) is a thin helper around the exact commands above;
each subcommand is independently runnable. The recommended rollout:

```
export CLAUDE_PIN=<pin>                              # same pin as the box build
./local-run.sh build                                # build the image from the repo root
./local-run.sh seed <your-repo-url> <base_branch> <name>   # clone into the volume (idempotent)
./local-run.sh put-env ./agent.env.local            # write agent.env into the volume (mode 600)
./local-run.sh dry-run <name>                        # a no-side-effects pass — no sends, no builds
# ...then, ONCE, a supervised real pass (see the warning below):
./local-run.sh pass <name> --yes
```

`IMAGE` / `VOLUME` default to `dev-workflow-agent:local` / `dev-workflow-agent-local`
(override via env). `./local-run.sh clean` removes both when you're done. Iterate
on `dry-run` until the tracker MCP answers, `gh auth` is OK, and the Telegram poll
succeeds — then take the **same image recipe** to a server via the systemd path
above (build → seed → agent.env → units).

> **A real `pass` acts on the REAL tracker, chat group, and GitHub** — it can move
> tickets, post messages, and open/merge PRs. That's why it requires an explicit
> `--yes`. And the loop's pid-file singleton lock **cannot arbitrate across
> machines, nor between this container and a host launchd/cron loop** — so before a
> real local pass, **stop every other runner for this repo** (the box timer, a
> laptop launchd loop, another container). Two loops on one repo will collide.
> `dry-run` is safe to run anytime; only `pass --yes` carries this.

## Run the upstream orchestrator on Docker Desktop

The orchestrator is the preferred always-on local shape. It is still the
upstream PID-1 scheduler and Telegram ingress; `tracker.provider: github` changes
only the queue pre-check and per-pass executor. Linear projects continue through
the canonical `/ticket-loop` skill unchanged.

Create a one-project `roster.yml` and start with `enabled: false` until the image,
auth and Telegram route have been checked:

```yaml
root: /home/agent
cadence: adaptive
projects:
  - name: super-singularity
    work_tree: /home/agent/super-singularity
    env_file: /home/agent/super-singularity.env
    state_dir: /home/agent/state/super-singularity
    repo: { url: https://github.com/your-org/super-singularity.git, branch: main }
    enabled: false
    model: opus
    tz: Asia/Kolkata
```

Then use the helper. Secret files are streamed into the Docker volume and never
printed or placed in `docker inspect`:

```bash
export CLAUDE_PIN=<pin>
export CONTAINER_TZ=Asia/Kolkata
./skills/ticket-loop/docker/local-run.sh build
./skills/ticket-loop/docker/local-run.sh seed <repo-url> main super-singularity
./skills/ticket-loop/docker/local-run.sh put-project-env super-singularity .local/super-singularity.env
./skills/ticket-loop/docker/local-run.sh put-roster .local/roster.yml
./skills/ticket-loop/docker/local-run.sh put-orch-env .local/orch.env
./skills/ticket-loop/docker/local-run.sh orchestrator-up
./skills/ticket-loop/docker/local-run.sh orchestrator-status
```

After validation, set `enabled: true`, upload the roster again and restart the
container. It uses Docker's `--restart unless-stopped` policy. Only one process
may call Telegram `getUpdates`; stop any legacy `dw-ticket-loop listen` process
before this cutover because the orchestrator's ingress becomes the sole bot
consumer.

```bash
./skills/ticket-loop/docker/local-run.sh run-now super-singularity
./skills/ticket-loop/docker/local-run.sh orchestrator-logs
./skills/ticket-loop/docker/local-run.sh orchestrator-down  # volume is preserved
```

The current GitHub phase handles ticket pickup, Claude coordinator/implementer
work, clarification questions, progress state and draft PR creation. Linear's
broader autonomous PR-babysitting/digest/scouting behavior is not yet claimed as
GitHub parity.

`CONTAINER_TZ` is passed to PID 1 and inherited by the scheduler, Telegram
ingress and every child pass. For the Super Singularity setup it is
`Asia/Kolkata`, so container and per-project logs consistently use IST.

## The `--plugin-dir` fallback

The runner invokes the baked plugin's skill as `/dev-workflow:ticket-loop` via
`--plugin-dir /opt/dev-workflow/plugin` **when the pinned `claude` supports
`--plugin-dir`**. If it doesn't, the runner logs a warning and falls back to a
repo-local `/ticket-loop` — so for that fallback to work, the work tree must also
carry the skill at `.claude/skills/ticket-loop/`. Newer claude builds make the
baked plugin the source of truth; the fallback is only for older pins.

## Operate

- Trigger a pass now: `sudo systemctl start <name>.service`
- Status: `systemctl list-timers <name>.timer` + `systemctl status <name>.service`
- Logs: `journalctl -u <name>.service`, plus the loop's own cron log at
  `<work_tree>/<state_dir>/logs/ticket-loop-cron.log` inside the volume
- Pause: `sudo systemctl disable --now <name>.timer`
