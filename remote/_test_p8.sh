#!/bin/bash
# Login as a staff user with WeCom-typical scope, hit /customers, time it.
set -e
USERNAME=$(docker exec -i smart-badge-postgres psql -U postgres -d smart_badge -tAc "select username from users where staff_id='0cb7d5bff6fb' limit 1")
echo "user=$USERNAME"
if [ -z "$USERNAME" ]; then
  echo "no user for staff 0cb7d5bff6fb; sampling another active staff"
  docker exec -i smart-badge-postgres psql -U postgres -d smart_badge -c "select u.username, s.id, s.name from users u join staff s on s.id=u.staff_id where s.is_active=true limit 5"
  exit 1
fi
# attempt login (assume password resettable; try via internal admin path)
# Use existing user - we don't know password. Instead, mint a token via an admin script if available. Otherwise use SQL to grab existing session.
echo "--- recent /customers requests in api log ---"
grep -E 'GET /api/v1/customers ' /opt/badge/logs/api.out | tail -10 || true
echo
echo "--- timing a fresh /customers fetch needs a valid token; testing endpoint shape ---"
curl -sS -o /tmp/cust_resp -w "HTTP %{http_code}  total %{time_total}s\n" "http://127.0.0.1:8000/api/v1/customers?page=1&page_size=20"
head -c 200 /tmp/cust_resp; echo
