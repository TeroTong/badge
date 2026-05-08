#!/usr/bin/env bash
set -euo pipefail

cd /opt/badge/apps/api

unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

export TASK_DISPATCH_MODE=dramatiq
export SMART_BADGE_WORKER_PROCESSES="${SMART_BADGE_WORKER_PROCESSES:-2}"
export SMART_BADGE_WORKER_THREADS="${SMART_BADGE_WORKER_THREADS:-1}"

exec /home/ymailancy/.local/bin/uv run dramatiq smart_badge_api.task_queue --processes "$SMART_BADGE_WORKER_PROCESSES" --threads "$SMART_BADGE_WORKER_THREADS"
