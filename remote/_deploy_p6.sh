#!/bin/bash
set -e
TS=$(date +%Y%m%d_%H%M%S)
BAK=/opt/badge/.bak.$TS
mkdir -p "$BAK/api/routes" "$BAK/api/migrations"
cp /opt/badge/apps/api/src/smart_badge_api/api/routes/quality_results.py "$BAK/api/routes/"
cp /opt/badge/apps/api/src/smart_badge_api/api/routes/dashboard.py "$BAK/api/routes/"
echo "Backup -> $BAK"

cp /tmp/p6_quality_results.py /opt/badge/apps/api/src/smart_badge_api/api/routes/quality_results.py
cp /tmp/p6_dashboard.py /opt/badge/apps/api/src/smart_badge_api/api/routes/dashboard.py
cp /tmp/p6_c2a8e4f10b91_add_performance_indexes.py /opt/badge/apps/api/migrations/versions/c2a8e4f10b91_add_performance_indexes.py
echo "Files installed."

cd /opt/badge/apps/api
/home/ymailancy/.local/bin/uv run alembic upgrade head
echo "Migration done."

bash /opt/badge/restart_badge.sh
sleep 4
curl -s -o /dev/null -w "api openapi=%{http_code}\n" http://127.0.0.1:8000/api/v1/openapi.json
echo "DONE"
