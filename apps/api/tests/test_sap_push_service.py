from __future__ import annotations

import hashlib
import hmac
import json

from smart_badge_api.core.config import get_settings
from smart_badge_api.sap_push_service import (
    _build_update_retry_payload,
    build_sap_gateway_request,
    summarize_sap_push_log_result,
)


def test_build_sap_gateway_request_wraps_and_signs_payload(monkeypatch) -> None:
    monkeypatch.setenv("SAP_RFC_APP_ID", "ai")
    monkeypatch.setenv("SAP_RFC_SECRET", "secret-key")
    get_settings.cache_clear()

    try:
        payload = {
            "text": "hello",
            "user": "u001",
            "zxxx": {"JGBM": "6101"},
            "TAB_SYZ": [{"CCKS": "Y3", "CCSYZ": "SYZ3020", "CCBW": "BW3001"}],
        }
        body = build_sap_gateway_request(payload, timestamp=1234567890)

        assert body["appId"] == "ai"
        assert body["timestamp"] == 1234567890

        data = json.loads(body["data"])
        assert data["functionName"] == "ZMC_FM_INT_YMC_SET"
        assert data["imType"] == "YMC_2013"
        assert data["resultField"] == "RE_DATA"
        assert json.loads(data["imData"]) == payload

        expected_signature = hmac.new(
            b"secret-key",
            f"ai1234567890{body['data']}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert body["signature"] == expected_signature
    finally:
        get_settings.cache_clear()


def test_summarize_sap_push_log_result_parses_embedded_business_error_from_msg() -> None:
    log = {
        "status": "succeeded",
        "response_items": [
            {
                "http_status_code": 200,
                "response_body": {
                    "code": 200,
                    "msg": json.dumps({"STATU": "E", "REMSG": "仅能操作当天的数据!"}, ensure_ascii=False),
                },
            }
        ],
    }

    summary = summarize_sap_push_log_result(log)

    assert summary["effective_status"] == "failed"
    assert summary["effective_business_status"] == "E"
    assert summary["effective_reason"] == "仅能操作当天的数据!"


def test_summarize_sap_push_log_result_parses_embedded_business_error_from_data() -> None:
    log = {
        "status": "succeeded",
        "response_items": [
            {
                "http_status_code": 200,
                "response_body": {
                    "code": 200,
                    "msg": "ok",
                    "data": json.dumps(
                        {
                            "STATU": "E",
                            "REMSG": "分诊单【2118232697-110】已有咨询单【3121092001】，不能再创建！",
                        },
                        ensure_ascii=False,
                    ),
                },
            }
        ],
    }

    summary = summarize_sap_push_log_result(log)

    assert summary["effective_status"] == "failed"
    assert summary["effective_business_status"] == "E"
    assert "已有咨询单" in str(summary["effective_reason"])


def test_summarize_sap_push_log_result_treats_update_retry_success_as_success() -> None:
    log = {
        "status": "failed",
        "response_items": [
            {
                "request_index": 1,
                "attempt": 1,
                "payload_mode": "C",
                "http_status_code": 200,
                "response_body": {
                    "code": 200,
                    "data": json.dumps(
                        {
                            "STATU": "E",
                            "REMSG": "分诊单【2118232697-110】已有咨询单【3121092001】，不能再创建！",
                        },
                        ensure_ascii=False,
                    ),
                },
            },
            {
                "request_index": 1,
                "attempt": 2,
                "payload_mode": "U",
                "http_status_code": 200,
                "response_body": {
                    "code": 200,
                    "data": json.dumps(
                        {
                            "STATU": "S",
                            "REMSG": "操作成功",
                            "ZXDH": "3121092001",
                        },
                        ensure_ascii=False,
                    ),
                },
            },
        ],
    }

    summary = summarize_sap_push_log_result(log)

    assert summary["effective_status"] == "succeeded"
    assert summary["effective_business_status"] == "S"
    assert summary["effective_reason"] == "操作成功"


def test_build_update_retry_payload_switches_to_update_mode() -> None:
    payload = {
        "text": "hello",
        "user": "u001",
        "zxxx": {
            "mode": "C",
            "zxdh": "",
            "fzdh": "2118232697-110",
        },
        "TAB_SYZ": [],
    }

    retried = _build_update_retry_payload(payload, "3121092001")

    assert retried["zxxx"]["mode"] == "U"
    assert retried["zxxx"]["zxdh"] == "3121092001"
    assert payload["zxxx"]["mode"] == "C"
