from smart_badge_api.analysis.agent_pipeline import (
    _EVENT_AGENT_SYSTEM_PROMPT,
    _EVENT_AGENT_USER_TEMPLATE,
    _normalize_event_graph,
)


def test_event_prompt_is_chinese_and_event_only() -> None:
    assert "事件图抽取 Agent" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "不要生成最终分析结果" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "不要选择最终 SAP 适应症" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "只输出 event_graph JSON" in _EVENT_AGENT_USER_TEMPLATE


def test_event_prompt_explains_polarity_purpose() -> None:
    assert "evidence_graph 表示“提到了什么”" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "event_graph 表示“这句话在面诊中起什么作用”" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "避免把客户随口提问、员工科普、备选比较、不适合或被拒绝的方案误当成最终推荐方案" in _EVENT_AGENT_SYSTEM_PROMPT


def test_event_prompt_keeps_participants_and_evidence_ids() -> None:
    assert "主咨询客户、同行客户、陪同人员不能串人" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "source_evidence_ids" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "evidence_turn_ids" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "related_demand" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "事件图不是二次证据抽取" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "不要从 recommendation_evidence 的方案描述里反推新的 demand_events" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "diagnosis_only 只能来自 diagnosis_evidence" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "other_customer 只用于现场同行客户正在咨询自己的项目" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "缺席第三方" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "不要生成 demand_events 或 plan_events" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "自然一点/夸张一点/小平扇/外开扇/宽窄/款式/风格" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "不要生成 demand_event" in _EVENT_AGENT_SYSTEM_PROMPT


def test_event_prompt_plan_polarity_boundaries() -> None:
    assert "current_recommendation" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "seed_recommendation" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "comparison_or_backup" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "not_recommended" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "staff_explanation" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "customer_question" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "若员工说“先做核心项目，整体设计/其他部位以后再做”" in _EVENT_AGENT_SYSTEM_PROMPT


def test_event_prompt_profile_and_budget_boundaries() -> None:
    assert "员工自述、产品背景、第三方案例、其他客户情况" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "不能作为当前客户标签" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "员工单纯报价、科普价格、解释优惠不是 budget_event" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "价格敏感、还价或支付压力" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "客户普通询价、询问价格差异" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "discount_request" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "payment_pressure" in _EVENT_AGENT_SYSTEM_PROMPT


def test_event_prompt_deal_boundary_requires_transaction_action() -> None:
    assert "带客户去医生/外科/皮肤科继续面诊" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "不等于 order_created 或 deal_confirmed" in _EVENT_AGENT_SYSTEM_PROMPT
    assert "开单、下单、付款、定金、核销、成交确认" in _EVENT_AGENT_SYSTEM_PROMPT


def test_event_graph_normalizer_removes_style_preference_demand() -> None:
    graph = {
        "demand_events": [
            {"id": "EV_D1", "event_type": "current_demand", "content": "想做双眼皮手术"},
            {"id": "EV_D2", "event_type": "current_demand", "content": "希望双眼皮风格偏自然、小平扇"},
            {"id": "EV_D3", "event_type": "current_demand", "content": "希望改善大小眼，风格自然一点"},
        ]
    }

    normalized = _normalize_event_graph(graph)

    assert [item["id"] for item in normalized["demand_events"]] == ["EV_D1", "EV_D3"]


def test_event_graph_normalizer_fills_missing_budget_event_type() -> None:
    graph = {"budget_events": [{"id": "EV_B1", "content": "询问是否还能优惠"}]}

    normalized = _normalize_event_graph(graph)

    assert normalized["budget_events"][0]["event_type"] == "unclear"
