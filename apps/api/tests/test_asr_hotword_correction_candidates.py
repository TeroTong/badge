from smart_badge_api.analysis.hotword_correction import select_asr_correction_hotword_candidates
from smart_badge_api.analysis.staged_pipeline import _build_preprocess_context


def test_selects_configured_hotword_alias_candidate_without_full_hotword_prompt() -> None:
    dialogue = "鼻基底不要打玻尿酸，会吸水馒化。这里首选一支read的1，不含玻尿酸。"
    hotwords = [
        {
            "term": "瑞德喜",
            "weight": 90,
            "group_name": "材料品牌热词",
            "group_type": "品牌",
        },
        {
            "term": "无关品牌",
            "weight": 90,
            "group_name": "材料品牌热词",
            "group_type": "品牌",
        },
    ]

    candidates = select_asr_correction_hotword_candidates(dialogue, hotwords, max_candidates=8)

    assert candidates[0]["term"] == "瑞德喜"
    assert candidates[0]["reason"] == "known_alias_from_configured_hotword"
    assert "read的1" in candidates[0]["matched_fragments"]


def test_excludes_auto_mined_hotwords_from_agent_correction_candidates() -> None:
    dialogue = "客户咨询鼻基底注射支撑。"
    hotwords = [
        {
            "term": "噪声词",
            "weight": 100,
            "group_name": "ASR自动挖词候选",
            "group_type": "品牌",
        },
        {
            "term": "鼻基底",
            "weight": 80,
            "group_name": "部位热词",
            "group_type": "部位",
        },
    ]

    candidates = select_asr_correction_hotword_candidates(dialogue, hotwords, max_candidates=8)

    terms = [item["term"] for item in candidates]
    assert "鼻基底" in terms
    assert "噪声词" not in terms


def test_preprocess_context_includes_compact_hotword_candidates() -> None:
    context = _build_preprocess_context(
        "鼻基底这里不能打玻尿酸，会馒化，首选一支瑞一。",
        {
            "staff_name": "李宇晴",
            "asr_correction_hotwords": [
                {
                    "term": "瑞德喜",
                    "weight": 90,
                    "group_name": "材料品牌热词",
                    "group_type": "品牌",
                }
            ],
        },
    )

    candidates = context["asr_hotword_correction_candidates"]
    assert candidates
    assert candidates[0]["term"] == "瑞德喜"
    assert "asr_hotword_correction_note" in context
