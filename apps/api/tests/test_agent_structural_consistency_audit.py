from smart_badge_api.analysis.agent_pipeline import _agent_run_structural_consistency_audit


def test_structural_audit_relinks_recommendation_to_matching_demand_only() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {"priority": 1, "demand": "希望做双眼皮，改善内双偏窄", "body_part": "眼部-双眼皮"},
                {"priority": 2, "demand": "想先做眼袋，改善下眼袋问题", "body_part": "眼部-眼袋"},
            ]
        },
        "staff_recommendations": {
            "items": [
                {
                    "recommendation": "微创内切去眼袋，必要时利用眼袋脂肪回填泪沟",
                    "body_part": "眼部-眼袋/泪沟",
                    "price": "单做3980元；与双眼皮等综合约6800元",
                    "evidence": "下面的话就是内切眼袋，自体脂肪填泪沟；也可以和双眼皮综合打包。",
                    "demand_priority": [1, 2],
                }
            ]
        },
    }

    assert _agent_run_structural_consistency_audit(result)

    assert result["staff_recommendations"]["items"][0]["demand_priority"] == [2]


def test_structural_audit_promotes_seed_plan_that_solves_current_demand() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {"priority": 1, "demand": "希望改善下巴后缩，让下巴更翘", "body_part": "下巴"},
            ]
        },
        "staff_recommendations": {"items": []},
        "staff_seed_recommendations": {
            "items": [
                {
                    "recommendation": "下巴两支硬质玻尿酸做支撑和翘度塑形",
                    "body_part": "下巴",
                    "material": "硬质玻尿酸",
                    "demand_priority": [],
                }
            ]
        },
    }

    assert _agent_run_structural_consistency_audit(result)

    assert result["staff_recommendations"]["items"][0]["recommendation"].startswith("下巴两支")
    assert result["staff_seed_recommendations"]["items"] == []


def test_structural_audit_recovers_explicit_customer_demand_generically() -> None:
    result = {
        "customer_primary_demands": {"items": []},
        "staff_recommendations": {"items": []},
    }
    context = "\n".join(
        [
            "[00:00-00:02] 主客户: 我想做下巴，想让下巴翘一点。",
            "[00:03-00:05] 咨询师: 可以用玻尿酸支撑。",
        ]
    )

    assert _agent_run_structural_consistency_audit(result, context=context)

    assert result["customer_primary_demands"]["items"][0]["body_part"] == "下巴"


def test_structural_audit_prunes_auxiliary_step_indication() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {"priority": 1, "demand": "希望做双眼皮，改善内双偏窄", "body_part": "眼部-双眼皮"},
            ]
        },
        "staff_recommendations": {
            "items": [
                {
                    "recommendation": "全切双眼皮自然款，术中眶隔释放，必要时少量脂肪填眼窝",
                    "body_part": "眼部-双眼皮",
                    "demand_priority": [1],
                }
            ]
        },
        "standardized_indications": {
            "items": [
                {
                    "department_name": "外科",
                    "indication_name": "双眼皮",
                    "body_part_name": "眼部",
                    "evidence": "客户想做双眼皮，医生建议全切双眼皮。",
                },
                {
                    "department_name": "外科",
                    "indication_name": "面部填充",
                    "body_part_name": "面部",
                    "evidence": "术中眶隔释放，必要时少量脂肪填眼窝，服务于双眼皮成型。",
                },
            ]
        },
    }

    assert _agent_run_structural_consistency_audit(result)

    assert [(item["indication_name"], item["body_part_name"]) for item in result["standardized_indications"]["items"]] == [
        ("双眼皮", "眼部")
    ]


