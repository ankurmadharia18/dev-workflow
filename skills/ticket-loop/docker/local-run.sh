#!/usr/bin/env bash
#
# local-run.sh — trial the containerized ticket-loop on a laptop before a server.
#
# Same image, same volume shape, same runner as the systemd box path — just local
# Docker, so a tenant can validate the WHOLE loop end to end before touching a
# server. Each subcommand is independently runnable. The recommended order is:
#
#   build → seed → put-env → dry-run   (then, once, a supervised `pass --yes`)
#
# then take the SAME image recipe to a server (the systemd path in README.md).
#
# Config via env:
#   CLAUDE_PIN   the claude version to bake (REQUIRED for `build` — no default)
#   IMAGE        image tag        (default: dev-workflow-agent:local)
#   VOLUME       docker volume    (default: dev-workflow-agent-local)
#   CONTAINER    orchestrator name (default: dw-orchestrator)
#   CONTAINER_TZ container log zone (default: Asia/Kolkata)
set -euo pipefail

IMAGE="${IMAGE:-dev-workflow-agent:local}"
VOLUME="${VOLUME:-dev-workflow-agent-local}"
CONTAINER="${CONTAINER:-dw-orchestrator}"
CONTAINER_TZ="${CONTAINER_TZ:-Asia/Kolkata}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"       # skills/ticket-loop/docker -> repo root
DOCKERFILE="skills/ticket-loop/docker/Dockerfile"
NODE_IMAGE="node:22-bookworm-slim"                   # the seed/chown helper image

usage() {
  cat <<EOF
local-run.sh — trial the containerized ticket-loop locally (image: $IMAGE, volume: $VOLUME)

Usage: $0 <command> [args]

  build                          Build \$IMAGE from the repo root. Requires CLAUDE_PIN.
  seed <repo-url> <branch> <name>  Create \$VOLUME (if absent) and clone <branch> into
                                 /home/agent/<name>. Idempotent (skips if .git exists).
  put-env <local-file>           Copy a local env file into the volume as
                                 /home/agent/agent.env (mode 600). Never prints it.
  put-project-env <name> <file>  Copy one tenant's secrets as /home/agent/<name>.env.
  put-roster <local-file>        Copy roster.yml into the volume.
  put-orch-env <local-file>      Copy orchestrator secrets as /home/agent/orch.env.
  dry-run <name>                 Run ONE pass with --dry-run: no sends, no builds.
  pass <name> --yes              Run ONE REAL pass. Acts on the real tracker/chat/GitHub.
                                 Requires --yes; stop every other loop for this repo first.
  orchestrator-up               Start the upstream daemon with restart=unless-stopped.
  orchestrator-status           Show container and project scheduler status.
  orchestrator-logs             Follow the daemon logs.
  run-now [name]                Wake one enabled project (or the next project).
  orchestrator-down             Stop the daemon without removing its volume.
  clean                          Remove the volume and the local image (asks first).

Env: CLAUDE_PIN (build), IMAGE, VOLUME, CONTAINER, CONTAINER_TZ.
EOF
}

# --- helpers ---------------------------------------------------------------

need_docker() {
  command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found on PATH" >&2; exit 1; }
}

volume_exists() { docker volume inspect "$VOLUME" >/dev/null 2>&1; }

validate_project_name() {
  local name="$1"
  case "$name" in
    ""|*[!A-Za-z0-9._-]*)
      echo "ERROR: project name must contain only letters, numbers, dot, underscore, or hyphen" >&2
      exit 1
      ;;
  esac
}

ensure_volume_layout() {
  volume_exists || { echo "ERROR: volume $VOLUME does not exist" >&2; exit 1; }
  docker run --rm -v "$VOLUME":/home/agent "$NODE_IMAGE" \
    bash -lc 'mkdir -p /home/agent/.cache/uv /home/agent/orch /home/agent/state && \
      chown 10001:10001 /home/agent /home/agent/.cache /home/agent/.cache/uv \
        /home/agent/orch /home/agent/state'
}

# --- subcommands -----------------------------------------------------------

cmd_build() {
  need_docker
  if [ -z "${CLAUDE_PIN:-}" ]; then
    echo "ERROR: CLAUDE_PIN is required (pin the claude version, e.g. CLAUDE_PIN=2.1.0)" >&2
    exit 1
  fi
  echo "Building $IMAGE (claude pin $CLAUDE_PIN) from $REPO_ROOT ..."
  ( cd "$REPO_ROOT" && docker build -f "$DOCKERFILE" \
      --build-arg CLAUDE_CODE_VERSION="$CLAUDE_PIN" -t "$IMAGE" . )
  echo "Built $IMAGE."
}

