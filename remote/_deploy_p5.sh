#!/bin/bash
set -e
TS=$(date +%Y%m%d_%H%M%S)
BAK=/opt/badge/.bak.$TS
mkdir -p "$BAK/api/routes" "$BAK/web"
cp /opt/badge/apps/api/src/smart_badge_api/api/routes/asr_monitoring.py "$BAK/api/routes/"
cp /opt/badge/apps/api/src/smart_badge_api/api/routes/sap_push_monitoring.py "$BAK/api/routes/"
cp /opt/badge/apps/api/src/smart_badge_api/api/routes/tasks.py "$BAK/api/routes/"
cp /opt/badge/apps/api/src/smart_badge_api/api/routes/dashboard.py "$BAK/api/routes/"
cp /opt/badge/apps/web/src/app/query-client.ts "$BAK/web/"
echo "Backup -> $BAK"

cp /tmp/p5_asr_monitoring.py /opt/badge/apps/api/src/smart_badge_api/api/routes/asr_monitoring.py
cp /tmp/p5_sap_push_monitoring.py /opt/badge/apps/api/src/smart_badge_api/api/routes/sap_push_monitoring.py
cp /tmp/p5_tasks.py /opt/badge/apps/api/src/smart_badge_api/api/routes/tasks.py
cp /tmp/p5_dashboard.py /opt/badge/apps/api/src/smart_badge_api/api/routes/dashboard.py
cp /tmp/p5_query-client.ts /opt/badge/apps/web/src/app/query-client.ts
echo "Files installed."

bash /opt/badge/restart_badge.sh
sleep 4
curl -s -o /dev/null -w "api openapi=%{http_code}\n" http://127.0.0.1:8000/api/v1/openapi.json

cd /opt/badge && pnpm --filter @smart-badge/web build 2>&1 | tail -20
docker exec smart-badge-web-proxy nginx -s reload
echo "DONE"
