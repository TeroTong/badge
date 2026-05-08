from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from smart_badge_api.asr.tencent_cloud_provider import (
    _SilenceSpan,
    _assign_local_diarization_to_utterances,
    _build_create_rec_task_payload_from_bytes,
    _build_create_rec_task_payload_from_url,
    _choose_silence_aware_cut_points,
    _distinct_speaker_ids,
    _offset_utterances,
    _prepare_direct_upload_chunks,
    _resolve_direct_upload_chunk_bitrate_kbps,
    _resolve_direct_upload_segment_seconds,
    _resolve_ffmpeg_executable,
    parse_tencent_task_data,
    transcribe_audio,
)
from smart_badge_api.asr.tencent_media_proxy import (
    build_tencent_media_token,
    build_tencent_media_url,
    resolve_tencent_media_token,
)
from smart_badge_api.core.config import get_settings


def test_tencent_media_token_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upload_dir = tmp_path / "uploads"
    audio_path = upload_dir / "recordings" / "demo.mp3"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"demo")

    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setenv("FRONTEND_URL", "https://badge.example.com")
    monkeypatch.setenv("API_V1_PREFIX", "/api/v1")
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret")
    monkeypatch.setenv("TENCENT_ASR_PUBLIC_MEDIA_TTL_SECONDS", "600")
    monkeypatch.setenv("TENCENT_ASR_PUBLIC_MEDIA_BASE_URL", "")
    get_settings.cache_clear()

    try:
        token = build_tencent_media_token(audio_path, filename="demo.mp3")
        resolved_path, filename = resolve_tencent_media_token(token)
        media_url = build_tencent_media_url(audio_path, filename="demo.mp3")

        assert resolved_path == audio_path.resolve()
        assert filename == "demo.mp3"
        assert media_url.startswith("https://badge.example.com/api/v1/asr/tencent-media?token=")

        broken = f"{token[:-1]}x"
        with pytest.raises(ValueError):
            resolve_tencent_media_token(broken)
    finally:
        get_settings.cache_clear()


