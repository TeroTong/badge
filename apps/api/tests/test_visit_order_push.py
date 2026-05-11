from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.deps import get_db
from smart_badge_api.api.routes.wecom_callback import _load_card_visit_context
from smart_badge_api.api.routes.visit_order_push import require_sap_hana_push_api_key
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import AuditLog, SapHanaVisitOrder, Staff, User, VisitOrder, VisitOrderAdvisorNotification, WecomTenant
from smart_badge_api.schemas.visit_order_push import SapHanaVisitOrderPushIn
from smart_badge_api.visit_order_notifications import _extract_candidates, notify_pushed_visit_order_advisors
from smart_badge_api.visit_order_push_service import (
    _build_sap_hana_visit_order_values,
    upsert_sap_hana_visit_orders,
)


def _set_push_api_key(value: str = "hana-test-key") -> None:
    os.environ["SAP_HANA_PUSH_API_KEY"] = value
    get_settings.cache_clear()


def _clear_push_api_key() -> None:
    os.environ.pop("SAP_HANA_PUSH_API_KEY", None)
    get_settings.cache_clear()


def _build_test_client(session_factory):
    app = FastAPI()
    from smart_badge_api.api.routes.visit_order_push import router

    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        async with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_require_sap_hana_push_api_key_rejects_invalid_key() -> None:
    _set_push_api_key()
    try:
        try:
            require_sap_hana_push_api_key("wrong-key")
        except Exception as exc:
            assert "invalid X-API-Key" in str(exc)
        else:
            raise AssertionError("invalid API key should be rejected")
    finally:
        _clear_push_api_key()


def test_build_sap_hana_visit_order_values_keeps_raw_payload_and_fzdata() -> None:
    payload = SapHanaVisitOrderPushIn(
        JGBM="6101",
        DZDH="DZ1001",
        CRTDT="20260415",
        CRTTM="093015",
        DZSTA="C",
        KUNR="70000088",
        NINAM="王女士",
        KUSEX="F",
        KUBSD="1990-05-06",
        FZUER="81034062",
        EXTRA_FIELD="extended",
        FZDATA=[
            {
                "FZDH": "FZ001",
                "ADVXC": "81034062",
                "ADVXC_LONG": "杜娟",
                "ASSXC": "81030001",
                "FZSJ": "094500",
                "FZSTA": "1",
                "DDSC": "20",
                "JCSTA": "N",
                "EXTRA_CHILD": "child-value",
            }
        ],
    )

    values = _build_sap_hana_visit_order_values(payload)

    assert values["jgbm"] == "6101"
    assert values["dzdh"] == "DZ1001"
    assert values["fzdata"][0]["FZDH"] == "FZ001"
    assert values["fzdata"][0]["EXTRA_CHILD"] == "child-value"
    assert values["source_payload"]["EXTRA_FIELD"] == "extended"
    assert values["source_payload"]["KUBSD"] == "1990-05-06"
    assert values["customer_birthday"] == "1990-05-06"


