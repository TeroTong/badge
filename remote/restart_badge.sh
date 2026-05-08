#!/usr/bin/env bash
set -u
cd /opt/badge
mkdir -p logs

# 终止旧 API 进程
pkill -TERM -f 'uvicorn smart_badge_api.main:app' || true
# 终止旧 worker 进程
pkill -TERM -f 'dramatiq smart_badge_api.task_queue' || true
sleep 3
pkill -KILL -f 'uvicorn smart_badge_api.main:app' || true
pkill -KILL -f 'dramatiq smart_badge_api.task_queue' || true
sleep 1

cd /opt/badge/apps/api

UV=/home/ymailancy/.local/bin/uv

nohup "$UV" run uvicorn smart_badge_api.main:app --host 0.0.0.0 --port 8000 \
    >> /opt/badge/logs/api.out 2>&1 < /dev/null &
disown || true

nohup "$UV" run dramatiq smart_badge_api.task_queue --processes 2 --threads 1 \
    >> /opt/badge/logs/worker.out 2>&1 < /dev/null &
disown || true

sleep 5
ps -ef | grep -E 'uvicorn smart_badge_api|dramatiq smart_badge_api' | grep -v grep
