from smart_badge_api.analysis.reference_data import normalize_standardized_indications_payload
from smart_badge_api.analysis.schemas import AnalysisResult
from smart_badge_api.api.analysis_normalization import normalize_analysis_result


def test_normalize_standardized_indications_corrects_department_code_from_catalog():
    payload = {
        "summary": "识别出面部局部减脂及面部吸脂两项适应症",
        "items": [
            {
                "department_code": "P1",
                "department_name": "皮肤",
                "indication_code": "SYZ3020",
                "indication_name": "局部减脂",
                "body_part_code": "BW3001",
                "body_part_name": "面部",
                "evidence": "[08:41] 可以做仪器，也可以打针",
            },
            {
                "department_code": "Y1",
                "department_name": "外科",
                "indication_code": "SYZ1018",
                "indication_name": "面部吸脂",
                "body_part_code": "BW1005",
                "body_part_name": "面部",
                "evidence": "[10:21] 面吸的话也便宜",
            },
        ],
    }

    normalized = normalize_standardized_indications_payload(payload)

    assert normalized["items"][0]["department_code"] == "Y3"
    assert normalized["items"][0]["department_name"] == "皮肤"
    assert normalized["items"][0]["indication_code"] == "SYZ3020"
    assert normalized["items"][0]["body_part_code"] == "BW3001"
    assert normalized["items"][1]["department_code"] == "Y1"


def test_normalize_standardized_indications_drops_unmatched_items_and_deduplicates():
    payload = {
        "summary": "",
        "items": [
            {
                "department_code": "P1",
                "department_name": "皮肤",
                "indication_code": "SYZ3020",
                "indication_name": "局部减脂",
                "body_part_code": "BW3001",
                "body_part_name": "面部",
                "evidence": "[08:41] 可以做仪器，也可以打针",
            },
            {
                "department_code": "Y3",
                "department_name": "皮肤",
                "indication_code": "SYZ3020",
                "indication_name": "局部减脂",
                "body_part_code": "BW3001",
                "body_part_name": "面部",
                "evidence": "[08:42] 打溶脂针",
            },
            {
                "department_code": "X9",
                "department_name": "未知",
                "indication_code": "BAD9999",
                "indication_name": "不存在的适应症",
                "body_part_code": "BW9999",
                "body_part_name": "未知部位",
                "evidence": "[00:01] 错误数据",
            },
        ],
    }

    normalized = normalize_standardized_indications_payload(payload)

    assert len(normalized["items"]) == 1
    assert normalized["items"][0]["department_code"] == "Y3"
    assert normalized["summary"] == "识别出1项适应症：局部减脂（面部）"


def test_normalize_standardized_indications_requires_exact_department_when_department_is_provided():
    payload = {
        "summary": "",
        "items": [
            {
                "department_code": "Y1",
                "department_name": "外科",
                "indication_code": "SYZ3020",
                "indication_name": "局部减脂",
                "body_part_code": "BW3001",
                "body_part_name": "面部",
                "evidence": "[08:41] 可以做仪器，也可以打针",
            }
        ],
    }

    normalized = normalize_standardized_indications_payload(payload)

    assert normalized["items"] == []
    assert normalized["summary"] == "对话中未识别出可标准化的适应症"


