#!/bin/bash
set -e
TS=$(date +%Y%m%d_%H%M%S)
BAK=/opt/badge/.bak.$TS
mkdir -p $BAK/apps/api/src/smart_badge_api/api/routes
cp /opt/badge/apps/api/src/smart_badge_api/api/routes/recordings.py $BAK/apps/api/src/smart_badge_api/api/routes/recordings.py
mv /tmp/recordings.py.new /opt/badge/apps/api/src/smart_badge_api/api/routes/recordings.py
echo "Backup at $BAK"
/opt/badge/restart_badge.sh
sleep 4
curl -sf http://127.0.0.1:8000/api/v1/openapi.json -o /dev/null && echo "API OK"
sleep 2
bash /tmp/_test_archive.sh