def test_tencent_media_url_prefers_public_media_base_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_dir = tmp_path / "uploads"
    audio_path = upload_dir / "recordings" / "demo.mp3"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"demo")

    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setenv("FRONTEND_URL", "https://frontend.example.com")
    monkeypatch.setenv("TENCENT_ASR_PUBLIC_MEDIA_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("API_V1_PREFIX", "/api/v1")
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret")
    get_settings.cache_clear()

    try:
        media_url = build_tencent_media_url(audio_path, filename="demo.mp3")
    finally:
        get_settings.cache_clear()

    assert media_url.startswith("https://api.example.com/api/v1/asr/tencent-media?token=")


def test_parse_tencent_task_data_builds_speaker_utterances() -> None:
    utterances, full_text, duration_ms = parse_tencent_task_data(
        {
            "AudioDuration": 2.6,
            "ResultDetail": [
                {
                    "FinalSentence": "您好，欢迎光临。",
                    "StartMs": 0,
                    "EndMs": 1200,
                    "SpeakerId": 0,
                },
                {
                    "FinalSentence": "我想了解热玛吉。",
                    "StartMs": 1500,
                    "EndMs": 2600,
                    "SpeakerId": 1,
                },
            ],
        }
    )

    assert [item["speaker_id"] for item in utterances] == ["speaker_0", "speaker_1"]
    assert [item["text"] for item in utterances] == ["您好，欢迎光临。", "我想了解热玛吉。"]
    assert full_text == "您好，欢迎光临。 我想了解热玛吉。"
    assert duration_ms == 2600


def test_parse_tencent_task_data_falls_back_to_result_text() -> None:
    utterances, full_text, duration_ms = parse_tencent_task_data(
        {
            "AudioDuration": 3.2,
            "Result": "[0:0.000,0:3.200]  测试识别结果。\n",
            "ResultDetail": [],
        }
    )

    assert len(utterances) == 1
    assert utterances[0]["speaker_id"] == "unknown"
    assert utterances[0]["text"] == "测试识别结果。"
    assert full_text == "测试识别结果。"
    assert duration_ms == 3200


def test_build_create_rec_task_payload_from_bytes_uses_direct_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TENCENT_ASR_ENGINE_MODEL_TYPE", "16k_zh")
    monkeypatch.setenv("TENCENT_ASR_SPEAKER_DIARIZATION", "0")
    monkeypatch.setenv("TENCENT_ASR_REPLACE_TEXT_ID", "replace-123")
    get_settings.cache_clear()

    try:
        payload = _build_create_rec_task_payload_from_bytes(
            b"demo-bytes",
            hotword_list="热玛吉|11,超声炮|10",
        )
    finally:
        get_settings.cache_clear()

    assert payload["SourceType"] == 1
    assert payload["EngineModelType"] == "16k_zh"
    assert payload["SpeakerDiarization"] == 0
    assert payload["DataLen"] == len(b"demo-bytes")
    assert payload["HotwordList"] == "热玛吉|11,超声炮|10"
    assert payload["ReplaceTextId"] == "replace-123"
    assert "Data" in payload
    assert "Url" not in payload


def test_build_create_rec_task_payload_from_url_uses_original_media_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TENCENT_ASR_ENGINE_MODEL_TYPE", "16k_zh")
    monkeypatch.setenv("TENCENT_ASR_SPEAKER_DIARIZATION", "1")
    monkeypatch.setenv("TENCENT_ASR_REPLACE_TEXT_ID", "replace-123")
    get_settings.cache_clear()

    try:
        payload = _build_create_rec_task_payload_from_url(
            "https://badge.example.com/api/v1/asr/tencent-media?token=abc",
            hotword_list="热玛吉|11",
        )
    finally:
        get_settings.cache_clear()

    assert payload["SourceType"] == 0
    assert payload["EngineModelType"] == "16k_zh"
    assert payload["SpeakerDiarization"] == 1
    assert payload["Url"] == "https://badge.example.com/api/v1/asr/tencent-media?token=abc"
    assert payload["HotwordList"] == "热玛吉|11"
    assert payload["ReplaceTextId"] == "replace-123"
    assert "Data" not in payload
    assert "DataLen" not in payload


def test_resolve_ffmpeg_executable_falls_back_to_bundled_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("smart_badge_api.asr.tencent_cloud_provider._FFMPEG_EXECUTABLE", None)
    monkeypatch.setattr("smart_badge_api.asr.tencent_cloud_provider.shutil.which", lambda name: None)

    class _BundledFfmpeg:
        @staticmethod
        def get_ffmpeg_exe() -> str:
            return "/tmp/bundled-ffmpeg"

    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", _BundledFfmpeg)

    assert _resolve_ffmpeg_executable() == "/tmp/bundled-ffmpeg"


def test_direct_upload_chunk_bitrate_has_quality_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TENCENT_ASR_DIRECT_UPLOAD_BITRATE_KBPS", "16")
    get_settings.cache_clear()

    try:
        assert _resolve_direct_upload_chunk_bitrate_kbps() == 40
    finally:
        get_settings.cache_clear()


def test_direct_upload_chunk_bitrate_allows_higher_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TENCENT_ASR_DIRECT_UPLOAD_BITRATE_KBPS", "64")
    get_settings.cache_clear()

    try:
        assert _resolve_direct_upload_chunk_bitrate_kbps() == 64
    finally:
        get_settings.cache_clear()


def test_direct_upload_segment_seconds_is_capped_by_payload_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TENCENT_ASR_DIRECT_UPLOAD_SEGMENT_SECONDS", "1200")
    get_settings.cache_clear()

    try:
        assert _resolve_direct_upload_segment_seconds(max_bytes=5_000_000, bitrate_kbps=40) == 900
    finally:
        get_settings.cache_clear()


def test_choose_silence_aware_cut_points_prefers_nearby_silence() -> None:
    cuts = _choose_silence_aware_cut_points(
        duration_seconds=1_200,
        segment_seconds=900,
        silence_spans=[
            _SilenceSpan(start_seconds=890, end_seconds=894),
        ],
        search_window_seconds=45,
    )

    assert cuts == [892.0]


def test_large_upload_audio_uses_signed_original_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    upload_dir = tmp_path / "uploads"
    audio_path = upload_dir / "recordings" / "long.mp3"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"x" * 1_000_001)

    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setenv("FRONTEND_URL", "https://badge.example.com")
    monkeypatch.setenv("API_V1_PREFIX", "/api/v1")
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret")
    monkeypatch.setenv("TENCENT_ASR_URL_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("TENCENT_ASR_DIRECT_UPLOAD_MAX_BYTES", "16")
    monkeypatch.setenv("TENCENT_ASR_PUBLIC_MEDIA_BASE_URL", "")
    get_settings.cache_clear()
    monkeypatch.setattr("smart_badge_api.asr.tencent_cloud_provider._probe_audio_duration_ms", lambda path: 12_345)
    monkeypatch.setattr(
        "smart_badge_api.asr.tencent_cloud_provider._resolve_ffmpeg_executable",
        lambda: (_ for _ in ()).throw(AssertionError("ffmpeg should not be used for URL upload")),
    )

    try:
        chunks = _prepare_direct_upload_chunks(audio_path)
    finally:
        get_settings.cache_clear()

    assert len(chunks) == 1
    assert chunks[0].data is None
    assert chunks[0].duration_ms == 12_345
    assert chunks[0].file_size_bytes == 1_000_001
    assert chunks[0].url is not None
    assert chunks[0].url.startswith("https://badge.example.com/api/v1/asr/tencent-media?token=")


def test_offset_utterances_shifts_chunk_timestamps() -> None:
    utterances = [
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "你好",
            "begin_ms": 100,
            "end_ms": 900,
        }
    ]

    shifted = _offset_utterances(utterances, 2_000)

    assert shifted[0]["begin_ms"] == 2_100
    assert shifted[0]["end_ms"] == 2_900
    assert utterances[0]["begin_ms"] == 100


