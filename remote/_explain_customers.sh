#!/bin/bash
# EXPLAIN ANALYZE the actual customer list query for a typical staff scope.
docker exec -i smart-badge-postgres psql -U postgres -d smart_badge <<'SQL'
\timing on
\echo === count customers visible to staff 0cb7d5bff6fb ===
explain (analyze, buffers, summary on)
select count(*) from customers c
 where exists (select 1 from visits v
                where v.customer_id = c.id
                  and (v.consultant_id = '0cb7d5bff6fb'
                       or v.doctor_id = '0cb7d5bff6fb'
                       or exists (select 1 from recordings r where r.visit_id=v.id and r.staff_id='0cb7d5bff6fb')
                       or exists (select 1 from recording_visit_links rvl join recordings r2 on r2.id=rvl.recording_id where rvl.visit_id=v.id and r2.staff_id='0cb7d5bff6fb')
                       or exists (select 1 from visit_orders vo, staff s
                                   where s.id='0cb7d5bff6fb' and s.external_account is not null
                                     and v.external_visit_order_no is not null
                                     and vo.dzdh = v.external_visit_order_no
                                     and s.hospital_code is not null
                                     and vo.jgbm = s.hospital_code
                                     and (s.external_account = vo.fzuer or s.external_account = vo.d_fzuer
                                          or s.external_account = vo.fzr_id_dq or s.external_account = vo.advxc
                                          or s.external_account = vo.assxc or s.external_account = vo.advyq
                                          or s.external_account = vo.yyuer or s.external_account = vo.vipkf
                                          or s.external_account = vo.d_vipkf))));
SQL
