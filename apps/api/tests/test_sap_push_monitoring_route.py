from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.deps import get_db
from smart_badge_api.api.routes.sap_push_monitoring import router
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import Recording, SapPushLog, VisitOrder


def _build_test_client(session_factory) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        async with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_sap_push_monitoring_splits_each_target_visit_order() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                db.add(
                    Recording(
                        id="rec000000001",
                        file_name="dingtalk_demo_recording.mp3",
                        file_path="/tmp/dingtalk_demo_recording.mp3",
                        status="analyzed",
                        created_at=datetime(2026, 4, 22, 9, 16, 10, tzinfo=timezone.utc),
                    )
                )
                db.add_all(
                    [
                        VisitOrder(
                            id="vo0000000001",
                            dzdh="2118246305",
                            dzseg="110",
                            ninam="衡馨",
                            kunr="72279724",
                            advxc_long="李文军",
                        ),
                        VisitOrder(
                            id="vo0000000002",
                            dzdh="2118246304",
                            dzseg="110",
                            ninam="衡千",
                            kunr="72279722",
                            advxc_long="李文军",
                        ),
                        SapPushLog(
                            id="saplog000001",
                            recording_id="rec000000001",
                            visit_order_no="2118246305",
                            visit_order_seg="110",
                            customer_name="衡馨",
                            customer_code="72279724",
                            advisor_name="李文军",
                            trigger_mode="auto_bind",
                            status="failed",
                            send_enabled=True,
                            request_payloads=[
                                {
                                    "zxxx": {
                                        "fzdh": "2118246305-110",
                                        "kunr": "72279724",
                                        "mode": "C",
                                    }
                                },
                                {
                                    "zxxx": {
                                        "fzdh": "2118246304-110",
                                        "kunr": "72279722",
                                        "mode": "C",
                                    }
                                },
                            ],
                            response_items=[
                                {
                                    "request_index": 1,
                                    "success": False,
                                    "http_status_code": 200,
                                    "gateway_code": 500,
                                    "business_status": None,
                                    "business_message": 'CNTL_ERROR on 2118246305-110',
                                    "response_body": {
                                        "code": 500,
                                        "msg": 'CNTL_ERROR on 2118246305-110',
                                        "data": None,
                                    },
                                },
                                {
                                    "request_index": 2,
                                    "success": True,
                                    "http_status_code": 200,
                                    "gateway_code": 200,
                                    "business_status": "S",
                                    "business_message": "咨询单维护成功！",
                                    "response_body": {
                                        "code": 200,
                                        "msg": '{"STATU":"S","REMSG":"咨询单维护成功！"}',
                                        "data": None,
                                    },
                                },
                            ],
                            sent_at=datetime(2026, 4, 22, 11, 13, 17, tzinfo=timezone.utc),
                            created_at=datetime(2026, 4, 22, 11, 13, 16, tzinfo=timezone.utc),
                            updated_at=datetime(2026, 4, 22, 11, 13, 17, tzinfo=timezone.utc),
                        ),
                    ]
                )
                await db.commit()

            with _build_test_client(session_factory) as client:
                overview_response = client.get("/api/v1/sap-push-monitoring/overview")
                assert overview_response.status_code == 200
                overview = overview_response.json()
                assert overview["total_count"] == 2
                assert overview["succeeded_count"] == 1
                assert overview["failed_count"] == 1
                assert overview["auto_count"] == 2

                logs_response = client.get("/api/v1/sap-push-monitoring/logs?page=1&page_size=10")
                assert logs_response.status_code == 200
                payload = logs_response.json()
                assert payload["total"] == 2
                assert len(payload["items"]) == 2

                by_visit_order_ref = {
                    f"{item['visit_order_no']}-{item['visit_order_seg']}": item
                    for item in payload["items"]
                }
                primary_row = by_visit_order_ref["2118246305-110"]
                secondary_row = by_visit_order_ref["2118246304-110"]

                assert primary_row["log_id"] == "saplog000001"
                assert primary_row["is_primary_target"] is True
                assert primary_row["result_status"] == "failed"
                assert primary_row["customer_name"] == "衡馨"
                assert "CNTL_ERROR" in str(primary_row["result_reason"])

                assert secondary_row["log_id"] == "saplog000001"
                assert secondary_row["is_primary_target"] is False
                assert secondary_row["result_status"] == "succeeded"
                assert secondary_row["customer_name"] == "衡千"
                assert secondary_row["result_reason"] == "咨询单维护成功！"

                keyword_response = client.get("/api/v1/sap-push-monitoring/logs?keyword=2118246304&page=1&page_size=10")
                assert keyword_response.status_code == 200
                keyword_payload = keyword_response.json()
                assert keyword_payload["total"] == 1
                assert keyword_payload["items"][0]["visit_order_no"] == "2118246304"
                assert keyword_payload["items"][0]["is_primary_target"] is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())