def test_upsert_sap_hana_visit_orders_creates_and_updates_snapshot_rows() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                first = SapHanaVisitOrderPushIn(
                    JGBM="6101",
                    DZDH="DZ1001",
                    CRTDT="20260415",
                    CRTTM="093015",
                    DZSTA="C",
                    KUNR="70000088",
                    NINAM="王女士",
                    KUSEX="F",
                    FZUER="81034062",
                    FZDATA=[
                        {
                            "FZDH": "FZ001",
                            "ADVXC": "81034062",
                            "ADVXC_LONG": "杜娟",
                            "ASSXC": "81030001",
                            "FZSJ": "094500",
                            "FZSTA": "1",
                            "DDSC": "20",
                            "JCSTA": "N",
                        }
                    ],
                )
                created = await upsert_sap_hana_visit_orders(db, [first])
                assert created.received_count == 1
                assert created.created_count == 1
                assert created.updated_count == 0

                stored = (
                    await db.execute(
                        select(SapHanaVisitOrder).where(
                            SapHanaVisitOrder.jgbm == "6101",
                            SapHanaVisitOrder.dzdh == "DZ1001",
                        )
                    )
                ).scalars().all()
                assert len(stored) == 1
                assert stored[0].fzdata[0]["FZDH"] == "FZ001"
                assert stored[0].source_payload["DZDH"] == "DZ1001"

                updated = SapHanaVisitOrderPushIn(
                    JGBM="6101",
                    DZDH="DZ1001",
                    CRTDT="20260415",
                    CRTTM="093015",
                    DZSTA="D",
                    KUNR="70000088",
                    NINAM="王女士",
                    KUSEX="F",
                    FZUER="81034062",
                    FZDATA=[
                        {
                            "FZDH": "FZ002",
                            "ADVXC": "81034062",
                            "ADVXC_LONG": "杜娟",
                            "ASSXC": "81030001",
                            "FZSJ": "101500",
                            "FZSTA": "A",
                            "DDSC": "5",
                            "JCSTA": "Y",
                        }
                    ],
                )
                result = await upsert_sap_hana_visit_orders(db, [updated])
                assert result.received_count == 1
                assert result.created_count == 0
                assert result.updated_count == 1

                refreshed = (
                    await db.execute(
                        select(SapHanaVisitOrder).where(
                            SapHanaVisitOrder.jgbm == "6101",
                            SapHanaVisitOrder.dzdh == "DZ1001",
                        )
                    )
                ).scalars().all()
                assert len(refreshed) == 1
                assert refreshed[0].dzsta == "D"
                assert refreshed[0].fzdata[0]["FZDH"] == "FZ002"

                compatible_rows = (
                    await db.execute(select(VisitOrder).where(VisitOrder.dzdh == "DZ1001"))
                ).scalars().all()
                assert compatible_rows == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_visit_order_notification_arrival_purpose_falls_back_to_dymd_code() -> None:
    payload = SapHanaVisitOrderPushIn(
        JGBM="6101",
        DZDH="DZ1003",
        CRTDT="20260509",
        CRTTM="101010",
        KUNR="70000123",
        NINAM="Customer A",
        KUT30_DQ="V",
        DYMD="A",
        FZDATA=[
            {
                "FZDH": "FZ1003-001",
                "ADVXC": "81034062",
                "ADVXC_LONG": "Advisor A",
                "FZSJ": "102030",
            }
        ],
    )

    candidates = _extract_candidates([payload])

    assert len(candidates) == 1
    assert candidates[0].arrival_purpose == "咨询"


