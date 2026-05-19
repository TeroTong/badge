from smart_badge_api.analysis.agent_pipeline import _agent_budget_fact_from_item
from smart_badge_api.analysis.staged_pipeline import (
    _budget_value_from_text,
    _build_consumption_intent,
    _is_explicit_budget_text,
)


def test_effect_explanation_is_not_budget():
    bad = "[02:52] 这个吃不哎，七块解决不了多少，能改善的程度有限。"

    assert _is_explicit_budget_text(bad) is False
    assert _build_consumption_intent({"budget_facts": [{"content": bad, "evidence": [bad]}]}) == {
        "budget": None,
        "decision_factors": [],
        "evidence": [bad],
    }
    assert _agent_budget_fact_from_item({"content": bad, "quote": bad}, source_id="test") is None


def test_explicit_customer_budget_is_preserved():
    budget = "客户明确表示本次预算最多三万，可以接受。"

    result = _build_consumption_intent({"budget_facts": [{"content": budget, "evidence": [budget]}]})

    assert _is_explicit_budget_text(budget) is True
    assert result["budget"] == budget
    assert result["decision_factors"] == [budget]


def test_implicit_budget_pressure_is_rendered_as_unclear_but_price_sensitive():
    pressure = "对总价约29000-30000元较为敏感并反复核算"
    expected = "未明确；对总价约29000-30000元较敏感，倾向希望低于该区间"

    result = _build_consumption_intent({"budget_facts": [{"content": pressure, "evidence": [pressure]}]})
    agent_fact = _agent_budget_fact_from_item({"content": pressure, "quote": pressure}, source_id="test")

    assert _budget_value_from_text(pressure) == expected
    assert result["budget"] == expected
    assert result["decision_factors"] == [pressure]
    assert agent_fact is not None
