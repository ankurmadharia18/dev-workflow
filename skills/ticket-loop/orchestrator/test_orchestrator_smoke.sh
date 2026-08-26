#!/bin/bash
# Offline end-to-end smoke test for orchestrator.sh: one full turn against a
# stub runner — no network, no docker, no claude. Uses fixed cadence so the
# pre-check (which would need Linear/Telegram) is skipped.
#
# Run: bash skills/ticket-loop/orchestrator/test_orchestrator_smoke.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/root/proj" "$TMP/orch"
touch "$TMP/root/proj/.dw-agent-clone"
: > "$TMP/agent.env"

cat > "$TMP/root/roster.yml" <<EOF
root: $TMP/root
cadence: fixed
interval: 30m
projects:
  - name: proj
    work_tree: $TMP/root/proj
    env_file: $TMP/agent.env
    state_dir: $TMP/state
    skill: ticket-loop-parent
    manager: true
EOF

# Orchestrator env file (auto-discovered next to the roster): the project's env
# file is empty, so the pass must receive the shared default bot in no-ack mode
# AND the common Claude token.
cat > "$TMP/root/orch.env" <<'EOF'
DEFAULT_TELEGRAM_BOT_TOKEN=fake-default-token
DEFAULT_CLAUDE_CODE_OAUTH_TOKEN=fake-claude-token
EOF

# Stub runner: pretends one pass opened a PR, via the outcome.json contract.
# Also asserts the default-bot + common-token injection (a missing env there →
# exit 1 → the outcome assertion below fails loudly instead of passing vacuously).
cat > "$TMP/stub-pass.sh" <<'EOF'
#!/bin/bash
[ "${TELEGRAM_BOT_TOKEN:-}" = "fake-default-token" ] || { echo "FAIL: default bot not injected" >&2; exit 1; }
[ "${TELEGRAM_SHARED_BOT:-}" = "1" ] || { echo "FAIL: shared-bot flag not set" >&2; exit 1; }
[ "${CLAUDE_CODE_OAUTH_TOKEN:-}" = "fake-claude-token" ] || { echo "FAIL: common Claude token not injected" >&2; exit 1; }
[ "${DW_SKILL:-}" = "ticket-loop-parent" ] || { echo "FAIL: roster skill not passed as DW_SKILL" >&2; exit 1; }
[ "${DW_MANAGER:-}" = "1" ] || { echo "FAIL: roster manager mode not passed as DW_MANAGER" >&2; exit 1; }
# No ingress marker for this tenant (its env has no chat id) → the pass must be
# told explicitly to poll Telegram directly, never left to guess.
[ "${TICKET_LOOP_INGRESS:-}" = "0" ] || { echo "FAIL: TICKET_LOOP_INGRESS not set to 0 for an unserved tenant" >&2; exit 1; }
mkdir -p "$TICKET_LOOP_STATE_DIR"
printf '{"picked":1,"pr_opened":1,"asked":0,"blocked":0,"progressed":true,"error":null}\n' \
  > "$TICKET_LOOP_STATE_DIR/outcome.json"
EOF
chmod +x "$TMP/stub-pass.sh"

ORCH_ROSTER="$TMP/root/roster.yml" ORCH_STATE_DIR="$TMP/orch" \
ORCH_RUN_PASS="$TMP/stub-pass.sh" ORCH_MAX_TURNS=1 \
  bash "$HERE/orchestrator.sh"

python3 - "$TMP/orch/orch-state.json" <<'PY'
import json, sys
st = json.load(open(sys.argv[1]))
ps = st["projects"]["proj"]
assert ps["last_outcome"] == "productive", ps
assert ps["next_eligible"], ps
assert "pass_started" not in st, st        # write-ahead consumed by record
print("smoke OK — outcome:", ps["last_outcome"], "next:", ps["next_eligible"])
PY
echo PASS
