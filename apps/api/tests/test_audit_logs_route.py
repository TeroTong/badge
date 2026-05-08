from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.deps import get_db
from smart_badge_api.api.routes.audit_logs import router
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import AuditLog


def _build_test_client(session_factory):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        async with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_list_audit_logs_excludes_sap_hana_push_records() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                db.add(
                    AuditLog(
                        operator_name="普通管理员",
                        ip_address="127.0.0.1",
                        module_name="人员管理",
                        action_name="编辑员工",
                        content="修改了员工机构信息",
                    )
                )
                db.add(
                    AuditLog(
                        operator_name="SAP HANA",
                        ip_address="10.0.0.8",
                        module_name="到诊单管理",
                        action_name="SAP HANA 推送到诊分诊单",
                        content="接收成功：1 条，新增 1 条，更新 0 条",
                    )
                )
                await db.commit()

            with _build_test_client(session_factory) as client:
                response = client.get("/api/v1/audit-logs")
                assert response.status_code == 200
                body = response.json()
                assert body["total"] == 1
                assert len(body["items"]) == 1
                assert body["items"][0]["operator_name"] == "普通管理员"
                assert body["items"][0]["action_name"] == "编辑员工"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
