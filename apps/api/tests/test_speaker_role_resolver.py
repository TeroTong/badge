from __future__ import annotations

from smart_badge_api.asr.speaker_role_resolver import resolve_speaker_roles


def test_resolve_speaker_roles_adds_badge_owner_and_primary_customer_labels() -> None:
    utterances = [
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "我是今天接待您的兰四秀，您直接跟我说情况就可以。",
            "begin_ms": 0,
            "end_ms": 6000,
        },
        {
            "speaker": "speaker_1",
            "speaker_id": "speaker_1",
            "text": "我想了解一下腰腹吸脂，我之前没有做过。",
            "begin_ms": 7000,
            "end_ms": 12000,
        },
    ]

    resolved = resolve_speaker_roles(
        utterances,
        staff_id="staff-1",
        staff_name="兰四秀",
        staff_role="consultant",
    )

    assert resolved[0]["speaker_role"] == "consultant"
    assert resolved[0]["speaker_identity_type"] == "staff"
    assert resolved[0]["speaker_business_role"] == "badge_owner"
    assert resolved[0]["speaker_display_label"] == "兰四秀（工牌本人）"

    assert resolved[1]["speaker_role"] == "customer"
    assert resolved[1]["speaker_identity_type"] == "visitor"
    assert resolved[1]["speaker_business_role"] == "primary_customer"
    assert resolved[1]["speaker_display_label"] == "主客户"


def test_resolve_speaker_roles_marks_extra_visitor_as_companion() -> None:
    utterances = [
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "我是今天接待您的兰四秀，先跟您说一下方案。",
            "begin_ms": 0,
            "end_ms": 4000,
        },
        {
            "speaker": "speaker_1",
            "speaker_id": "speaker_1",
            "text": "我想了解吸脂和恢复期，我之前没有做过。",
            "begin_ms": 5000,
            "end_ms": 14000,
        },
        {
            "speaker": "speaker_2",
            "speaker_id": "speaker_2",
            "text": "我陪她来的，也想一起了解一下价格。",
            "begin_ms": 14500,
            "end_ms": 18000,
        },
    ]

    resolved = resolve_speaker_roles(
        utterances,
        staff_id="staff-1",
        staff_name="兰四秀",
        staff_role="consultant",
    )

    assert resolved[1]["speaker_business_role"] == "primary_customer"
    assert resolved[1]["speaker_identity_type"] == "visitor"
    assert resolved[1]["speaker_display_label"] == "主客户"
    assert resolved[2]["speaker_business_role"] == "visitor_companion"
    assert resolved[2]["speaker_identity_type"] == "visitor"
    assert resolved[2]["speaker_display_label"] == "同行人"


def test_resolve_speaker_roles_marks_late_medical_speaker_as_doctor() -> None:
    utterances = [
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "我是今天接待您的兰四秀，先帮您了解一下情况。",
            "begin_ms": 0,
            "end_ms": 4000,
        },
        {
            "speaker": "speaker_1",
            "speaker_id": "speaker_1",
            "text": "我想了解一下腰腹吸脂，我比较担心恢复期。",
            "begin_ms": 5000,
            "end_ms": 12000,
        },
        {
            "speaker": "speaker_2",
            "speaker_id": "speaker_2",
            "text": "如果你在意形态和塑形，我们会结合你的基础条件评估，疤痕松解和填充都要看存活率。",
            "begin_ms": 300000,
            "end_ms": 312000,
        },
    ]

    resolved = resolve_speaker_roles(
        utterances,
        staff_id="staff-1",
        staff_name="兰四秀",
        staff_role="consultant",
    )

    assert resolved[2]["speaker_role"] == "doctor"
    assert resolved[2]["speaker_identity_type"] == "staff"
    assert resolved[2]["speaker_business_role"] == "doctor"
    assert resolved[2]["speaker_display_label"] == "医生"


