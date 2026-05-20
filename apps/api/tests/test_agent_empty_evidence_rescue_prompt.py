from smart_badge_api.analysis.agent_pipeline import (
    _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT,
    _EMPTY_EVIDENCE_RESCUE_USER_TEMPLATE,
    _normalize_rescue_evidence_graph,
)


def test_empty_evidence_rescue_prompt_is_chinese_and_preserves_schema() -> None:
    assert "空证据兜底 Agent" in _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    assert "scene_assessment" in _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    assert "active_consultation | internal_staff_chat | frontdesk_order | third_party_case_discussion | casual_chat | unclear" in (
        _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    )
    assert "customer_demand_evidence" in _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    assert "recommendation_evidence" in _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    assert "员工 / 录音上下文" in _EMPTY_EVIDENCE_RESCUE_USER_TEMPLATE
    assert "只输出 rescue JSON" in _EMPTY_EVIDENCE_RESCUE_USER_TEMPLATE


def test_empty_evidence_rescue_prompt_defines_non_consultation_boundaries() -> None:
    assert "当前顾客面诊必须满足" in _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    assert "非当前顾客面诊时，所有 evidence_graph 列表必须保持为空" in _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    assert "内部员工闲聊、前台订单处理、同事抱怨、缺席第三方" in _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    assert "我有个顾客/那个顾客/有个美团的/他问我/她说/医生说/未成交" in (
        _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    )


def test_empty_evidence_rescue_prompt_requires_high_precision_rescue() -> None:
    assert "高精度兜底" in _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    assert "只抽取原文直接支持的证据" in _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    assert "不要为了“补齐字段”而补全" in _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    assert "严格区分当前顾客、同行客户、陪同人员、员工自述和其他客户案例" in (
        _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    )


def test_empty_evidence_rescue_prompt_uses_exact_evidence_item_schema() -> None:
    assert "不要自造 demand_summary、plan_summary、speaker_role" in _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    assert '"content": ""' in _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    assert '"participant_scope": "primary_customer|other_customer|companion_or_family|unknown"' in (
        _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    )
    assert '"relation_to_current_demand": "current_main_plan|possible_current_plan|planting_or_later|alternative_not_recommended|auxiliary_or_care|not_current_or_referral|unclear"' in (
        _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    )
    assert "员工单纯报价、算价或解释优惠不算预算证据" in _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    assert "“去看看方案/价格”“继续面诊”这类下一步沟通不等于成交或开单" in (
        _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    )
    assert "员工/医生用“要不要、还要不要、需不需要、是不是要”提出的疑问或建议，不等于客户主诉" in (
        _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT
    )


def test_empty_evidence_rescue_normalizer_drops_next_step_as_deal() -> None:
    graph = {
        "deal_evidence": [
            {"content": "表示去看看方案和价格，尚未成交", "quote": "那我们就去看看方案和价格。"},
            {"content": "已付款开单", "quote": "已经付款开单了。"},
        ]
    }

    normalized = _normalize_rescue_evidence_graph(graph)

    assert [item["content"] for item in normalized["deal_evidence"]] == ["已付款开单"]


def test_empty_evidence_rescue_normalizer_drops_non_history_product_mentions() -> None:
    graph = {
        "medical_history_evidence": [
            {"content": "欧文是350，不能打", "quote": "欧文是350，那就俄文不能打。"},
            {"content": "既往中耳炎手术导致面神经受损", "quote": "中耳炎就做过手术导致了面神经受损。"},
        ]
    }

    normalized = _normalize_rescue_evidence_graph(graph)

    assert [item["content"] for item in normalized["medical_history_evidence"]] == [
        "既往中耳炎手术导致面神经受损"
    ]
