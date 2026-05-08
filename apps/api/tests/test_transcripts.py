from __future__ import annotations

import json
from pathlib import Path

import pytest

from smart_badge_api.api.routes.transcripts import (
    _build_import_source_key,
    _infer_recording_file_name,
    _normalize_external_analysis_result,
    _parse_transcript_payload,
)


def test_parse_plain_text_transcript_builds_utterances() -> None:
    utterances, full_text, duration_ms, external_analysis = _parse_transcript_payload(
        "manual.txt",
        "customer asks about options\nconsultant explains the plan".encode("utf-8"),
    )

    assert [item["speaker"] for item in utterances] == ["unknown", "unknown"]
    assert full_text == "customer asks about options\nconsultant explains the plan"
    assert utterances[0]["begin_ms"] == 0
    assert utterances[0]["end_ms"] > utterances[0]["begin_ms"]
    assert utterances[1]["begin_ms"] == utterances[0]["end_ms"]
    assert duration_ms == utterances[-1]["end_ms"]
    assert external_analysis is None


def test_parse_json_transcript_supports_nested_payload_shape() -> None:
    payload = {
        "payload": {
            "transcribeResult": [
                {"speaker": "advisor", "text": "let me understand your case first", "begin": 100, "end": 1400},
                {"speaker": "patient", "text": "I mainly worry about recovery time", "begin_ms": 1500, "end_ms": 3200},
            ]
        }
    }

    utterances, full_text, duration_ms, external_analysis = _parse_transcript_payload(
        "vendor.json",
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )

    assert utterances == [
        {"speaker": "consultant", "text": "let me understand your case first", "begin_ms": 100, "end_ms": 1400},
        {"speaker": "customer", "text": "I mainly worry about recovery time", "begin_ms": 1500, "end_ms": 3200},
    ]
    assert full_text == "let me understand your case first\nI mainly worry about recovery time"
    assert duration_ms == 3200
    assert external_analysis is None


def test_parse_jsonl_payload_extracts_transcript_and_embedded_analysis() -> None:
    payload = {
        "audioId": 123,
        "requirementAnalyzeResult": {
            "summary": {
                "Need summary": {
                    "content": "customer wants a more natural-looking result",
                    "evidence": "[00:12] customer asks for a natural outcome",
                }
            }
        },
        "tagsAnalyzeResult": {
            "extracted_data": [
                {"category": "Need", "sub_tag": "Natural result", "confidence": "High"},
            ],
            "summary": "customer values natural outcomes",
            "error": None,
        },
        "faceAnalyzeResult": {
            "analysis_details": {
                "1.1": {
                    "name": "Need discovery",
                    "score": 8.5,
                    "reasoning": "consultant identified the core need clearly",
                    "suggestion": "keep asking open questions",
                }
            },
            "overall_summary": {"total_score": 86, "consultant_level": "advanced"},
        },
        "strategyAnalyzeResult": {
            "strategy": {
                "key_concerns": "price sensitivity; worries about looking unnatural",
            }
        },
        "transcribeResult": [
            {"begin": 0, "end": 1200, "role": "staff", "speakerRole": "staff", "text": "welcome to the clinic"},
            {"begin": 1300, "end": 2600, "role": "customer", "speakerRole": "customer", "text": "I want a natural result"},
        ],
    }

    utterances, full_text, duration_ms, external_analysis = _parse_transcript_payload(
        "vendor.jsonl",
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )

    assert utterances == [
        {"speaker": "consultant", "text": "welcome to the clinic", "begin_ms": 0, "end_ms": 1200},
        {"speaker": "customer", "text": "I want a natural result", "begin_ms": 1300, "end_ms": 2600},
    ]
    assert full_text == "welcome to the clinic\nI want a natural result"
    assert duration_ms == 2600
    assert external_analysis is not None
    assert set(external_analysis) == {
        "requirementAnalyzeResult",
        "tagsAnalyzeResult",
        "strategyAnalyzeResult",
        "faceAnalyzeResult",
    }


def test_normalize_external_analysis_builds_recording_summary_shape() -> None:
    external_analysis = {
        "requirementAnalyzeResult": {
            "summary": {
                "Need summary": {
                    "content": "customer wants a more natural-looking result",
                    "evidence": "[00:12] customer asks for a natural outcome",
                }
            }
        },
        "tagsAnalyzeResult": {
            "extracted_data": [
                {"category": "Need", "sub_tag": "Natural result", "confidence": "High"},
            ],
            "summary": "customer values natural outcomes",
            "error": None,
        },
        "strategyAnalyzeResult": {
            "strategy": {
                "key_concerns": "price sensitivity; worries about looking unnatural",
            }
        },
        "faceAnalyzeResult": {
            "analysis_details": {
                "1.1": {
                    "name": "Need discovery",
                    "score": 8.5,
                    "reasoning": "consultant identified the core need clearly",
                    "suggestion": "keep asking open questions",
                }
            },
            "overall_summary": {"total_score": 86, "consultant_level": "advanced"},
        },
    }

    result = _normalize_external_analysis_result(external_analysis)

    assert result is not None
    assert result["source"] == "external_upload"
    assert result["customer_demands"]["focus_areas"][0]["area"] == "Need summary"
    assert "natural-looking result" in result["customer_demands"]["focus_areas"][0]["surface_need"]
    assert result["customer_concerns"]["items"][0]["content"] == "price sensitivity"
    assert result["customer_profile"]["tags"][0] == {"category": "Need", "value": "Natural result"}
    assert result["consultation_evaluation"]["overall_score"] == 86.0
    assert result["consultation_evaluation"]["dimensions"][0]["name"] == "Need discovery"


def test_batch_import_source_key_is_content_based() -> None:
    key_a = _build_import_source_key(b"same-content")
    key_b = _build_import_source_key(b"same-content")
    key_c = _build_import_source_key(b"other-content")

    assert key_a == key_b
    assert key_a != key_c


def test_infer_recording_file_name_prefers_audio_url_then_audio_id() -> None:
    assert (
        _infer_recording_file_name({"audioUrl": "https://example.com/audio/test_888888.mp3"}, Path("payload.jsonl"))
        == "test_888888.mp3"
    )
    assert (
        _infer_recording_file_name(
            {"audioUrl": "https://example.com/audio/9781b5d5a65e4f049d1f21d85f84c780.mp3"},
            Path("validated/case_001/payload.jsonl"),
        )
        == "case_001.mp3"
    )
    assert _infer_recording_file_name({"audioId": 123456}, Path("payload.jsonl")) == "audio_123456.mp3"
    assert _infer_recording_file_name({}, Path("validated/case_001/payload.jsonl")) == "case_001.mp3"


def test_parse_json_transcript_rejects_unsupported_shape() -> None:
    with pytest.raises(ValueError, match="Unsupported JSON transcript format"):
        _parse_transcript_payload(
            "bad.json",
            json.dumps({"foo": "bar"}).encode("utf-8"),
        )
