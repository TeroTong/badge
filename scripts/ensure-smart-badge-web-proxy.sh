#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${SMART_BADGE_WEB_PROXY_CONTAINER:-smart-badge-web-proxy}"
HOST_PORT="${SMART_BADGE_WEB_PROXY_PORT:-5173}"
START_SCRIPT="/opt/badge/scripts/start-smart-badge-web-proxy.sh"
SS_BIN="/usr/bin/ss"
AWK_BIN="/usr/bin/awk"
GREP_BIN="/usr/bin/grep"

is_port_listening() {
    "$SS_BIN" -ltn | "$AWK_BIN" '{print $4}' | "$GREP_BIN" -Eq "(^|:)${HOST_PORT}$"
}

container_running() {
    docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | "$GREP_BIN" -qx 'true'
}

if container_running && is_port_listening; then
    exit 0
fi

if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
fi

"$START_SCRIPT" >/dev/null

sleep 3
container_running
is_port_listening
