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


def test_build_analysis_payload_does_not_label_expert_assistant_as_doctor() -> None:
    payload, _, _ = build_analysis_payload_from_utterances(
        [
            {
                "speaker": "doctor",
                "speaker_id": "SPEAKER_00",
                "speaker_role": "doctor",
                "speaker_role_source": "local_heuristic",
                "speaker_business_role": "doctor",
                "speaker_display_label": "医生",
                "text": "我是今天接待您们的专家助理，是想要咨询眼袋是吗？",
                "begin_ms": 0,
                "end_ms": 3000,
            },
            {
                "speaker": "doctor",
                "speaker_id": "SPEAKER_00",
                "speaker_role": "doctor",
                "speaker_role_source": "local_heuristic",
                "speaker_business_role": "doctor",
                "speaker_display_label": "医生",
                "text": "王院长的手术，来，我们先到这个房间坐一下啊。",
                "begin_ms": 24000,
                "end_ms": 39000,
            },
        ]
    )

    segments = payload["payload"]["transcribeResult"]
    assert segments[0]["role"] == "staff_peer"
    assert segments[0]["speaker_label"] == "专家助理"
    assert segments[1]["role"] == "staff_peer"
    assert segments[1]["speaker_label"] == "员工同事"


def test_build_analysis_payload_recovers_customer_labeled_doctor_explanation() -> None:
    payload, _, _ = build_analysis_payload_from_utterances(
        [
            {
                "speaker": "customer",
                "speaker_id": "SPEAKER_01",
                "speaker_role": "customer",
                "speaker_role_source": "local_heuristic",
                "speaker_business_role": "primary_customer",
                "speaker_display_label": "主客户",
                "text": "给你讲一下啊，那个眼袋呢，不仅仅是单纯的眼袋的问题，除了眼袋以外，你的泪沟和苹果肌上面这个凹陷也很明显。",
                "begin_ms": 269000,
                "end_ms": 301000,
                "asr_original_speaker_id": "speaker_4",
            },
            {
                "speaker": "customer",
                "speaker_id": "SPEAKER_01",
                "speaker_role": "customer",
                "speaker_role_source": "local_heuristic",
                "speaker_business_role": "primary_customer",
                "speaker_display_label": "主客户",
                "text": "要推平整一点，你是不是感觉好像这边瞬间就好很多了是吧？",
                "begin_ms": 315000,
                "end_ms": 320000,
                "asr_original_speaker_id": "speaker_4",
            },
        ]
    )

    segments = payload["payload"]["transcribeResult"]
    assert segments[0]["role"] == "doctor"
    assert segments[0]["speaker_label"] == "医生"
    assert segments[1]["role"] == "doctor"
    assert segments[1]["speaker_label"] == "医生"
