#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="/opt/badge"
API_DIR="$ROOT_DIR/apps/api"
LOG_DIR="$ROOT_DIR/logs"
API_START="$ROOT_DIR/scripts/start-smart-badge-api.sh"
WORKER_START="$ROOT_DIR/scripts/start-smart-badge-worker.sh"
AUDIO_ENSURE="$ROOT_DIR/scripts/ensure-smart-badge-audio-worker.sh"
AUDIO_SESSION="${SMART_BADGE_AUDIO_WORKER_TMUX_SESSION:-smart-badge-audio-worker}"

mkdir -p "$LOG_DIR"

kill_by_cwd() {
    local signal="$1"
    local pattern="$2"
    local cwd_prefix="$3"
    local pid
    while read -r pid; do
        [ -n "$pid" ] || continue
        [ "$pid" != "$$" ] || continue
        local cwd
        cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
        case "$cwd" in
            "$cwd_prefix"*)
                kill "-$signal" "$pid" 2>/dev/null || true
                ;;
        esac
    done < <(pgrep -f "$pattern" || true)
}

kill_by_cwd TERM 'uvicorn smart_badge_api.main:app' "$API_DIR"
kill_by_cwd TERM 'dramatiq smart_badge_api.task_queue' "$API_DIR"
kill_by_cwd TERM 'python -m smart_badge_api.audio_worker' "$API_DIR"
tmux kill-session -t "$AUDIO_SESSION" 2>/dev/null || true

sleep 3

kill_by_cwd KILL 'uvicorn smart_badge_api.main:app' "$API_DIR"
kill_by_cwd KILL 'dramatiq smart_badge_api.task_queue' "$API_DIR"
kill_by_cwd KILL 'python -m smart_badge_api.audio_worker' "$API_DIR"

nohup "$API_START" >> "$LOG_DIR/api.out" 2>&1 < /dev/null &
disown || true

nohup "$WORKER_START" >> "$LOG_DIR/worker.out" 2>&1 < /dev/null &
disown || true

"$AUDIO_ENSURE"

sleep 5
ps -ef | grep -E 'uvicorn smart_badge_api|dramatiq smart_badge_api|smart_badge_api.audio_worker' | grep -v grep
