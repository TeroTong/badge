#!/bin/bash
set -e
TS=$(date +%Y%m%d_%H%M%S)
BAK=/opt/badge/.bak.$TS
mkdir -p $BAK/apps/api/src/smart_badge_api/api/routes
cp /opt/badge/apps/api/src/smart_badge_api/api/routes/customers.py $BAK/apps/api/src/smart_badge_api/api/routes/customers.py
mv /tmp/customers.py.new /opt/badge/apps/api/src/smart_badge_api/api/routes/customers.py
echo "Backup at $BAK"
/opt/badge/restart_badge.sh
sleep 2
echo "--- last 30 lines of api log ---"
tail -30 /opt/badge/logs/api.out
echo "--- health ---"
curl -sf http://127.0.0.1:8000/api/v1/openapi.json -o /dev/null && echo "API OK" || echo "API FAIL"
