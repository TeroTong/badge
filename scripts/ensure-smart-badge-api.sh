#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${SMART_BADGE_API_TMUX_SESSION:-smart-badge-api}"
START_SCRIPT="/opt/badge/scripts/start-smart-badge-api.sh"
LOG_FILE="/tmp/smart-badge-api-supervisor.log"
TMUX_BIN="/usr/bin/tmux"
SS_BIN="/usr/bin/ss"
AWK_BIN="/usr/bin/awk"
GREP_BIN="/usr/bin/grep"
TAIL_BIN="/usr/bin/tail"

is_port_listening() {
    "$SS_BIN" -ltn | "$AWK_BIN" '{print $4}' | "$GREP_BIN" -Eq '(^|:)8000$'
}

session_exists() {
    "$TMUX_BIN" has-session -t "$SESSION_NAME" 2>/dev/null
}

wait_for_port() {
    for _ in {1..30}; do
        if is_port_listening; then
            return 0
        fi
        sleep 1
    done
    return 1
}

if is_port_listening; then
    exit 0
fi

if session_exists && ! is_port_listening; then
    "$TMUX_BIN" kill-session -t "$SESSION_NAME" >/dev/null 2>&1 || true
fi

"$TMUX_BIN" new-session -d -s "$SESSION_NAME" "/bin/bash -lc '$START_SCRIPT >>$LOG_FILE 2>&1'"

if wait_for_port; then
    exit 0
fi

if [ -f "$LOG_FILE" ]; then
    "$TAIL_BIN" -n 80 "$LOG_FILE" >&2 || true
fi
exit 1
