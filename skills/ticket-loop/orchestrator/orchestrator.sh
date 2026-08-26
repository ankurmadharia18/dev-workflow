#!/bin/bash
# Long-lived round-robin orchestrator over N ticket-loop projects (mode axis:
# multi-project; Approach A). It replaces the SCHEDULER, not the runner: each
# turn shells out to the same run-pass.sh → cron-run.sh → `claude -p
# /ticket-loop` chain the single-project timer shapes use, one pass at a time,
# never two. All scheduling state/math (roster, ladder, windows, write-ahead,
# classification) lives in orch.py next to this script; this file owns process
# concerns: PID-1 signal handling, the per-pass process-group timeout, the
# pre-check commands (which need each project's own secrets), and Telegram
# escalation.
#
# Secret scoping: this process holds NO project secrets. Each pass — and each
# pre-check — runs in a child that sources only that project's env file; the
# orchestrator's own env carries at most the OPS alert channel creds and the
# shared default Telegram bot (a chat credential shared across projects by design).
#
# stdout is the live dashboard: one decision line per turn (`docker logs -f`).
#
# Env:
#   ORCH_ROSTER              roster.yml           (default /home/agent/roster.yml)
#   ORCH_STATE_DIR           orch state dir       (default <roster dir>/orch)
#   ORCH_ENV_FILE            orchestrator env file (default <roster dir>/orch.env);
#                            holds the three keys below so `docker run` needs no -e —
#                            explicit environment still wins over the file
#   ORCH_RUN_PASS            per-pass runner override (tests; default sibling run-pass.sh)
#   ORCH_MAX_TURNS           exit after N turns   (tests; default: run forever)
#   ORCH_TELEGRAM_BOT_TOKEN / ORCH_TELEGRAM_CHAT_ID   optional ops alert channel
#   DEFAULT_TELEGRAM_BOT_TOKEN   fallback bot for projects whose env file has no
#                            TELEGRAM_BOT_TOKEN of its own — those run telegram.py
#                            in shared (no-ack) mode; a new tenant then only needs
#                            its own group + AGENT_TELEGRAM_CHAT_ID
#   DEFAULT_CLAUDE_CODE_OAUTH_TOKEN  common Claude token for every pass; a project
#                            whose env file sets its own CLAUDE_CODE_OAUTH_TOKEN
#                            (a separate account / limit pool) overrides it
#   TICKET_LOOP_MCP_CONFIG / DW_PLUGIN_DIR / DW_PYTHON  forwarded to each pass when set
#
# Control surface: `touch <ORCH_STATE_DIR>/run-now` (optionally echo a project
# name into it) forces the next turn to run that project, pre-check bypassed.
# `<ORCH_STATE_DIR>/run-now.d/<project>` is the Telegram ingress's wake (one slot
# per tenant, eligibility-only: honors window/memory/parking, skips the ladder).
#   ORCH_TELEGRAM_INGRESS=0  disable the ingress daemon (passes then poll Telegram
#                            directly, the pre-v0.7 behaviour); ORCH_TELEGRAM_WAKE
#                            is honored as the old name
set -uo pipefail

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Sibling discovery covers both layouts: repo (orchestrator/ under
# skills/ticket-loop/, queue-count.py in dev-workflow/) and image (flat
# /opt/dev-workflow/bin).
find_sibling() {
  local f
  for f in "$HERE/$1" "$HERE/../$1" "$HERE/../../../dev-workflow/$1"; do
    [ -f "$f" ] && { echo "$f"; return 0; }
  done
  return 1
}
ORCH_PY="$HERE/orch.py"
RUN_PASS="${ORCH_RUN_PASS:-$(find_sibling run-pass.sh || true)}"
TELEGRAM="$(find_sibling telegram.py || true)"
QUEUE_COUNT="$(find_sibling queue-count.py || true)"
LOCK_SH="$(find_sibling loop-lock.sh || true)"
ROLLUP_PY="$(find_sibling usage-rollup.py || true)"

ROSTER="${ORCH_ROSTER:-/home/agent/roster.yml}"
ORCH_STATE_DIR="${ORCH_STATE_DIR:-$(dirname "$ROSTER")/orch}"
mkdir -p "$ORCH_STATE_DIR"
STATE_FILE="$ORCH_STATE_DIR/orch-state.json"
RUN_NOW_FILE="$ORCH_STATE_DIR/run-now"        # human force-run (highest priority)
RUN_NOW_DIR="$ORCH_STATE_DIR/run-now.d"       # ingress wakes, one file per tenant
mkdir -p "$RUN_NOW_DIR"

# Orchestrator-level env file: ops alert channel + the shared default bot live
# here instead of `docker run -e` flags. Explicit environment wins over the file.
ORCH_ENV_FILE="${ORCH_ENV_FILE:-$(dirname "$ROSTER")/orch.env}"
if [ -f "$ORCH_ENV_FILE" ]; then
  _bot="${ORCH_TELEGRAM_BOT_TOKEN:-}" _chat="${ORCH_TELEGRAM_CHAT_ID:-}" _dflt="${DEFAULT_TELEGRAM_BOT_TOKEN:-}"
  _ctok="${DEFAULT_CLAUDE_CODE_OAUTH_TOKEN:-}"
  set -a; . "$ORCH_ENV_FILE"; set +a
  [ -n "$_bot" ]  && ORCH_TELEGRAM_BOT_TOKEN="$_bot"
  [ -n "$_chat" ] && ORCH_TELEGRAM_CHAT_ID="$_chat"
  [ -n "$_dflt" ] && DEFAULT_TELEGRAM_BOT_TOKEN="$_dflt"
  [ -n "$_ctok" ] && DEFAULT_CLAUDE_CODE_OAUTH_TOKEN="$_ctok"
  unset _bot _chat _dflt _ctok
fi

# Python runner for orch.py (PEP 723 pyyaml): same dance as cron-run.sh.
if [ -n "${DW_PYTHON:-}" ]; then PY="$DW_PYTHON"
elif command -v uv >/dev/null 2>&1; then PY="uv run --quiet --no-project"
else PY="python3"; fi

ts()  { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { echo "[$(ts)] $*"; }

ops_alert() {
  log "OPS: $*"
  if [ -n "${ORCH_TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${ORCH_TELEGRAM_CHAT_ID:-}" ] \
     && [ -n "$TELEGRAM" ]; then
    TELEGRAM_BOT_TOKEN="$ORCH_TELEGRAM_BOT_TOKEN" \
    AGENT_TELEGRAM_CHAT_ID="$ORCH_TELEGRAM_CHAT_ID" \
    TICKET_LOOP_STATE_DIR="$ORCH_STATE_DIR" \
      python3 "$TELEGRAM" send "🚨 orchestrator: $*" >/dev/null 2>&1 \
      || log "WARN: ops alert failed to send"
  fi
}

# Send a message to the ops channel VERBATIM (no 🚨 prefix) — for the daily
# usage rollup, which is informational, not an alert.
ops_send_raw() {  # $1 message
  [ -n "${ORCH_TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${ORCH_TELEGRAM_CHAT_ID:-}" ] \
    && [ -n "$TELEGRAM" ] || return 0
  TELEGRAM_BOT_TOKEN="$ORCH_TELEGRAM_BOT_TOKEN" \
  AGENT_TELEGRAM_CHAT_ID="$ORCH_TELEGRAM_CHAT_ID" \
  TICKET_LOOP_STATE_DIR="$ORCH_STATE_DIR" \
    python3 "$TELEGRAM" send "$1" >/dev/null 2>&1 || log "WARN: rollup send failed"
}

# Once-daily per-tenant usage rollup to the ops channel. Pure aggregation over
# every tenant's usage.jsonl under the state root (scans ALL state dirs, not just
# roster-enabled projects, so single-mode/paused tenants are included) — no model
# call, no subscription tokens. Timestamp-gated in ORCH_STATE_DIR.
maybe_rollup() {
  [ -n "$ROLLUP_PY" ] || return 0
  local today last="" root stamp summary
  today="$(date +%Y-%m-%d)"
  stamp="$ORCH_STATE_DIR/last-rollup"
  [ -f "$stamp" ] && last="$(cat "$stamp" 2>/dev/null)"
  [ "$last" = "$today" ] && return 0
  root="${ORCH_STATE_ROOT:-$(dirname "$ROSTER")/state}"
  summary="$(python3 "$ROLLUP_PY" --state-root "$root" --date "$today" 2>/dev/null || true)"
  echo "$today" > "$stamp"
  [ -n "$summary" ] && ops_send_raw "$summary"
}

# Call INSIDE a subshell after sourcing a project's env file: projects that
# bring no TELEGRAM_BOT_TOKEN of their own fall back to the shared default bot,
# and TELEGRAM_SHARED_BOT=1 switches telegram.py to no-ack mode — one project
# offset-acking a shared bot's getUpdates stream would destroy the other
# projects' pending messages.
tg_fallback() {
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${DEFAULT_TELEGRAM_BOT_TOKEN:-}" ]; then
    export TELEGRAM_BOT_TOKEN="$DEFAULT_TELEGRAM_BOT_TOKEN" TELEGRAM_SHARED_BOT=1
  fi
}

project_alert() {  # $1 env_file, $2 state_dir, $3 message — the project's chat bot
  [ -n "$TELEGRAM" ] || return 0
  ( set -a; . "$1"; set +a
    tg_fallback
    TICKET_LOOP_STATE_DIR="$2" python3 "$TELEGRAM" send "$3" ) >/dev/null 2>&1 \
    || log "WARN: project alert failed to send"
}

# PID-1 discipline (supervision §4): run the container with --init; on SIGTERM
# finish (or timeout-kill) the current pass, persist, exit — never leave a live
# claude mid-ticket. bash runs the trap only between commands, so every wait
# below is chunked ≤5s.
DRAIN=0
trap 'DRAIN=1; log "SIGTERM — draining (current pass finishes or hits its timeout)"' TERM INT

# setsid puts each pass in its own session/process group so a timeout can kill
# the whole tree (supervision §1). Linux/Docker (the deploy target) always has
# it; some dev hosts (macOS) do not — fall back to a plain background child and
# a single-PID kill there so the driver still runs end to end.
if command -v setsid >/dev/null 2>&1; then SETSID="setsid"; else SETSID=""; fi

# Run "$@" in its own session/process group; TERM the whole group after $1
# seconds, KILL 30s later (supervision §1 — a wedged MCP must not freeze the fleet).
run_with_timeout() {
  local limit="$1"; shift
  $SETSID "$@" &
  local pid=$!
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 5
    waited=$((waited + 5))
    if [ "$waited" -ge "$limit" ]; then
      log "pass timeout (${limit}s) — killing process group $pid"
      if [ -n "$SETSID" ]; then
        kill -TERM -- "-$pid" 2>/dev/null
        sleep 30
        kill -KILL -- "-$pid" 2>/dev/null
      else
        kill -TERM "$pid" 2>/dev/null
        sleep 30
        kill -KILL "$pid" 2>/dev/null
      fi
      wait "$pid" 2>/dev/null
      return 124
    fi
  done
  wait "$pid"
}

sleep_interruptible() {  # wake early on drain, a run-now touch, or a NEW ingress wake
  local remain="$1" chunk stamp="$ORCH_STATE_DIR/.sleep-stamp"
  # Only wakes created AFTER this sleep began count: a wake the scheduler just
  # deferred (window closed, low memory) stays pending without spinning us.
  touch "$stamp"
  while [ "$remain" -gt 0 ]; do
    [ "$DRAIN" = 1 ] && return 0
    [ -f "$RUN_NOW_FILE" ] && return 0
    [ -n "$(find "$RUN_NOW_DIR" -type f -newer "$stamp" 2>/dev/null | head -1)" ] && return 0
    chunk=$(( remain > 30 ? 30 : remain ))
    sleep "$chunk"
    remain=$(( remain - chunk ))
  done
}

# Is the Telegram ingress daemon serving this state dir right now? Prints
# `spool` (fresh <state>/ingress.json) or `direct`. Local files only.
ingress_mode_for() {  # $1 env_file, $2 state_dir
  local m=""
  [ -n "$TELEGRAM" ] && m="$( ( set -a; . "$1"; set +a
                               tg_fallback
                               TICKET_LOOP_STATE_DIR="$2" python3 "$TELEGRAM" ingress --mode ) 2>/dev/null )"
  case "$m" in spool) echo spool ;; *) echo direct ;; esac
}

