#!/bin/bash
# Time /customers as different user types
cd /opt/badge/apps/api

# Get admin and a hospital_admin user
ADMIN=$(docker exec -i smart-badge-postgres psql -U postgres -d smart_badge -tAc "SELECT id FROM users WHERE role IN ('super_admin','system_admin') LIMIT 1")
HOSP=$(docker exec -i smart-badge-postgres psql -U postgres -d smart_badge -tAc "SELECT id FROM users WHERE role='hospital_admin' LIMIT 1")
STAFF=$(docker exec -i smart-badge-postgres psql -U postgres -d smart_badge -tAc "SELECT u.id FROM users u WHERE u.staff_id IS NOT NULL AND u.role NOT IN ('super_admin','system_admin','hospital_admin') LIMIT 1")

echo "admin=$ADMIN"
echo "hosp =$HOSP"
echo "staff=$STAFF"

for label in admin hosp staff; do
  case $label in
    admin) U=$ADMIN ;;
    hosp)  U=$HOSP ;;
    staff) U=$STAFF ;;
  esac
  if [ -z "$U" ]; then echo "--- $label: skip (no user) ---"; continue; fi
  TOKEN=$(/home/ymailancy/.local/bin/uv run python -c "from smart_badge_api.core.security import create_access_token; print(create_access_token('$U'))")
  echo "--- $label ($U) ---"
  for i in 1 2 3; do
    curl -sS -o /tmp/cust_$label -w "  Run $i: HTTP %{http_code} %{time_total}s\n" \
      -H "Authorization: Bearer $TOKEN" \
      "http://127.0.0.1:8000/api/v1/customers?include_date_summaries=false&page=1&page_size=12"
  done
  python3 -c "import json; d=json.load(open('/tmp/cust_$label')); print('  total=',d.get('total'),'items=',len(d.get('items',[])))"
done
