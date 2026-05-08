#!/bin/bash
docker exec -i smart-badge-postgres psql -U postgres -d smart_badge <<'SQL'
\echo === sample WeCom staff candidates ===
select s.id, s.name, s.permission_role, s.hospital_code,
       (select count(*) from visits v where v.consultant_id=s.id or v.doctor_id=s.id) as as_consultant_or_doctor,
       (select count(*) from recordings r where r.staff_id=s.id) as as_recorder
  from staff s
 where s.is_active=true
   and (s.wecom_user_id is not null or exists (select 1 from users u where u.staff_id=s.id))
 order by as_recorder desc nulls last, as_consultant_or_doctor desc nulls last
 limit 10;
SQL
