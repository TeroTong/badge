#!/bin/bash
set -e
USER_ID=$(docker exec -i smart-badge-postgres psql -U postgres -d smart_badge -tAc "SELECT id FROM users WHERE staff_id='0cb7d5bff6fb' LIMIT 1")
cd /opt/badge/apps/api
TOKEN=$(/home/ymailancy/.local/bin/uv run python -c "
from smart_badge_api.core.security import create_access_token
print(create_access_token('$USER_ID'))
")
echo "--- /analysis/results: cache miss after TTL expiry (memo retained) ---"
sleep 65
for i in 1 2; do
  curl -sS -o /tmp/x_$i -w "Run $i (post-TTL miss): HTTP %{http_code} %{time_total}s\n" \
    -H "Authorization: Bearer $TOKEN" \
    "http://127.0.0.1:8000/api/v1/analysis/results?page=1&page_size=20"
done
