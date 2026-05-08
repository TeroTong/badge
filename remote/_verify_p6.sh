#!/bin/bash
docker exec -i smart-badge-postgres psql -U postgres -d smart_badge <<'SQL'
select tablename, count(*) as idx_count from pg_indexes
 where schemaname='public' and indexname like 'ix_%'
   and tablename in ('recordings','analysis_tasks','sap_push_logs','visits','customers','transcripts','segments')
 group by tablename order by tablename;
SQL
echo === recent api errors ===
tail -100 /opt/badge/logs/api.out | grep -iE 'error|exception|traceback' | tail -10 || true
echo === smoke test ===
curl -s -o /dev/null -w "openapi=%{http_code}\n" http://127.0.0.1:8000/api/v1/openapi.json
