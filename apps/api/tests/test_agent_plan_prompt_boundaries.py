from smart_badge_api.analysis.agent_pipeline import (
    _PLAN_AGENT_SYSTEM_PROMPT,
    _PLAN_AGENT_USER_TEMPLATE,
    _agent_preserve_deferred_seed_recommendations,
    _apply_event_graph_constraints,
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
    assert "不要为了显得专业而改写成营销式或论文式标题" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "中面部/内侧苹果肌先做一支瑞德喜支撑" in _PLAN_AGENT_SYSTEM_PROMPT


def test_plan_prompt_avoids_over_specific_body_part_exceptions() -> None:
    assert "不要为某个部位写死例外规则" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "结构支撑、注射填充、光电、皮肤管理、手术等不同项目按同一原则裁决" in _PLAN_AGENT_SYSTEM_PROMPT
    assert "下颌角拐点 structural support" not in _PLAN_AGENT_SYSTEM_PROMPT
    assert "current nasal-axis structural recommendations" not in _PLAN_AGENT_SYSTEM_PROMPT


def test_plan_adjudication_prevents_old_deferred_seed_from_being_reintroduced() -> None:
    fact_graph = {
        "recommendations": [],
        "seed_recommendations": [{"content": "隐痕精雕眼周收紧"}],
        "_recommendation_adjudication": {"notes": ["plan agent already adjudicated"]},
    }
    evidence_graph = {
        "recommendation_evidence": [
            {
                "content": "玻尿酸或胶原填充泪沟作为后期补充方案",
                "implementation_notes": "后期仍有凹陷可考虑再做",
                "participant_scope": "primary_customer",
            }
        ]
    }
    repaired = _agent_preserve_deferred_seed_recommendations(fact_graph, evidence_graph)
    assert [item["content"] for item in repaired["seed_recommendations"]] == ["隐痕精雕眼周收紧"]


def test_plan_adjudicated_recommendations_are_not_reclassified_by_event_constraints() -> None:
    fact_graph = {
        "recommendations": [
            {
                "id": "R1",
                "content": "眶外C线注射调整作为第一步优化脸型",
                "body_part": "眶外C线",
            }
        ],
        "seed_recommendations": [],
        "_recommendation_adjudication": {"notes": ["plan agent kept this as current recommendation"]},
    }
    event_graph = {
        "plan_events": [
            {
                "id": "EV_P1",
                "event_type": "seed_recommendation",
                "plan": "眶外C线注射调整",
                "body_part": "眶外C线",
            }
        ]
    }
    constrained = _apply_event_graph_constraints(fact_graph, event_graph)
    assert constrained["recommendations"] == fact_graph["recommendations"]
    assert constrained["seed_recommendations"] == []
