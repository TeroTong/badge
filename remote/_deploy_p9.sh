#!/bin/bash
set -e
TS=$(date +%Y%m%d_%H%M%S)
BAK=/opt/badge/.bak.$TS
mkdir -p $BAK/apps/api/src/smart_badge_api/api/routes
cp /opt/badge/apps/api/src/smart_badge_api/api/routes/visits.py $BAK/apps/api/src/smart_badge_api/api/routes/visits.py
cp /opt/badge/apps/api/src/smart_badge_api/api/routes/analysis.py $BAK/apps/api/src/smart_badge_api/api/routes/analysis.py
mv /tmp/visits.py.new /opt/badge/apps/api/src/smart_badge_api/api/routes/visits.py
mv /tmp/analysis.py.new /opt/badge/apps/api/src/smart_badge_api/api/routes/analysis.py
echo "Backup at $BAK"
/opt/badge/restart_badge.sh
sleep 3
curl -sf http://127.0.0.1:8000/api/v1/openapi.json -o /dev/null && echo "API OK" || echo "API FAIL"
