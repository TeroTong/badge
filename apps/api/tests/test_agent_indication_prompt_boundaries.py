from smart_badge_api.analysis.agent_pipeline import (
    _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT,
    _AGENT_INDICATION_ADJUDICATION_USER_TEMPLATE,
    _agent_dedupe_result_demands,
    _agent_finalize_analysis_result,
    _agent_ensure_common_indications,
    _agent_merge_candidate_indications_from_fact_graph,
    _agent_prune_seed_only_indications,
    _agent_remove_rejected_indications,
    _agent_restore_missing_display_recommendations,
)
from smart_badge_api.analysis.staged_pipeline import (
    _build_analysis_result_from_fact_graph,
    _candidate_indications_from_text,
    _has_lip_current_context,
)


def test_agent_indication_prompt_is_chinese_and_agent_scoped() -> None:
    assert "Agent 10" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "Indication adjudication / SAP 适应症裁决 Agent" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "只输出 JSON" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "预裁决 fact_graph" in _AGENT_INDICATION_ADJUDICATION_USER_TEMPLATE
    assert "本地 SAP 适应症字典召回候选" in _AGENT_INDICATION_ADJUDICATION_USER_TEMPLATE
    assert "final_indications" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "rejected_indications" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT


def test_agent_indication_prompt_prioritizes_precision_and_dictionary_bounds() -> None:
    assert "优先保证准确性；宁可少选" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "只能从这里复制" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "不得自造编码、项目名或部位" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "standardized_indication 必须完全复制 candidate_indications" in (
        _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    )
    assert "selection_note / note / reason" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT


def test_agent_indication_prompt_enforces_current_scope_and_participants() -> None:
    assert "主咨询客户、同行客户A/B 必须分别裁决" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "不能用同行客户证据支持主咨询客户" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "单纯员工科普、客户随口提问" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "备选比较、明确不推荐/不适合" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "转科/下次/种草需求可保留在主诉或种草方案中" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT


def test_agent_indication_prompt_contains_generalized_boundary_rules() -> None:
    assert "肉毒/大提拉/咬肌/瘦脸/下颌缘放松不等于“面部除皱”" in (
        _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    )
    assert "痘印/痘坑不能单独推出痤疮" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "毛孔、黑头、出油、鼻头/鼻翼皮肤质地问题不能推出鼻综合" in (
        _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    )
    assert "痘坑、凹陷性痘坑、痤疮瘢痕" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "疤痕类候选" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "祛痣/祛疣必须按证据里的痣所在部位选择" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "卧蚕、泪沟、眼下/眶下凹陷" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "塑美-眼部（D）" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "注射材料不要补成" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "副乳/腋前/胸外侧鼓出" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "富贵包没有专用候选时" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT
    assert "独立术前检查、术后护理、药物、疤痕膏" in _AGENT_INDICATION_ADJUDICATION_SYSTEM_PROMPT


def test_agent_indication_candidates_include_fact_graph_fallbacks() -> None:
    merged = _agent_merge_candidate_indications_from_fact_graph(
        [],
        {
            "indication_candidates": [
                {
                    "department_code": "Y3",
                    "department_name": "皮肤",
                    "indication_code": "SYZ3021",
                    "indication_name": "疤痕",
                    "body_part_code": "BW3001",
                    "body_part_name": "面部",
                    "evidence": ["当前主诉为痘坑/凹陷性痤疮瘢痕"],
                    "reason": "agent deterministic indication fallback",
                }
            ]
        },
    )

    assert any(item["standardized_indication"] == "Y3|皮肤|SYZ3021|疤痕|BW3001|面部" for item in merged)
    assert "痘坑/凹陷性痤疮瘢痕" in merged[0]["selection_note"]


