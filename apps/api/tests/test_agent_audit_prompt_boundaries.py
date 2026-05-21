from smart_badge_api.analysis.agent_pipeline import (
    _AUDIT_AGENT_SYSTEM_PROMPT,
    _AUDIT_AGENT_USER_TEMPLATE,
    _FINAL_RESULT_AUDIT_SYSTEM_PROMPT,
    _FINAL_RESULT_AUDIT_USER_TEMPLATE,
    _apply_final_result_audit_patch,
    _agent_finalize_analysis_result,
    _agent_repair_deal_participant_scope_from_evidence,
    _audit_needed,
    _final_result_audit_needed,
)


def test_fact_graph_audit_prompt_is_chinese_and_schema_bound() -> None:
    assert "Audit / fact_graph 审计 Agent" in _AUDIT_AGENT_SYSTEM_PROMPT
    assert "不要重新分析整段录音" in _AUDIT_AGENT_SYSTEM_PROMPT
    assert "不要重写 SAP 咨询备注" in _AUDIT_AGENT_SYSTEM_PROMPT
    assert '"audit"' in _AUDIT_AGENT_SYSTEM_PROMPT
    assert '"corrected_fact_graph": null' in _AUDIT_AGENT_SYSTEM_PROMPT
    assert "只输出 audit JSON" in _AUDIT_AGENT_USER_TEMPLATE
    assert "触发审计的 fact_graph" in _AUDIT_AGENT_USER_TEMPLATE
    assert "证据图：" in _AUDIT_AGENT_USER_TEMPLATE
    assert "事件图：" in _AUDIT_AGENT_USER_TEMPLATE


def test_fact_graph_audit_prompt_uses_generic_evidence_and_event_rules() -> None:
    assert "event_graph 的事件极性优先" in _AUDIT_AGENT_SYSTEM_PROMPT
    assert "customer_question、staff_explanation" in _AUDIT_AGENT_SYSTEM_PROMPT
    assert "current_recommendation、seed_recommendation" in _AUDIT_AGENT_SYSTEM_PROMPT
    assert "参与者必须隔离" in _AUDIT_AGENT_SYSTEM_PROMPT
    assert "SAP 适应症只能来自 candidate_indications" in _AUDIT_AGENT_SYSTEM_PROMPT
    assert "不要因相邻部位、泛化部位词" in _AUDIT_AGENT_SYSTEM_PROMPT
    assert "尊重前序裁决" in _AUDIT_AGENT_SYSTEM_PROMPT
    assert "成交金额把备选报价" in _AUDIT_AGENT_SYSTEM_PROMPT
    assert "corrected_fact_graph.deal_outcome" in _AUDIT_AGENT_SYSTEM_PROMPT


def test_fact_graph_audit_prompt_keeps_repair_scope_narrow() -> None:
    assert "必须实际解决对应 high/medium" in _AUDIT_AGENT_SYSTEM_PROMPT
    assert "补齐 related_demand_ids" in _AUDIT_AGENT_SYSTEM_PROMPT
    assert "只返回需要替换的 fact_graph 字段" in _AUDIT_AGENT_SYSTEM_PROMPT
    assert "未修改字段不要重复返回" in _AUDIT_AGENT_SYSTEM_PROMPT
    assert "demands、doctor_diagnoses、indication_candidates" in _AUDIT_AGENT_SYSTEM_PROMPT
    assert "deal_outcome" in _AUDIT_AGENT_SYSTEM_PROMPT


def test_final_result_audit_agent_number_follows_fact_graph_audit() -> None:
    assert "Agent 9" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "Final result audit / 展示结果审计 Agent" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT


def test_final_result_audit_prompt_is_chinese_and_display_scoped() -> None:
    assert "已渲染成 analysis_result" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "不要重新构建 fact_graph" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "只做小范围、证据支持的展示结果修复" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert '"final_result_audit"' in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert '"analysis_result_patch": null' in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "触发展示结果审计的原因：" in _FINAL_RESULT_AUDIT_USER_TEMPLATE
    assert "已渲染的 analysis_result：" in _FINAL_RESULT_AUDIT_USER_TEMPLATE
    assert "只输出 final_result_audit JSON" in _FINAL_RESULT_AUDIT_USER_TEMPLATE


