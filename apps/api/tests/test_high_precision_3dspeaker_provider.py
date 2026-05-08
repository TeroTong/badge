from __future__ import annotations

from types import SimpleNamespace

from smart_badge_api.asr.high_precision_3dspeaker_provider import (
    _MAX_WHISPER_HOTWORD_CHARS,
    _MAX_WHISPER_HOTWORDS,
    _build_whisper_hotwords,
    _segments_to_word_tokens,
)


def test_build_whisper_hotwords_deduplicates_and_limits_length() -> None:
    hotwords = [f"词{i:02d}" for i in range(100)] + ["词10", "词20"]

    prompt = _build_whisper_hotwords(hotwords)

    assert prompt is not None
    parts = prompt.split(",")
    assert parts[:3] == ["词00", "词01", "词02"]
    assert len(parts) <= _MAX_WHISPER_HOTWORDS
    assert len(prompt) <= _MAX_WHISPER_HOTWORD_CHARS
    assert parts.count("词10") == 1


def test_segments_to_word_tokens_prefers_word_timestamps() -> None:
    segments = [
        SimpleNamespace(
            text="您好，请坐。",
            start=0.0,
            end=1.2,
            words=[
                SimpleNamespace(word="您好", start=0.0, end=0.5),
                SimpleNamespace(word="，", start=0.5, end=0.6),
                SimpleNamespace(word="请坐", start=0.7, end=1.1),
                SimpleNamespace(word="。", start=1.1, end=1.2),
            ],
        )
    ]

    tokens, fallback = _segments_to_word_tokens(segments)

    assert [token.text for token in tokens] == ["您好", "，", "请坐", "。"]
    assert tokens[0].begin_ms == 0
    assert tokens[-1].end_ms == 1200
    assert fallback == []


def test_segments_to_word_tokens_falls_back_to_segment_text_when_words_missing() -> None:
    segments = [
        SimpleNamespace(
            text="您好，请问今天想咨询什么？",
            start=1.0,
            end=3.5,
            words=None,
        )
    ]

    tokens, fallback = _segments_to_word_tokens(segments)

    assert tokens == []
    assert fallback == [
        {
            "speaker": "unknown",
            "speaker_id": "unknown",
            "text": "您好，请问今天想咨询什么？",
            "begin_ms": 1000,
            "end_ms": 3500,
        }
    ]