def test_named_primary_customer_indications_survive_single_result_filter() -> None:
    result = _build_analysis_result_from_fact_graph(
        {
            "_indication_adjudicated": True,
            "_indication_adjudication": {"selected_count": 1, "rejected_indications": []},
            "demands": [
                {
                    "content": "想做鼻子手术变美",
                    "participant": "主咨询客户",
                    "participant_scope": "primary_customer",
                }
            ],
            "recommendations": [
                {
                    "content": "鼻综合手术",
                    "participant": "主咨询客户",
                    "participant_scope": "primary_customer",
                }
            ],
            "indication_candidates": [
                {
                    "standardized_indication": "Y1|外科|SYZ1006|鼻综合|BW1002|鼻部",
                    "indication_name": "鼻综合",
                    "body_part_name": "鼻部",
                    "evidence": "想做鼻子手术变美；鼻综合手术",
                    "confidence": 0.96,
                    "participant": "许莲",
                    "participant_scope": "primary_customer",
                }
            ],
        },
        {},
        allow_raw_augmentation=False,
    )

    indications = result["standardized_indications"]["items"]
    assert [(item["indication_name"], item["body_part_name"]) for item in indications] == [("鼻综合", "鼻部")]


def test_mole_candidate_recall_keeps_face_body_when_recording_mentions_eyes_elsewhere() -> None:
    rows = _candidate_indications_from_text("客户本次想做眼睛和鼻子，同时咨询面部突起痣手术切除，点痣多少钱。")
    standardized = {
        "|".join(
            row[key]
            for key in (
                "department_code",
                "department_name",
                "indication_code",
                "indication_name",
                "body_part_code",
                "body_part_name",
            )
        )
        for row in rows
    }

    assert "Y3|皮肤|SYZ3012|祛痣/祛疣|BW3001|面部" in standardized


def test_eye_injection_candidate_recall_uses_micro_injection_eye_body() -> None:
    rows = _candidate_indications_from_text("客户本次做卧蚕注射，医生建议胶原类材料，不建议纯玻尿酸。")
    standardized = {
        "|".join(
            row[key]
            for key in (
                "department_code",
                "department_name",
                "indication_code",
                "indication_name",
                "body_part_code",
                "body_part_name",
            )
        )
        for row in rows
    }

    assert "Y2|微创|SYZ2001|塑美|BW2007|眼部（D）" in standardized


def test_adjudicated_indications_are_not_pruned_by_seed_only_cleanup() -> None:
    repaired = _agent_prune_seed_only_indications(
        {
            "demands": [{"content": "本次想做鼻子和眼睛"}],
            "recommendations": [{"content": "鼻综合手术"}],
            "seed_recommendations": [{"content": "面部突起痣手术切除"}],
            "indication_candidates": [
                {
                    "standardized_indication": "Y3|皮肤|SYZ3012|祛痣/祛疣|BW3001|面部",
                    "indication_name": "祛痣/祛疣",
                    "body_part_name": "面部",
                    "evidence": "面部突起痣手术切除",
                    "adjudication_reason": "第10步裁决为当前真实处理意向",
                }
            ],
        }
    )

    assert repaired["indication_candidates"][0]["indication_name"] == "祛痣/祛疣"


def test_rejected_indications_override_force_include_fallbacks() -> None:
    repaired = _agent_remove_rejected_indications(
        {
            "indication_candidates": [
                {
                    "standardized_indication": "Y2|微创|SYZ2001|塑美|BW2018|下颌轮廓线（大O）",
                    "indication_name": "塑美",
                    "body_part_name": "下颌轮廓线（大O）",
                    "force_include": True,
                }
            ]
        },
        {
            "rejected_indications": [
                {
                    "standardized_indication": "Y2|微创|SYZ2001|塑美|BW2018|下颌轮廓线（大O）",
                    "reason": "明确暂停注射，不写入SAP",
                }
            ]
        },
    )

    assert repaired["indication_candidates"] == []


def test_agent_finalize_can_disable_post_adjudication_indication_backfill() -> None:
    result = _agent_finalize_analysis_result(
        {
            "staff_recommendations": {
                "items": [
                    {
                        "recommendation": "当前阶段仅建议进行仪器类治疗，暂停下巴玻尿酸注射",
                    }
                ]
            },
            "standardized_indications": {"items": [], "summary": ""},
        },
        allow_indication_backfill=False,
    )

    assert result["standardized_indications"]["items"] == []


def test_lip_current_context_not_blocked_by_repeat_injection_warning() -> None:
    assert _has_lip_current_context(
        "唇部的话不建议反复打便宜材料，润致单项1980，我打胶没问题，马上给你安排。"
    )


