#!/bin/bash
set -e
TS=$(date +%Y%m%d_%H%M%S)
mkdir -p /opt/badge/.bak.$TS
cp /opt/badge/apps/api/src/smart_badge_api/dingtalk.py \
   /opt/badge/apps/api/src/smart_badge_api/api/routes/recordings.py \
   /opt/badge/apps/api/src/smart_badge_api/core/config.py \
   /opt/badge/.bak.$TS/
echo "BACKUP=/opt/badge/.bak.$TS"
echo "=== syntax check ==="
python3 -m py_compile \
  /opt/badge/apps/api/src/smart_badge_api/dingtalk.py \
  /opt/badge/apps/api/src/smart_badge_api/api/routes/recordings.py \
  /opt/badge/apps/api/src/smart_badge_api/core/config.py
echo SYNTAX_OK
echo "=== restart ==="
bash /opt/badge/restart_badge.sh
sleep 6
echo "=== api log tail ==="
tail -n 30 /opt/badge/logs/api.out
echo "=== worker log tail ==="
tail -n 12 /opt/badge/logs/worker.out
echo "=== health ==="
curl -sS -o /dev/null -w "HTTP=%{http_code}\n" http://127.0.0.1:8000/api/v1/openapi.json
