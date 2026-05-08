#!/bin/bash
set -e
TS=$(date +%Y%m%d_%H%M%S)
BAK=/opt/badge/.bak.$TS
mkdir -p "$BAK/api/routes"
cp /opt/badge/apps/api/src/smart_badge_api/api/routes/dashboard.py "$BAK/api/routes/"
cp /opt/badge/apps/api/src/smart_badge_api/api/routes/recordings.py "$BAK/api/routes/"
cp /opt/badge/apps/api/src/smart_badge_api/api/routes/risk_records.py "$BAK/api/routes/"
cp /opt/badge/apps/api/src/smart_badge_api/api/routes/audit_logs.py "$BAK/api/routes/"
echo "Backup -> $BAK"

cp /tmp/p7_dashboard.py /opt/badge/apps/api/src/smart_badge_api/api/routes/dashboard.py
cp /tmp/p7_recordings.py /opt/badge/apps/api/src/smart_badge_api/api/routes/recordings.py
cp /tmp/p7_risk_records.py /opt/badge/apps/api/src/smart_badge_api/api/routes/risk_records.py
cp /tmp/p7_audit_logs.py /opt/badge/apps/api/src/smart_badge_api/api/routes/audit_logs.py
cp /tmp/p7_d3b9f5e21c02_add_audit_logs_created_at_index.py /opt/badge/apps/api/migrations/versions/d3b9f5e21c02_add_audit_logs_created_at_index.py
echo "Files installed."

cd /opt/badge/apps/api
/home/ymailancy/.local/bin/uv run alembic upgrade head
echo "Migration done."

bash /opt/badge/restart_badge.sh
sleep 4
curl -s -o /dev/null -w "api openapi=%{http_code}\n" http://127.0.0.1:8000/api/v1/openapi.json
echo === recent api errors ===
tail -100 /opt/badge/logs/api.out | grep -iE 'error|exception|traceback' | tail -10 || true
echo "DONE"