def test_final_result_audit_prompt_uses_generic_display_rules() -> None:
    assert "主诉必须是本次客户明确想改善的部位、问题或审美目标" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "后续又明确否定或收窄" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "最终展示必须按最后确认的范围重写或删除宽泛主诉" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "推荐方案是针对本次主诉的当前核心方案" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "种草方案是额外、低优先级、下次、转科、维养" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "SAP 适应症必须与最终推荐方案或已确认当前主诉精确匹配" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "付款/定金金额不等于客户预算" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "不能把同行客户成交归到主咨询客户" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "客户画像只保留客户本人的事实" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "不要把既往项目花费、外院价格" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT


def test_final_result_audit_prompt_limits_patch_sections() -> None:
    assert "在 analysis_result_patch 中返回删除/修正后的完整模块列表" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "summary 与 items 保持一致" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "analysis_result_patch 只返回需要替换的顶层展示模块" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "未修改模块不要重复返回" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    assert "customer_primary_demands, customer_concerns, staff_recommendations" in (
        _FINAL_RESULT_AUDIT_SYSTEM_PROMPT
    )
    assert "consultation_result, customer_profile" in _FINAL_RESULT_AUDIT_SYSTEM_PROMPT


def test_fact_graph_audit_triggers_for_duplicate_demands_and_raw_budget() -> None:
    fact_graph = {
        "demands": [
            {"content": "改善面颊凹陷，考虑填充", "participant_scope": "primary_customer"},
            {"content": "关注脸颊凹陷，想通过填充改善", "participant_scope": "primary_customer"},
            {"content": "希望改善咬肌肥大", "participant_scope": "primary_customer"},
            {"content": "改善眼周细纹", "participant_scope": "primary_customer"},
            {"content": "想让面部整体更年轻", "participant_scope": "primary_customer"},
        ],
        "recommendations": [{"content": "面颊填充", "related_demand_ids": ["D1"]}],
        "seed_recommendations": [{"content": "后续可考虑眼周抗衰"}],
        "indication_candidates": [{"name": "面部填充", "body_part": "面部"}],
        "budget_facts": [{"content": "[02:52] 客户反复核算总价，觉得三万元偏高"}],
    }
    needed, reasons = _audit_needed(
        fact_graph,
        {"customer_demand_evidence": [{"quote": "脸颊凹"}]},
        {},
        {"rejected_indications": []},
    )

    assert needed is True
    assert "many_fact_demands_need_consistency_check" in reasons
    assert "duplicate_or_near_duplicate_fact_demands" in reasons
    assert "seed_recommendations_partially_without_demand_links" in reasons
    assert "raw_quote_or_overlong_fact_budget" in reasons


def test_fact_graph_audit_triggers_when_recommendation_contains_worry_but_concern_empty() -> None:
    fact_graph = {
        "demands": [{"content": "改善颊凹"}],
        "recommendations": [
            {
                "content": "颊凹玻尿酸少量填充",
                "customer_response": "客户担心安全性和后遗症",
                "related_demand_ids": ["D1"],
            }
        ],
        "seed_recommendations": [],
        "concerns": [],
        "indication_candidates": [{"name": "塑美", "body_part": "面部"}],
    }
    needed, reasons = _audit_needed(fact_graph, {}, {}, {"rejected_indications": []})

    assert needed is True
    assert "worry_in_fact_recommendation_response_without_concern" in reasons


def test_deal_participant_scope_is_repaired_from_deal_evidence() -> None:
    fact_graph = {
        "deal_outcome": {
            "status": "已成交",
            "deal_items": [
                {
                    "plan": "唇部玻尿酸注射",
                    "participant": "同行客户A",
                    "participant_scope": "primary_customer",
                    "evidence_ids": ["E_DEAL1"],
                }
            ],
        }
    }
    evidence_graph = {
        "deal_evidence": [
            {
                "id": "E_DEAL1",
                "participant": "同行客户A",
                "participant_scope": "other_customer",
                "quote": "马上给你安排",
            }
        ]
    }

    repaired = _agent_repair_deal_participant_scope_from_evidence(fact_graph, evidence_graph)

    assert repaired["deal_outcome"]["deal_items"][0]["participant_scope"] == "other_customer"


