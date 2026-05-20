from smart_badge_api.analysis.agent_pipeline import (
    _EVIDENCE_AGENT_CHUNK_USER_TEMPLATE,
    _EVIDENCE_AGENT_SYSTEM_PROMPT,
    _EVIDENCE_AGENT_USER_TEMPLATE,
    _normalize_evidence_graph_demands,
)


def test_evidence_prompt_is_chinese_and_evidence_only() -> None:
    assert "证据抽取 Agent" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "不要判断最终 SAP 适应症" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "不要生成最终分析结果" in _EVIDENCE_AGENT_SYSTEM_PROMPT


def test_evidence_prompt_keeps_participants_separate() -> None:
    assert "参与者必须隔离" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "主咨询客户、同行客户A/B、陪同人员" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "不要把一个人的主诉、顾虑、预算、方案、既往史、标签或成交状态合并到另一个人身上" in _EVIDENCE_AGENT_SYSTEM_PROMPT


def test_evidence_prompt_classification_boundaries() -> None:
    assert "不要把担心、价格、流程、项目选择、设计偏好或治疗顺序本身当主诉" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "担心疼、怕风险、问价格、问流程、选择先做某项目、今天想做光子" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "自然一点/夸张一点/小平扇/外开扇/宽窄" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "先把鼻子调好/第一步先做某部位" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "首先，一定要鼻子调好，这是第一步" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "若已有“缩小鼻头、缩窄鼻翼、改善鼻部结构”等目标型主诉" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "主诉以“问题/目标”为中心，不以“项目/产品/成交动作”为中心" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "先做光子、今天做光子、想了解嗨体/水光/某产品" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "如果只是询问水光/光电/某产品且没有说明问题或目标" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "购买、开单、核销、已买几支、今天打一支、安排某产品/某项目" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "买了一支瑞丽/安排面中" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "客户明确提到但本次不处理、转科、下次再做的“问题/目标”也要保留" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "若 customer_response 中出现安全、风险、副作用" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "明确否定或接受的表达不是顾虑" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "只有客户对价格作出承受度反应时才进 budget_evidence" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "客户普通询价、询问价格差异" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "否定史" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "不要把流程问题当主诉" in _EVIDENCE_AGENT_SYSTEM_PROMPT


def test_evidence_prompt_keeps_structural_and_skin_boundaries() -> None:
    assert "副乳、富贵包、手臂、后背、腰腹" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "松弛/紧致/抗衰要和毛孔、痘印、暗沉" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "不能在没有明确鼻部轮廓/手术/注射方案时推成鼻综合" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "不要只写成泛化产品名" in _EVIDENCE_AGENT_SYSTEM_PROMPT


def test_evidence_prompt_blocks_auxiliary_care_as_plan() -> None:
    assert "医用面膜" in _EVIDENCE_AGENT_SYSTEM_PROMPT
    assert "不要单独抽成 recommendation_evidence" in _EVIDENCE_AGENT_SYSTEM_PROMPT


def test_evidence_user_prompts_are_chinese() -> None:
    assert "员工 / 录音上下文" in _EVIDENCE_AGENT_USER_TEMPLATE
    assert "只输出 evidence_graph JSON" in _EVIDENCE_AGENT_USER_TEMPLATE
    assert "这是转写分块" in _EVIDENCE_AGENT_CHUNK_USER_TEMPLATE
    assert "只输出 evidence_graph JSON" in _EVIDENCE_AGENT_CHUNK_USER_TEMPLATE


def test_evidence_graph_normalizer_removes_priority_only_duplicate_demand() -> None:
    graph = {
        "customer_demand_evidence": [
            {
                "id": "E_D1",
                "content": "希望通过结构调整缩小鼻头、缩窄鼻翼",
                "participant": "主咨询客户",
                "participant_scope": "primary_customer",
                "quote": "才能把鼻头缩小，把鼻子缩窄",
            },
            {
                "id": "E_D2",
                "content": "希望把鼻子调好，作为优先改善部位",
                "participant": "主咨询客户",
                "participant_scope": "primary_customer",
                "quote": "首先，一定要鼻子调好，这是第一步。",
            },
        ]
    }

    normalized = _normalize_evidence_graph_demands(graph)

    assert [item["id"] for item in normalized["customer_demand_evidence"]] == ["E_D1"]


def test_evidence_graph_normalizer_keeps_priority_demand_when_no_specific_goal_exists() -> None:
    graph = {
        "customer_demand_evidence": [
            {
                "id": "E_D1",
                "content": "希望把鼻子调好，作为优先改善部位",
                "participant": "主咨询客户",
                "participant_scope": "primary_customer",
                "quote": "首先，一定要鼻子调好，这是第一步。",
            }
        ]
    }

    normalized = _normalize_evidence_graph_demands(graph)

    assert [item["id"] for item in normalized["customer_demand_evidence"]] == ["E_D1"]
