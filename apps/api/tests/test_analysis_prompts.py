from smart_badge_api.analysis import extraction_prompts, pipeline
from smart_badge_api.analysis.reference_data import load_analysis_reference_data


def test_analysis_prompt_uses_compact_general_guardrails() -> None:
    prompt = extraction_prompts.SYSTEM_PROMPT_TEMPLATE

    assert len(prompt) <= 6_000
    assert "正确率优先" in prompt
    assert "宁可漏掉弱线索" in prompt
    assert "录音有效内容少 + 确认为医美业务场景" in prompt
    assert "当前意图优先于历史提及" in prompt
    assert "过滤否定、比较、风格边界、假设和效果范围" in prompt
    assert "体质和风险不是项目需求" in prompt
    assert "鼻基底/鼻底/面中/苹果肌/八字纹" in prompt
    assert "“用X填充/注射Y改善Z”属于有效推荐" in prompt


def test_built_prompt_and_merge_prompt_stay_under_length_budget() -> None:
    reference = load_analysis_reference_data()
    built = extraction_prompts.SYSTEM_PROMPT_TEMPLATE.format(
        feature_objectives=reference.feature_objectives,
        indication_reference=reference.indication_reference,
        tag_categories="-",
        hotword_reference="-",
    )
    assert len(built) <= 10_000
    assert len(reference.indication_reference) <= 4_000

    prompt = pipeline._MERGE_SYSTEM_PROMPT

    assert len(prompt) <= 1_500
    assert "既往治疗后出现当前残留" in prompt
    assert "鼻基底/面中/苹果肌/八字纹" in prompt
    assert "正确率优先" in prompt
    assert "有效内容少 + 确认为医美业务场景" in prompt
    assert "效果范围、比较、假设、案例数字" in prompt