def test_final_result_audit_triggers_when_deal_has_no_displayed_recommendations() -> None:
    analysis_result = {
        "customer_primary_demands": {"items": [{"priority": 1, "demand": "改善太阳穴凹陷"}]},
        "staff_recommendations": {"items": []},
        "staff_seed_recommendations": {"items": []},
        "consultation_result": {
            "deal_outcome": {
                "status": "已成交",
                "deal_items": [{"plan": "太阳穴玻尿酸填充", "amount": "9900"}],
            }
        },
    }
    fact_graph = {
        "recommendations": [{"content": "太阳穴玻尿酸填充", "related_demand_ids": ["D1"]}],
        "seed_recommendations": [{"content": "后续光电维养"}],
    }
    needed, reasons = _final_result_audit_needed(
        analysis_result,
        corrected_dialogue="客户已支付太阳穴玻尿酸填充费用",
        fact_graph=fact_graph,
        event_graph={},
    )

    assert needed is True
    assert "deal_outcome_without_displayed_recommendations" in reasons
    assert "fact_recommendations_lost_in_rendered_result" in reasons
    assert "fact_seed_recommendations_lost_in_rendered_result" in reasons


def test_final_result_audit_patch_deep_merges_without_dropping_deal_outcome() -> None:
    result = {
        "consultation_result": {
            "deal_outcome": {"status": "已成交", "amount": "9900"},
            "chief_complaint_and_indications": {"primary_demands": ["旧主诉"]},
        }
    }
    patch = {
        "consultation_result": {
            "chief_complaint_and_indications": {"primary_demands": ["太阳穴凹陷"]}
        }
    }

    updated = _apply_final_result_audit_patch(result, patch)

    assert updated["consultation_result"]["deal_outcome"] == {"status": "已成交", "amount": "9900"}
    assert updated["consultation_result"]["chief_complaint_and_indications"]["primary_demands"] == ["太阳穴凹陷"]


def test_final_result_audit_removes_unsupported_concern_even_without_patch() -> None:
    result = {
        "customer_concerns": {
            "items": [
                {"content": "担心咬肌肉毒后面颊凹陷加重", "evidence": "方案反馈中误带出的内容"},
                {"content": "担心太阳穴填充后肿胀", "evidence": "客户说害怕打得特别肿"},
            ],
            "summary": "担心咬肌肉毒后面颊凹陷加重；担心太阳穴填充后肿胀",
        },
        "consultation_result": {
            "deal_factors": {
                "concerns": ["担心咬肌肉毒后面颊凹陷加重", "担心太阳穴填充后肿胀"]
            }
        },
    }
    audit = {
        "issues": [
            {
                "severity": "high",
                "type": "unsupported_concern",
                "description": "顾客顾虑中包含“担心咬肌肉毒后面颊凹陷加重”，未找到客户明确表达。",
                "evidence": "concern_evidence 中无对应证据。",
            }
        ]
    }

    updated = _apply_final_result_audit_patch(result, None, audit=audit)

    assert [item["content"] for item in updated["customer_concerns"]["items"]] == ["担心太阳穴填充后肿胀"]
    assert updated["customer_concerns"]["summary"] == "担心太阳穴填充后肿胀"
    assert updated["consultation_result"]["deal_factors"]["concerns"] == ["担心太阳穴填充后肿胀"]


def test_final_result_finalize_syncs_nested_demands_from_top_level() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {"priority": 1, "demand": "改善眼袋和下睑松弛"},
                {"priority": 2, "demand": "改善泪沟凹陷"},
            ]
        },
        "standardized_indications": {
            "items": [
                {"indication_name": "眼袋", "body_part_name": "眼部"},
                {"indication_name": "面部填充", "body_part_name": "面部"},
            ]
        },
        "consultation_result": {
            "chief_complaint_and_indications": {
                "primary_demands": ["咨询黄金微针项目", "觉得眼睛有问题"],
                "summary": "旧主诉",
            }
        },
    }

    updated = _agent_finalize_analysis_result(result)
    chief = updated["consultation_result"]["chief_complaint_and_indications"]

    assert chief["primary_demands"] == ["改善眼袋和下睑松弛", "改善泪沟凹陷"]
    assert chief["summary"] == "改善眼袋和下睑松弛；改善泪沟凹陷"
    assert chief["standardized_indications"] == ["眼袋（眼部）", "面部填充（面部）"]


