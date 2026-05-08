from smart_badge_api.recording_analysis_service import build_analysis_payload_from_utterances


def test_build_analysis_payload_prefers_business_role_and_display_label() -> None:
    payload, segment_count, duration_ms = build_analysis_payload_from_utterances(
        [
            {
                "speaker": "doctor",
                "speaker_id": "SPEAKER_00",
                "speaker_role": "doctor",
                "speaker_business_role": "staff_peer",
                "speaker_display_label": "员工同事",
                "text": "我先帮您看一下腰腹基础。",
                "begin_ms": 1200,
                "end_ms": 3600,
            },
            {
                "speaker": "customer",
                "speaker_id": "SPEAKER_02",
                "speaker_role": "customer",
                "speaker_business_role": "primary_customer",
                "speaker_display_label": "主客户",
                "text": "我主要想做腰腹吸脂。",
                "begin_ms": 4000,
                "end_ms": 6400,
            },
        ]
    )

    segments = payload["payload"]["transcribeResult"]
    assert segment_count == 2
    assert duration_ms == 6400
    assert segments[0]["role"] == "staff_peer"
    assert segments[0]["speaker_label"] == "员工同事"
    assert segments[0]["speaker_id"] == "SPEAKER_00"
    assert segments[1]["role"] == "primary_customer"
    assert segments[1]["speaker_label"] == "主客户"


def test_build_analysis_payload_falls_back_to_raw_speaker_id_label() -> None:
    payload, segment_count, duration_ms = build_analysis_payload_from_utterances(
        [
            {
                "speaker": "SPEAKER_03",
                "speaker_id": "SPEAKER_03",
                "text": "我陪她一起来的。",
                "begin_ms": 0,
                "end_ms": 1800,
            }
        ]
    )

    segments = payload["payload"]["transcribeResult"]
    assert segment_count == 1
    assert duration_ms == 1800
    assert segments[0]["role"] == "unknown"
    assert segments[0]["speaker_label"] == "其他在场人员（SPEAKER_03）"
