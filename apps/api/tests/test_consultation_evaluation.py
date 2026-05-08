from smart_badge_api.analysis.consultation_evaluation import (
    rebuild_consultation_evaluation,
    rebuild_consultation_process_evaluation,
)


def test_rebuild_consultation_evaluation_scores_indications_without_doctor_confirmation() -> None:
    result = rebuild_consultation_evaluation(
        {
            "standardized_indications": {
                "items": [
                    {
                        "indication_name": "身体吸脂",
                        "body_part_name": "腰腹部",
                        "evidence": "[02:18] 我主要想做腰腹吸脂。",
                    }
                ]
            },
            "customer_profile": {"tags": []},
            "consultation_evaluation": {
                "overall_summary": "原始总结",
                "dimensions": [
                    {
                        "name": "".join(("标准", "适应症获取")),
                        "summary": "需要医生确认适应症",
                        "issues": [{"description": "需要医生确认适应症", "evidence": ""}],
                    }
                ],
            },
        },
        dialogue="[02:18] 主客户：我主要想做腰腹吸脂。",
    )

    indication_dimension = next(item for item in result["dimensions"] if item["name"] == "适应症获取")
    assert indication_dimension["point_score"] == 1.0
    assert "需要医生确认" not in indication_dimension["summary"]
    assert result["total_score"] >= 1.0


def test_rebuild_consultation_evaluation_scores_profile_by_important_tag_count() -> None:
    result = rebuild_consultation_evaluation(
        {
            "standardized_indications": {"items": []},
            "customer_profile": {
                "tags": [
                    {"category": "出生日期", "value": "30岁", "weight_level": 1, "evidence": "[00:08] 我今年30。"},
                    {"category": "疼痛耐受度", "value": "低", "weight_level": 1, "evidence": "[03:12] 我特别怕疼。"},
                    {"category": "常驻城市", "value": "外地", "weight_level": 2, "evidence": "[05:33] 我是外地过来的。"},
                ]
            },
            "consultation_evaluation": {"dimensions": []},
        },
        dialogue="[00:08] 主客户：我今年30。\n[03:12] 主客户：我特别怕疼。\n[05:33] 主客户：我是外地过来的。",
    )

    profile_dimension = next(item for item in result["dimensions"] if item["name"] == "顾客标签获取")
    assert profile_dimension["point_score"] == 0.14
    assert profile_dimension["score"] == 1.4
    assert "当前已获取 2/14 个必问/重要标签" in profile_dimension["summary"]


def test_rebuild_consultation_evaluation_counts_no_prior_treatment_triplet() -> None:
    result = rebuild_consultation_evaluation(
        {
            "standardized_indications": {"items": []},
            "customer_profile": {
                "tags": [
                    {"category": "治疗项目", "value": "未做过医美项目", "weight_level": 1, "evidence": "[00:40] 没有做过医美项目。"},
                    {"category": "历史用的设备/原材料名称", "value": "无", "weight_level": 1, "evidence": "[00:40] 没有做过医美项目。"},
                    {"category": "负面项目/设备/原材料", "value": "无", "weight_level": 1, "evidence": "[00:40] 没有做过医美项目。"},
                ]
            },
            "consultation_evaluation": {"dimensions": []},
        },
        dialogue="[00:40] 主客户：没有做过医美项目。",
    )

    profile_dimension = next(item for item in result["dimensions"] if item["name"] == "顾客标签获取")
    assert profile_dimension["point_score"] == 0.21
    assert profile_dimension["score"] == 2.1
    assert "当前已获取 3/14 个必问/重要标签" in profile_dimension["summary"]
    assert "已覆盖：" not in profile_dimension["summary"]