def test_resolve_speaker_roles_keeps_customer_when_customer_signal_is_dominant() -> None:
    utterances = [
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "我是今天接待您的兰四秀，先帮您了解一下情况。",
            "begin_ms": 0,
            "end_ms": 4000,
        },
        {
            "speaker": "speaker_1",
            "speaker_id": "speaker_1",
            "text": "我想做腰腹吸脂，我之前没有做过，我担心恢复期，但是我要求比较高。",
            "begin_ms": 5000,
            "end_ms": 15000,
        },
        {
            "speaker": "speaker_2",
            "speaker_id": "speaker_2",
            "text": "如果你在意形态和塑形，我们会结合你的基础条件评估，疤痕松解和填充都要看存活率。",
            "begin_ms": 300000,
            "end_ms": 312000,
        },
    ]

    resolved = resolve_speaker_roles(
        utterances,
        staff_id="staff-1",
        staff_name="兰四秀",
        staff_role="consultant",
    )

    assert resolved[1]["speaker_business_role"] == "primary_customer"
    assert resolved[1]["speaker_identity_type"] == "visitor"
    assert resolved[2]["speaker_business_role"] == "doctor"


def test_resolve_speaker_roles_does_not_treat_family_background_as_companion() -> None:
    utterances = [
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "我是今天接待您的兰四秀，先帮您了解一下情况。",
            "begin_ms": 0,
            "end_ms": 4000,
        },
        {
            "speaker": "speaker_1",
            "speaker_id": "speaker_1",
            "text": "我自己想做腰腹吸脂，我妹妹之前在这边做过，所以我今天来面诊，也担心恢复期。",
            "begin_ms": 5000,
            "end_ms": 18000,
        },
        {
            "speaker": "speaker_2",
            "speaker_id": "speaker_2",
            "text": "如果你在意形态和塑形，我们会结合你的基础条件评估，疤痕松解和填充都要看存活率。",
            "begin_ms": 300000,
            "end_ms": 312000,
        },
    ]

    resolved = resolve_speaker_roles(
        utterances,
        staff_id="staff-1",
        staff_name="兰四秀",
        staff_role="consultant",
    )

    assert resolved[1]["speaker_business_role"] == "primary_customer"
    assert resolved[1]["speaker_display_label"] == "主客户"


def test_resolve_speaker_roles_keeps_customer_display_when_medical_terms_appear() -> None:
    utterances = [
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "我是今天接待您的兰四秀，先帮您了解一下情况。",
            "begin_ms": 0,
            "end_ms": 4000,
        },
        {
            "speaker": "speaker_1",
            "speaker_id": "speaker_1",
            "text": "我担心恢复期和风险，也在意填充之后的存活率，会不会影响上班。",
            "begin_ms": 5000,
            "end_ms": 12000,
        },
    ]

    resolved = resolve_speaker_roles(
        utterances,
        staff_id="staff-1",
        staff_name="兰四秀",
        staff_role="consultant",
    )

    assert resolved[1]["speaker_role"] == "customer"
    assert resolved[1]["speaker_identity_type"] == "visitor"
    assert resolved[1]["speaker_business_role"] == "primary_customer"
    assert resolved[1]["speaker_display_label"] == "主客户"


def test_resolve_speaker_roles_splits_badge_owner_question_and_customer_brief_answers() -> None:
    utterances = [
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "我是今天接待您的兰四秀，先帮您了解一下情况。",
            "begin_ms": 0,
            "end_ms": 4000,
        },
        {
            "speaker": "speaker_1",
            "speaker_id": "speaker_1",
            "text": "我想了解一下鼻子，我之前没有做过。",
            "begin_ms": 5000,
            "end_ms": 9000,
        },
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "你之前做过吗？没有。你还在读书吗？马上工作。",
            "begin_ms": 10000,
            "end_ms": 18000,
        },
    ]

    resolved = resolve_speaker_roles(
        utterances,
        staff_id="staff-1",
        staff_name="兰四秀",
        staff_role="consultant",
    )

    customer_lines = [item for item in resolved if item["speaker_business_role"] == "primary_customer"]
    customer_texts = [item["text"] for item in customer_lines]

    assert any(text == "没有" for text in customer_texts)
    assert any(text == "马上工作" for text in customer_texts)
    assert any(text == "你之前做过吗" for text in [item["text"] for item in resolved if item["speaker_business_role"] == "badge_owner"])


