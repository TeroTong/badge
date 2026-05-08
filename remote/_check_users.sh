#!/bin/bash
docker exec -i smart-badge-postgres psql -U postgres -d smart_badge <<'SQL'
SELECT id, role, staff_id, hospital_code FROM users WHERE id IN ('bb6f5b3101bf','da5a90e8fc71','672303f2044d');
SQL