def test_final_result_finalize_dedupes_duplicate_recommendations() -> None:
    result = {
        "staff_recommendations": {
            "items": [
                {
                    "recommendation": "鼻基底玻尿酸填充",
                    "body_part": "鼻基底",
                    "material": "玻尿酸",
                    "evidence": "先打鼻基底",
                },
                {
                    "recommendation": "鼻基底玻尿酸填充",
                    "body_part": "鼻基底",
                    "material": "玻尿酸",
                    "evidence": "先打鼻基底",
                    "implementation_notes": "少量多次，先用一支材料尝试",
                },
            ]
        }
    }

    updated = _agent_finalize_analysis_result(result)
    items = updated["staff_recommendations"]["items"]

    assert len(items) == 1
    assert items[0]["implementation_notes"] == "少量多次，先用一支材料尝试"


def test_final_result_finalize_backfills_top_level_concerns_from_deal_factors() -> None:
    result = {
        "customer_concerns": {"items": []},
        "consultation_result": {
            "deal_factors": {
                "concerns": [
                    "担心眼袋手术恢复期过长",
                    {"content": "担心术后疤痕", "evidence": "客户问外切会不会留疤"},
                ]
            }
        },
    }

    updated = _agent_finalize_analysis_result(result)
    concern_block = updated["customer_concerns"]

    assert concern_block["summary"] == "担心眼袋手术恢复期过长；担心术后疤痕"
    assert [item["content"] for item in concern_block["items"]] == ["担心眼袋手术恢复期过长", "担心术后疤痕"]
    assert concern_block["items"][1]["evidence"] == "客户问外切会不会留疤"


def test_final_result_finalize_syncs_customer_profile_summary_from_top_level() -> None:
    result = {
        "customer_profile": {
            "age": "42",
            "age_evidence": "客户说自己42岁",
            "tags": [{"category": "价格敏感度", "value": "反复核算总价", "evidence": "客户反复问总价"}],
        },
        "consultation_result": {
            "customer_profile_summary": {
                "summary": "旧画像",
                "extracted_tag_count": 2,
                "tags": [{"category": "健康风险/禁忌", "value": "过敏史"}],
            }
        },
    }

    updated = _agent_finalize_analysis_result(result)
    profile_summary = updated["consultation_result"]["customer_profile_summary"]

    assert profile_summary["summary"] == "本次录音共提取 1 个画像标签。"
    assert profile_summary["extracted_tag_count"] == 1
    assert profile_summary["age"] == "42"
    assert profile_summary["tags"] == result["customer_profile"]["tags"]


def test_final_result_finalize_removes_prior_spend_as_current_budget_profile_tag() -> None:
    result = {
        "customer_profile": {
            "tags": [
                {
                    "category": "本次消费预算",
                    "value": "既往腰腹+手臂吸脂花费一万多",
                    "evidence": "你吸脂在那边花了多少钱？腰腹加手臂一万多。",
                },
                {
                    "category": "治疗项目",
                    "value": "吸脂",
                    "evidence": "以前在其他地方做过腰腹和手臂吸脂。",
                },
            ]
        },
        "consultation_result": {"customer_profile_summary": {"tags": []}},
    }

    updated = _agent_finalize_analysis_result(result)
    tags = updated["customer_profile"]["tags"]
    nested_tags = updated["consultation_result"]["customer_profile_summary"]["tags"]

    assert [(item["category"], item["value"]) for item in tags] == [("治疗项目", "吸脂")]
    assert nested_tags == tags


def test_final_result_finalize_keeps_current_price_sensitive_profile_tag() -> None:
    result = {
        "customer_profile": {
            "tags": [
                {
                    "category": "价格敏感度",
                    "value": "高",
                    "evidence": "客户反复核算本次方案总价，认为15800太贵，希望申请优惠。",
                }
            ]
        }
    }

    updated = _agent_finalize_analysis_result(result)

    assert updated["customer_profile"]["tags"][0]["value"] == "高"
