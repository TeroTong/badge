#!/bin/bash
docker exec -i smart-badge-postgres psql -U postgres -d smart_badge <<'SQL'
\echo === recordings/visit_orders/staff_management indexes ===
select tablename, indexname, indexdef from pg_indexes
 where schemaname='public'
   and tablename in ('recordings','visit_orders','staff_management_relations','staff','customers','visits')
 order by tablename, indexname;
\echo
\echo === counts ===
select 'staff' tbl, count(*) ct from staff
 union all select 'staff_management_relations', count(*) from staff_management_relations
 union all select 'visit_orders', count(*) from visit_orders;
SQL