def test_distinct_speaker_ids_ignores_unknown_values() -> None:
    utterances = [
        {"speaker": "unknown", "speaker_id": "unknown"},
        {"speaker": "speaker_0", "speaker_id": "speaker_0"},
        {"speaker": "speaker_0", "speaker_id": "speaker_0"},
        {"speaker": "speaker_1", "speaker_id": "speaker_1"},
        {"speaker": "", "speaker_id": ""},
    ]

    assert _distinct_speaker_ids(utterances) == {"speaker_0", "speaker_1"}


def test_assign_local_diarization_to_utterances_prefers_overlap_speaker() -> None:
    utterances = [
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "您好",
            "begin_ms": 0,
            "end_ms": 1800,
        },
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "我想咨询",
            "begin_ms": 2000,
            "end_ms": 4200,
        },
    ]
    diarization_segments = [
        SimpleNamespace(begin_ms=0, end_ms=1900, speaker_id="SPEAKER_00"),
        SimpleNamespace(begin_ms=1900, end_ms=4500, speaker_id="SPEAKER_01"),
    ]

    assigned = _assign_local_diarization_to_utterances(utterances, diarization_segments)

    assert [item["speaker_id"] for item in assigned] == ["SPEAKER_00", "SPEAKER_01"]
    assert all(item["speaker_diarization_source"] == "3dspeaker" for item in assigned)
    assert utterances[0]["speaker_id"] == "speaker_0"


def test_assign_local_diarization_to_utterances_falls_back_to_nearest_segment() -> None:
    utterances = [
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "嗯。",
            "begin_ms": 5000,
            "end_ms": 5200,
        }
    ]
    diarization_segments = [
        SimpleNamespace(begin_ms=0, end_ms=1200, speaker_id="SPEAKER_00"),
        SimpleNamespace(begin_ms=7000, end_ms=8200, speaker_id="SPEAKER_01"),
    ]

    assigned = _assign_local_diarization_to_utterances(utterances, diarization_segments)

    assert assigned[0]["speaker_id"] == "SPEAKER_01"
    assert assigned[0]["speaker_diarization_source"] == "3dspeaker"


