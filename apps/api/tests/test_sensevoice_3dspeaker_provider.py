from __future__ import annotations

from unittest.mock import patch

from smart_badge_api.asr.sensevoice_3dspeaker_provider import (
    _DiarizationSegment,
    _TimedToken,
    _apply_role_classification,
    _assign_speakers_to_tokens,
    _build_speaker_windows,
    _extract_timed_tokens,
    _merge_sentence_utterances,
    _merge_tokens_into_utterances,
)


def test_assign_speakers_to_tokens_and_merge_into_utterances() -> None:
    tokens = [
        _TimedToken(text="您", begin_ms=0, end_ms=300),
        _TimedToken(text="好", begin_ms=320, end_ms=600),
        _TimedToken(text="我", begin_ms=1600, end_ms=1900),
        _TimedToken(text="想了解", begin_ms=1920, end_ms=2600),
    ]
    diarization_segments = [
        _DiarizationSegment(begin_ms=0, end_ms=1000, speaker_id="SPEAKER_00"),
        _DiarizationSegment(begin_ms=1200, end_ms=3000, speaker_id="SPEAKER_01"),
    ]

    assigned = _assign_speakers_to_tokens(tokens, diarization_segments)
    utterances = _merge_tokens_into_utterances(
        assigned,
        gap_ms=1000,
        punctuation_pause_ms=500,
    )

    assert [item["speaker_id"] for item in utterances] == ["SPEAKER_00", "SPEAKER_01"]
    assert [item["text"] for item in utterances] == ["您好", "我想了解"]
    assert utterances[0]["begin_ms"] == 0
    assert utterances[1]["end_ms"] == 2600


def test_apply_role_classification_preserves_raw_speaker_id() -> None:
    utterances = [
        {
            "speaker": "SPEAKER_00",
            "speaker_id": "SPEAKER_00",
            "text": "您好，我是今天的咨询师。",
            "begin_ms": 0,
            "end_ms": 2000,
        },
        {
            "speaker": "SPEAKER_01",
            "speaker_id": "SPEAKER_01",
            "text": "我想了解一下热玛吉。",
            "begin_ms": 2200,
            "end_ms": 4200,
        },
    ]

    with patch(
        "smart_badge_api.asr.speaker_classifier.classify_speakers",
        return_value=[
            {"speaker": "consultant", "text": utterances[0]["text"], "begin_ms": 0, "end_ms": 2000},
            {"speaker": "customer", "text": utterances[1]["text"], "begin_ms": 2200, "end_ms": 4200},
        ],
    ):
        resolved = _apply_role_classification(utterances)

    assert resolved[0]["speaker"] == "consultant"
    assert resolved[1]["speaker"] == "customer"
    assert resolved[0]["speaker_id"] == "SPEAKER_00"
    assert resolved[1]["speaker_id"] == "SPEAKER_01"


def test_extract_timed_tokens_supports_sensevoice_char_timestamp_format() -> None:
    item = {
        "text": "<|zh|><|HAPPY|><|Speech|><|withitn|>你好。 <|zh|><|NEUTRAL|><|Speech|><|withitn|>请坐。",
        "timestamp": [
            [0, 100],
            [100, 200],
            [200, 300],
            [320, 420],
            [420, 520],
            [520, 620],
        ],
    }

    tokens = _extract_timed_tokens(item)

    assert [token.text for token in tokens] == ["你", "好", "。", "请", "坐", "。"]
    assert tokens[0].begin_ms == 0
    assert tokens[-1].end_ms == 620


def test_merge_sentence_utterances_assigns_speaker_by_interval_overlap() -> None:
    tokens = [
        _TimedToken(text="您", begin_ms=0, end_ms=100),
        _TimedToken(text="好", begin_ms=100, end_ms=200),
        _TimedToken(text="。", begin_ms=200, end_ms=260),
        _TimedToken(text="请", begin_ms=400, end_ms=500),
        _TimedToken(text="坐", begin_ms=500, end_ms=600),
        _TimedToken(text="。", begin_ms=600, end_ms=660),
    ]
    diarization_segments = [
        _DiarizationSegment(begin_ms=0, end_ms=300, speaker_id="SPEAKER_00"),
        _DiarizationSegment(begin_ms=350, end_ms=800, speaker_id="SPEAKER_01"),
    ]

    utterances = _merge_sentence_utterances(
        tokens,
        diarization_segments,
        gap_ms=1000,
        punctuation_pause_ms=500,
    )

    assert [item["speaker_id"] for item in utterances] == ["SPEAKER_00", "SPEAKER_01"]
    assert [item["text"] for item in utterances] == ["您好。", "请坐。"]


def test_build_speaker_windows_merges_small_gaps_for_same_speaker() -> None:
    diarization_segments = [
        _DiarizationSegment(begin_ms=0, end_ms=2_000, speaker_id="SPEAKER_00"),
        _DiarizationSegment(begin_ms=2_300, end_ms=4_000, speaker_id="SPEAKER_00"),
        _DiarizationSegment(begin_ms=4_500, end_ms=5_500, speaker_id="SPEAKER_01"),
    ]

    windows = _build_speaker_windows(
        diarization_segments,
        max_window_ms=10_000,
        merge_gap_ms=500,
    )

    assert [(item.begin_ms, item.end_ms, item.speaker_id) for item in windows] == [
        (0, 4_000, "SPEAKER_00"),
        (4_500, 5_500, "SPEAKER_01"),
    ]


def test_build_speaker_windows_respects_max_window_duration() -> None:
    diarization_segments = [
        _DiarizationSegment(begin_ms=0, end_ms=6_000, speaker_id="SPEAKER_00"),
        _DiarizationSegment(begin_ms=6_100, end_ms=9_000, speaker_id="SPEAKER_00"),
    ]

    windows = _build_speaker_windows(
        diarization_segments,
        max_window_ms=5_000,
        merge_gap_ms=500,
    )

    assert [(item.begin_ms, item.end_ms, item.speaker_id) for item in windows] == [
        (0, 5_000, "SPEAKER_00"),
        (5_000, 9_000, "SPEAKER_00"),
    ]
