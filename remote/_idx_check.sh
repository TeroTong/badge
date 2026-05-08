#!/bin/bash
docker exec -i smart-badge-postgres psql -U postgres -d smart_badge <<'SQL'
\echo === current indexes ===
select tablename, indexname from pg_indexes
 where schemaname='public'
   and tablename in ('recordings','analysis_tasks','sap_push_logs','visits','customers','transcripts','segments','recording_visit_links')
 order by tablename, indexname;
\echo
\echo === row counts ===
select 'recordings' tbl, count(*) ct from recordings
 union all select 'analysis_tasks', count(*) from analysis_tasks
 union all select 'sap_push_logs', count(*) from sap_push_logs
 union all select 'visits', count(*) from visits
 union all select 'customers', count(*) from customers
 union all select 'transcripts', count(*) from transcripts
 union all select 'segments', count(*) from segments;
SQL