# ── startup: roster validation (marker guard §8), crash write-ahead recovery
#    (§3), lock-clear on boot (§5 — we are PID 1, no pass can be live) ─────────
if ! STARTUP_SH="$($PY "$ORCH_PY" startup --sh --roster "$ROSTER" --state "$STATE_FILE")"; then
  ops_alert "startup failed — roster invalid or work-tree guard tripped; refusing to run"
  sleep 60   # keep `--restart unless-stopped` from hot-looping on a config error
  exit 1
fi
eval "$STARTUP_SH"
[ -n "${CRASH_RECOVERED:-}" ] && log "recovered crash write-ahead: died mid-pass on $CRASH_RECOVERED"
[ -n "${LOCKS_CLEARED:-}" ]   && log "cleared stale lock(s): $LOCKS_CLEARED"
[ -n "${ESCALATE_OPS:-}" ]    && ops_alert "$ESCALATE_OPS"
if [ -z "$RUN_PASS" ]; then
  ops_alert "run-pass.sh not found next to orchestrator.sh — cannot run passes"
  exit 1
fi
log "orchestrator up — roster: ${PROJECTS:-?}"

# ── Telegram ingress: telegram-ingress.py is the SOLE getUpdates consumer for
# every rostered bot — it spools each human message to the tenant's
# <state>/inbox/, reacts 👀, and drops a wake in run-now.d/, so intake is
# seconds regardless of whether a pass is mid-build (the pass drains the spool
# at its next re-drain point instead of polling Telegram). Supervised by its
# own subshell, INDEPENDENT of the turn loop (which blocks for a whole pass):
# any exit restarts it — rc≠0 pages ops on the first and every 10th restart —
# and a roster edit makes it exit 0 to reload. The EXIT trap stops it with us.
INGRESS_PY="$HERE/telegram-ingress.py"
INGRESS_STOP="$ORCH_STATE_DIR/.ingress-stop"
INGRESS_READY="$ORCH_STATE_DIR/.ingress-ready"
INGRESS_SUP_PID=""
INGRESS_ENABLED=0
rm -f "$INGRESS_STOP" "$INGRESS_READY"
if [ "${ORCH_TELEGRAM_INGRESS:-${ORCH_TELEGRAM_WAKE:-1}}" = 1 ] && [ -f "$INGRESS_PY" ]; then
  INGRESS_ENABLED=1
  ( restarts=0
    while [ ! -f "$INGRESS_STOP" ]; do
      started=$(date +%s)
      $PY "$INGRESS_PY" --roster "$ROSTER" --run-now-dir "$RUN_NOW_DIR" --ready-file "$INGRESS_READY"
      rc=$?
      [ -f "$INGRESS_STOP" ] && break
      ran=$(( $(date +%s) - started ))
      if [ "$rc" = 0 ]; then
        log "telegram-ingress exited (roster reload) after ${ran}s — restarting"
        sleep 2
        continue
      fi
      restarts=$((restarts + 1))
      log "telegram-ingress died rc=$rc after ${ran}s — restart #$restarts"
      if [ "$restarts" -eq 1 ] || [ $((restarts % 10)) -eq 0 ]; then
        ops_alert "telegram-ingress died (rc=$rc after ${ran}s, restart #$restarts) — inbound Telegram is degraded; see docker logs"
      fi
      [ "$ran" -lt 60 ] && sleep 30 || sleep 5
    done ) &
  INGRESS_SUP_PID=$!
  log "telegram-ingress supervisor up (pid $INGRESS_SUP_PID)"
  # Readiness handoff: the first turn derives each pass's mode from the daemon's
  # markers — wait (bounded) until they exist, or a pass could start in direct
  # mode against a bot the daemon is about to consume.
  for _i in $(seq 1 30); do [ -f "$INGRESS_READY" ] && break; sleep 1; done
  [ -f "$INGRESS_READY" ] && log "telegram-ingress ready" || log "WARN: telegram-ingress not ready after 30s — passes fall back to direct polling until its markers appear"
