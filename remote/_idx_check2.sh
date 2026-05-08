#!/bin/bash
docker exec -i smart-badge-postgres psql -U postgres -d smart_badge <<'SQL'
select tablename, indexname from pg_indexes
 where schemaname='public' and tablename in ('audit_logs','risk_records')
 order by tablename, indexname;
SQL