def test_structural_audit_dedupes_same_body_liposuction_region_demands() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {
                    "priority": 1,
                    "demand": "产后腰腹脂肪堆积、减重效果不佳，希望通过吸脂改善腰腹线条",
                    "body_part": "腰腹",
                    "evidence": "想要了解一下腰腹吸脂；生了孩子腰上脂肪开始堆了，怎么减都不好减。",
                },
                {
                    "priority": 2,
                    "demand": "减肥一年多肚子仍未改善，想解决腹部肥胖问题",
                    "body_part": "腹部",
                    "evidence": "减肥一年多了，肚子还是这个样子。",
                },
                {
                    "priority": 3,
                    "demand": "生孩子后腰部脂肪堆积，难以通过减重改善，希望解决",
                    "body_part": "腰部",
                    "evidence": "生了孩子腰上脂肪就开始堆了，怎么减它都不好减。",
                },
                {
                    "priority": 4,
                    "demand": "手臂赘肉明显，希望通过吸脂改善拜拜肉",
                    "body_part": "手臂",
                    "evidence": "手臂这块也能吸脂吗？",
                },
            ]
        }
    }

    assert _agent_run_structural_consistency_audit(result)

    demand_texts = [item["demand"] for item in result["customer_primary_demands"]["items"]]
    assert demand_texts == [
        "产后腰腹脂肪堆积、减重效果不佳，希望通过吸脂改善腰腹线条",
        "手臂赘肉明显，希望通过吸脂改善拜拜肉",
    ]


def test_structural_audit_removes_chin_filler_artifact_from_double_chin_liposuction_context() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {
                    "priority": 1,
                    "demand": "产后腰腹脂肪堆积、减重效果不佳，希望通过吸脂改善腰腹线条",
                    "body_part": "腰腹",
                    "evidence": "想了解腰腹吸脂。",
                },
                {
                    "priority": 2,
                    "demand": "改善下巴两侧衔接及中间翘度/长度",
                    "body_part": "下巴",
                    "evidence": "医生: 双下巴也就是一般做吸脂 / 主客户: 也是这些脂啊",
                },
            ]
        }
    }
    context = "\n".join(
        [
            "[00:00-00:02] 主客户: 我想了解腰腹吸脂。",
            "[00:03-00:04] 医生: 双下巴也就是一般做吸脂。",
            "[00:05-00:06] 主客户: 也是这些脂啊。",
        ]
    )

    assert _agent_run_structural_consistency_audit(result, context=context)

    assert [item["demand"] for item in result["customer_primary_demands"]["items"]] == [
        "产后腰腹脂肪堆积、减重效果不佳，希望通过吸脂改善腰腹线条"
    ]


def test_structural_audit_demotes_recommendation_that_no_longer_matches_any_current_demand() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {
                    "priority": 1,
                    "demand": "产后腰腹脂肪堆积、减重效果不佳，希望通过吸脂改善腰腹线条",
                    "body_part": "腰腹",
                },
                {
                    "priority": 2,
                    "demand": "咨询手臂是否可以一起吸脂",
                    "body_part": "手臂",
                },
            ]
        },
        "staff_recommendations": {
            "items": [
                {
                    "recommendation": "腰腹环吸+妈妈臀吸脂",
                    "body_part": "腰腹/妈妈臀",
                    "demand_priority": [1],
                },
                {
                    "recommendation": "微创内切去眼袋，必要时利用眼袋脂肪回填泪沟",
                    "body_part": "眼部-眼袋/泪沟",
                    "demand_priority": [2],
                },
            ]
        },
        "staff_seed_recommendations": {"items": []},
    }

    assert _agent_run_structural_consistency_audit(result)

    assert [item["recommendation"] for item in result["staff_recommendations"]["items"]] == ["腰腹环吸+妈妈臀吸脂"]
    assert [item["recommendation"] for item in result["staff_seed_recommendations"]["items"]] == [
        "微创内切去眼袋，必要时利用眼袋脂肪回填泪沟"
    ]