def test_deal_outcome_can_seed_current_indication_candidates() -> None:
    repaired = _agent_ensure_common_indications(
        {
            "demands": [{"content": "客户本次咨询唇部注射"}],
            "deal_outcome": {
                "status": "已成交",
                "deal_items": [{"content": "娇兰唇部注射，马上安排治疗"}],
            },
            "indication_candidates": [],
        }
    )

    assert any(
        item.get("department_code") == "Y2"
        and item.get("indication_code") == "SYZ2001"
        and item.get("body_part_code") == "BW2008"
        for item in repaired["indication_candidates"]
    )


def test_restore_missing_display_recommendations_from_fact_graph() -> None:
    result = _agent_restore_missing_display_recommendations(
        {
            "staff_recommendations": {"items": [], "summary": ""},
            "consultation_result": {"recommended_plan": {"items": [], "summary": ""}},
        },
        fact_graph={
            "demands": [
                {
                    "id": "D1",
                    "content": "希望改善鼻小柱支撑",
                    "body_part": "鼻小柱",
                    "participant": "主咨询客户",
                    "participant_scope": "primary_customer",
                }
            ],
            "recommendations": [
                {
                    "content": "鼻小柱玻尿酸注射支撑",
                    "body_part": "鼻小柱",
                    "material": "玻尿酸",
                    "dosage": "1支",
                    "related_demand_ids": ["D1"],
                    "evidence": "我打定彩鼻。一支够了吧？",
                    "participant": "主咨询客户",
                    "participant_scope": "primary_customer",
                }
            ],
        },
        raw={},
    )

    items = result["staff_recommendations"]["items"]
    assert items and items[0]["recommendation"].startswith("鼻小柱玻尿酸注射支撑")
    assert result["consultation_result"]["recommended_plan"]["items"]
    assert result["staged_pipeline_debug"]["agent_display_recommendations_restored"] is True


def test_dedupe_result_demands_merges_duplicate_nose_tip_shape_demands() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {
                    "demand": "改善鼻头偏大问题",
                    "body_part": "鼻头",
                    "evidence": "客户说只是觉得鼻头很大。",
                },
                {
                    "demand": "觉得鼻头大",
                    "body_part": "鼻头",
                    "evidence": "客户说只是觉得鼻头很大。",
                },
            ]
        }
    }

    assert _agent_dedupe_result_demands(result) is True

    items = result["customer_primary_demands"]["items"]
    assert len(items) == 1
    assert items[0]["demand"] == "改善鼻头偏大问题"


def test_dedupe_result_demands_merges_vague_midface_empty_region_demands() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {
                    "demand": "感觉中面部有部位空着，想了解下次可改善的项目（后续指向鼻基底/中面部支撑）",
                    "body_part": "鼻基底/中面部",
                    "evidence": "他想问一下他下次可以要做点什么？我觉得有一个部位一直是空着的。",
                },
                {
                    "demand": "觉得有一个部位一直空着，想了解下次可以做什么",
                    "body_part": "未明确（后续指向鼻基底/中面部）",
                    "evidence": "他想问一下他下次可以要做点什么？我觉得有一个部位一直是空着的。",
                },
            ]
        }
    }

    assert _agent_dedupe_result_demands(result) is True

    items = result["customer_primary_demands"]["items"]
    assert len(items) == 1
    assert items[0]["body_part"] == "鼻基底/中面部"


def test_finalize_removes_unsupported_botox_concern_and_keeps_filling_concern() -> None:
    result = {
        "customer_concerns": {
            "items": [
                {
                    "type": "顾虑",
                    "content": "担心鼻基底及苹果肌内侧填充后仍会出现凹陷",
                    "evidence": "那我这个就不会有凹陷吧。",
                },
                {
                    "type": "顾虑",
                    "content": "担心咬肌肉毒后面颊凹陷加重",
                    "evidence": "客户担心填充后仍会有凹陷",
                },
            ]
        },
        "staff_recommendations": {"items": []},
        "staff_seed_recommendations": {"items": []},
    }

    updated = _agent_finalize_analysis_result(result, context="", allow_indication_backfill=False)

    concerns = updated["customer_concerns"]["items"]
    assert [item["content"] for item in concerns] == ["担心鼻基底及苹果肌内侧填充后仍会出现凹陷"]
    assert updated["consultation_result"]["deal_factors"]["concerns"] == ["担心鼻基底及苹果肌内侧填充后仍会出现凹陷"]


