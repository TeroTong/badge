#!/bin/bash
set -e
USER_ID=$(docker exec -i smart-badge-postgres psql -U postgres -d smart_badge -tAc "SELECT id FROM users WHERE staff_id='0cb7d5bff6fb' LIMIT 1")
cd /opt/badge/apps/api
TOKEN=$(/home/ymailancy/.local/bin/uv run python -c "
from smart_badge_api.core.security import create_access_token
print(create_access_token('$USER_ID'))
")
echo "user=$USER_ID"
echo "--- /visits ---"
for i in 1 2 3; do
  curl -sS -o /tmp/r_v_$i -w "Run $i: HTTP %{http_code} %{time_total}s\n" \
    -H "Authorization: Bearer $TOKEN" \
    "http://127.0.0.1:8000/api/v1/visits?page=1&page_size=20"
done
python3 -c "import json; d=json.load(open('/tmp/r_v_3')); print('  total=',d.get('total'),'items=',len(d.get('items',[])))"

echo "--- /recordings ---"
for i in 1 2 3; do
  curl -sS -o /tmp/r_r_$i -w "Run $i: HTTP %{http_code} %{time_total}s\n" \
    -H "Authorization: Bearer $TOKEN" \
    "http://127.0.0.1:8000/api/v1/recordings?page=1&page_size=20"
done
python3 -c "import json; d=json.load(open('/tmp/r_r_3')); print('  total=',d.get('total'),'items=',len(d.get('items',[])))"

echo "--- /visit-orders ---"
for i in 1 2 3; do
  curl -sS -o /tmp/r_vo_$i -w "Run $i: HTTP %{http_code} %{time_total}s\n" \
    -H "Authorization: Bearer $TOKEN" \
    "http://127.0.0.1:8000/api/v1/visit-orders?page=1&page_size=20"
done
python3 -c "import json; d=json.load(open('/tmp/r_vo_3')); print('  total=',d.get('total'),'items=',len(d.get('items',[])))"

echo "--- /analysis/results ---"
for i in 1 2 3; do
  curl -sS -o /tmp/r_a_$i -w "Run $i: HTTP %{http_code} %{time_total}s\n" \
    -H "Authorization: Bearer $TOKEN" \
    "http://127.0.0.1:8000/api/v1/analysis/results?page=1&page_size=20"
done
python3 -c "import json; d=json.load(open('/tmp/r_a_3')); print('  total=',d.get('total'),'items=',len(d.get('items',[])))"
