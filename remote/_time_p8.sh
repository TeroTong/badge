#!/bin/bash
set -e
cd /opt/badge/apps/api
TOKEN=$(/home/ymailancy/.local/bin/uv run python -c "
from smart_badge_api.core.security import create_access_token
print(create_access_token('86000995'))
")
echo "token len=${#TOKEN}"
echo "--- pre-warm (cold cache OK timing) ---"
for i in 1 2 3; do
  curl -sS -o /tmp/cust_$i -w "Run $i: HTTP %{http_code}  total %{time_total}s\n" \
    -H "Authorization: Bearer $TOKEN" \
    "http://127.0.0.1:8000/api/v1/customers?page=1&page_size=20&include_date_summaries=false"
done
echo "--- response shape ---"
python3 -c "import json; d=json.load(open('/tmp/cust_3')); print('total=',d.get('total'),'items=',len(d.get('items',[])),'page=',d.get('page'))"