fi
stop_ingress() {
  [ -n "$INGRESS_SUP_PID" ] || return 0
  touch "$INGRESS_STOP"
  pkill -TERM -P "$INGRESS_SUP_PID" 2>/dev/null
  kill "$INGRESS_SUP_PID" 2>/dev/null
}
trap stop_ingress EXIT

TURN=0
while :; do
  TURN=$((TURN + 1))
  if [ -n "${ORCH_MAX_TURNS:-}" ] && [ "$TURN" -gt "$ORCH_MAX_TURNS" ]; then
    log "ORCH_MAX_TURNS=$ORCH_MAX_TURNS reached — exiting (test mode)"
    exit 0
  fi
  [ "$DRAIN" = 1 ] && { log "drained — exiting"; exit 0; }

  maybe_rollup   # once-daily usage summary to ops (no-op the rest of the day)

  RUN_NOW_ARGS=()
  if [ -f "$RUN_NOW_FILE" ]; then
    RUN_NOW_ARGS=(--run-now "$(head -1 "$RUN_NOW_FILE" 2>/dev/null | tr -d '[:space:]')")
  fi
  # ALL pending ingress wakes, oldest first (ls -tr) — the scheduler picks the
  # first runnable one, so a deferred tenant never blocks the others.
  WAKES="$(ls -tr "$RUN_NOW_DIR" 2>/dev/null | paste -sd, - 2>/dev/null)"
  [ -n "$WAKES" ] && RUN_NOW_ARGS+=(--inbox-wakes "$WAKES")
  if ! DECISION_SH="$($PY "$ORCH_PY" next --sh --roster "$ROSTER" --state "$STATE_FILE" \
                       ${RUN_NOW_ARGS[@]+"${RUN_NOW_ARGS[@]}"})"; then
    log "WARN: orch.py next failed — retrying in 60s"
    sleep 60
    continue
  fi
  WAKE_KIND="" WAKE_NAME="" DROP_WAKES=""
  eval "$DECISION_SH"
  [ "${CONSUME_RUN_NOW:-0}" = 1 ] && rm -f "$RUN_NOW_FILE"
  for _w in $DROP_WAKES; do rm -f "$RUN_NOW_DIR/$_w"; done   # unknown/paused/parked
  RUN_NOW_PATH=""
  [ -n "$WAKE_NAME" ] && RUN_NOW_PATH="$RUN_NOW_DIR/$WAKE_NAME"

  if [ "$ACTION" = "sleep" ]; then
    log "sleep ${SLEEP_S}s — $REASON"
    sleep_interruptible "$SLEEP_S"
    continue
  fi

  # ACTION=run → PROJECT WORK_TREE ENV_FILE STATE_DIR MODEL PROJECT_TZ SKILL MANAGER CADENCE
  #              PRECHECK FORCE_FULL TIMEOUT_S
  record() {  # $1 = outcome class
    local RECORD_SH
    if ! RECORD_SH="$($PY "$ORCH_PY" record --sh --roster "$ROSTER" \
                       --state "$STATE_FILE" --project "$PROJECT" --outcome "$1")"; then
      log "WARN: record failed for $PROJECT ($1)"
      return 1
    fi
    eval "$RECORD_SH"
    log "turn $PROJECT: outcome=$1 next_eligible=${NEXT_ELIGIBLE:-?}"
    [ -n "${ESCALATE_PROJECT:-}" ] && project_alert "$ENV_FILE" "$STATE_DIR" "$ESCALATE_PROJECT"
    [ -n "${ESCALATE_OPS:-}" ] && ops_alert "$PROJECT: $ESCALATE_OPS"
    return 0
  }

  # An interactive `/loop` session holding this project's singleton lock is
  # being actively worked — requeue shortly, never a dry pass (§5).
  if [ -n "$LOCK_SH" ] && TICKET_LOOP_STATE_DIR="$STATE_DIR" bash "$LOCK_SH" status >/dev/null 2>&1; then
    log "$PROJECT: singleton lock held (interactive session?) — requeue"
    record skipped-lock
    continue      # an inbox wake stays pending; the scheduler skips a locked tenant
  fi
  # The wake is consumed here, once the lock check passed. If the runner still
  # yields the lock (a session grabbed it in between), classify re-signals it.
  [ -n "$RUN_NOW_PATH" ] && rm -f "$RUN_NOW_PATH"

  INGRESS_MODE=direct
  [ "$INGRESS_ENABLED" = 1 ] && INGRESS_MODE="$(ingress_mode_for "$ENV_FILE" "$STATE_DIR")"

  if [ "$WAKE_KIND" = inbox ]; then
    # The message that raised this wake may already be drained: a pass that was
    # running when it arrived re-drains the spool after every send. Peek is a
    # local file count here (no network, no secrets) — a non-numeric answer
    # (peek failed) FAILS OPEN to a pass rather than hiding a broken spool.
    PK="$( ( set -a; . "$ENV_FILE"; set +a
             tg_fallback
             TICKET_LOOP_INGRESS=1 TICKET_LOOP_STATE_DIR="$STATE_DIR" python3 "$TELEGRAM" peek ) 2>&1 )"
    case "$PK" in
      0)           log "wake $PROJECT: spool already drained by the last pass — skipping"; continue ;;
      ''|*[!0-9]*) log "wake $PROJECT: peek failed (${PK:0:100}) — running the pass (fail open)" ;;
      *)           log "wake $PROJECT: $PK spooled message(s) — running pass" ;;
    esac
  elif [ "${PRECHECK:-0}" = 1 ]; then
    # Cheap pre-check (spec §3): queue depth + read-only peek. Open questions
    # are deliberately NOT a signal: an answer a human sent is an unconsumed
    # update the peek sees, so "questions still open" alone means nobody has
    # answered yet — running a pass to re-check would burn a full claude pass
    # to learn nothing (the 8h forced-full pass remains the drift backstop).
    # queue-count failures FAIL OPEN (run the pass — it is the source of truth,
    # and a real outage then surfaces as a loud error class, not a silent skip).
    SIGNAL=0 WHY="no work signal"
    QC="$( ( set -a; . "$ENV_FILE"; set +a
             python3 "$QUEUE_COUNT" --config "$WORK_TREE/dev-workflow.yml" ) 2>&1 )"
    case "$QC" in
      0)           : ;;
      ''|*[!0-9]*) SIGNAL=1; WHY="queue-count failed, failing open: ${QC:0:120}" ;;
      *)           SIGNAL=1; WHY="queue depth $QC" ;;
    esac
    if [ "$SIGNAL" = 0 ] && [ -n "$TELEGRAM" ]; then
      PK="$( ( set -a; . "$ENV_FILE"; set +a
               tg_fallback
               TICKET_LOOP_INGRESS="$([ "$INGRESS_MODE" = spool ] && echo 1 || echo 0)" \
               TICKET_LOOP_STATE_DIR="$STATE_DIR" python3 "$TELEGRAM" peek ) 2>&1 )"
      case "$PK" in
        '')          : ;;
        *[!0-9]*)    SIGNAL=1; WHY="peek failed, failing open: ${PK:0:120}" ;;
        0)           : ;;
        *)           SIGNAL=1; WHY="$PK unread group message(s)" ;;
      esac
    fi
    if [ "$SIGNAL" = 0 ]; then
      log "pre-check $PROJECT: idle (queue 0, no pending messages) — skipping pass"
      record precheck-idle
      continue
    fi
    log "pre-check $PROJECT: $WHY — running pass"
  else
    FF_NOTE=""
    [ "${FORCE_FULL:-0}" = 1 ] && FF_NOTE="forced-full, "
    log "pass $PROJECT: no pre-check (${FF_NOTE}cadence=$CADENCE)"
  fi

  # Crash write-ahead BEFORE launch (§3).
  $PY "$ORCH_PY" pass-start --roster "$ROSTER" --state "$STATE_FILE" \
    --project "$PROJECT" >/dev/null 2>&1 \
    || log "WARN: could not persist pass-start write-ahead"

  # Minimal, project-scoped child env (spec §5): the pass sources its own
  # agent.env via DW_ENV_FILE; nothing from any other project leaks in.
  # DW_ORCHESTRATED=1 tells cron-run.sh that escalation is the orchestrator's job
  # (its existing threshold path) — so the pass does NOT page ops itself. Only
  # single-mode timers (flag unset) self-page. It carries no secret.
  # TICKET_LOOP_INGRESS is decided HERE, per pass, from the daemon's liveness
  # marker in this tenant's state dir — 1: the pass drains the spool and never
  # calls getUpdates; 0: no daemon serves this tenant (no bot/chat in its env,
  # ingress disabled or down), so the pass polls Telegram directly as before.
  ENV_ARGS=( HOME="$HOME" PATH="$PATH" LANG="${LANG:-C.UTF-8}"
             DW_ENV_FILE="$ENV_FILE" DW_WORK_TREE="$WORK_TREE"
             TICKET_LOOP_STATE_DIR="$STATE_DIR" DW_ORCHESTRATED=1
             TICKET_LOOP_INGRESS="$([ "$INGRESS_MODE" = spool ] && echo 1 || echo 0)" )
  [ -n "${MODEL:-}" ]                  && ENV_ARGS+=( TICKET_LOOP_MODEL="$MODEL" )
  [ -n "${PROJECT_TZ:-}" ]             && ENV_ARGS+=( TICKET_LOOP_TZ="$PROJECT_TZ" )
  # Per-entry mode (roster overrides the repo's own agent.*): which skill the pass
  # invokes, and manager mode (the runner must NOT reset a parent work tree).
  [ -n "${SKILL:-}" ]                  && ENV_ARGS+=( DW_SKILL="$SKILL" )
  [ "${MANAGER:-0}" = 1 ]              && ENV_ARGS+=( DW_MANAGER=1 )
  [ -n "${TICKET_LOOP_MCP_CONFIG:-}" ] && ENV_ARGS+=( TICKET_LOOP_MCP_CONFIG="$TICKET_LOOP_MCP_CONFIG" )
  [ -n "${DW_PLUGIN_DIR:-}" ]          && ENV_ARGS+=( DW_PLUGIN_DIR="$DW_PLUGIN_DIR" )
  # Shared default bot: run-pass sources the env file over this, so a project's
  # own TELEGRAM_BOT_TOKEN (present in the file) always wins over the injection.
  if [ -n "${DEFAULT_TELEGRAM_BOT_TOKEN:-}" ] \
     && ! grep -qE '^[[:space:]]*TELEGRAM_BOT_TOKEN=' "$ENV_FILE" 2>/dev/null; then
    ENV_ARGS+=( TELEGRAM_BOT_TOKEN="$DEFAULT_TELEGRAM_BOT_TOKEN" TELEGRAM_SHARED_BOT=1 )
  fi
  # Common Claude token: injected as a baseline; run-pass sources the project's
  # env file AFTER this, so a project that carries its own CLAUDE_CODE_OAUTH_TOKEN
  # (its own account / limit pool) transparently overrides it. Unlike the bot,
  # no mode flag rides along, so always-inject + let-the-file-win is correct.
  [ -n "${DEFAULT_CLAUDE_CODE_OAUTH_TOKEN:-}" ] \
    && ENV_ARGS+=( CLAUDE_CODE_OAUTH_TOKEN="$DEFAULT_CLAUDE_CODE_OAUTH_TOKEN" )

  run_with_timeout "$TIMEOUT_S" env -i "${ENV_ARGS[@]}" "$RUN_PASS"
  RC=$?
  TO_ARGS=()
  [ "$RC" -eq 124 ] && TO_ARGS=(--timed-out)
  CLASSIFY_OUT="$($PY "$ORCH_PY" classify --state-dir "$STATE_DIR" --rc "$RC" \
                   ${TO_ARGS[@]+"${TO_ARGS[@]}"} 2>/dev/null)" \
    || CLASSIFY_OUT="error classify itself failed"
  CLASS="${CLASSIFY_OUT%% *}"
  log "classify $PROJECT: $CLASSIFY_OUT"
  if [ "$CLASS" = skipped-lock ] && [ "$WAKE_KIND" = inbox ]; then
    echo inbox > "$RUN_NOW_DIR/$PROJECT"     # the runner yielded: the wake is still owed
    log "wake $PROJECT: re-signalled (runner yielded the lock)"
  fi
  record "$CLASS"
done
