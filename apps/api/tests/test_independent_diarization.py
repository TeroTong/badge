from __future__ import annotations

from smart_badge_api.asr.independent_diarization import (
    DiarizationSegment,
    apply_diarization_segments_to_utterances,
)
from smart_badge_api.core.config import get_settings


def test_independent_diarization_reassigns_asr_speakers(monkeypatch) -> None:
    monkeypatch.setenv("ASR_INDEPENDENT_DIARIZATION_MIN_SPEAKERS", "2")
    monkeypatch.setenv("ASR_INDEPENDENT_DIARIZATION_MAX_SPEAKERS", "5")
    get_settings.cache_clear()

    try:
        utterances, metadata = apply_diarization_segments_to_utterances(
            [
                {
                    "speaker": "speaker_0",
                    "speaker_id": "speaker_0",
                    "text": "您好，我先了解一下您的需求。",
                    "begin_ms": 0,
                    "end_ms": 3000,
                },
                {
                    "speaker": "speaker_1",
                    "speaker_id": "speaker_1",
                    "text": "我主要想改善法令纹。",
                    "begin_ms": 3200,
                    "end_ms": 6000,
                },
            ],
            [
                DiarizationSegment(begin_ms=0, end_ms=3100, speaker_id="SPEAKER_00"),
                DiarizationSegment(begin_ms=3100, end_ms=6200, speaker_id="SPEAKER_01"),
            ],
        )
    finally:
        get_settings.cache_clear()

    assert metadata["applied"] is True
    assert [item["speaker_id"] for item in utterances] == ["SPEAKER_00", "SPEAKER_01"]
    assert utterances[0]["asr_original_speaker_id"] == "speaker_0"
    assert utterances[0]["speaker_diarization_source"] == "independent_3dspeaker"


def test_independent_diarization_splits_mixed_asr_utterance(monkeypatch) -> None:
    monkeypatch.setenv("ASR_INDEPENDENT_DIARIZATION_SPLIT_MIXED_UTTERANCES", "true")
    monkeypatch.setenv("ASR_INDEPENDENT_DIARIZATION_MIN_SPLIT_DURATION_MS", "2000")
    monkeypatch.setenv("ASR_INDEPENDENT_DIARIZATION_MIN_SPEAKER_OVERLAP_MS", "500")
    get_settings.cache_clear()

    try:
        utterances, metadata = apply_diarization_segments_to_utterances(
            [
                {
                    "speaker": "speaker_0",
                    "speaker_id": "speaker_0",
                    "text": "您想改善哪里？我主要想改善法令纹。",
                    "begin_ms": 0,
                    "end_ms": 6000,
                }
            ],
            [
                DiarizationSegment(begin_ms=0, end_ms=3000, speaker_id="SPEAKER_00"),
                DiarizationSegment(begin_ms=3000, end_ms=6000, speaker_id="SPEAKER_01"),
            ],
        )
    finally:
        get_settings.cache_clear()

    assert metadata["applied"] is True
    assert metadata["split_mixed_utterance_count"] == 1
    assert [item["speaker_id"] for item in utterances] == ["SPEAKER_00", "SPEAKER_01"]
    assert [item["text"] for item in utterances] == ["您想改善哪里？", "我主要想改善法令纹。"]


def test_independent_diarization_skips_unreasonable_speaker_count(monkeypatch) -> None:
    monkeypatch.setenv("ASR_INDEPENDENT_DIARIZATION_MAX_SPEAKERS", "5")
    get_settings.cache_clear()

    try:
        utterances, metadata = apply_diarization_segments_to_utterances(
            [
                {
                    "speaker": "speaker_0",
                    "speaker_id": "speaker_0",
                    "text": "测试文本。",
                    "begin_ms": 0,
                    "end_ms": 6000,
                }
            ],
            [
                DiarizationSegment(begin_ms=index * 1000, end_ms=(index + 1) * 1000, speaker_id=f"SPEAKER_{index:02d}")
                for index in range(6)
            ],
        )
    finally:
        get_settings.cache_clear()

    assert metadata["applied"] is False
    assert metadata["reason"] == "too_many_speakers"
    assert utterances[0]["speaker_id"] == "speaker_0"
