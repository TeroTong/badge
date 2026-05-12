from smart_badge_api.analysis import extraction_prompts, pipeline
from smart_badge_api.analysis.reference_data import load_analysis_reference_data


def test_analysis_prompt_uses_compact_general_guardrails() -> None:
    prompt = extraction_prompts.SYSTEM_PROMPT_TEMPLATE

    assert len(prompt) <= 8_000
    assert "staff_seed_recommendations" in prompt
    assert "正确率优先" in prompt
    assert "宁可漏掉弱线索" in prompt
    assert "不能写“治疗条件限制/时间到院限制/流程限制”这类大类" in prompt
    assert "录音有效内容少 + 确认为医美业务场景" in prompt
    assert "当前意图优先于历史提及" in prompt
    assert "过滤否定、比较、风格边界、假设和效果范围" in prompt
    assert "体质和风险不是项目需求" in prompt
    assert "鼻基底/鼻底/面中/苹果肌/八字纹" in prompt
    assert "是否解决当前主诉" in prompt
    assert "staff_recommendations 只写" in prompt
    assert "staff_seed_recommendations 只写" in prompt


def test_built_prompt_and_merge_prompt_stay_under_length_budget() -> None:
    reference = load_analysis_reference_data()
    built = extraction_prompts.SYSTEM_PROMPT_TEMPLATE.format(
        feature_objectives=reference.feature_objectives,
        indication_reference=reference.indication_reference,
        tag_categories="-",
        hotword_reference="-",
    )
    assert len(built) <= 11_500
    assert len(reference.indication_reference) <= 4_000

    prompt = pipeline._MERGE_SYSTEM_PROMPT

    assert len(prompt) <= 1_500
    assert "既往治疗后出现当前残留" in prompt
    assert "鼻基底/面中/苹果肌/八字纹" in prompt
    assert "正确率优先" in prompt
    assert "有效内容少 + 确认为医美业务场景" in prompt
    assert "效果范围、比较、假设、案例数字" in prompt


def test_recommendation_source_does_not_treat_customer_price_request_as_staff_plan() -> None:
    segments = [
        {
            "speaker_role": "customer",
            "speaker_business_role": "primary_customer",
            "speaker_display_label": "主客户",
            "text": "您帮我算一下这些多少钱，给我看一下，我考虑一下。",
        }
    ]

    assert not pipeline._segment_can_source_staff_recommendation(segments[0], segments)


def test_recommendation_source_allows_staff_explanation_even_if_labeled_customer() -> None:
    segments = [
        {
            "speaker_role": "customer",
            "speaker_business_role": "primary_customer",
            "speaker_display_label": "主客户",
            "text": "我们这边建议你继续打一次吉适肉毒，主要改善咬肌和脸型。",
        }
    ]

    assert pipeline._segment_can_source_staff_recommendation(segments[0], segments)


def test_seed_recommendations_are_split_from_current_complaint_recommendations() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {
                    "priority": 1,
                    "demand": "山根偏低，希望鼻型更立体",
                    "body_part": "鼻部",
                    "evidence": "[00:10] 我主要想看鼻子山根。",
                }
            ]
        },
        "staff_recommendations": {
            "items": [
                {
                    "recommendation": "鼻综合塑形改善山根高度",
                    "product_or_solution": "鼻综合",
                    "body_part": "鼻部",
                    "evidence": "[00:20] 建议你先做鼻综合，把山根垫起来。",
                    "demand_priority": [],
                },
                {
                    "recommendation": "后期可以做水光维护皮肤状态",
                    "product_or_solution": "水光针",
                    "body_part": "面部",
                    "evidence": "[00:30] 你还有一点皮肤干，后期也可以做水光维护。",
                    "demand_priority": [1],
                },
            ]
        },
        "staff_seed_recommendations": {"items": []},
    }

    assert pipeline._split_seed_recommendations_from_staff_recommendations(result)
    recommendation_items = result["staff_recommendations"]["items"]
    seed_items = result["staff_seed_recommendations"]["items"]

    assert [item["recommendation"] for item in recommendation_items] == ["鼻综合塑形改善山根高度"]
    assert recommendation_items[0]["demand_priority"] == [1]
    assert [item["recommendation"] for item in seed_items] == ["后期可以做水光维护皮肤状态"]
    assert seed_items[0]["demand_priority"] == []


def test_current_complaint_plan_misfiled_as_seed_is_promoted_back() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {
                    "priority": 1,
                    "demand": "泪沟明显，希望改善眼周疲态",
                    "body_part": "眼部",
                    "evidence": "[00:10] 我主要想改善泪沟。",
                }
            ]
        },
        "staff_recommendations": {"items": []},
        "staff_seed_recommendations": {
            "items": [
                {
                    "recommendation": "胶原蛋白填充泪沟",
                    "product_or_solution": "胶原蛋白",
                    "body_part": "眼部",
                    "evidence": "[00:20] 针对你这个泪沟，可以用胶原蛋白来填。",
                    "demand_priority": [],
                }
            ]
        },
    }

    assert pipeline._split_seed_recommendations_from_staff_recommendations(result)
    assert [item["recommendation"] for item in result["staff_recommendations"]["items"]] == ["胶原蛋白填充泪沟"]
    assert result["staff_recommendations"]["items"][0]["demand_priority"] == [1]
    assert result["staff_seed_recommendations"]["items"] == []


def test_decision_factor_generic_labels_are_rewritten_to_specific_facts() -> None:
    factors = ["时间/到院限制", "治疗条件限制", "支付/流程限制", "特殊身份"]
    evidence_texts = [
        "[00:10] 我今天赶时间，过几天要回去，高铁票已经买了。",
        "[00:20] 她现在备孕，医生说暂时不能打。",
        "[00:30] 刷卡付不了，扫码也失败。",
        "[00:40] 客户说自己在医美上班，是同行。",
    ]

    filtered = pipeline._filter_overlapping_decision_factors(
        factors,
        concern_texts=[],
        evidence_texts=evidence_texts,
        loss_reasons=[],
    )

    assert filtered == [
        "客户赶时间，治疗安排受限",
        "客户备孕，治疗条件受限",
        "支付或扫码失败影响下单",
        "客户疑似竞对或同行身份，接待需谨慎",
    ]
    assert "时间/到院限制" not in filtered
    assert "治疗条件限制" not in filtered