def test_resolve_speaker_roles_can_preserve_diarized_speaker_turns() -> None:
    utterances = [
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "我是今天接待您的兰四秀，先帮您了解一下情况。",
            "begin_ms": 0,
            "end_ms": 4000,
        },
        {
            "speaker": "speaker_1",
            "speaker_id": "speaker_1",
            "text": "我想了解一下鼻子，我之前没有做过。",
            "begin_ms": 5000,
            "end_ms": 9000,
        },
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "你之前做过吗？没有。你还在读书吗？马上工作。",
            "begin_ms": 10000,
            "end_ms": 18000,
        },
    ]

    resolved = resolve_speaker_roles(
        utterances,
        staff_id="staff-1",
        staff_name="兰四秀",
        staff_role="consultant",
        respect_speaker_diarization=True,
    )

    assert len(resolved) == 3
    assert resolved[2]["speaker_id"] == "speaker_0"
    assert resolved[2]["speaker_business_role"] == "badge_owner"
    assert resolved[2]["text"] == "你之前做过吗？没有。你还在读书吗？马上工作。"


def test_resolve_speaker_roles_splits_strong_mixed_turns_for_analysis() -> None:
    utterances = [
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "你好，我是今天接待你的美学设计师张鑫。",
            "begin_ms": 0,
            "end_ms": 3000,
        },
        {
            "speaker": "speaker_1",
            "speaker_id": "speaker_1",
            "text": "嗯，就是有些时候有点干。",
            "begin_ms": 39000,
            "end_ms": 42000,
        },
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "最近有没有暴晒过？没有没有暴晒过。之前从来没做过啥子医美项目，也没做过皮肤项目。嘎美容院也没去过嘎嗯，皮肤挺好的。",
            "begin_ms": 55000,
            "end_ms": 66000,
        },
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "从来没打过吧，你主要是想解决啥子皮肤问题干吗？",
            "begin_ms": 66000,
            "end_ms": 70000,
        },
    ]

    resolved = resolve_speaker_roles(
        utterances,
        staff_id="staff-zhangxin",
        staff_name="张鑫",
        staff_role="consultant",
        respect_speaker_diarization=True,
        split_mixed_turns=True,
    )

    owner_texts = [item["text"] for item in resolved if item["speaker_business_role"] == "badge_owner"]
    customer_texts = [item["text"] for item in resolved if item["speaker_business_role"] == "primary_customer"]

    assert "最近有没有暴晒过" in owner_texts
    assert "你主要是想解决啥子皮肤问题干吗" in owner_texts
    assert "没有没有暴晒过" in customer_texts
    assert "之前从来没做过啥子医美项目" in customer_texts
    assert "也没做过皮肤项目" in customer_texts
    assert "从来没打过吧" in customer_texts


def test_resolve_speaker_roles_overrides_explicit_staff_intro_with_owner_name() -> None:
    utterances = [
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "诶，你好，陈玉。",
            "begin_ms": 231850,
            "end_ms": 233380,
        },
        {
            "speaker": "speaker_1",
            "speaker_id": "speaker_1",
            "text": "我我这样子呃呃",
            "begin_ms": 235030,
            "end_ms": 236240,
        },
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "你是网上跟那个玲玲联系的对不对",
            "begin_ms": 236240,
            "end_ms": 238840,
        },
        {
            "speaker": "speaker_1",
            "speaker_id": "speaker_1",
            "text": "我是负责现场接待你的美学设计师张鑫诶",
            "begin_ms": 240750,
            "end_ms": 243880,
        },
        {
            "speaker": "speaker_1",
            "speaker_id": "speaker_1",
            "text": "我去给你找个充电宝",
            "begin_ms": 246130,
            "end_ms": 247690,
        },
    ]

    resolved = resolve_speaker_roles(
        utterances,
        staff_id="staff-zhangxin",
        staff_name="张鑫",
        staff_role="consultant",
        respect_speaker_diarization=True,
    )

    assert resolved[1]["speaker_business_role"] == "badge_owner"
    assert resolved[1]["speaker_identity_type"] == "staff"
    assert resolved[1]["speaker_display_label"] == "张鑫（工牌本人）"
    assert resolved[3]["speaker_role"] == "consultant"
    assert resolved[3]["speaker_role_source"] == "explicit_staff_intro"
    assert resolved[3]["speaker_identity_type"] == "staff"
    assert resolved[3]["speaker_business_role"] == "badge_owner"
    assert resolved[3]["speaker_staff_name"] == "张鑫"
    assert resolved[3]["speaker_display_label"] == "张鑫（工牌本人）"
    assert resolved[4]["speaker_role_source"] == "explicit_staff_intro_context"
    assert resolved[4]["speaker_business_role"] == "badge_owner"


