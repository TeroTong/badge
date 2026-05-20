from smart_badge_api.analysis.agent_pipeline import _SCOPE_AGENT_SYSTEM_PROMPT


def test_scope_prompt_keeps_scheduling_and_preop_flow_decisions() -> None:
    assert "是否能当天做" in _SCOPE_AGENT_SYSTEM_PROMPT
    assert "检验科是否下班" in _SCOPE_AGENT_SYSTEM_PROMPT
    assert "等待医生/等待检查/转场/签字/术前流程" in _SCOPE_AGENT_SYSTEM_PROMPT


def test_scope_prompt_keeps_mixed_casual_business_and_health_screening() -> None:
    assert "有效业务信息高于闲聊外壳" in _SCOPE_AGENT_SYSTEM_PROMPT
    assert "有没有感冒" in _SCOPE_AGENT_SYSTEM_PROMPT
    assert "有没有暴晒" in _SCOPE_AGENT_SYSTEM_PROMPT
    assert "身体各方面还好" in _SCOPE_AGENT_SYSTEM_PROMPT
    assert "项目建议/报价/禁忌筛查" in _SCOPE_AGENT_SYSTEM_PROMPT
    assert "为什么量这些数据" in _SCOPE_AGENT_SYSTEM_PROMPT
    assert "舒敏之星299" in _SCOPE_AGENT_SYSTEM_PROMPT


def test_scope_prompt_keeps_current_customer_handoff_to_colleagues() -> None:
    assert "员工对医生/同事转述当前客户情况" in _SCOPE_AGENT_SYSTEM_PROMPT
    assert "前两天晒了" in _SCOPE_AGENT_SYSTEM_PROMPT
    assert "皮肤检测做不了" in _SCOPE_AGENT_SYSTEM_PROMPT