def test_notify_pushed_visit_order_advisors_sends_wecom_card_once() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    id="staff_001",
                    name="Advisor A",
                    external_account="81034062",
                    wecom_user_id="advisor_a",
                    wecom_corp_id="ww6101",
                    hospital_code="6101",
                    hospital_short_name="Test Hospital",
                    permission_role="staff",
                    is_active=True,
                )
                user = User(
                    username="81034062",
                    hashed_password="hashed",
                    display_name="Advisor A",
                    staff_id=staff.id,
                    role="staff",
                    hospital_code="6101",
                    hospital_name="Test Hospital",
                    is_active=True,
                )
                tenant = WecomTenant(
                    name="Test Hospital",
                    host="wx.example.com",
                    corp_id="ww6101",
                    agent_id="1000001",
                    agent_secret="secret",
                    frontend_url="https://wx.example.com",
                    default_hospital_code="6101",
                    default_hospital_name="Test Hospital",
                    is_active=True,
                    is_default=True,
                )
                db.add_all([staff, user, tenant])
                await db.commit()

                payload = SapHanaVisitOrderPushIn(
                    JGBM="6101",
                    DZDH="DZ1003",
                    CRTDT="20260509",
                    CRTTM="101010",
                    KUNR="70000123",
                    NINAM="Customer A",
                    KUT30_DQ="V",
                    DYMD="A",
                    DYMD_TXT="Consultation",
                    FZDATA=[
                        {
                            "FZDH": "FZ1003-001",
                            "ADVXC": "81034062",
                            "ADVXC_LONG": "Advisor A",
                            "FZSJ": "102030",
                        }
                    ],
                )

                with patch(
                    "smart_badge_api.visit_order_notifications.send_wecom_button_interaction_card",
                    new=AsyncMock(return_value={"errcode": 0, "response_code": "resp-001"}),
                ) as send_mock:
                    sent = await notify_pushed_visit_order_advisors(db, [payload])
                    sent_again = await notify_pushed_visit_order_advisors(db, [payload])

                assert sent == 1
                assert sent_again == 0
                assert send_mock.await_count == 1
                _, kwargs = send_mock.await_args
                assert kwargs["to_user"] == "advisor_a"
                assert kwargs["title"] == "Customer A（70000123）｜老客"
                assert "｜" in kwargs["title"]
                assert kwargs["main_title_desc"] is None
                horizontal_items = kwargs["horizontal_content_list"]
                assert [item["keyname"] for item in horizontal_items] == [
                    "客户姓名",
                    "客户编号",
                    "新老客标识",
                    "到诊单号",
                    "到院目的",
                    "卡片推送时间",
                ]
                assert horizontal_items[0]["value"] == "Customer A"
                assert horizontal_items[1]["value"] == "70000123"
                assert horizontal_items[2]["value"] == "老客"
                assert horizontal_items[3]["value"] == "DZ1003-001"
                assert horizontal_items[4]["value"] == "Consultation"
                assert len(horizontal_items[5]["value"]) == 19
                assert "DZ1003-001" not in kwargs["description"]
                assert "Consultation" not in kwargs["description"]
                assert "Customer A" not in kwargs["description"]
                assert "请确认工牌在线后点击开始录音" in kwargs["description"]
                assert "录音完成上传后，系统会自动关联该到诊单" in kwargs["description"]
                assert kwargs["task_id"].startswith("vor_")
                assert kwargs["buttons"][0]["text"] == "开始录音"
                assert kwargs["buttons"][0]["key"].startswith("visit_order_recording__start__")

                logs = (await db.execute(select(VisitOrderAdvisorNotification))).scalars().all()
                assert len(logs) == 1
                assert logs[0].status == "sent"
                assert logs[0].advisor_staff_id == staff.id
                assert logs[0].visit_order_no == "DZ1003"
                assert logs[0].wecom_task_id.startswith("vor_")
                assert logs[0].wecom_response_code == "resp-001"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_load_card_visit_context_keeps_required_fields_for_card_updates() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                order = VisitOrder(
                    dzdh="DZ1004",
                    dzseg="002",
                    sjrq="20260510",
                    jgbm="6101",
                    kunr="70000124",
                    ninam="Customer B",
                    kut30_dq="Q",
                    dymd="A",
                    dymd_txt="Consultation",
                )
                log = VisitOrderAdvisorNotification(
                    hospital_code="6101",
                    visit_order_no="DZ1004",
                    visit_order_seg="002",
                    advisor_name="Advisor B",
                    wecom_user_id="advisor_b",
                    customer_code="70000124",
                    customer_name="Customer B",
                    status="sent",
                    sent_at=datetime(2026, 5, 10, 3, 4, 5, tzinfo=timezone.utc),
                )
                db.add_all([order, log])
                await db.commit()
                await db.refresh(log)

                title, subtitle, horizontal_items = await _load_card_visit_context(db, log.id)

                assert title == "Customer B｜新客"
                assert subtitle == "到诊单：DZ1004-002"
                assert [item["keyname"] for item in horizontal_items] == [
                    "客户姓名",
                    "客户编号",
                    "新老客标识",
                    "到诊单号",
                    "到院目的",
                    "卡片推送时间",
                ]
                field_values = {str(item["keyname"]): str(item["value"]) for item in horizontal_items}
                assert field_values["客户姓名"] == "Customer B"
                assert field_values["客户编号"] == "70000124"
                assert field_values["新老客标识"] == "新客"
                assert field_values["到诊单号"] == "DZ1004-002"
                assert field_values["到院目的"] == "Consultation"
                assert field_values["卡片推送时间"] == "2026-05-10 11:04:05"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_notify_pushed_visit_order_advisors_skips_non_arrival_purposes() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    id="staff_001",
                    name="Advisor A",
                    external_account="81034062",
                    wecom_user_id="advisor_a",
                    wecom_corp_id="ww6101",
                    hospital_code="6101",
                    hospital_short_name="Test Hospital",
                    permission_role="staff",
                    is_active=True,
                )
                user = User(
                    username="81034062",
                    hashed_password="hashed",
                    display_name="Advisor A",
                    staff_id=staff.id,
                    role="staff",
                    hospital_code="6101",
                    hospital_name="Test Hospital",
                    is_active=True,
                )
                tenant = WecomTenant(
                    name="Test Hospital",
                    host="wx.example.com",
                    corp_id="ww6101",
                    agent_id="1000001",
                    agent_secret="secret",
                    frontend_url="https://wx.example.com",
                    default_hospital_code="6101",
                    default_hospital_name="Test Hospital",
                    is_active=True,
                    is_default=True,
                )
                db.add_all([staff, user, tenant])
                await db.commit()

                payloads = [
                    SapHanaVisitOrderPushIn(
                        JGBM="6101",
                        DZDH=f"DZ_SKIP_{dymd}",
                        CRTDT="20260509",
                        CRTTM="101010",
                        KUNR="70000123",
                        NINAM="Customer A",
                        KUT30_DQ="V",
                        DYMD=dymd,
                        FZDATA=[
                            {
                                "FZDH": f"FZ_SKIP_{dymd}-001",
                                "ADVXC": "81034062",
                                "ADVXC_LONG": "Advisor A",
                                "FZSJ": "102030",
                            }
                        ],
                    )
                    for dymd in ("X", "Z")
                ]

                with patch(
                    "smart_badge_api.visit_order_notifications.send_wecom_button_interaction_card",
                    new=AsyncMock(return_value={"errcode": 0, "response_code": "resp-001"}),
                ) as send_mock:
                    sent = await notify_pushed_visit_order_advisors(db, payloads)

                assert sent == 0
                assert send_mock.await_count == 0
                logs = (await db.execute(select(VisitOrderAdvisorNotification))).scalars().all()
                assert logs == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_push_visit_orders_from_sap_hana_returns_success_ack_without_writing_audit_log() -> None:
    async def scenario() -> None:
        _set_push_api_key()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            payload = {
                "JGBM": "6101",
                "DZDH": "DZ1002",
                "CRTDT": "20260415",
                "CRTTM": "103000",
                "DZSTA": "A",
                "KUNR": "70000099",
                "NINAM": "李女士",
                "KUSEX": "F",
                "FZUER": "81034062",
                "FZDATA": [
                    {
                        "FZDH": "FZ003",
                        "ADVXC": "81034062",
                        "ADVXC_LONG": "杜娟",
                        "ASSXC": "81030002",
                        "FZSJ": "104500",
                        "FZSTA": "A",
                        "DDSC": "10",
                        "JCSTA": "N",
                    }
                ],
            }
            with _build_test_client(session_factory) as client:
                response = client.post(
                    "/api/v1/visit-orders/push",
                    json=payload,
                    headers={"X-API-Key": "hana-test-key"},
                )
                assert response.status_code == 200
                body = response.json()
                assert body["STATE"] == "S"
                assert "received=1" in body["MSG"]

            async with session_factory() as db:
                rows = (
                    await db.execute(select(SapHanaVisitOrder).where(SapHanaVisitOrder.dzdh == "DZ1002"))
                ).scalars().all()
                assert len(rows) == 1
                assert rows[0].source_payload["DZDH"] == "DZ1002"
                compatible_rows = (
                    await db.execute(select(VisitOrder).where(VisitOrder.dzdh == "DZ1002"))
                ).scalars().all()
                assert compatible_rows == []
                logs = (await db.execute(select(AuditLog))).scalars().all()
                assert not any(log.action_name == "SAP HANA 推送到诊分诊单" for log in logs)
        finally:
            _clear_push_api_key()
            await engine.dispose()

    asyncio.run(scenario())


def test_push_visit_orders_from_sap_hana_http_returns_ack_for_invalid_key_and_payload() -> None:
    async def scenario() -> None:
        _set_push_api_key()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            with _build_test_client(session_factory) as client:
                invalid_key_response = client.post(
                    "/api/v1/visit-orders/push",
                    json={"JGBM": "6101", "DZDH": "DZ1009"},
                    headers={"X-API-Key": "wrong-key"},
                )
                assert invalid_key_response.status_code == 401
                assert invalid_key_response.json() == {
                    "STATE": "E",
                    "MSG": "invalid X-API-Key",
                }

                invalid_payload_response = client.post(
                    "/api/v1/visit-orders/push",
                    json={"JGBM": "6101"},
                    headers={"X-API-Key": "hana-test-key"},
                )
                assert invalid_payload_response.status_code == 400
                body = invalid_payload_response.json()
                assert body["STATE"] == "E"
                assert "request validation failed" in body["MSG"]
        finally:
            _clear_push_api_key()
            await engine.dispose()

    asyncio.run(scenario())