def test_structural_audit_removes_generated_chin_filler_plan_artifact_without_explicit_context() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {
                    "priority": 1,
                    "demand": "产后腰腹脂肪堆积、减重效果不佳，希望通过吸脂改善腰腹线条",
                    "body_part": "腰腹",
                }
            ]
        },
        "staff_recommendations": {"items": []},
        "staff_seed_recommendations": {
            "items": [
                {
                    "recommendation": "下巴两支硬质玻尿酸做两侧衔接和中间翘度/长度塑形",
                    "body_part": "下巴",
                    "evidence": "医生: 双下巴也就是一般做吸脂。",
                }
            ]
        },
    }
    context = "[00:00-00:02] 医生: 双下巴也就是一般做吸脂。"

    assert _agent_run_structural_consistency_audit(result, context=context)

    assert result["staff_seed_recommendations"]["items"] == []


def test_structural_audit_collapses_overbroad_injection_demands() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {
                    "priority": 1,
                    "demand": "希望改善两边脸型不对称、存在断层感",
                    "body_part": "面部（面颊/中面部）",
                    "evidence": "两边脸型有点完全不一样。",
                },
                {
                    "priority": 2,
                    "demand": "面颊及外轮廓两侧不对称、凹陷及断层感，希望通过注射改善整体脸型",
                    "body_part": "面部-面颊/外轮廓",
                    "evidence": "我现在其实最想解决的就是这个注射这个脸部…两边脸型有点完全不一样",
                },
                {
                    "priority": 3,
                    "demand": "希望将既往注射后形成的一坨材料部分溶解",
                    "body_part": "面部（注射区域）",
                    "evidence": "我想把这一点融一下，我想把这里融掉一点。",
                },
                {
                    "priority": 4,
                    "demand": "希望把之前注射后形成的一坨融掉一点（考虑用溶解酶）",
                    "body_part": "面部（具体部位未明确）",
                    "evidence": "以前打的少女针在这里有一坨坨，我也想融。",
                },
                {
                    "priority": 5,
                    "demand": "希望改善胶原问题",
                    "body_part": "胶原",
                    "evidence": "我知道溶解酶还没到呢，我想把这一点融一下。",
                },
                {
                    "priority": 6,
                    "demand": "认为面中部位注射偏多，希望不过度填充",
                    "body_part": "面中部",
                    "evidence": "笑的时候膨出来，感觉太满。",
                },
                {
                    "priority": 7,
                    "demand": "鼻基底凹陷既往问题，填充后觉得不自然",
                    "body_part": "鼻基底",
                    "evidence": "之前鼻基底凹陷，后面填了之后觉得不自然。",
                },
                {
                    "priority": 8,
                    "demand": "希望改善鼻子问题",
                    "body_part": "鼻子",
                    "evidence": "鼻子我现在不考虑在长沙做，我想去外省做。",
                },
                {
                    "priority": 9,
                    "demand": "下巴多次注射后不满意，希望改善骨感与支撑",
                    "body_part": "下巴",
                    "evidence": "其实，我还想做个人中缩短手术。",
                },
                {
                    "priority": 10,
                    "demand": "下巴多次注射后不满意，觉得不好看",
                    "body_part": "下巴",
                    "evidence": "我每一次打完下巴的时候，我都是不满意的，我说不好看。",
                },
            ]
        },
        "staff_recommendations": {
            "items": [
                {
                    "recommendation": "面颊、耳前及太阳穴玻尿酸各1ml对称微调",
                    "body_part": "面颊/耳前/太阳穴",
                    "demand_priority": [2],
                },
                {
                    "recommendation": "下巴运动位凝胶型玻尿酸少量塑形",
                    "body_part": "下巴",
                    "demand_priority": [9],
                },
            ]
        },
        "staff_seed_recommendations": {"items": []},
    }
    context = "\n".join(
        [
            "[00:00-00:02] 主客户: 我现在其实最想解决的就是注射这个脸部，两边脸型不一样。",
            "[00:03-00:05] 主客户: 我想把这一点融一下，以前打的少女针在这里有一坨坨。",
            "[00:06-00:08] 主客户: 鼻子我现在不考虑在长沙做，我想去外省做。",
            "[00:09-00:11] 主客户: 每一次打完下巴我都不满意，觉得不好看。",
        ]
    )

    assert _agent_run_structural_consistency_audit(result, context=context)

    assert [item["demand"] for item in result["customer_primary_demands"]["items"]] == [
        "面颊及外轮廓两侧不对称、凹陷及断层感，希望通过注射改善整体脸型",
        "希望把之前注射后形成的一坨融掉一点（考虑用溶解酶）",
        "下巴多次注射后不满意，觉得不好看",
    ]


