#!/bin/bash
cd /opt/badge/apps/api
ADMIN=$(docker exec -i smart-badge-postgres psql -U postgres -d smart_badge -tAc "SELECT id FROM users WHERE role IN ('super_admin','system_admin') LIMIT 1")
HOSP=$(docker exec -i smart-badge-postgres psql -U postgres -d smart_badge -tAc "SELECT id FROM users WHERE role='hospital_admin' LIMIT 1")
STAFF=$(docker exec -i smart-badge-postgres psql -U postgres -d smart_badge -tAc "SELECT u.id FROM users u WHERE u.staff_id IS NOT NULL AND u.role NOT IN ('super_admin','system_admin','hospital_admin') LIMIT 1")
STAFF_SID=$(docker exec -i smart-badge-postgres psql -U postgres -d smart_badge -tAc "SELECT staff_id FROM users WHERE id='$STAFF'")
echo "staff_sid=$STAFF_SID"
for label in admin hosp staff; do
  case $label in admin) U=$ADMIN; SID="" ;; hosp) U=$HOSP; SID="" ;; staff) U=$STAFF; SID=$STAFF_SID ;; esac
  TOKEN=$(/home/ymailancy/.local/bin/uv run python -c "from smart_badge_api.core.security import create_access_token; print(create_access_token('$U'))")
  Q="page=1&page_size=20&exclude_quality_filtered=true&include_date_summaries=false"
  if [ -n "$SID" ]; then Q="staff_id=$SID&$Q"; fi
  echo "--- $label ($U) /recordings/archive?$Q ---"
  for i in 1 2 3; do
    curl -sS -o /tmp/arc_$label -w "  Run $i: HTTP %{http_code} %{time_total}s\n" \
      -H "Authorization: Bearer $TOKEN" \
      "http://127.0.0.1:8000/api/v1/recordings/archive?$Q"
  done
  python3 -c "import json; d=json.load(open('/tmp/arc_$label')); print('  total=',d.get('total'),'items=',len(d.get('items',[])))" 2>/dev/null || cat /tmp/arc_$label | head -c 300; echo
done