def test_transcribe_audio_writes_submitted_event_before_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        audio_path = tmp_path / "demo.mp3"
        audio_path.write_bytes(b"demo")
        events: list[dict] = []
        registry_updates: list[dict] = []

        async def fake_append(**kwargs):
            events.append(kwargs)

        async def fake_upsert(**kwargs):
            registry_updates.append(kwargs)

        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider._validate_runtime_prerequisites",
            lambda: None,
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider.get_tencent_task_registry_entry",
            lambda **kwargs: None,
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider.upsert_tencent_task_registry_entry",
            fake_upsert,
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider._prepare_direct_upload_chunks",
            lambda path: [SimpleNamespace(name="demo.mp3", data=b"demo", duration_ms=337000)],
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider._create_rec_task",
            AsyncMock(return_value=(123456, "req-123")),
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider._wait_for_task",
            AsyncMock(return_value={"AudioDuration": 3.37, "ResultDetail": []}),
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider.parse_tencent_task_data",
            lambda payload: ([], "", 337000),
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider._maybe_apply_local_diarization",
            AsyncMock(side_effect=lambda audio_path, utterances: utterances),
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider.append_tencent_request_event",
            fake_append,
        )

        utterances, full_text, duration_ms = await transcribe_audio(audio_path, source_id="src-1")

        assert utterances == []
        assert full_text == ""
        assert duration_ms == 337000
        assert [item["status"] for item in events] == ["submitted", "completed"]
        assert [item["status"] for item in registry_updates] == ["submitting", "submitted", "completed"]
        assert events[0]["request_id"] == "req-123"
        assert events[0]["task_id"] == 123456
        assert events[0]["submitted_duration_ms"] == 337000
        assert events[1]["request_id"] == "req-123"
        assert events[1]["task_id"] == 123456
        assert events[1]["recognized_duration_ms"] == 337000
        assert events[1]["submitted_duration_ms"] is None

    asyncio.run(scenario())


def test_transcribe_audio_reuses_existing_task_without_new_submit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        audio_path = tmp_path / "demo.mp3"
        audio_path.write_bytes(b"demo")
        events: list[dict] = []
        registry_updates: list[dict] = []
        create_task_mock = AsyncMock(side_effect=AssertionError("should not create new Tencent task"))

        async def fake_append(**kwargs):
            events.append(kwargs)

        async def fake_upsert(**kwargs):
            registry_updates.append(kwargs)

        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider._validate_runtime_prerequisites",
            lambda: None,
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider.get_tencent_task_registry_entry",
            lambda **kwargs: {
                "status": "submitted",
                "request_id": "req-existing",
                "task_id": 654321,
            },
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider.upsert_tencent_task_registry_entry",
            fake_upsert,
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider._prepare_direct_upload_chunks",
            lambda path: [SimpleNamespace(name="demo.mp3", data=b"demo", duration_ms=337000)],
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider._create_rec_task",
            create_task_mock,
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider._wait_for_task",
            AsyncMock(return_value={"AudioDuration": 3.37, "ResultDetail": []}),
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider.parse_tencent_task_data",
            lambda payload: ([], "", 337000),
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider._maybe_apply_local_diarization",
            AsyncMock(side_effect=lambda audio_path, utterances: utterances),
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider.append_tencent_request_event",
            fake_append,
        )

        utterances, full_text, duration_ms = await transcribe_audio(audio_path, source_id="src-1")

        assert utterances == []
        assert full_text == ""
        assert duration_ms == 337000
        assert [item["status"] for item in events] == ["completed"]
        assert [item["status"] for item in registry_updates] == ["completed"]
        create_task_mock.assert_not_called()
        assert events[0]["request_id"] == "req-existing"
        assert events[0]["task_id"] == 654321

    asyncio.run(scenario())


def test_transcribe_audio_blocks_duplicate_submit_when_registry_has_pending_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        audio_path = tmp_path / "demo.mp3"
        audio_path.write_bytes(b"demo")
        create_task_mock = AsyncMock(side_effect=AssertionError("should not create new Tencent task"))

        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider._validate_runtime_prerequisites",
            lambda: None,
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider.get_tencent_task_registry_entry",
            lambda **kwargs: {
                "status": "submitting",
                "request_id": None,
                "task_id": None,
            },
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider._prepare_direct_upload_chunks",
            lambda path: [SimpleNamespace(name="demo.mp3", data=b"demo", duration_ms=337000)],
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider._create_rec_task",
            create_task_mock,
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider.append_tencent_request_event",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider.upsert_tencent_task_registry_entry",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "smart_badge_api.asr.tencent_cloud_provider.release_tencent_submit_lock",
            AsyncMock(),
        )

        with pytest.raises(Exception, match="已阻止自动重试以避免重复消耗额度"):
            await transcribe_audio(audio_path, source_id="src-dup")

        create_task_mock.assert_not_called()

    asyncio.run(scenario())