def test_structural_audit_keeps_current_skin_abnormality_demand() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {
                    "priority": 1,
                    "demand": "面部出现小疹子、发红并伴有黄色渗出，希望处理当前皮肤异常",
                    "body_part": "面部皮肤",
                    "evidence": "刚爆发的时候，就只是长疹子，然后会红，但是没有这种黄色的…现在也是会有一点黄色的那种渗出液。",
                },
                {
                    "priority": 2,
                    "demand": "面部有轻微瘙痒感，希望缓解不适",
                    "body_part": "面部皮肤",
                    "evidence": "现在是会有一点点痒。",
                },
                {
                    "priority": 3,
                    "demand": "面部有瘙痒感",
                    "body_part": "面部皮肤",
                    "evidence": "有点痒。",
                },
            ]
        },
        "staff_recommendations": {
            "items": [
                {
                    "recommendation": "先进行冷喷湿敷舒缓当前皮肤炎症，再观察恢复情况",
                    "body_part": "面部皮肤",
                    "demand_priority": [1],
                }
            ]
        },
        "staff_seed_recommendations": {"items": []},
    }

    assert _agent_run_structural_consistency_audit(result)

    assert [item["demand"] for item in result["customer_primary_demands"]["items"]] == [
        "面部出现小疹子、发红并伴有黄色渗出，希望处理当前皮肤异常",
    ]


def test_structural_audit_collapses_eye_tail_fragments() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {"priority": 1, "demand": "改善眼尾下垂问题", "body_part": "眼部", "evidence": "想改善这个眼尾下垂的问题。"},
                {"priority": 2, "demand": "外侧眼尾下垂", "body_part": "眼部", "evidence": "外侧眼尾也有一点垂。"},
                {
                    "priority": 3,
                    "demand": "喜欢当前示例方案中的眼尾效果，希望眼尾做出类似效果",
                    "body_part": "眼部",
                    "evidence": "那个眼尾做的我很喜欢。",
                },
            ]
        },
        "staff_recommendations": {"items": [{"recommendation": "眼尾去皮收紧提升", "body_part": "眼尾", "demand_priority": [1]}]},
        "staff_seed_recommendations": {"items": []},
    }

    assert _agent_run_structural_consistency_audit(result)

    assert [item["demand"] for item in result["customer_primary_demands"]["items"]] == ["改善眼尾下垂问题"]


def test_structural_audit_keeps_indian_line_when_plan_uses_midface_body() -> None:
    result = {
        "customer_primary_demands": {
            "items": [
                {
                    "priority": 1,
                    "demand": "改善左侧两条印第安纹",
                    "body_part": "中面部/印第安纹",
                    "evidence": "之前觉得这两条还是会有一点点。",
                }
            ]
        },
        "staff_recommendations": {
            "items": [
                {
                    "recommendation": "6月中上旬复打一次童颜针",
                    "body_part": "中面部",
                    "demand_priority": [1],
                }
            ]
        },
        "staff_seed_recommendations": {"items": []},
    }

    _agent_run_structural_consistency_audit(result)

    assert [item["demand"] for item in result["customer_primary_demands"]["items"]] == ["改善左侧两条印第安纹"]