def test_resolve_speaker_roles_does_not_treat_looking_for_staff_as_staff_intro() -> None:
    utterances = [
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "您好，今天想咨询什么项目呢？",
            "begin_ms": 0,
            "end_ms": 3000,
        },
        {
            "speaker": "speaker_1",
            "speaker_id": "speaker_1",
            "text": "我是来找张鑫老师咨询鼻子的，我之前打过玻尿酸。",
            "begin_ms": 4000,
            "end_ms": 9000,
        },
    ]

    resolved = resolve_speaker_roles(
        utterances,
        staff_id="staff-zhangxin",
        staff_name="张鑫",
        staff_role="consultant",
        respect_speaker_diarization=True,
    )

    assert resolved[1]["speaker_role_source"] != "explicit_staff_intro"
    assert resolved[1]["speaker_business_role"] == "primary_customer"


def test_resolve_speaker_roles_keeps_price_calculation_request_as_customer() -> None:
    utterances = [
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "我是今天接待您的李苏玲，先帮您看方案。",
            "begin_ms": 0,
            "end_ms": 3000,
        },
        {
            "speaker": "speaker_1",
            "speaker_id": "speaker_1",
            "text": "您帮我算一下这些多少钱，给我看一下，我考虑一下。",
            "begin_ms": 4000,
            "end_ms": 9000,
        },
    ]

    resolved = resolve_speaker_roles(
        utterances,
        staff_id="staff-lsl",
        staff_name="李苏玲",
        staff_role="consultant",
        respect_speaker_diarization=True,
    )

    assert resolved[1]["speaker_business_role"] == "primary_customer"
    assert resolved[1]["speaker_identity_type"] == "visitor"


def test_resolve_speaker_roles_overrides_direct_customer_treatment_history_self_report() -> None:
    utterances = [
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "我是负责现场接待你的美学设计师张鑫。",
            "begin_ms": 0,
            "end_ms": 3000,
        },
        {
            "speaker": "speaker_1",
            "speaker_id": "speaker_1",
            "text": "好看。",
            "begin_ms": 1369610,
            "end_ms": 1370410,
        },
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "我眉弓打过，我鼻子打过我嘴巴打过我下巴打过我耳朵打过我全脸都打过。",
            "begin_ms": 1386990,
            "end_ms": 1392190,
        },
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "然后我双眼皮也做过，但是我都是微调，没有动手术。",
            "begin_ms": 1392290,
            "end_ms": 1396440,
        },
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "前段时间我这个脸特别宽，然后打了瘦脸针之后，现在好很多了。",
            "begin_ms": 1413590,
            "end_ms": 1420790,
        },
        {
            "speaker": "speaker_0",
            "speaker_id": "speaker_0",
            "text": "我们医院麻醉体系比较严格，这个要先给医生评估。",
            "begin_ms": 1450000,
            "end_ms": 1455000,
        },
    ]

    resolved = resolve_speaker_roles(
        utterances,
        staff_id="staff-zhangxin",
        staff_name="张鑫",
        staff_role="consultant",
        respect_speaker_diarization=True,
    )

    assert resolved[2]["speaker_role_source"] == "explicit_customer_treatment_history"
    assert resolved[2]["speaker_business_role"] == "primary_customer"
    assert resolved[2]["speaker_display_label"] == "主客户"
    assert resolved[3]["speaker_role_source"] == "explicit_customer_treatment_history_context"
    assert resolved[3]["speaker_business_role"] == "primary_customer"
    assert resolved[4]["speaker_role_source"] == "explicit_customer_treatment_history"
    assert resolved[4]["speaker_business_role"] == "primary_customer"
    assert resolved[5]["speaker_business_role"] == "badge_owner"