cmd_seed() {
  need_docker
  local url="${1:-}" branch="${2:-}" name="${3:-}"
  if [ -z "$url" ] || [ -z "$branch" ] || [ -z "$name" ]; then
    echo "ERROR: usage: $0 seed <repo-url> <branch> <name>" >&2
    exit 1
  fi
  validate_project_name "$name"
  volume_exists || { echo "Creating volume $VOLUME ..."; docker volume create "$VOLUME"; }
  ensure_volume_layout
  if docker run --rm -v "$VOLUME":/home/agent "$NODE_IMAGE" \
       test -d "/home/agent/$name/.git"; then
    echo "Already seeded: /home/agent/$name (has .git) — skipping clone."
    return 0
  fi
  echo "Cloning $url ($branch) into $VOLUME:/home/agent/$name ..."
  docker run --rm -v "$VOLUME":/home/agent "$NODE_IMAGE" \
    bash -lc "apt-get update && apt-get install -y git && \
      git clone --branch '$branch' '$url' '/home/agent/$name'"
  docker run --rm -v "$VOLUME":/home/agent "$NODE_IMAGE" \
    chown -R 10001:10001 "/home/agent/$name"
  # Orchestrator work-tree guard: mark this clone as orchestrator-ownable
  # (roster entries without this marker are refused at startup).
  docker run --rm -v "$VOLUME":/home/agent "$NODE_IMAGE" \
    bash -lc "touch '/home/agent/$name/.dw-agent-clone' && chown 10001:10001 '/home/agent/$name/.dw-agent-clone'"
  echo "Seeded /home/agent/$name. Ensure dev-workflow.yml is at its root."
}

cmd_put_env() {
  need_docker
  local file="${1:-}"
  if [ -z "$file" ] || [ ! -f "$file" ]; then
    echo "ERROR: usage: $0 put-env <local-file>  (an existing env file)" >&2
    exit 1
  fi
  volume_exists || { echo "ERROR: volume $VOLUME does not exist — run 'seed' first" >&2; exit 1; }
  ensure_volume_layout
  # Piped over stdin; the contents are never echoed to the terminal.
  docker run --rm -i -v "$VOLUME":/home/agent "$NODE_IMAGE" \
    bash -lc 'cat > /home/agent/agent.env && chmod 600 /home/agent/agent.env && \
      chown 10001:10001 /home/agent/agent.env' < "$file"
  echo "Wrote /home/agent/agent.env (mode 600) into $VOLUME."
}

put_volume_file() {
  local file="$1" destination="$2" mode="${3:-600}"
  [ -f "$file" ] || { echo "ERROR: file not found: $file" >&2; exit 1; }
  volume_exists || { echo "ERROR: volume $VOLUME does not exist — run 'seed' first" >&2; exit 1; }
  ensure_volume_layout
  docker run --rm -i -v "$VOLUME":/home/agent "$NODE_IMAGE" \
    bash -lc "cat > '/home/agent/$destination' && chmod '$mode' '/home/agent/$destination' && chown 10001:10001 '/home/agent/$destination'" < "$file"
  echo "Wrote /home/agent/$destination (mode $mode) into $VOLUME."
}

cmd_put_project_env() {
  local name="${1:-}" file="${2:-}"
  [ -n "$name" ] && [ -n "$file" ] || {
    echo "ERROR: usage: $0 put-project-env <name> <local-file>" >&2; exit 1;
  }
  validate_project_name "$name"
  put_volume_file "$file" "$name.env" 600
}

cmd_put_roster() {
  local file="${1:-}"
  [ -n "$file" ] || { echo "ERROR: usage: $0 put-roster <local-file>" >&2; exit 1; }
  put_volume_file "$file" roster.yml 600
}

cmd_put_orch_env() {
  local file="${1:-}"
  [ -n "$file" ] || { echo "ERROR: usage: $0 put-orch-env <local-file>" >&2; exit 1; }
  put_volume_file "$file" orch.env 600
}

container_exists() { docker container inspect "$CONTAINER" >/dev/null 2>&1; }

cmd_orchestrator_up() {
  need_docker
  volume_exists || { echo "ERROR: volume $VOLUME does not exist — run 'seed' first" >&2; exit 1; }
  ensure_volume_layout
  if container_exists; then
    if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" = true ]; then
      echo "$CONTAINER is already running."
    else
      docker start "$CONTAINER" >/dev/null
      echo "Started existing $CONTAINER."
    fi
    return 0
  fi
  docker run -d --name "$CONTAINER" --init --restart unless-stopped \
    -v "$VOLUME":/home/agent \
    -e TZ="$CONTAINER_TZ" \
    "$IMAGE" /opt/dev-workflow/bin/orchestrator.sh >/dev/null
  echo "Started $CONTAINER with restart=unless-stopped (timezone $CONTAINER_TZ)."
}

cmd_orchestrator_status() {
  need_docker
  container_exists || { echo "$CONTAINER does not exist."; return 1; }
  docker ps -a --filter "name=^/${CONTAINER}$" --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
  if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" = true ]; then
    docker exec "$CONTAINER" python3 /opt/dev-workflow/bin/orch.py status \
      --roster /home/agent/roster.yml --state /home/agent/orch/orch-state.json
  fi
}

