from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from smart_badge_api.asr.xfyun_asr_provider import (
    _build_upload_query_params,
    parse_xfyun_order_result,
    transcribe_audio,
)
from smart_badge_api.core.config import get_settings


def test_build_upload_query_params_uses_configured_role_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = tmp_path / "consultation.mp3"
    audio_path.write_bytes(b"demo-audio")

    monkeypatch.setenv("ASR_RUNTIME_DIR", str(tmp_path / "asr_runtime"))
    monkeypatch.setenv("XFYUN_ASR_APP_ID", "appid-demo")
    monkeypatch.setenv("XFYUN_ASR_ACCESS_KEY_ID", "apikey-demo")
    monkeypatch.setenv("XFYUN_ASR_ACCESS_KEY_SECRET", "secret-demo")
    monkeypatch.setenv("XFYUN_ASR_LANGUAGE", "autodialect")
    monkeypatch.setenv("XFYUN_ASR_DOMAIN", "medical")
    monkeypatch.setenv("XFYUN_ASR_ROLE_TYPE", "1")
    monkeypatch.setenv("XFYUN_ASR_ROLE_NUM", "2")
    monkeypatch.setenv("XFYUN_ASR_DURATION_CHECK_DISABLE", "true")
    monkeypatch.setenv("XFYUN_ASR_ENG_SMOOTHPROC", "true")
    monkeypatch.setenv("XFYUN_ASR_ENG_COLLOQPROC", "false")
    get_settings.cache_clear()

    try:
        params, signature_random = _build_upload_query_params(audio_path)
    finally:
        get_settings.cache_clear()

    assert params["appId"] == "appid-demo"
    assert params["accessKeyId"] == "apikey-demo"
    assert params["fileName"] == "consultation.mp3"
    assert params["fileSize"] == str(len(b"demo-audio"))
    assert params["pd"] == "medical"
    assert params["roleType"] == "1"
    assert params["roleNum"] == "2"
    assert params["durationCheckDisable"] == "true"
    assert params["eng_smoothproc"] == "true"
    assert params["eng_colloqproc"] == "false"
    assert len(signature_random) >= 16


def test_parse_xfyun_order_result_builds_role_separated_utterances() -> None:
    order_result = json.dumps(
        {
            "lattice": [
                {
                    "json_1best": json.dumps(
                        {
                            "st": {
                                "bg": "0",
                                "ed": "1800",
                                "rl": "1",
                                "rt": [
                                    {
                                        "ws": [
                                            {"cw": [{"w": "您好", "wp": "n"}]},
                                            {"cw": [{"w": "。", "wp": "p"}]},
                                            {"cw": [{"w": "", "wp": "g"}]},
                                        ]
                                    }
                                ],
                            }
                        }
                    )
                },
                {
                    "json_1best": json.dumps(
                        {
                            "st": {
                                "bg": "2000",
                                "ed": "4200",
                                "rl": "2",
                                "rt": [
                                    {
                                        "ws": [
                                            {"cw": [{"w": "我想了解热玛吉", "wp": "n"}]},
                                            {"cw": [{"w": "。", "wp": "p"}]},
                                        ]
                                    }
                                ],
                            }
                        }
                    )
                },
            ]
        }
    )

    utterances, full_text, duration_ms = parse_xfyun_order_result(order_result, original_duration_ms=5000)

    assert [item["speaker_id"] for item in utterances] == ["speaker_1", "speaker_2"]
    assert [item["text"] for item in utterances] == ["您好。", "我想了解热玛吉。"]
    assert full_text == "您好。 我想了解热玛吉。"
    assert duration_ms == 4200


def test_transcribe_audio_returns_normalized_utterances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        audio_path = tmp_path / "demo.mp3"
        audio_path.write_bytes(b"demo")
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("ASR_RUNTIME_DIR", str(tmp_path / "asr_runtime"))
        get_settings.cache_clear()

        order_result = json.dumps(
            {
                "lattice": [
                    {
                        "json_1best": json.dumps(
                            {
                                "st": {
                                    "bg": "100",
                                    "ed": "1600",
                                    "rl": "1",
                                    "rt": [
                                        {"ws": [{"cw": [{"w": "欢迎光临", "wp": "n"}]}]},
                                    ],
                                }
                            }
                        )
                    }
                ]
            }
        )

        monkeypatch.setattr(
            "smart_badge_api.asr.xfyun_asr_provider._validate_runtime_prerequisites",
            lambda: None,
        )
        upload_mock = AsyncMock(return_value=("order-demo", "random-demo"))
        wait_mock = AsyncMock(
            return_value={
                "orderInfo": {
                    "orderId": "order-demo",
                    "status": 4,
                    "failType": 0,
                    "originalDuration": 1600,
                },
                "orderResult": order_result,
            }
        )
        monkeypatch.setattr("smart_badge_api.asr.xfyun_asr_provider._upload_audio", upload_mock)
        monkeypatch.setattr("smart_badge_api.asr.xfyun_asr_provider._wait_for_result", wait_mock)

        try:
            utterances, full_text, duration_ms = await transcribe_audio(audio_path)
        finally:
            get_settings.cache_clear()

        upload_mock.assert_awaited_once()
        wait_mock.assert_awaited_once_with("order-demo", "random-demo")
        assert utterances == [
            {
                "speaker": "speaker_1",
                "speaker_id": "speaker_1",
                "speaker_role_source": "xfyun_asr",
                "text": "欢迎光临",
                "begin_ms": 100,
                "end_ms": 1600,
            }
        ]
        assert full_text == "欢迎光临"
        assert duration_ms == 1600

    import asyncio

    asyncio.run(scenario())


