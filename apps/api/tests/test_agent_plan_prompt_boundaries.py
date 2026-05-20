from smart_badge_api.analysis.agent_pipeline import (
    _PLAN_AGENT_SYSTEM_PROMPT,
    _PLAN_AGENT_USER_TEMPLATE,
)


def test_plan_prompt_is_chinese_and_scoped_to_plan_adjudication() -> None:
    assert "Plan adjudication / 推荐方案与种草方案裁决 Agent" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "不要选择 SAP 适应症" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "不要修改主诉、客户标签、预算、顾虑或成交结论" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "不要生成最终分析文案或 SAP 咨询备注" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "当前 fact_graph:" in _PLAN_AGENT_USER_TEMPLATE
    assert "证据图:" in _PLAN_AGENT_USER_TEMPLATE
    assert "事件图:" in _PLAN_AGENT_USER_TEMPLATE
    assert "只输出 recommendation_adjudication JSON" in _PLAN_AGENT_USER_TEMPLATE


def test_plan_prompt_uses_event_polarity_before_reclassification() -> None:
    assert "先看 event_graph 的事件极性" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "current_recommendation、deal_confirmed 支持放入 recommendations" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "seed_recommendation、deferred_demand、referral_or_deferred 支持放入 seed_recommendations" in (
        _PLAN_AGENT_SYSTEM_PROMPT
    )
    assert "customer_question、staff_explanation、comparison_or_backup、diagnosis_only、not_recommended" in (
        _PLAN_AGENT_SYSTEM_PROMPT
    )
    assert "不能单独进入 recommendations" in _PLAN_AGENT_SYSTEM_PROMPT


def test_plan_prompt_defines_recommendation_seed_and_rejected_boundaries() -> None:
    assert "recommendations = 本次围绕当前主诉/诊断" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "seed_recommendations = 当前主诉之外、可选、低优先级、维养、下次/后续/转科/暂缓" in (
        _PLAN_AGENT_SYSTEM_PROMPT
    )
    assert "单纯比较或科普、客户随口询问、明确不建议/不适合" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "术前检查、术后用药、疤痕膏" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "排期、开单/付款/核销、护理注意事项" in _PLAN_AGENT_SYSTEM_PROMPT


def test_plan_prompt_preserves_details_and_participant_boundaries() -> None:
    assert "分阶段治疗的判断看目的" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "参与者必须隔离" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "不要把同行客户自己的方案" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "brand、material、dosage、price、course_or_frequency" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "customer_response、related_demand_ids、evidence_ids" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "必须使用 content 字段" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "不要用 plan、plan_summary、title" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "备选、对比、替代材料放入" in _PLAN_AGENT_SYSTEM_PROMPT


def test_plan_prompt_avoids_over_specific_body_part_exceptions() -> None:
    assert "不要为某个部位写死例外规则" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "结构支撑、注射填充、光电、皮肤管理、手术等不同项目按同一原则裁决" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "下颌角拐点 structural support" not in _PLAN_AGENT_SYSTEM_PROMPT
    assert "current nasal-axis structural recommendations" not in _PLAN_AGENT_SYSTEM_PROMPT
