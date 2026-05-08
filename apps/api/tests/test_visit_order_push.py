from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.deps import get_db
from smart_badge_api.api.routes.visit_order_push import require_sap_hana_push_api_key
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import AuditLog, SapHanaVisitOrder, VisitOrder
from smart_badge_api.schemas.visit_order_push import SapHanaVisitOrderPushIn
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
            assert "X-API-Key 无效" in str(exc)
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
                assert "接收成功" in body["MSG"]

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
                    "MSG": "X-API-Key 无效",
                }

                invalid_payload_response = client.post(
                    "/api/v1/visit-orders/push",
                    json={"JGBM": "6101"},
                    headers={"X-API-Key": "hana-test-key"},
                )
                assert invalid_payload_response.status_code == 400
                body = invalid_payload_response.json()
                assert body["STATE"] == "E"
                assert "请求参数校验失败" in body["MSG"]
        finally:
            _clear_push_api_key()
            await engine.dispose()

    asyncio.run(scenario())
