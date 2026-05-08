from __future__ import annotations

import asyncio
import csv
import json
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from smart_badge_api.api.routes.asr_monitoring import router
from smart_badge_api.core.config import get_settings


def _build_test_client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_asr_monitoring_overview_and_requests(monkeypatch) -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="asr-monitoring-test-") as temp_dir:
            tmp_path = Path(temp_dir)
            request_log_path = tmp_path / "tencent_asr_requests.jsonl"
            cloud_audit_log_path = tmp_path / "cloud_audit.csv"

            request_log_path.write_text(
                json.dumps(
                    {
                        "id": "local-1",
                        "source": "local_audit",
                        "action": "CreateRecTask",
                        "occurred_at": "2026-04-17T10:00:00+00:00",
                        "status": "completed",
                        "audio_name": "0417_180000.mp3",
                        "audio_path": "/tmp/0417_180000.mp3",
                        "source_id": "recording-1",
                        "chunk_index": 1,
                        "chunk_count": 1,
                        "submitted_duration_ms": 62000,
                        "recognized_duration_ms": 61800,
                        "file_size_bytes": 123456,
                        "request_id": "req-local-1",
                        "task_id": 123,
                        "error_code": None,
                        "error_message": None,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            with cloud_audit_log_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "AccountID",
                        "CloudAuditEvent",
                        "ErrorCode",
                        "EventName",
                        "EventTime",
                        "RequestID",
                        "Resources",
                        "SecretId",
                        "SourceIPAddress",
                        "Username",
                        "eventRegion",
                        "eventSource",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "AccountID": "100046239814",
                        "CloudAuditEvent": json.dumps(
                            {
                                "eventTime": 1776394439,
                                "requestID": "req-cloud-1",
                                "apiErrorMessage": "FailedOperation.UserHasNoAmount",
                                "sourceIPAddress": "198.2.210.236",
                            },
                            ensure_ascii=False,
                        ),
                        "ErrorCode": "0",
                        "EventName": "CreateRecTask",
                        "EventTime": "2026-04-17 10:53:59",
                        "RequestID": "req-cloud-1",
                        "Resources": "{}",
                        "SecretId": "AKID-demo",
                        "SourceIPAddress": "198.2.210.236",
                        "Username": "root",
                        "eventRegion": "ap-shanghai",
                        "eventSource": "asr.tencentcloudapi.com",
                    }
                )

            monkeypatch.setenv("TENCENT_ASR_REQUEST_AUDIT_LOG_PATH", str(request_log_path))
            monkeypatch.setenv("TENCENT_ASR_CLOUD_AUDIT_LOG_PATH", str(cloud_audit_log_path))
            monkeypatch.setenv("TENCENT_ASR_SECRET_ID", "secret-id")
            monkeypatch.setenv("TENCENT_ASR_SECRET_KEY", "secret-key")
            get_settings.cache_clear()

            async def fake_usage(*, start_date, end_date, biz_name_list=None):
                return {"asr_rec": {"count": 3, "duration": 180}}

            async def fake_packages():
                return {
                    "total_seconds": 1332000,
                    "remaining_seconds": 0,
                    "used_seconds": 1332000,
                    "package_count": 6,
                    "active_package_count": 0,
                    "exhausted_package_count": 6,
                    "packages": [
                        {
                            "name": "录音文件识别预付费包 60小时",
                            "fee_mode": False,
                            "total_seconds": 216000,
                            "remaining_seconds": 0,
                            "used_seconds": 216000,
                            "effective_time": "2026-04-10 18:26:09",
                            "expiry_time": "2027-04-10 23:59:59",
                            "pid": 1001199,
                            "unit": "demo-unit",
                            "sub_product_code": "sp_asr_file_prepay",
                            "available_type": 2,
                        }
                    ],
                }

            monkeypatch.setattr(
                "smart_badge_api.api.routes.asr_monitoring.get_usage_totals_by_date_range",
                fake_usage,
            )
            monkeypatch.setattr(
                "smart_badge_api.api.routes.asr_monitoring.get_file_recognition_resource_packages",
                fake_packages,
            )

            try:
                with _build_test_client() as client:
                    overview_response = client.get("/api/v1/asr-monitoring/overview")
                    assert overview_response.status_code == 200
                    overview = overview_response.json()
                    assert overview["provider"] in {"mock", "tencent_asr", "whisper", "sensevoice_3dspeaker", "high_precision_3dspeaker"}
                    assert overview["has_tencent_credentials"] is True
                    assert overview["quota_state"] == "exhausted"
                    assert overview["local_exact_count"] == 1
                    assert overview["cloud_total_count"] == 1
                    assert len(overview["usage_ranges"]) == 3
                    assert overview["usage_ranges"][0]["duration_seconds"] == 180
                    assert overview["quota_total_seconds"] == 1332000
                    assert overview["quota_remaining_seconds"] == 0
                    assert overview["quota_package_count"] == 6
                    assert len(overview["quota_packages"]) == 1

                    requests_response = client.get("/api/v1/asr-monitoring/requests?page=1&page_size=10")
                    assert requests_response.status_code == 200
                    payload = requests_response.json()
                    assert payload["total"] == 2
                    by_request_id = {item["request_id"]: item for item in payload["items"]}
                    assert by_request_id["req-cloud-1"]["source"] == "cloud_audit"
                    assert by_request_id["req-cloud-1"]["status"] == "submit_failed"
                    assert by_request_id["req-local-1"]["source"] == "local_audit"
                    assert by_request_id["req-local-1"]["submitted_duration_ms"] == 62000
            finally:
                get_settings.cache_clear()

    asyncio.run(scenario())