cmd_orchestrator_logs() {
  need_docker
  container_exists || { echo "$CONTAINER does not exist." >&2; return 1; }
  docker logs -f "$CONTAINER"
}

cmd_run_now() {
  need_docker
  local name="${1:-}"
  container_exists || { echo "$CONTAINER does not exist." >&2; return 1; }
  if [ -n "$name" ]; then
    validate_project_name "$name"
    docker exec "$CONTAINER" bash -lc "printf '%s\\n' '$name' > /home/agent/orch/run-now"
    echo "Queued an immediate pass for $name."
  else
    docker exec "$CONTAINER" bash -lc ': > /home/agent/orch/run-now'
    echo "Queued an immediate pass for the next eligible project."
  fi
}

cmd_orchestrator_down() {
  need_docker
  container_exists || { echo "$CONTAINER does not exist."; return 0; }
  docker stop "$CONTAINER" >/dev/null
  echo "Stopped $CONTAINER. Volume $VOLUME was preserved."
}

# Run one pass. $1=name, $2="--dry-run" for the safe pass (empty for a real one).
_run_pass() {
  need_docker
  local name="$1" dry="${2:-}"
  volume_exists || { echo "ERROR: volume $VOLUME does not exist — run 'seed' first" >&2; exit 1; }
  # shellcheck disable=SC2086  # $dry is intentionally word-split (empty or --dry-run)
  docker run --rm \
    -v "$VOLUME":/home/agent \
    -e DW_WORK_TREE="/home/agent/$name" \
    "$IMAGE" /opt/dev-workflow/bin/run-pass.sh $dry
}

cmd_dry_run() {
  local name="${1:-}"
  [ -n "$name" ] || { echo "ERROR: usage: $0 dry-run <name>" >&2; exit 1; }
  validate_project_name "$name"
  echo "Dry-run pass for /home/agent/$name (no sends, no builds) ..."
  _run_pass "$name" --dry-run
}

cmd_pass() {
  local name="" yes=""
  for arg in "$@"; do
    case "$arg" in
      --yes) yes=1 ;;
      -*)    echo "ERROR: unknown flag: $arg" >&2; exit 1 ;;
      *)     name="$arg" ;;
    esac
  done
  [ -n "$name" ] || { echo "ERROR: usage: $0 pass <name> --yes" >&2; exit 1; }
  validate_project_name "$name"
  cat <<'WARN' >&2
========================================================================
  WARNING: this is a REAL ticket-loop pass, not a dry run.
  It acts on the REAL tracker, the REAL chat group, and REAL GitHub —
  it can move tickets, post messages, and open/merge PRs.

  The loop's pid-file singleton lock CANNOT arbitrate across machines,
  nor between this container and a host launchd/cron loop. Stop EVERY
  other runner for this repo (box timer, laptop launchd, another
  container) BEFORE you continue, or two loops will collide.
========================================================================
WARN
  if [ -z "$yes" ]; then
    echo "Refusing to run without --yes. Re-run: $0 pass $name --yes" >&2
    exit 1
  fi
  echo "Running a REAL pass for /home/agent/$name ..."
  _run_pass "$name" ""
}

cmd_clean() {
  need_docker
  printf 'Remove volume %s AND image %s? [y/N] ' "$VOLUME" "$IMAGE"
  read -r reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; return 0 ;;
  esac
  docker volume rm "$VOLUME" 2>/dev/null && echo "Removed volume $VOLUME." || echo "No volume $VOLUME."
  docker image rm "$IMAGE" 2>/dev/null && echo "Removed image $IMAGE." || echo "No image $IMAGE."
}

# --- dispatch --------------------------------------------------------------

main() {
  [ $# -ge 1 ] || { usage; exit 1; }
  local cmd="$1"; shift || true
  case "$cmd" in
    build)    cmd_build "$@" ;;
    seed)     cmd_seed "$@" ;;
    put-env)  cmd_put_env "$@" ;;
    put-project-env) cmd_put_project_env "$@" ;;
    put-roster) cmd_put_roster "$@" ;;
    put-orch-env) cmd_put_orch_env "$@" ;;
    dry-run)  cmd_dry_run "$@" ;;
    pass)     cmd_pass "$@" ;;
    orchestrator-up) cmd_orchestrator_up "$@" ;;
    orchestrator-status) cmd_orchestrator_status "$@" ;;
    orchestrator-logs) cmd_orchestrator_logs "$@" ;;
    run-now) cmd_run_now "$@" ;;
    orchestrator-down) cmd_orchestrator_down "$@" ;;
    clean)    cmd_clean "$@" ;;
    -h|--help|help) usage ;;
    *) echo "ERROR: unknown command: $cmd" >&2; usage; exit 1 ;;
  esac
}

main "$@"
