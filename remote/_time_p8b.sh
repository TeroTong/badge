#!/bin/bash
set -e
USER_ID=$(docker exec -i smart-badge-postgres psql -U postgres -d smart_badge -tAc "SELECT id FROM users WHERE staff_id='0cb7d5bff6fb' LIMIT 1")
echo "user_id=$USER_ID"
cd /opt/badge/apps/api
TOKEN=$(/home/ymailancy/.local/bin/uv run python -c "
from smart_badge_api.core.security import create_access_token
print(create_access_token('$USER_ID'))
")
echo "token len=${#TOKEN}"
for i in 1 2 3; do
  curl -sS -o /tmp/cust_$i -w "Run $i: HTTP %{http_code}  total %{time_total}s\n" \
    -H "Authorization: Bearer $TOKEN" \
    "http://127.0.0.1:8000/api/v1/customers?page=1&page_size=20&include_date_summaries=false"
done
python3 -c "import json; d=json.load(open('/tmp/cust_3')); print('total=',d.get('total'),'items=',len(d.get('items',[])))"
echo "--- with date_summaries=true ---"
curl -sS -o /tmp/cust_ds -w "HTTP %{http_code}  total %{time_total}s\n" \
  -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/v1/customers?page=1&page_size=20&include_date_summaries=true"