def test_finalize_normalizes_shuangmei_and_rhythies_recommendation_terms() -> None:
    context = (
        "鼻基底千万别打玻尿酸，玻尿酸会吸水馒化。"
        "两边内侧苹果肌和鼻基底四个点，总共一支read的1，这个材料不含玻尿酸。"
        "最好是三明治内沟，深层有一个双美的支撑，把泪沟撑起来。"
        "其实你也可以随时做童颜针的。颞区耳区和下巴园区每边一支童颜针收紧。"
        "耳朵可以免费打两只，但是前提是你现在把面中部打起来。"
    )
    result = {
        "staff_recommendations": {
            "items": [
                {
                    "recommendation": "鼻基底及苹果肌内侧四点位玻尿酸填充（瑞蓝1一支）",
                    "body_part": "鼻基底/苹果肌内侧",
                    "brand": "瑞蓝1",
                    "material": "玻尿酸",
                    "evidence": "这4个点一支首选瑞1就可以了。",
                },
                {
                    "recommendation": "泪沟三明治内沟填充（深层双美玻尿酸支撑）",
                    "body_part": "泪沟",
                    "brand": "双美",
                    "material": "玻尿酸",
                    "evidence": "深层有一个双美的支撑，把泪沟撑起来。",
                },
                {
                    "recommendation": "下颌线/下颌角拐点结构支撑提升（材料：童颜针）",
                    "body_part": "下颌线/下颌角拐点",
                    "material": "童颜针",
                    "evidence": "两边的内侧苹果肌和鼻基底总共一支。",
                },
            ]
        },
        "staff_seed_recommendations": {
            "items": [
                {
                    "recommendation": "耳部玻尿酸塑形（可免费两只）",
                    "body_part": "耳部",
                    "material": "玻尿酸",
                    "evidence": "卧蚕下面这条沟垫起来一点。",
                }
            ]
        },
    }

    updated = _agent_finalize_analysis_result(result, context=context, allow_indication_backfill=False)

    items = updated["staff_recommendations"]["items"]
    assert items[0]["brand"] == "瑞德喜"
    assert items[0]["material"] == "再生类骨性支撑材料"
    assert "玻尿酸填充" not in items[0]["recommendation"]
    assert items[1]["material"] == "胶原蛋白"
    assert "双美胶原蛋白" in items[1]["recommendation"]
    assert "童颜针" in items[2]["evidence"]
    seed_items = updated["staff_seed_recommendations"]["items"]
    assert "耳朵可以免费" in seed_items[0]["evidence"]


def test_finalize_demotes_history_only_chin_recommendation_to_seed() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {
                    "priority": 1,
                    "demand": "希望卧蚕呈现前窄后宽的形态",
                    "body_part": "卧蚕",
                    "evidence": "我是想要就是前窄后宽的。",
                },
                {
                    "priority": 2,
                    "demand": "认为下巴之前注射后已被吸收，关注下巴后缩及效果维持，希望改善下巴形态",
                    "body_part": "下巴",
                    "evidence": "中间立马就之前打了没了吸收。",
                },
            ]
        },
        "staff_recommendations": {
            "items": [
                {
                    "recommendation": "泪沟三明治内沟填充改善卧蚕清晰度",
                    "body_part": "泪沟/卧蚕",
                    "material": "胶原蛋白",
                    "demand_priority": [1],
                    "evidence": "最好是三明治内沟。",
                },
                {
                    "recommendation": "下巴玻尿酸填充两支改善后缩及形态不足",
                    "body_part": "下巴",
                    "material": "玻尿酸",
                    "demand_priority": [2],
                    "evidence": "下巴可以来个两个玻尿酸。",
                },
            ]
        },
        "staff_seed_recommendations": {"items": []},
    }

    updated = _agent_finalize_analysis_result(result, context="", allow_indication_backfill=False)

    demands = updated["customer_primary_demands"]["items"]
    assert [item["demand"] for item in demands] == ["希望卧蚕呈现前窄后宽的形态"]
    recs = updated["staff_recommendations"]["items"]
    assert len(recs) == 1
    assert "泪沟" in recs[0]["recommendation"]
    seeds = updated["staff_seed_recommendations"]["items"]
    assert any("下巴玻尿酸" in item["recommendation"] for item in seeds)
