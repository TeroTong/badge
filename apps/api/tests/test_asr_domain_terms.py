from __future__ import annotations

from types import SimpleNamespace

from smart_badge_api.asr.domain_terms import (
    _HotwordEntry,
    _build_tencent_hotword_list_from_entries,
    _is_tencent_hotword_group_enabled,
    _normalize_tencent_hotword_weight,
    apply_medical_aesthetic_term_normalization,
    normalize_medical_aesthetic_text,
)


def test_normalize_tencent_hotword_weight_maps_internal_weights() -> None:
    assert _normalize_tencent_hotword_weight(1) == 1
    assert _normalize_tencent_hotword_weight(10) == 2
    assert _normalize_tencent_hotword_weight(55) == 7
    assert _normalize_tencent_hotword_weight(99) == 11
    assert _normalize_tencent_hotword_weight(100) == 100


def test_build_tencent_hotword_list_from_entries_deduplicates_and_limits_terms() -> None:
    hotword_list = _build_tencent_hotword_list_from_entries(
        [
            _HotwordEntry(term="热玛吉", weight=11, priority=1),
            _HotwordEntry(term="热玛吉", weight=10, priority=3),
            _HotwordEntry(term="超声炮", weight=10, priority=1),
            _HotwordEntry(term="濡白天使", weight=100, priority=0),
        ]
    )

    assert hotword_list == "濡白天使|100,热玛吉|11,超声炮|10"


def test_tencent_hotword_group_filter_keeps_projects_and_material_brands_only() -> None:
    assert _is_tencent_hotword_group_enabled(SimpleNamespace(group_type="project", name="项目通用热词", source_label="行业"))
    assert _is_tencent_hotword_group_enabled(SimpleNamespace(group_type="行业", name="材料品牌热词", source_label="行业"))
    assert not _is_tencent_hotword_group_enabled(SimpleNamespace(group_type="concern", name="常见顾虑热词", source_label="运营"))
    assert not _is_tencent_hotword_group_enabled(SimpleNamespace(group_type="通用", name="通用服务热词", source_label="运营"))
    assert not _is_tencent_hotword_group_enabled(SimpleNamespace(group_type="competitor", name="竞品机构热词", source_label="行业"))
    assert not _is_tencent_hotword_group_enabled(SimpleNamespace(group_type="industry", name="ASR自动挖词", source_label="ASR自动挖词"))


def test_normalize_medical_aesthetic_text_fixes_common_medical_terms() -> None:
    normalized, corrections = normalize_medical_aesthetic_text(
        "今天主要想了解热马吉、超声泡，还有如白天使和外去眼袋。"
    )

    assert normalized == "今天主要想了解热玛吉、超声炮，还有濡白天使和外切眼袋。"
    assert [item["to"] for item in corrections] == ["热玛吉", "超声炮", "濡白天使", "外切眼袋"]


def test_normalize_medical_aesthetic_text_fixes_body_contouring_terms() -> None:
    normalized, corrections = normalize_medical_aesthetic_text(
        "我主要想做腰腹锡纸、手臂锡脂，再看看腹部环西和马甲现。"
    )

    assert normalized == "我主要想做腰腹吸脂、手臂吸脂，再看看腹部环吸和马甲线。"
    assert [item["to"] for item in corrections] == ["马甲线", "腰腹吸脂", "手臂吸脂", "腹部环吸"]


def test_normalize_medical_aesthetic_text_fixes_eye_and_nose_terms() -> None:
    normalized, corrections = normalize_medical_aesthetic_text(
        "我之前做过外去眼代，这次想看看框隔脂肪释放，还有棚体鼻和耳卵骨。"
    )

    assert normalized == "我之前做过外切眼袋，这次想看看眶隔脂肪释放，还有膨体鼻和耳软骨。"
    targets = {item["to"] for item in corrections}
    assert {"眶隔", "膨体", "耳软骨", "外切眼袋"} <= targets


def test_normalize_medical_aesthetic_text_fixes_recovery_and_ptosis_terms() -> None:
    normalized, corrections = normalize_medical_aesthetic_text(
        "这个位置会先加咳，另外她是轻度体积，还有一点体肌问题。"
    )

    assert normalized == "这个位置会先结痂，另外她是轻度提肌，还有一点提肌问题。"
    targets = {item["to"] for item in corrections}
    assert {"结痂", "轻度提肌", "提肌"} <= targets


def test_normalize_medical_aesthetic_text_fixes_real_world_brand_and_device_terms() -> None:
    normalized, corrections = normalize_medical_aesthetic_text(
        "我想了解黄金位置、光子乘复、外戚眼袋，还有腰腹环膝、乐体宝、瑞德仪、菲林浮利和润no v。"
    )

    assert normalized == "我想了解黄金微针、光子嫩肤、外切眼袋，还有腰腹环吸、乐提葆、瑞德喜、菲林普利和润诺威。"
    targets = {item["to"] for item in corrections}
    assert {"黄金微针", "光子嫩肤", "外切眼袋", "腰腹环吸", "乐提葆", "瑞德喜", "菲林普利", "润诺威"} <= targets


def test_normalize_medical_aesthetic_text_fixes_recent_project_and_brand_errors() -> None:
    normalized, corrections = normalize_medical_aesthetic_text(
        "客户想了解英文大提升和英轮大提升，也提到了宝妥市、宝头市、乐奇宝、乐体堡和乐体乐提葆。"
    )

    assert normalized == "客户想了解英伦大提升和英伦大提升，也提到了保妥适、保妥适、乐提葆、乐提葆和乐提葆。"
    assert [item["to"] for item in corrections] == [
        "英伦大提升",
        "英伦大提升",
        "保妥适",
        "保妥适",
        "乐提葆",
        "乐提葆",
        "乐提葆",
    ]


def test_apply_medical_aesthetic_term_normalization_preserves_original_text() -> None:
    utterances = [
        {"text": "我之前打过玻料酸和海体。", "speaker": "speaker_0"},
        {"text": "这句不需要改。", "speaker": "speaker_1"},
    ]

    normalized, correction_count = apply_medical_aesthetic_term_normalization(utterances)

    assert correction_count == 2
    assert normalized[0]["text"] == "我之前打过玻尿酸和嗨体。"
    assert normalized[0]["text_original"] == "我之前打过玻料酸和海体。"
    assert len(normalized[0]["term_corrections"]) == 2
    assert "text_original" not in normalized[1]
    assert utterances[0]["text"] == "我之前打过玻料酸和海体。"
