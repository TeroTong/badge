from __future__ import annotations

import json

from smart_badge_api.asr.speaker_voiceprint import (
    apply_staff_voiceprints,
    auto_enroll_staff_voiceprint,
    get_staff_voiceprint_review,
    list_staff_voiceprint_reviews,
    resolve_staff_voiceprint_review,
)
from smart_badge_api.core.config import get_settings


def test_apply_staff_voiceprints_assigns_staff_and_counterparty(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()

    fake_audio = tmp_path / "demo.wav"
    fake_audio.write_bytes(b"demo")

    registry_path = get_settings().resolved_speaker_voiceprint_registry_path
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "staff": [
                    {
                        "staff_id": "staff001",
                        "staff_name": "兰兰",
                        "staff_role": "consultant",
                        "sample_count": 2,
                        "total_duration_ms": 24000,
                        "embedding": [1.0, 0.0, 0.0],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "smart_badge_api.asr.speaker_voiceprint._extract_embeddings_for_speakers",
        lambda _audio_path, _intervals_by_speaker: {
            "SPEAKER_00": [1.0, 0.0, 0.0],
            "SPEAKER_01": [0.0, 1.0, 0.0],
        },
    )

    utterances = [
        {
            "speaker": "SPEAKER_00",
            "speaker_id": "SPEAKER_00",
            "text": "您好，我先了解一下您的需求。",
            "begin_ms": 0,
            "end_ms": 15000,
        },
        {
            "speaker": "SPEAKER_01",
            "speaker_id": "SPEAKER_01",
            "text": "我主要想改善法令纹。",
            "begin_ms": 16000,
            "end_ms": 30000,
        },
    ]

    resolved = apply_staff_voiceprints(fake_audio, utterances)

    assert resolved[0]["speaker"] == "consultant"
    assert resolved[0]["speaker_staff_id"] == "staff001"
    assert resolved[0]["speaker_staff_name"] == "兰兰"
    assert resolved[0]["speaker_role_source"] == "voiceprint"
    assert resolved[1]["speaker"] == "customer"
    assert resolved[1]["speaker_role_source"] == "voiceprint_counterparty"

    get_settings.cache_clear()


def test_auto_enroll_staff_voiceprint_writes_registry(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()

    fake_audio = tmp_path / "demo.wav"
    fake_audio.write_bytes(b"demo")

    registry_path = get_settings().resolved_speaker_voiceprint_registry_path
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "staff": [
                    {
                        "staff_id": "staff001",
                        "staff_name": "兰兰",
                        "staff_role": "consultant",
                        "sample_count": 1,
                        "total_duration_ms": 14000,
                        "embedding": [0.6, 0.8, 0.0],
                        "sources": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "smart_badge_api.asr.speaker_voiceprint._extract_embeddings_for_speakers",
        lambda _audio_path, _intervals_by_speaker: {"SPEAKER_00": [0.6, 0.8, 0.0]},
    )

    utterances = [
        {
            "speaker": "consultant",
            "speaker_id": "SPEAKER_00",
            "speaker_staff_id": "staff001",
            "speaker_staff_name": "兰兰",
            "speaker_role_source": "voiceprint_bound_staff",
            "speaker_voiceprint_similarity": 0.92,
            "text": "您好，我是今天接待您的咨询师。",
            "begin_ms": 0,
            "end_ms": 14000,
        },
        {
            "speaker": "customer",
            "speaker_id": "SPEAKER_01",
            "text": "我想改善法令纹。",
            "begin_ms": 15000,
            "end_ms": 22000,
        },
    ]

    success = auto_enroll_staff_voiceprint(
        fake_audio,
        utterances,
        staff_id="staff001",
        staff_name="兰兰",
        staff_role="consultant",
        source_id="recording001",
    )

    assert success is True

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert payload["staff"][0]["staff_id"] == "staff001"
    assert payload["staff"][0]["staff_name"] == "兰兰"
    assert payload["staff"][0]["staff_role"] == "consultant"
    assert payload["staff"][0]["sample_count"] == 2
    assert payload["staff"][0]["sources"][-1]["source_id"] == "recording001"

    get_settings.cache_clear()


def test_auto_enroll_staff_voiceprint_queues_review_when_missing_template(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()

    fake_audio = tmp_path / "demo.wav"
    fake_audio.write_bytes(b"demo")

    utterances = [
        {
            "speaker": "consultant",
            "speaker_id": "SPEAKER_00",
            "text": "您好，我是今天接待您的咨询师。",
            "begin_ms": 0,
            "end_ms": 14000,
        },
        {
            "speaker": "customer",
            "speaker_id": "SPEAKER_01",
            "text": "我想改善法令纹。",
            "begin_ms": 15000,
            "end_ms": 22000,
        },
    ]

    success = auto_enroll_staff_voiceprint(
        fake_audio,
        utterances,
        staff_id="staff009",
        staff_name="新员工",
        staff_role="consultant",
        source_id="recording009",
    )

    assert success is False
    items = list_staff_voiceprint_reviews(status="pending")
    assert len(items) == 1
    assert items[0]["staff_id"] == "staff009"
    assert "missing_staff_template" in items[0]["reasons"]

    get_settings.cache_clear()


def test_auto_enroll_staff_voiceprint_queues_low_similarity_review(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()

    fake_audio = tmp_path / "demo.wav"
    fake_audio.write_bytes(b"demo")

    registry_path = get_settings().resolved_speaker_voiceprint_registry_path
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "staff": [
                    {
                        "staff_id": "staff001",
                        "staff_name": "兰兰",
                        "staff_role": "consultant",
                        "sample_count": 1,
                        "total_duration_ms": 14000,
                        "embedding": [0.6, 0.8, 0.0],
                        "sources": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    utterances = [
        {
            "speaker": "consultant",
            "speaker_id": "SPEAKER_00",
            "speaker_staff_id": "staff001",
            "speaker_staff_name": "兰兰",
            "speaker_role_source": "voiceprint_bound_staff",
            "speaker_voiceprint_similarity": 0.75,
            "text": "您好，我是今天接待您的咨询师。",
            "begin_ms": 0,
            "end_ms": 14000,
        },
        {
            "speaker": "customer",
            "speaker_id": "SPEAKER_01",
            "text": "我想改善法令纹。",
            "begin_ms": 15000,
            "end_ms": 22000,
        },
    ]

    success = auto_enroll_staff_voiceprint(
        fake_audio,
        utterances,
        staff_id="staff001",
        staff_name="兰兰",
        staff_role="consultant",
        source_id="recording010",
    )

    assert success is False
    item = get_staff_voiceprint_review("non-existent")
    assert item is None
    items = list_staff_voiceprint_reviews(status="pending")
    assert len(items) == 1
    assert items[0]["source_id"] == "recording010"
    assert "low_voiceprint_similarity" in items[0]["reasons"]

    resolved = resolve_staff_voiceprint_review(items[0]["id"], status="rejected", resolved_by="tester", note="skip")
    assert resolved is not None
    assert resolved["status"] == "rejected"

    get_settings.cache_clear()


def test_apply_staff_voiceprints_prefers_bound_staff_on_two_speaker_recording(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()

    fake_audio = tmp_path / "demo.wav"
    fake_audio.write_bytes(b"demo")

    registry_path = get_settings().resolved_speaker_voiceprint_registry_path
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "staff": [
                    {
                        "staff_id": "staff001",
                        "staff_name": "钟露",
                        "staff_role": "consultant",
                        "sample_count": 3,
                        "total_duration_ms": 36000,
                        "embedding": [1.0, 0.0, 0.0],
                    },
                    {
                        "staff_id": "staff002",
                        "staff_name": "兰四秀",
                        "staff_role": "consultant",
                        "sample_count": 3,
                        "total_duration_ms": 36000,
                        "embedding": [0.0, 1.0, 0.0],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "smart_badge_api.asr.speaker_voiceprint._extract_embeddings_for_speakers",
        lambda _audio_path, _intervals_by_speaker: {
            "SPEAKER_00": [1.0, 0.0, 0.0],
            "SPEAKER_01": [0.0, 1.0, 0.0],
        },
    )

    utterances = [
        {
            "speaker": "customer",
            "speaker_id": "SPEAKER_00",
            "text": "您好，我先帮您了解一下情况。",
            "begin_ms": 0,
            "end_ms": 18000,
        },
        {
            "speaker": "SPEAKER_01",
            "speaker_id": "SPEAKER_01",
            "text": "我主要想改善法令纹。",
            "begin_ms": 18500,
            "end_ms": 36000,
        },
    ]

    resolved = apply_staff_voiceprints(fake_audio, utterances, staff_id="staff001")

    assert resolved[0]["speaker"] == "consultant"
    assert resolved[0]["speaker_staff_id"] == "staff001"
    assert resolved[0]["speaker_role_source"] == "voiceprint_bound_staff"
    assert resolved[1]["speaker"] == "customer"
    assert "speaker_staff_id" not in resolved[1]

    get_settings.cache_clear()


def test_apply_staff_voiceprints_prevents_duplicate_staff_assignment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()

    fake_audio = tmp_path / "demo.wav"
    fake_audio.write_bytes(b"demo")

    registry_path = get_settings().resolved_speaker_voiceprint_registry_path
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "staff": [
                    {
                        "staff_id": "staff001",
                        "staff_name": "钟露",
                        "staff_role": "consultant",
                        "sample_count": 3,
                        "total_duration_ms": 36000,
                        "embedding": [1.0, 0.0, 0.0],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "smart_badge_api.asr.speaker_voiceprint._extract_embeddings_for_speakers",
        lambda _audio_path, _intervals_by_speaker: {
            "SPEAKER_00": [1.0, 0.0, 0.0],
            "SPEAKER_01": [1.0, 0.0, 0.0],
        },
    )

    utterances = [
        {
            "speaker": "SPEAKER_00",
            "speaker_id": "SPEAKER_00",
            "text": "您好，我先帮您看一下。",
            "begin_ms": 0,
            "end_ms": 18000,
        },
        {
            "speaker": "SPEAKER_01",
            "speaker_id": "SPEAKER_01",
            "text": "我主要是想改善法令纹。",
            "begin_ms": 18500,
            "end_ms": 36000,
        },
    ]

    resolved = apply_staff_voiceprints(fake_audio, utterances)

    assert resolved[0]["speaker"] == "consultant"
    assert resolved[0]["speaker_staff_id"] == "staff001"
    assert resolved[1]["speaker"] == "customer"
    assert "speaker_staff_id" not in resolved[1]

    get_settings.cache_clear()


def test_apply_staff_voiceprints_skips_global_staff_match_for_customer_in_bound_context(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()

    fake_audio = tmp_path / "demo.wav"
    fake_audio.write_bytes(b"demo")

    registry_path = get_settings().resolved_speaker_voiceprint_registry_path
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "staff": [
                    {
                        "staff_id": "staff001",
                        "staff_name": "钟露",
                        "staff_role": "consultant",
                        "sample_count": 3,
                        "total_duration_ms": 36000,
                        "embedding": [1.0, 0.0, 0.0],
                    },
                    {
                        "staff_id": "staff002",
                        "staff_name": "兰四秀",
                        "staff_role": "consultant",
                        "sample_count": 3,
                        "total_duration_ms": 36000,
                        "embedding": [0.0, 1.0, 0.0],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "smart_badge_api.asr.speaker_voiceprint._extract_embeddings_for_speakers",
        lambda _audio_path, _intervals_by_speaker: {
            "SPEAKER_00": [1.0, 0.0, 0.0],
            "SPEAKER_01": [0.0, 1.0, 0.0],
            "SPEAKER_02": [0.0, 1.0, 0.0],
        },
    )

    utterances = [
        {
            "speaker": "consultant",
            "speaker_id": "SPEAKER_00",
            "text": "您好，我先帮您了解一下。",
            "begin_ms": 0,
            "end_ms": 15000,
        },
        {
            "speaker": "customer",
            "speaker_id": "SPEAKER_01",
            "text": "我主要想改善法令纹。",
            "begin_ms": 15500,
            "end_ms": 31000,
        },
        {
            "speaker": "unknown",
            "speaker_id": "SPEAKER_02",
            "text": "嗯嗯好的。",
            "begin_ms": 31500,
            "end_ms": 47000,
        },
    ]

    resolved = apply_staff_voiceprints(fake_audio, utterances, staff_id="staff001")

    assert resolved[0]["speaker_staff_id"] == "staff001"
    assert resolved[0]["speaker_role_source"] == "voiceprint_bound_staff"
    assert "speaker_staff_id" not in resolved[1]
    assert resolved[1]["speaker"] == "customer"
    assert "speaker_staff_id" not in resolved[2]
    assert resolved[2]["speaker"] == "unknown"

    get_settings.cache_clear()
