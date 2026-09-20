#!/usr/bin/env bash
# howto-broadcast.sh — (re)send the "how to use this group" message to every
# tenant Telegram group after a release, so the pinned how-to never goes stale.
#
# The canonical text lives in skills/ticket-loop/telegram-howto.md (update it
# alongside release notes); this script stamps it with the current plugin
# version and sends it via each tenant's own bot+group creds from .local/.
#
#   scripts/howto-broadcast.sh                 # send to the default groups
#   scripts/howto-broadcast.sh --dry-run       # show targets, send nothing
#   scripts/howto-broadcast.sh --pin           # also pin (bot must be group admin;
#                                              #   best-effort — pin by hand otherwise)
#   scripts/howto-broadcast.sh .local/rasa-agent.env   # explicit env file(s) only
#   scripts/howto-broadcast.sh --howto FILE …  # send FILE instead of the generic
#                                              #   how-to (e.g. PubX Data's custom
#                                              #   .local/pubx-data-howto.md — pair it
#                                              #   with that group's env file)
#
# Each env file must provide TELEGRAM_BOT_TOKEN + AGENT_TELEGRAM_CHAT_ID
# (the same contract telegram.py uses). Secrets are sourced per-subshell and
# never printed.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOWTO="$ROOT/skills/ticket-loop/telegram-howto.md"
VERSION="$(jq -r .version "$ROOT/.claude-plugin/plugin.json" 2>/dev/null || echo unknown)"

# pubx-analytics is DELIBERATELY not a default: its group ("PubX Data") carries a
# custom data-flavored pinned how-to — broadcasting the generic text there with
# --pin would clobber it. Its text lives in .local/pubx-data-howto.md; after
# updating it, send with:
#   scripts/howto-broadcast.sh --pin --howto .local/pubx-data-howto.md .local/pubx-analytics-agent.env
DEFAULT_ENVS=(.local/niptao-agent.env .local/rasa-agent.env .local/pubx-hq-agent.env)

DRY=0 PIN=0 ENVS=() WANT_HOWTO=0
for a in "$@"; do
  if [ "$WANT_HOWTO" = 1 ]; then HOWTO="$a"; WANT_HOWTO=0; continue; fi
  case "$a" in
    --dry-run) DRY=1 ;;
    --pin)     PIN=1 ;;
    --howto)   WANT_HOWTO=1 ;;
    --howto=*) HOWTO="${a#--howto=}" ;;
    *)         ENVS+=("$a") ;;
  esac
done
[ "$WANT_HOWTO" = 1 ] && { echo "--howto needs a FILE" >&2; exit 2; }
case "$HOWTO" in /*) ;; *) [ -f "$HOWTO" ] || HOWTO="$ROOT/$HOWTO" ;; esac
[ "${#ENVS[@]}" -eq 0 ] && ENVS=("${DEFAULT_ENVS[@]}")
[ -f "$HOWTO" ] || { echo "missing $HOWTO" >&2; exit 2; }

MSG="$(cat "$HOWTO")

— dev-workflow v$VERSION ($(date +%Y-%m-%d)). This message replaces any older pinned how-to."

FAIL=0
for envfile in "${ENVS[@]}"; do
  path="$ROOT/${envfile#"$ROOT"/}"
  name="$(basename "$path" | sed 's/-agent\.env$//;s/\.env$//')"
  if [ ! -f "$path" ]; then echo "SKIP $name — $envfile not found"; continue; fi
  if [ "$DRY" = 1 ]; then echo "WOULD SEND → $name ($envfile)"; continue; fi
  out="$(
    set -a; . "$path"; set +a
    python3 "$ROOT/skills/ticket-loop/telegram.py" send "$MSG" 2>&1
  )" || true
  msg_id="$(printf '%s' "$out" | sed -n 's/.*"message_id"[: ]*\([0-9][0-9]*\).*/\1/p')"
  if [ -z "$msg_id" ]; then
    echo "FAIL $name — $(printf '%s' "$out" | head -1)"; FAIL=1; continue
  fi
  echo "SENT $name — message_id $msg_id"
  if [ "$PIN" = 1 ]; then
    pin_out="$(
      set -a; . "$path"; set +a
      # Drop every existing pin first so only the fresh how-to stays pinned,
      # then pin the new message. Both need the bot to be a group admin.
      curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/unpinAllChatMessages" \
        -d "chat_id=${AGENT_TELEGRAM_CHAT_ID}" >/dev/null 2>&1
      curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/pinChatMessage" \
        -d "chat_id=${AGENT_TELEGRAM_CHAT_ID}" -d "message_id=${msg_id}" \
        -d "disable_notification=true" 2>&1
    )"
    case "$pin_out" in
      *'"ok":true'*) echo "     pinned (old pins cleared)" ;;
      *) echo "     pin failed (bot not admin? pin it by hand): $(printf '%s' "$pin_out" | head -c 120)" ;;
    esac
  fi
done
exit "$FAIL"