def test_rebuild_consultation_evaluation_strips_repeated_score_prefix_from_existing_summary() -> None:
    result = rebuild_consultation_evaluation(
        {
            "standardized_indications": {"items": []},
            "customer_profile": {"tags": []},
            "consultation_evaluation": {
                "overall_summary": "六维得分 3.21/6。六维得分 3.21/6。整体咨询能够抓住主诉，但医院和医生介绍缺失。",
                "dimensions": [],
            },
        },
        dialogue="[00:12] 主客户：我想了解热玛吉。",
    )

    assert result["overall_summary"] == "六维得分 1.00/6。整体咨询能够抓住主诉，但医院和医生介绍缺失。"


def test_rebuild_consultation_evaluation_scores_profile_by_customer_cumulative_tags() -> None:
    result = rebuild_consultation_evaluation(
        {
            "standardized_indications": {"items": []},
            "customer_profile": {
                "tags": [
                    {"category": "出生日期", "value": "30岁", "weight_level": 1, "evidence": "[00:08] 我今年30。"},
                    {"category": "疼痛耐受度", "value": "低", "weight_level": 1, "evidence": "[03:12] 我特别怕疼。"},
                    {"category": "常驻城市", "value": "外地", "weight_level": 2, "evidence": "[05:33] 我是外地过来的。"},
                ]
            },
            "consultation_evaluation": {"dimensions": []},
        },
        historical_profile_tags=[
            {"category": "出生日期", "value": "30岁", "weight_level": 1, "evidence": "[历史] 出生日期"},
            {"category": "健康风险/禁忌", "value": "疤痕体质", "weight_level": 1, "evidence": "[历史] 疤痕体质"},
            {"category": "治疗项目", "value": "光电治疗", "weight_level": 1, "evidence": "[历史] 做过光电"},
            {"category": "创伤倾向", "value": "微创", "weight_level": 1, "evidence": "[历史] 偏微创"},
            {"category": "价格敏感度", "value": "高", "weight_level": 2, "evidence": "[历史] 比较看价格"},
        ],
    )

    profile_dimension = next(item for item in result["dimensions"] if item["name"] == "顾客标签获取")
    assert profile_dimension["point_score"] == 0.43
    assert profile_dimension["score"] == 4.3
    assert "本次录音获取 2 个必问/重要标签" in profile_dimension["summary"]
    assert "关联客户历史后累计获取 6/14 个" in profile_dimension["summary"]


def test_rebuild_consultation_evaluation_marks_referral_dimension_zero_when_not_mentioned() -> None:
    result = rebuild_consultation_evaluation(
        {
            "standardized_indications": {"items": []},
            "customer_profile": {"tags": []},
            "consultation_evaluation": {
                "dimensions": [
                    {
                        "name": "老带新等特别事项",
                        "point_score": 1,
                        "summary": "未提及老带新、会员或转介绍等特别事项。",
                        "issues": [],
                    }
                ]
            },
        },
        dialogue="[00:18] 主客户：我主要想做腰腹吸脂。",
    )

    referral_dimension = next(item for item in result["dimensions"] if item["name"] == "老带新等特别事项")
    assert referral_dimension["point_score"] == 0.0
    assert referral_dimension["status"] == "未达标"


def test_rebuild_consultation_evaluation_ignores_customer_complaint_for_negative_dimension() -> None:
    result = rebuild_consultation_evaluation(
        {
            "standardized_indications": {"items": []},
            "customer_profile": {"tags": []},
            "consultation_evaluation": {
                "dimensions": [
                    {
                        "name": "负面交流检测",
                        "point_score": 0,
                        "summary": "客户对预约和沟通表达不满。",
                        "issues": [
                            {
                                "description": "客户对预约和沟通表达不满",
                                "evidence": "[05:08] 主客户：跟我说一声嘛……",
                            }
                        ],
                    }
                ]
            },
        }
    )

    negative_dimension = next(item for item in result["dimensions"] if item["name"] == "负面交流检测")
    assert negative_dimension["point_score"] == 1.0
    assert negative_dimension["status"] == "达标"