def test_normalize_analysis_result_applies_indication_catalog_correction():
    result = {
        "customer_profile": {"tags": []},
        "staff_recommendations": {"items": []},
        "standardized_indications": {
            "summary": "识别出面部局部减脂",
            "items": [
                {
                    "department_code": "P1",
                    "department_name": "皮肤",
                    "indication_code": "SYZ3020",
                    "indication_name": "局部减脂",
                    "body_part_code": "BW3001",
                    "body_part_name": "面部",
                    "evidence": "[08:41] 可以做仪器，也可以打针",
                }
            ],
        },
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    items = normalized["standardized_indications"]["items"]
    assert len(items) == 1
    assert items[0]["department_code"] == "Y3"


def test_normalize_analysis_result_moves_budget_out_of_customer_profile_tags():
    result = {
        "customer_profile": {
            "tags": [
                {"category": "出生日期", "value": "32岁"},
                {"category": "本次消费预算", "value": "2万-3万"},
            ]
        },
        "consumption_intent": {
            "budget": None,
            "willingness": "中",
            "decision_factors": [],
            "evidence": [],
        },
        "staff_recommendations": {"items": []},
        "standardized_indications": {"summary": "", "items": []},
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    assert normalized["consumption_intent"]["budget"] == "2万-3万"
    assert normalized["customer_profile"]["tags"] == []
    assert normalized["customer_profile"]["age"] == "32岁"


def test_normalize_analysis_result_aligns_legacy_profile_tags_to_label_catalog():
    result = {
        "customer_profile": {
            "tags": [
                {"category": "常住城市", "value": "本地"},
                {"category": "负面项目/设备/原材料名称", "value": "某设备"},
                {"category": "对比机构", "value": "某机构"},
                {"category": "基本信息_年龄", "value": "30岁"},
            ]
        },
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": [],
            "evidence": [],
        },
        "staff_recommendations": {"items": []},
        "standardized_indications": {"summary": "", "items": []},
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    assert normalized["customer_profile"]["tags"] == [
        {"category": "常驻城市", "value": "本地"},
        {"category": "负面项目/设备/原材料", "value": "某设备"},
    ]
    assert normalized["customer_profile"]["age"] == "30岁"


def test_normalize_analysis_result_defaults_negative_project_tag_to_none_when_missing():
    result = {
        "customer_profile": {
            "tags": [
                {"category": "常住城市", "value": "本地"},
            ]
        },
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": [],
            "evidence": [],
        },
        "staff_recommendations": {"items": []},
        "standardized_indications": {"summary": "", "items": []},
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    assert normalized["customer_profile"]["tags"] == [
        {"category": "常驻城市", "value": "本地"},
    ]


def test_normalize_analysis_result_supports_legacy_shape_during_model_validation():
    raw = {
        "customer_primary_demands": {},
        "standardized_indications": {"summary": "", "items": []},
        "consumption_intent": {"budget": None, "willingness": None, "decision_factors": None, "evidence": None},
        "staff_recommendations": {"summary": "", "items": []},
        "customer_demands": {
            "focus_areas": [],
            "expectation": "未出现可构成需求链路的内容。",
            "product_preference": {},
        },
        "customer_concerns": {"summary": "", "items": []},
        "customer_profile": {"tags": []},
        "consultation_evaluation": {"overall_summary": "", "dimensions": []},
        "consultation_result": {
            "deal_outcome": {
                "status": "未明确",
                "summary": "",
                "deal_items": [{"item": "肉毒素200单位瘦脸针", "amount": "2980"}],
                "amount": None,
                "loss_reasons": [],
            }
        },
    }

    normalized = normalize_analysis_result(raw)

    assert normalized is not None
    model = AnalysisResult.model_validate(normalized)
    assert model.customer_primary_demands.summary == ""
    assert model.customer_demands.expectation.entry_state == "未出现可构成需求链路的内容。"
    assert model.consumption_intent.willingness == "未明确"
    assert model.consultation_result.deal_outcome.deal_items == []
    assert model.consultation_result.deal_outcome.amount is None


def test_normalize_analysis_result_clears_deal_fields_when_not_closed():
    raw = {
        "customer_primary_demands": {"summary": "", "items": []},
        "standardized_indications": {"summary": "", "items": []},
        "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
        "staff_recommendations": {
            "summary": "全切双眼皮",
            "items": [
                {
                    "recommendation": "全切双眼皮",
                    "customer_response": "接受",
                    "evidence": "[04:56] 切开全切是你一步到位的一个选择",
                }
            ],
        },
        "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
        "customer_concerns": {"summary": "", "items": []},
        "customer_profile": {"tags": []},
        "consultation_evaluation": {"overall_summary": "", "dimensions": []},
        "consultation_result": {
            "deal_outcome": {
                "status": "未成交",
                "summary": "客户确认当天下午进行双眼皮手术，已进入办病历、体检及术前流程。",
                "deal_items": ["全切双眼皮"],
                "amount": "5800左右",
                "loss_reasons": ["价格因素"],
            }
        },
    }

    normalized = normalize_analysis_result(raw)

    assert normalized is not None
    outcome = normalized["consultation_result"]["deal_outcome"]
    assert outcome["status"] == "未成交"
    assert outcome["deal_items"] == []
    assert outcome["amount"] is None
    assert outcome["loss_reasons"] == ["价格因素"]
    assert "成交方案" not in outcome["summary"]
    assert "成交金额" not in outcome["summary"]


def test_normalize_analysis_result_adds_negative_project_none_only_with_prior_treatment_context():
    result = {
        "customer_profile": {
            "tags": [
                {"category": "治疗项目", "value": "光电治疗"},
            ]
        },
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": [],
            "evidence": [],
        },
        "staff_recommendations": {"items": []},
        "standardized_indications": {"summary": "", "items": []},
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    assert normalized["customer_profile"]["tags"] == [
        {"category": "治疗项目", "value": "光电类"},
        {"category": "负面项目/设备/原材料", "value": "无"},
    ]


def test_normalize_analysis_result_drops_negative_project_none_without_prior_treatment_context():
    result = {
        "customer_profile": {
            "tags": [
                {"category": "出生日期", "value": "32岁"},
                {"category": "负面项目/设备/原材料", "value": "无"},
            ]
        },
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": [],
            "evidence": [],
        },
        "staff_recommendations": {"items": []},
        "standardized_indications": {"summary": "", "items": []},
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    assert normalized["customer_profile"]["tags"] == []
    assert normalized["customer_profile"]["age"] == "32岁"


def test_normalize_analysis_result_drops_invalid_placeholder_profile_tags():
    result = {
        "customer_profile": {
            "tags": [
                {"category": "常住城市", "value": "未提及"},
                {"category": "健康风险/禁忌", "value": "未知"},
                {"category": "出生日期", "value": "32岁"},
                {"category": "负面项目/设备/原材料", "value": "无"},
            ]
        },
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": [],
            "evidence": [],
        },
        "staff_recommendations": {"items": []},
        "standardized_indications": {"summary": "", "items": []},
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    assert normalized["customer_profile"]["tags"] == []
    assert normalized["customer_profile"]["age"] == "32岁"


def test_normalize_analysis_result_canonicalizes_or_drops_invalid_enum_profile_tags():
    result = {
        "customer_profile": {
            "tags": [
                {"category": "价格敏感度", "value": "较高"},
                {"category": "亲属/子女情况", "value": "2孩"},
                {"category": "决策主体", "value": "自主决策"},
                {"category": "常驻城市", "value": "外地（沈阳）"},
                {"category": "个人情况", "value": "在校学生"},
                {"category": "常驻城市", "value": "成都"},
            ]
        },
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": [],
            "evidence": [],
        },
        "staff_recommendations": {"items": []},
        "standardized_indications": {"summary": "", "items": []},
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    assert normalized["customer_profile"]["tags"] == [
        {"category": "价格敏感度", "value": "高"},
        {"category": "亲属/子女情况", "value": "2孩及以上"},
        {"category": "决策主体", "value": "自主"},
        {"category": "常驻城市", "value": "外地"},
    ]


def test_normalize_analysis_result_collapses_conflicting_single_select_profile_tags():
    result = {
        "customer_profile": {
            "tags": [
                {"category": "价格敏感度", "value": "中", "evidence": "[00:10] 可以优惠一点吗"},
                {"category": "价格敏感度", "value": "高", "evidence": "[00:20] 这个价格太高了"},
                {"category": "决策主体", "value": "自主", "evidence": "[00:30] 我自己看看"},
                {"category": "决策主体", "value": "伴侣", "evidence": "[00:40] 还要跟老公商量"},
            ]
        },
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": [],
            "evidence": [],
        },
        "staff_recommendations": {"items": []},
        "standardized_indications": {"summary": "", "items": []},
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    assert normalized["customer_profile"]["tags"] == [
        {"category": "价格敏感度", "value": "高", "evidence": "[00:20] 这个价格太高了"},
        {"category": "决策主体", "value": "伴侣", "evidence": "[00:40] 还要跟老公商量"},
    ]


def test_normalize_analysis_result_drops_no_risk_placeholder_when_concrete_health_risk_exists():
    result = {
        "customer_profile": {
            "tags": [
                {"category": "健康风险/禁忌", "value": "无风险禁忌", "evidence": "[00:05] 不过敏"},
                {"category": "健康风险/禁忌", "value": "高血压", "evidence": "[00:15] 我有高血压"},
            ]
        },
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": [],
            "evidence": [],
        },
        "staff_recommendations": {"items": []},
        "standardized_indications": {"summary": "", "items": []},
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    assert normalized["customer_profile"]["tags"] == [
        {"category": "健康风险/禁忌", "value": "高血压", "evidence": "[00:15] 我有高血压"},
    ]


def test_normalize_analysis_result_prefers_no_prior_treatment_over_conflicting_history_values():
    result = {
        "customer_profile": {
            "tags": [
                {"category": "治疗项目", "value": "无医美史", "evidence": "[00:10] 以前没做过"},
                {"category": "治疗项目", "value": "手术类", "evidence": "[00:20] 以前做过双眼皮"},
            ]
        },
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": [],
            "evidence": [],
        },
        "staff_recommendations": {"items": []},
        "standardized_indications": {"summary": "", "items": []},
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    assert normalized["customer_profile"]["tags"] == [
        {"category": "治疗项目", "value": "无医美史", "evidence": "[00:10] 以前没做过"},
        {"category": "历史用的设备/原材料名称", "value": "无"},
        {"category": "负面项目/设备/原材料", "value": "无"},
    ]


def test_normalize_analysis_result_replaces_concrete_history_device_when_no_prior_treatment_present():
    result = {
        "customer_profile": {
            "tags": [
                {"category": "治疗项目", "value": "无医美史", "evidence": "[00:10] 以前没做过"},
                {"category": "历史用的设备/原材料名称", "value": "光子", "evidence": "[00:20] 以前做过光子"},
                {"category": "负面项目/设备/原材料", "value": "光电", "evidence": "[00:20] 以前做过光子"},
            ]
        },
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": [],
            "evidence": [],
        },
        "staff_recommendations": {"items": []},
        "standardized_indications": {"summary": "", "items": []},
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    assert normalized["customer_profile"]["tags"] == [
        {"category": "治疗项目", "value": "无医美史", "evidence": "[00:10] 以前没做过"},
        {"category": "历史用的设备/原材料名称", "value": "无"},
        {"category": "负面项目/设备/原材料", "value": "无"},
    ]


def test_normalize_analysis_result_drops_open_text_placeholder_but_keeps_allowed_negative_none():
    result = {
        "customer_profile": {
            "tags": [
                {"category": "治疗项目", "value": "光电治疗"},
                {"category": "历史用的设备/原材料名称", "value": "未提及具体设备"},
                {"category": "负面项目/设备/原材料", "value": "无"},
            ]
        },
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": [],
            "evidence": [],
        },
        "staff_recommendations": {"items": []},
        "standardized_indications": {"summary": "", "items": []},
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    assert normalized["customer_profile"]["tags"] == [
        {"category": "治疗项目", "value": "光电类"},
        {"category": "负面项目/设备/原材料", "value": "无"},
    ]


def test_normalize_analysis_result_maps_explicit_no_prior_treatment_to_treatment_project():
    result = {
        "customer_profile": {
            "tags": [
                {"category": "既往医美治疗", "value": "未做过医美项目"},
                {"category": "护肤习惯", "value": "基础水乳防晒"},
                {"category": "负面项目/设备/原材料", "value": "无"},
            ]
        },
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": [],
            "evidence": [],
        },
        "staff_recommendations": {"items": []},
        "standardized_indications": {"summary": "", "items": []},
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    assert normalized["customer_profile"]["tags"] == [
        {"category": "治疗项目", "value": "未做过医美项目"},
        {"category": "历史用的设备/原材料名称", "value": "无"},
        {"category": "负面项目/设备/原材料", "value": "无"},
        {"category": "护肤习惯", "value": "基础水乳防晒"},
    ]


def test_normalize_analysis_result_replaces_negative_project_placeholder_with_concrete_value():
    result = {
        "customer_profile": {
            "tags": [
                {"category": "负面项目/设备/原材料", "value": "项目/设备/原材料名称"},
                {"category": "负面项目/设备/原材料名称", "value": "热玛吉"},
                {"category": "负面项目/设备/原材料", "value": "无"},
            ]
        },
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": [],
            "evidence": [],
        },
        "staff_recommendations": {"items": []},
        "standardized_indications": {"summary": "", "items": []},
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    assert normalized["customer_profile"]["tags"] == [
        {"category": "负面项目/设备/原材料", "value": "热玛吉"},
    ]


def test_normalize_analysis_result_builds_consultation_result_from_legacy_fields():
    result = {
        "customer_primary_demands": {
            "summary": "面部松弛，希望提升紧致度",
            "items": [
                {
                    "priority": 1,
                    "demand": "面部松弛，希望提升紧致度",
                    "body_part": "面部",
                    "evidence": "[00:18] 我就想让脸紧一点",
                }
            ],
        },
        "standardized_indications": {
            "summary": "识别出1项适应症：局部减脂（面部）",
            "items": [
                {
                    "department_code": "Y3",
                    "department_name": "皮肤",
                    "indication_code": "SYZ3020",
                    "indication_name": "局部减脂",
                    "body_part_code": "BW3001",
                    "body_part_name": "面部",
                    "evidence": "[02:01] 脸这块肉感比较重，想瘦一点",
                }
            ],
        },
        "consumption_intent": {
            "budget": "2万-3万",
            "willingness": "中",
            "decision_factors": ["恢复期"],
            "evidence": ["[06:10] 恢复期会不会很长"],
        },
        "staff_recommendations": {
            "summary": "建议超声炮配合面部提升方案",
            "items": [
                {
                    "recommendation": "超声炮",
                    "product_or_solution": "超声炮",
                    "body_part": "面部",
                    "demand_priority": [1],
                    "evidence": "[02:11] 你更适合先做超声炮提升",
                    "customer_response": "犹豫",
                }
            ],
        },
        "customer_demands": {
            "focus_areas": [],
            "expectation": {"turning_points": []},
            "product_preference": {},
        },
        "customer_concerns": {
            "summary": "担心恢复期影响上班。",
            "items": [
                {
                    "type": "效果类",
                    "content": "担心恢复期影响上班",
                    "evidence": "[06:10] 恢复期会不会影响我上班",
                }
            ],
        },
        "customer_profile": {
            "tags": [
                {"category": "出生日期", "value": "1995-05-01"},
            ]
        },
        "consultation_evaluation": {
            "overall_summary": "整体沟通较完整。",
            "dimensions": [],
        },
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    consultation_result = normalized["consultation_result"]
    assert (
        consultation_result["chief_complaint_and_indications"]["primary_demands"]
        == ["面部松弛，希望提升紧致度"]
    )
    assert (
        consultation_result["chief_complaint_and_indications"]["standardized_indications"]
        == ["皮肤（Y3）｜局部减脂（SYZ3020）｜面部（BW3001）"]
    )
    assert consultation_result["customer_profile_summary"]["extracted_tag_count"] == 1
    assert consultation_result["deal_factors"]["budget"] == "2万-3万"
    assert consultation_result["deal_factors"]["concerns"] == ["担心恢复期影响上班"]
    assert consultation_result["recommended_plan"]["items"][0]["acceptance"] == "犹豫"
    assert consultation_result["deal_outcome"]["status"] == "未明确"


def test_normalize_analysis_result_refreshes_stale_profile_summary_when_tags_exist():
    result = {
        "customer_primary_demands": {"summary": "", "items": []},
        "standardized_indications": {"summary": "", "items": []},
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": [],
            "evidence": [],
        },
        "staff_recommendations": {"summary": "", "items": []},
        "customer_demands": {
            "focus_areas": [],
            "expectation": {"turning_points": []},
            "product_preference": {},
        },
        "customer_concerns": {"summary": "", "items": []},
        "customer_profile": {
            "tags": [
                {"category": "治疗项目", "value": "无医美史"},
                {"category": "倾向回访方式", "value": "微信"},
            ]
        },
        "consultation_evaluation": {"overall_summary": "", "dimensions": []},
        "consultation_result": {
            "customer_profile_summary": {
                "summary": "本次录音暂未提取出明确画像标签。",
                "extracted_tag_count": 0,
                "tags": [],
            }
        },
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    profile_summary = normalized["consultation_result"]["customer_profile_summary"]
    assert profile_summary["summary"] == "本次录音共提取 4 个画像标签。"
    assert profile_summary["extracted_tag_count"] == 4


def test_normalize_analysis_result_clears_stale_profile_summary_when_tags_absent():
    result = {
        "customer_primary_demands": {"summary": "", "items": []},
        "standardized_indications": {"summary": "", "items": []},
        "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
        "staff_recommendations": {"summary": "", "items": []},
        "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}, "product_preference": {}},
        "customer_concerns": {"summary": "", "items": []},
        "customer_profile": {"tags": []},
        "consultation_evaluation": {"overall_summary": "", "dimensions": []},
        "consultation_result": {
            "customer_profile_summary": {
                "summary": "识别到客户有注射史、玻尿酸使用史、无禁忌",
                "extracted_tag_count": 3,
                "tags": [],
            }
        },
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    profile_summary = normalized["consultation_result"]["customer_profile_summary"]
    assert profile_summary["summary"] == "本次录音暂未提取出明确画像标签。"
    assert profile_summary["extracted_tag_count"] == 0
    assert profile_summary["tags"] == []


def test_normalize_analysis_result_rebuilds_stale_profile_summary_when_tags_changed():
    result = {
        "customer_primary_demands": {"summary": "", "items": []},
        "standardized_indications": {"summary": "", "items": []},
        "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
        "staff_recommendations": {"summary": "", "items": []},
        "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}, "product_preference": {}},
        "customer_concerns": {"summary": "", "items": []},
        "customer_profile": {
            "tags": [
                {"category": "治疗项目", "value": "注射类", "weight_level": 1},
            ]
        },
        "consultation_evaluation": {"overall_summary": "", "dimensions": []},
        "consultation_result": {
            "customer_profile_summary": {
                "summary": "识别到客户有注射史、玻尿酸使用史、无禁忌",
                "extracted_tag_count": 3,
                "tags": [],
            }
        },
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    profile_summary = normalized["consultation_result"]["customer_profile_summary"]
    assert profile_summary["summary"] == "本次录音共提取 2 个画像标签。"
    assert profile_summary["extracted_tag_count"] == 2


def test_normalize_analysis_result_does_not_fallback_to_stale_first_item_existing_values():
    result = {
        "customer_primary_demands": {"summary": "", "items": []},
        "standardized_indications": {"summary": "未识别出明确适应症。", "items": []},
        "consumption_intent": {"budget": None, "willingness": None, "decision_factors": [], "evidence": []},
        "staff_recommendations": {"summary": "", "items": []},
        "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}, "product_preference": {}},
        "customer_concerns": {"summary": "", "items": []},
        "customer_profile": {"tags": []},
        "consultation_evaluation": {"overall_summary": "", "dimensions": []},
        "consultation_result": {
            "chief_complaint_and_indications": {
                "summary": "识别出1项适应症：双眼皮（眼部）",
                "primary_demands": ["改善面部松弛下垂"],
                "standardized_indications": ["外科（Y1）｜双眼皮（SYZ1002）｜眼部（BW1001）"],
            }
        },
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    chief = normalized["consultation_result"]["chief_complaint_and_indications"]
    assert chief["primary_demands"] == []
    assert chief["standardized_indications"] == []
    assert chief["summary"] == "对话中未识别出可标准化的适应症"


def test_normalize_analysis_result_rebuilds_stale_outcome_summary_without_legacy_score_text():
    result = {
        "customer_primary_demands": {"summary": "", "items": []},
        "standardized_indications": {"summary": "", "items": []},
        "consumption_intent": {
            "budget": "2万-3万",
            "willingness": "未明确",
            "decision_factors": ["恢复期"],
            "evidence": [],
        },
        "staff_recommendations": {
            "summary": "建议超声炮联合提升方案",
            "items": [
                {
                    "recommendation": "超声炮联合提升",
                    "product_or_solution": "超声炮联合提升",
                    "customer_response": "犹豫",
                    "evidence": "[02:01] 你可以先做超声炮联合提升",
                }
            ],
        },
        "customer_demands": {
            "focus_areas": [],
            "expectation": {"turning_points": []},
            "product_preference": {},
        },
        "customer_concerns": {
            "summary": "担心恢复期和价格。",
            "items": [
                {"type": "效果类", "content": "担心恢复期", "evidence": "[06:10] 恢复期会不会太长"},
                {"type": "价格类", "content": "觉得价格偏高", "evidence": "[06:18] 这个价格还是有点高"},
            ],
        },
        "customer_profile": {"tags": []},
        "consultation_evaluation": {
            "overall_summary": "六维得分 3.07/6。咨询师整体专业度较高。",
            "dimensions": [],
        },
        "consultation_result": {
            "deal_outcome": {
                "status": "未明确",
                "summary": "六维得分 3.07/6。咨询师整体专业度较高。",
                "deal_items": [],
                "amount": None,
                "loss_reasons": [],
            }
        },
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    outcome = normalized["consultation_result"]["deal_outcome"]
    assert "六维得分" not in outcome["summary"]
    assert "九点评价" not in outcome["summary"]
    assert "已形成方案沟通与决策讨论" in outcome["summary"]


def test_normalize_analysis_result_builds_process_evaluation_skeleton_from_legacy_dimensions():
    result = {
        "customer_primary_demands": {"summary": "", "items": []},
        "standardized_indications": {"summary": "", "items": []},
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": [],
            "evidence": [],
        },
        "staff_recommendations": {"summary": "", "items": []},
        "customer_demands": {
            "focus_areas": [],
            "expectation": {"turning_points": []},
            "product_preference": {},
        },
        "customer_concerns": {"summary": "", "items": []},
        "customer_profile": {"tags": []},
        "consultation_evaluation": {
            "overall_summary": "总体沟通尚可，医生介绍和转介绍动作有提及。",
            "dimensions": [
                {
                    "name": "医院和医生介绍",
                    "point_score": 1,
                    "max_score": 1,
                    "status": "达标",
                    "summary": "介绍了医生专业背景。",
                    "issues": [],
                },
                {
                    "name": "老带新等特别事项",
                    "point_score": 0,
                    "max_score": 1,
                    "status": "未达标",
                    "summary": "没有明确做老带新开口种草。",
                    "issues": [
                        {
                            "description": "未主动提及老带新权益",
                            "evidence": "[18:30] 全程未提到老带新",
                        }
                    ],
                },
            ],
        },
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    process_evaluation = normalized["consultation_process_evaluation"]
    assert process_evaluation["overall_summary"] == "总体沟通尚可，医生介绍和转介绍动作有提及。"
    assert len(process_evaluation["sections"]) == 9

    doctor_section = next(
        section for section in process_evaluation["sections"] if section["code"] == "doctor_consultation"
    )
    doctor_intro = next(
        checkpoint for checkpoint in doctor_section["checkpoints"] if checkpoint["code"] == "4.1"
    )
    assert doctor_intro["point_score"] == 1
    assert doctor_intro["summary"] == "介绍了医生专业背景。"

    required_actions = next(
        section for section in process_evaluation["sections"] if section["code"] == "required_actions"
    )
    referral_checkpoint = next(
        checkpoint for checkpoint in required_actions["checkpoints"] if checkpoint["code"] == "8.2"
    )
    assert referral_checkpoint["point_score"] == 0
    assert referral_checkpoint["summary"] == "没有明确做老带新开口种草。"
    assert referral_checkpoint["issues"] == [
        {
            "description": "未主动提及老带新权益",
            "evidence": "[18:30] 全程未提到老带新",
        }
    ]


def test_normalize_analysis_result_dedupes_decision_factors_against_concerns():
    result = {
        "customer_profile": {"tags": []},
        "staff_recommendations": {"items": []},
        "standardized_indications": {"summary": "", "items": []},
        "customer_concerns": {
            "items": [
                {
                    "type": "效果类",
                    "content": "担心效果不够自然或不明显",
                    "evidence": "[00:10] 我怕效果不自然。",
                },
                {
                    "type": "恢复类",
                    "content": "担心恢复期、肿胀或影响上班",
                    "evidence": "[00:20] 我还要上班，怕肿。",
                },
                {
                    "type": "风险类",
                    "content": "担心风险、副作用或安全性",
                    "evidence": "[00:30] 我有点担心风险。",
                },
            ]
        },
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": ["价格", "恢复期", "效果", "风险", "家庭决策"],
            "evidence": [
                "[00:10] 我怕效果不自然。",
                "[00:20] 我还要上班，怕肿。",
                "[00:30] 我有点担心风险。",
                "[00:40] 妈妈眼袋还是蛮明显的。",
            ],
        },
        "consultation_result": {
            "deal_factors": {
                "concerns": [
                    "担心效果不够自然或不明显",
                    "担心恢复期、肿胀或影响上班",
                    "担心风险、副作用或安全性",
                ],
                "decision_factors": ["价格", "恢复期", "效果", "风险", "家庭决策"],
            }
        },
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    assert normalized["consultation_result"]["deal_factors"]["decision_factors"] == []
    assert normalized["consumption_intent"]["decision_factors"] == ["价格", "恢复期", "效果", "风险", "家庭决策"]


def test_normalize_analysis_result_keeps_family_decision_when_explicitly_mentioned():
    result = {
        "customer_profile": {"tags": []},
        "staff_recommendations": {"items": []},
        "standardized_indications": {"summary": "", "items": []},
        "customer_concerns": {
            "items": [
                {
                    "type": "决策类",
                    "content": "仍需考虑、商量或继续比较",
                    "evidence": "[00:50] 我得跟老公商量一下再决定。",
                }
            ]
        },
        "consumption_intent": {
            "budget": None,
            "willingness": "未明确",
            "decision_factors": ["家庭决策"],
            "evidence": ["[00:50] 我得跟老公商量一下再决定。"],
        },
        "consultation_result": {
            "deal_factors": {
                "concerns": ["仍需考虑、商量或继续比较"],
                "decision_factors": ["家庭决策"],
            },
            "deal_outcome": {
                "status": "未成交",
                "loss_reasons": ["仍需考虑或商量"],
            },
        },
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    assert normalized["consultation_result"]["deal_factors"]["decision_factors"] == []


def test_normalize_analysis_result_backfills_indication_for_waterlight_primary_demand():
    result = {
        "customer_primary_demands": {
            "summary": "脸上痘印多，希望通过光子淡化痘印；想补水，纠结是否光子和水光一起做",
            "items": [
                {
                    "priority": 1,
                    "demand": "脸上痘印多，希望通过光子淡化痘印",
                    "body_part": "面部",
                    "evidence": "[03:00] 我就想就是光子的话，我主要是想去一下痘印。",
                },
                {
                    "priority": 2,
                    "demand": "想补水，纠结是否光子和水光一起做",
                    "body_part": "面部",
                    "evidence": "[01:57] 如果我今天要做的话，我是不是要光子跟水光一起做？",
                },
            ],
        },
        "standardized_indications": {
            "summary": "识别出1项适应症：痤疮（面部）",
            "items": [
                {
                    "department_code": "Y3",
                    "department_name": "皮肤",
                    "indication_code": "SYZ3005",
                    "indication_name": "痤疮",
                    "body_part_code": "BW3001",
                    "body_part_name": "面部",
                    "evidence": "[03:00] 我就想就是光子的话，我主要是想去一下痘印。",
                }
            ],
        },
        "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
        "staff_recommendations": {"summary": "", "items": []},
        "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}, "product_preference": {}},
        "customer_concerns": {"summary": "", "items": []},
        "customer_profile": {"tags": []},
        "consultation_evaluation": {"overall_summary": "", "dimensions": []},
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    indications = normalized["standardized_indications"]["items"]
    assert [(item["indication_code"], item["body_part_code"]) for item in indications] == [
        ("SYZ3005", "BW3001"),
        ("SYZ3006", "BW3001"),
    ]
    assert normalized["consultation_result"]["chief_complaint_and_indications"]["standardized_indications"] == [
        "皮肤（Y3）｜痤疮（SYZ3005）｜面部（BW3001）",
        "皮肤（Y3）｜干燥（SYZ3006）｜面部（BW3001）",
    ]


def test_normalize_analysis_result_fills_closed_deal_plan_and_unknown_amount():
    result = {
        "customer_primary_demands": {
            "summary": "脸上痘印多，希望通过光子淡化痘印",
            "items": [
                {
                    "priority": 1,
                    "demand": "脸上痘印多，希望通过光子淡化痘印",
                    "body_part": "面部",
                    "evidence": "[03:00] 我主要是想去一下痘印。",
                }
            ],
        },
        "standardized_indications": {"summary": "", "items": []},
        "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
        "staff_recommendations": {
            "summary": "水光足量两支或叠加娃娃针",
            "items": [
                {
                    "recommendation": "水光足量两支或叠加娃娃针",
                    "product_or_solution": "嗨体2.5两支/叠加娃娃针",
                    "customer_response": "犹豫",
                    "evidence": "[06:21] 你可以单独购一支润百颜波波，或者娃娃针。",
                }
            ],
        },
        "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}, "product_preference": {}},
        "customer_concerns": {"summary": "", "items": []},
        "customer_profile": {"tags": []},
        "consultation_evaluation": {"overall_summary": "", "dimensions": []},
        "consultation_result": {
            "deal_outcome": {
                "status": "已成交",
                "summary": "客户完成会员注册及套餐核销流程，倾向当天先做光子。",
                "deal_items": [],
                "amount": None,
                "loss_reasons": [],
            }
        },
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    outcome = normalized["consultation_result"]["deal_outcome"]
    assert outcome["status"] == "已成交"
    assert outcome["deal_items"] == ["光子嫩肤", "水光/嗨体"]
    assert outcome["amount"] == "未明确"
    assert outcome["loss_reasons"] == []


def test_normalize_analysis_result_drops_future_only_laxity_primary_demand():
    result = {
        "customer_primary_demands": {
            "summary": "双眼皮修复；改善面部松弛下垂",
            "items": [
                {
                    "priority": 1,
                    "demand": "双眼皮塌陷偏厚、肿泡明显，想修复得自然",
                    "body_part": "眼部",
                    "evidence": "[03:15] 这种肿泡眼，我是怕那个做出来就是肉条感很重。",
                },
                {
                    "priority": 2,
                    "demand": "改善面部松弛下垂",
                    "body_part": "面部",
                    "evidence": "[16:26] 然后我就怕以后上年纪再想做，然后又又松弛。",
                },
            ],
        },
        "standardized_indications": {"summary": "", "items": []},
        "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
        "staff_recommendations": {"summary": "", "items": []},
        "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}, "product_preference": {}},
        "customer_concerns": {"summary": "", "items": []},
        "customer_profile": {"tags": []},
        "consultation_evaluation": {"overall_summary": "", "dimensions": []},
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    demands = normalized["customer_primary_demands"]["items"]
    assert [item["demand"] for item in demands] == ["双眼皮塌陷偏厚、肿泡明显，想修复得自然"]
    assert normalized["consultation_result"]["chief_complaint_and_indications"]["primary_demands"] == [
        "双眼皮塌陷偏厚、肿泡明显，想修复得自然"
    ]


def test_normalize_analysis_result_downgrades_closed_status_when_summary_says_no_deposit_and_discuss():
    result = {
        "customer_primary_demands": {"summary": "", "items": []},
        "standardized_indications": {"summary": "", "items": []},
        "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
        "staff_recommendations": {"summary": "", "items": []},
        "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}, "product_preference": {}},
        "customer_concerns": {"summary": "", "items": []},
        "customer_profile": {"tags": []},
        "consultation_evaluation": {"overall_summary": "", "dimensions": []},
        "consultation_result": {
            "deal_outcome": {
                "status": "已成交",
                "summary": "客户认可修复方案与医生，但暂未支付定金，计划回去与老公商量后再决定。",
                "deal_items": [],
                "amount": None,
                "loss_reasons": [],
            }
        },
    }

    normalized = normalize_analysis_result(result)

    assert normalized is not None
    outcome = normalized["consultation_result"]["deal_outcome"]
    assert outcome["status"] == "未成交"
    assert outcome["deal_items"] == []
    assert outcome["amount"] is None
