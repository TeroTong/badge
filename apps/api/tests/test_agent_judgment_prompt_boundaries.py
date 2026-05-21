from smart_badge_api.analysis.agent_pipeline import (
    _JUDGMENT_AGENT_SYSTEM_PROMPT,
    _JUDGMENT_AGENT_USER_TEMPLATE,
    _agent_ensure_common_indications,
    _agent_finalize_analysis_result,
    _agent_filter_non_deal_factors,
    _agent_has_jawline_support_context,
    _agent_normalize_non_deal_outcome,
    _agent_normalize_demands,
    _agent_participant_key,
)


def test_judgment_prompt_is_chinese_and_preserves_fact_graph_schema() -> None:
    assert "Judgment / 事实图生成 Agent" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "不要写最终分析文案" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "不要写 SAP 咨询备注" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert '"fact_graph"' in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert '"demands": []' in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert '"indication_candidates": []' in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "证据图:" in _JUDGMENT_AGENT_USER_TEMPLATE
    assert "事件图:" in _JUDGMENT_AGENT_USER_TEMPLATE
    assert "只输出 fact_graph JSON" in _JUDGMENT_AGENT_USER_TEMPLATE


def test_judgment_prompt_uses_event_polarity_and_participant_boundaries() -> None:
    assert "event_graph 的事件极性优先" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "current_recommendation、deal_confirmed 支持 recommendations" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "customer_question、staff_explanation、comparison_or_backup、diagnosis_only、not_recommended" in (
        _JUDGMENT_AGENT_SYSTEM_PROMPT
    )
    assert "参与者必须隔离" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "不要把同行客户自己的需求合并到主咨询客户" in _JUDGMENT_AGENT_SYSTEM_PROMPT


def test_judgment_prompt_keeps_fact_categories_distinct() -> None:
    assert "demands 只保留当前客户明确想解决的问题或审美目标" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "doctor_diagnoses 保留医生/咨询师对当前客户的观察" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "recommendations 是为解决当前 demands 的当前方案" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "seed_recommendations 是额外种草、维养、低优先级、下次/转科/可延后" in (
        _JUDGMENT_AGENT_SYSTEM_PROMPT
    )
    assert "recommendation implementation_notes" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "concerns 和 deal_factors 必须具体" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "budget_evidence 全部转换为 budget_facts" in _JUDGMENT_AGENT_SYSTEM_PROMPT


def test_judgment_prompt_profile_and_indication_boundaries() -> None:
    assert "既往治疗/材料/仪器标签必须有正向既往史证据" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "员工/医生自述、产品描述、其他客户案例" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "“皮肤敏感/敏感肌/玫瑰痤疮”不等于“过敏史”" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "只能复制 candidate_indications 中已经给出的" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "宁可少选，不要错选" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "仅种草、备选、比较、员工科普、客户随口问" in _JUDGMENT_AGENT_SYSTEM_PROMPT


def test_judgment_prompt_specific_medical_aesthetic_boundaries() -> None:
    assert "副乳有明确诉求/方案时优先选择具体“副乳整形”" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "“闭口时/闭上嘴”等口部动作不能映射为痤疮" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "咬肌肉毒/瘦脸不能映射为面部除皱" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "塑美（鼻中轴线（H））" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "塑美（下颌轮廓线（大O））" in _JUDGMENT_AGENT_SYSTEM_PROMPT
    assert "泪沟、黑眼圈、法令纹" in _JUDGMENT_AGENT_SYSTEM_PROMPT


def test_agent_participant_key_normalizes_primary_customer_aliases() -> None:
    assert _agent_participant_key({"participant_scope": "primary_customer", "participant": "丁青女士"}) == (
        "primary_customer",
        "",
    )
    assert _agent_participant_key({"participant": "未提及姓名的现场主咨询客户"}) == ("primary_customer", "")
    assert _agent_participant_key({"participant_scope": "other_customer", "participant": "同行客户A"}) == (
        "other_customer",
        "同行客户A",
    )


def test_agent_normalize_demands_merges_same_primary_customer_synonyms() -> None:
    graph = {
        "demands": [
            {
                "content": "希望整体改善面部状态，让面部更好看但不追求过度改变",
                "participant_scope": "primary_customer",
                "participant": "丁青女士",
            },
            {
                "content": "希望改善面部整体状态，让面部看着更好看，不追求过度改变",
                "participant": "未提及姓名的现场主咨询客户",
            },
            {"content": "改善面颊凹陷，考虑填充", "participant_scope": "primary_customer"},
            {"content": "关注脸颊凹陷，考虑是否填充改善", "participant_scope": "primary_customer"},
        ]
    }
    normalized = _agent_normalize_demands(graph)
    assert [item["content"] for item in normalized["demands"]] == [
        "希望整体改善面部状态，让面部更好看但不追求过度改变",
        "改善面颊凹陷，考虑填充",
    ]