def test_rebuild_consultation_process_evaluation_hits_budget_wecom_and_referral() -> None:
    result = rebuild_consultation_process_evaluation(
        {
            "customer_primary_demands": {
                "items": [{"priority": 1, "demand": "想改善泪沟", "evidence": "[00:20] 我想改善泪沟。"}]
            },
            "customer_concerns": {
                "items": [{"type": "价格类", "content": "担心预算超支", "evidence": "[03:10] 我预算有限。"}]
            },
            "staff_recommendations": {
                "items": [{"recommendation": "玻尿酸填充", "evidence": "[02:20] 可以考虑玻尿酸填充。"}]
            },
            "consumption_intent": {"budget": "3000-5000"},
            "consultation_result": {"deal_outcome": {"status": "未成交"}},
            "consultation_evaluation": {"dimensions": []},
        },
        dialogue=(
            "[00:01] 咨询师：您好，先坐一下。\n"
            "[00:12] 咨询师：我是今天接待您的咨询师，先跟您了解下需求，后面再安排医生面诊。\n"
            "[00:20] 主客户：我想改善泪沟。\n"
            "[02:20] 咨询师：可以考虑玻尿酸填充。\n"
            "[03:10] 主客户：我预算有限。\n"
            "[03:20] 咨询师：您预算大概在多少，3000到5000也能做基础方案。\n"
            "[05:10] 咨询师：您加我企业微信，回去考虑后我再联系您。\n"
            "[05:40] 咨询师：如果朋友也有需要，也可以走老带新权益。"
        ),
    )

    sections = {item["code"]: item for item in result["sections"]}
    quotation = sections["quotation_and_close"]
    required_actions = sections["required_actions"]
    lost_followup = sections["lost_deal_followup"]

    budget_checkpoint = next(item for item in quotation["checkpoints"] if item["code"] == "5.1")
    wecom_checkpoint = next(item for item in required_actions["checkpoints"] if item["code"] == "8.1")
    referral_checkpoint = next(item for item in required_actions["checkpoints"] if item["code"] == "8.2")
    lost_checkpoint = next(item for item in lost_followup["checkpoints"] if item["code"] == "7.1")

    assert budget_checkpoint["point_score"] == 1.0
    assert wecom_checkpoint["point_score"] == 1.0
    assert referral_checkpoint["point_score"] == 1.0
    assert lost_checkpoint["point_score"] == 1.0
    assert "待提升" in result["overall_summary"] or "已完成" in result["overall_summary"]


def test_rebuild_consultation_process_evaluation_marks_negative_language_and_wrong_intro() -> None:
    result = rebuild_consultation_process_evaluation(
        {
            "consultation_evaluation": {
                "dimensions": [
                    {
                        "name": "负面交流检测",
                        "point_score": 0,
                        "summary": "检测到负面交流。",
                        "issues": [{"description": "咨询师存在负面语言", "evidence": "[06:00] 你这个问题早就该处理了"}],
                    },
                    {
                        "name": "医美专业知识",
                        "point_score": 0,
                        "summary": "存在不正确的产品介绍。",
                        "issues": [{"description": "产品介绍错误，存在夸大表述", "evidence": "[04:00] 这个产品永久有效"}],
                    },
                ]
            }
        },
        dialogue="[04:00] 咨询师：这个产品永久有效。\n[06:00] 咨询师：你这个问题早就该处理了。",
    )

    negative_section = next(item for item in result["sections"] if item["code"] == "negative_feedback")
    negative_language = next(item for item in negative_section["checkpoints"] if item["code"] == "9.1")
    wrong_intro = next(item for item in negative_section["checkpoints"] if item["code"] == "9.2")

    assert negative_language["point_score"] == 0.0
    assert wrong_intro["point_score"] == 0.0
    assert negative_language["issues"][0]["description"] == "咨询师存在负面语言"
    assert "产品介绍错误" in wrong_intro["issues"][0]["description"]
