#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${SMART_BADGE_WORKER_TMUX_SESSION:-smart-badge-worker}"
START_SCRIPT="/opt/badge/scripts/start-smart-badge-worker.sh"
LOG_FILE="/tmp/smart-badge-worker.log"
TMUX_BIN="/usr/bin/tmux"
TAIL_BIN="/usr/bin/tail"

session_exists() {
    "$TMUX_BIN" has-session -t "$SESSION_NAME" 2>/dev/null
}

wait_for_session() {
    for _ in {1..20}; do
        if session_exists; then
            return 0
        fi
        sleep 1
    done
    return 1
}

if session_exists; then
    exit 0
fi

"$TMUX_BIN" new-session -d -s "$SESSION_NAME" "/bin/bash -lc '$START_SCRIPT >>$LOG_FILE 2>&1'"

if wait_for_session; then
    exit 0
fi

if [ -f "$LOG_FILE" ]; then
    "$TAIL_BIN" -n 80 "$LOG_FILE" >&2 || true
fi
exit 1
