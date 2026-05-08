#!/bin/bash
set -e
TS=$(date +%Y%m%d_%H%M%S)
echo "=== syntax check ==="
python3 -m py_compile \
  /opt/badge/apps/api/src/smart_badge_api/main.py \
  /opt/badge/apps/api/src/smart_badge_api/asr/speaker_voiceprint.py \
  /opt/badge/apps/api/src/smart_badge_api/api/ws_hub.py \
  /opt/badge/apps/api/src/smart_badge_api/recording_analysis_service.py \
  /opt/badge/apps/api/src/smart_badge_api/analysis/runner.py \
  /opt/badge/apps/api/src/smart_badge_api/visit_order_sync.py
echo "SYNTAX_OK"
echo "=== restart ==="
bash /opt/badge/restart_badge.sh
sleep 6
echo "=== api log tail ==="
tail -n 40 /opt/badge/logs/api.out
echo "=== worker log tail ==="
tail -n 20 /opt/badge/logs/worker.out
echo "=== health ==="
curl -sS -o /dev/null -w "HTTP=%{http_code}\n" http://127.0.0.1:8000/api/v1/openapi.json
echo "=== done TS=$TS ==="