def test_transcribe_audio_reuses_cache_without_reuploading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        audio_path = tmp_path / "demo.mp3"
        audio_path.write_bytes(b"demo")
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("ASR_RUNTIME_DIR", str(tmp_path / "asr_runtime"))
        monkeypatch.setenv("XFYUN_ASR_APP_ID", "appid-demo")
        monkeypatch.setenv("XFYUN_ASR_ACCESS_KEY_ID", "apikey-demo")
        monkeypatch.setenv("XFYUN_ASR_ACCESS_KEY_SECRET", "secret-demo")
        monkeypatch.setenv("XFYUN_ASR_ROLE_TYPE", "0")
        monkeypatch.setenv("XFYUN_ASR_ROLE_NUM", "0")
        get_settings.cache_clear()

        order_result = json.dumps(
            {
                "lattice": [
                    {
                        "json_1best": json.dumps(
                            {
                                "st": {
                                    "bg": "100",
                                    "ed": "1600",
                                    "rl": "1",
                                    "rt": [
                                        {"ws": [{"cw": [{"w": "欢迎光临", "wp": "n"}]}]},
                                    ],
                                }
                            }
                        )
                    }
                ]
            }
        )

        monkeypatch.setattr(
            "smart_badge_api.asr.xfyun_asr_provider._validate_runtime_prerequisites",
            lambda: None,
        )
        upload_mock = AsyncMock(return_value=("order-demo", "random-demo"))
        wait_mock = AsyncMock(
            return_value={
                "orderInfo": {
                    "orderId": "order-demo",
                    "status": 4,
                    "failType": 0,
                    "originalDuration": 1600,
                },
                "orderResult": order_result,
            }
        )
        monkeypatch.setattr("smart_badge_api.asr.xfyun_asr_provider._upload_audio", upload_mock)
        monkeypatch.setattr("smart_badge_api.asr.xfyun_asr_provider._wait_for_result", wait_mock)

        try:
            first = await transcribe_audio(audio_path)
            second = await transcribe_audio(audio_path)
        finally:
            get_settings.cache_clear()

        upload_mock.assert_awaited_once()
        wait_mock.assert_awaited_once_with("order-demo", "random-demo")
        assert first == second

    import asyncio

    asyncio.run(scenario())


def test_transcribe_audio_does_not_reuse_cache_when_role_options_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        audio_path = tmp_path / "demo.mp3"
        audio_path.write_bytes(b"demo")
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("ASR_RUNTIME_DIR", str(tmp_path / "asr_runtime"))
        monkeypatch.setenv("XFYUN_ASR_APP_ID", "appid-demo")
        monkeypatch.setenv("XFYUN_ASR_ACCESS_KEY_ID", "apikey-demo")
        monkeypatch.setenv("XFYUN_ASR_ACCESS_KEY_SECRET", "secret-demo")
        get_settings.cache_clear()

        order_result = json.dumps(
            {
                "lattice": [
                    {
                        "json_1best": json.dumps(
                            {
                                "st": {
                                    "bg": "100",
                                    "ed": "1600",
                                    "rl": "1",
                                    "rt": [
                                        {"ws": [{"cw": [{"w": "欢迎光临", "wp": "n"}]}]},
                                    ],
                                }
                            }
                        )
                    }
                ]
            }
        )

        monkeypatch.setattr(
            "smart_badge_api.asr.xfyun_asr_provider._validate_runtime_prerequisites",
            lambda: None,
        )
        upload_mock = AsyncMock(side_effect=[("order-demo-1", "random-1"), ("order-demo-2", "random-2")])
        wait_mock = AsyncMock(
            return_value={
                "orderInfo": {
                    "status": 4,
                    "failType": 0,
                    "originalDuration": 1600,
                },
                "orderResult": order_result,
            }
        )
        monkeypatch.setattr("smart_badge_api.asr.xfyun_asr_provider._upload_audio", upload_mock)
        monkeypatch.setattr("smart_badge_api.asr.xfyun_asr_provider._wait_for_result", wait_mock)

        try:
            monkeypatch.setenv("XFYUN_ASR_ROLE_TYPE", "0")
            monkeypatch.setenv("XFYUN_ASR_ROLE_NUM", "0")
            get_settings.cache_clear()
            await transcribe_audio(audio_path)

            monkeypatch.setenv("XFYUN_ASR_ROLE_TYPE", "1")
            monkeypatch.setenv("XFYUN_ASR_ROLE_NUM", "2")
            get_settings.cache_clear()
            await transcribe_audio(audio_path)
        finally:
            get_settings.cache_clear()

        assert upload_mock.await_count == 2
        assert wait_mock.await_count == 2

    import asyncio

    asyncio.run(scenario())
