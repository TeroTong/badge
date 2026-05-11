from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import select

from smart_badge_api.db.models import SapHanaVisitOrder, Staff, User, WecomTenant
from smart_badge_api.db.session import _session_factory
from smart_badge_api.schemas.visit_order_push import SapHanaVisitOrderPushIn
from smart_badge_api.visit_order_notifications import (
    _build_card_description,
    _build_card_url,
    _build_card_horizontal_items,
    _build_card_title,
    _extract_candidates,
    _load_advisor_staff,
    _resolve_tenant_for_hospital,
    build_recording_action_key,
    build_recording_card_task_id,
)
from smart_badge_api.wecom import WecomApiError, send_wecom_button_interaction_card, send_wecom_textcard_message
from smart_badge_api.wecom_tenants import resolve_wecom_tenant_config


async def _load_super_admin_staff(db):
    return (
        await db.execute(
            select(Staff)
            .join(User, User.staff_id == Staff.id)
            .where(
                User.role == "super_admin",
                User.is_active.is_(True),
                Staff.wecom_user_id.is_not(None),
                Staff.wecom_user_id != "",
            )
            .order_by(User.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _load_sample_candidate(db):
    rows = (
        await db.execute(
            select(SapHanaVisitOrder)
            .where(SapHanaVisitOrder.source_payload.is_not(None))
            .order_by(SapHanaVisitOrder.last_received_at.desc())
            .limit(100)
        )
    ).scalars().all()
    for row in rows:
        try:
            payload = SapHanaVisitOrderPushIn.model_validate(row.source_payload)
        except Exception:
            continue
        candidates = _extract_candidates([payload])
        if candidates:
            return row, candidates[0]
    return None, None


async def _load_tenant_candidates(db, hospital_code: str):
    result = []
    seen = set()

    try:
        tenant = await _resolve_tenant_for_hospital(db, hospital_code)
        key = (tenant.corp_id, tenant.agent_id)
        result.append(tenant)
        seen.add(key)
    except Exception:
        pass

    tenant_ids = (
        await db.execute(
            select(WecomTenant.id)
            .where(
                WecomTenant.is_active.is_(True),
                WecomTenant.corp_id.is_not(None),
                WecomTenant.corp_id != "",
                WecomTenant.agent_id.is_not(None),
                WecomTenant.agent_id != "",
                WecomTenant.agent_secret.is_not(None),
                WecomTenant.agent_secret != "",
                WecomTenant.frontend_url.is_not(None),
                WecomTenant.frontend_url != "",
            )
            .order_by(WecomTenant.is_default.desc(), WecomTenant.updated_at.desc())
        )
    ).scalars().all()
    for tenant_id in tenant_ids:
        try:
            tenant = await resolve_wecom_tenant_config(db, tenant_id=tenant_id)
        except Exception:
            continue
        key = (tenant.corp_id, tenant.agent_id)
        if key in seen:
            continue
        result.append(tenant)
        seen.add(key)

    return result


async def main() -> None:
    async with _session_factory() as db:
        target_staff = await _load_super_admin_staff(db)
        if target_staff is None:
            print(json.dumps({"status": "failed", "reason": "no_super_admin_wecom_user"}, ensure_ascii=False))
            return

        row, candidate = await _load_sample_candidate(db)
        if row is None or candidate is None:
            print(json.dumps({"status": "failed", "reason": "no_visit_order_candidate"}, ensure_ascii=False))
            return

        advisor_staff = await _load_advisor_staff(db, candidate) or target_staff
        tenants = await _load_tenant_candidates(db, candidate.hospital_code)
        if not tenants:
            print(json.dumps({"status": "failed", "reason": "no_wecom_tenant"}, ensure_ascii=False))
            return

        attempts = 0
        last_error = None
        for tenant in tenants:
            attempts += 1
            pushed_at = datetime.now(timezone.utc)
            try:
                try:
                    await send_wecom_button_interaction_card(
                        to_user=target_staff.wecom_user_id,
                        title=f"测试：{_build_card_title(candidate)}",
                        description=_build_card_description(candidate, advisor_staff, pushed_at=pushed_at),
                        main_title_desc=None,
                        horizontal_content_list=_build_card_horizontal_items(candidate, pushed_at=pushed_at),
                        task_id=build_recording_card_task_id(
                            "sample",
                            action=f"{attempts}_{int(pushed_at.timestamp())}",
                        ),
                        buttons=[
                            {
                                "text": "开始录音",
                                "key": build_recording_action_key("start", "sample"),
                                "style": 1,
                            }
                        ],
                        tenant=tenant,
                    )
                    print(json.dumps({"status": "sent", "attempts": attempts}, ensure_ascii=False))
                    return
                except WecomApiError as exc:
                    if exc.errcode != 43012:
                        raise
                    await send_wecom_textcard_message(
                        to_user=target_staff.wecom_user_id,
                        title=f"测试：{_build_card_title(candidate)}",
                        description=_build_card_description(candidate, advisor_staff, pushed_at=pushed_at).replace("\n", "<br/>"),
                        url=_build_card_url(frontend_url=tenant.frontend_url, visit_order_no=candidate.visit_order_no),
                        btn_text="开始录音",
                        tenant=tenant,
                    )
                    print(
                        json.dumps(
                            {"status": "fallback_sent", "attempts": attempts, "reason": str(exc)},
                            ensure_ascii=False,
                        )
                    )
                return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue

        print(
            json.dumps(
                {"status": "failed", "reason": "send_failed", "attempts": attempts, "last_error": last_error},
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