def test_agent_normalize_demands_merges_profile_and_nose_variants() -> None:
    graph = {
        "demands": [
            {"content": "面部轮廓外扩，想改善整体及侧面轮廓线条", "participant_scope": "primary_customer"},
            {"content": "侧面轮廓明显，想改善侧面线条", "participant_scope": "primary_customer"},
            {"content": "希望鼻子更高一点，改善鼻部高度与立体度", "participant_scope": "primary_customer"},
            {"content": "希望对鼻部特定位置做微调优化", "participant_scope": "primary_customer"},
        ]
    }
    normalized = _agent_normalize_demands(graph)
    assert [item["content"] for item in normalized["demands"]] == [
        "面部轮廓外扩，想改善整体及侧面轮廓线条",
        "希望鼻子更高一点，改善鼻部高度与立体度",
    ]


def test_agent_normalize_demands_drops_schedule_or_order_only_items() -> None:
    graph = {
        "demands": [
            {"content": "希望通过热玛吉进行面部抗衰、提升紧致", "participant_scope": "primary_customer"},
            {"content": "六月到九月份做一次热玛吉", "participant_scope": "other_customer"},
            {"content": "热玛吉后间隔一段时间再做水光", "participant_scope": "other_customer"},
        ]
    }
    normalized = _agent_normalize_demands(graph)
    assert [item["content"] for item in normalized["demands"]] == [
        "希望通过热玛吉进行面部抗衰、提升紧致"
    ]


def test_agent_filter_non_deal_factors_drops_next_step_chatter() -> None:
    graph = {
        "deal_factors": [
            {"content": "客户表示去看看方案和价格后再说"},
            {"content": "客户已支付定金并开单"},
        ]
    }
    filtered = _agent_filter_non_deal_factors(graph)
    assert filtered["deal_factors"] == [{"content": "客户已支付定金并开单"}]


def test_agent_normalize_non_deal_outcome_clears_next_step_chatter() -> None:
    graph = {
        "deal_outcome": {
            "status": "未明确",
            "content": "客户表示先去查看方案和价格，尚未明确成交或预约",
            "quote": "那我们就去看看方案和价格",
        }
    }
    assert _agent_normalize_non_deal_outcome(graph)["deal_outcome"] == {"status": "未明确"}


def test_agent_common_indications_do_not_map_injection_support_to_surgical_face_fill() -> None:
    graph = {
        "demands": [{"content": "改善颊凹和面部凹陷"}],
        "recommendations": [{"content": "颊凹玻尿酸填充支撑", "material": "玻尿酸"}],
        "indication_candidates": [
            {
                "department_name": "外科",
                "indication_name": "面部填充",
                "body_part_name": "面部",
                "standardized_indication": "外科|面部填充|面部",
            }
        ],
    }
    normalized = _agent_ensure_common_indications(graph)
    assert all(
        not (item.get("indication_name") == "面部填充" and item.get("body_part_name") == "面部")
        for item in normalized["indication_candidates"]
    )


def test_agent_finalizer_does_not_append_surgical_face_fill_for_injection_support() -> None:
    result = {
        "staff_recommendations": {
            "items": [
                {
                    "recommendation": "颊凹玻尿酸填充支撑",
                    "product_or_solution": "玻尿酸",
                    "material": "玻尿酸",
                }
            ]
        },
        "standardized_indications": {"items": []},
    }
    finalized = _agent_finalize_analysis_result(result, context="颊凹玻尿酸填充支撑")

    assert all(
        not (item.get("indication_name") == "面部填充" and item.get("body_part_name") == "面部")
        for item in finalized["standardized_indications"]["items"]
    )


def test_agent_finalizer_normalizes_haiwei_brand_from_asr_near_sound() -> None:
    result = {
        "staff_recommendations": {
            "items": [
                {
                    "recommendation": "颊凹海派玻尿酸填充",
                    "brand": "海派",
                    "evidence": "首选海派这样的一支",
                }
            ]
        },
        "staff_seed_recommendations": {"items": []},
        "standardized_indications": {"items": []},
    }
    finalized = _agent_finalize_analysis_result(result, context="首选海派这样的一支玻尿酸填充")
    item = finalized["staff_recommendations"]["items"][0]

    assert item["brand"] == "海薇"
    assert "海薇" in item["recommendation"]


def test_agent_common_indications_remove_jawline_candidate_for_outer_cheek_fill() -> None:
    graph = {
        "demands": [{"content": "改善上半脸轮廓外扩和颊凹"}],
        "recommendations": [{"content": "外轮廓线后侧玻尿酸填充，颊凹海派填充"}],
        "indication_candidates": [
            {
                "department_name": "微创",
                "indication_name": "塑美",
                "body_part_name": "下颌轮廓线（大O）",
            }
        ],
    }
    normalized = _agent_ensure_common_indications(graph)
    assert all("下颌" not in (item.get("body_part_name") or "") for item in normalized["indication_candidates"])


def test_agent_jawline_support_context_requires_injection_or_structural_material() -> None:
    assert not _agent_has_jawline_support_context("热玛吉下颌线提升打法，五代头更大")
    assert not _agent_has_jawline_support_context("外轮廓线后侧玻尿酸填充")
    assert _agent_has_jawline_support_context("下颌线芭比针注射支撑提升")
