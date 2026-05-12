import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.core.config import get_settings
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import (
    AnalysisTask,
    Customer,
    Recording,
    RecordingVisitAnalysis,
    RecordingVisitLink,
    SapConsultationReview,
    SapPushLog,
    Staff,
    Visit,
    VisitOrder,
)
from smart_badge_api.api.routes.sap_consultation_reviews import (
    _extract_consultation_no_from_push_log,
    _status_from_review_and_log,
)
from smart_badge_api.sap_consultation import (
    _extract_sap_preview_text,
    _merge_analysis_results,
    _synthesize_visit_analysis_results,
    build_consultation_text,
    generate_sap_consultation_payloads,
)


def test_sap_review_status_ignores_system_refresh_after_success() -> None:
    pushed_at = datetime(2026, 5, 9, 9, 0, 0, tzinfo=timezone.utc)
    review = SapConsultationReview(
        id="review-status-001",
        visit_id="visit-status-001",
        status="succeeded",
        blocks=[
            {
                "recording_id": "rec001",
                "generated_body": "●顾客主诉：系统生成内容",
                "edited_body": None,
                "effective_body": "●顾客主诉：系统生成内容",
            }
        ],
        generated_text="●备注人员：员工\n●顾客主诉：系统生成内容",
        effective_text="●备注人员：员工\n●顾客主诉：系统生成内容",
        updated_at=datetime(2026, 5, 9, 9, 0, 1, tzinfo=timezone.utc),
    )
    latest_log = SapPushLog(
        id="log-status-001",
        visit_id=review.visit_id,
        status="succeeded",
        sent_at=pushed_at,
        updated_at=pushed_at,
        created_at=pushed_at,
    )

    status, label, error, _last_push_at = _status_from_review_and_log(review, latest_log)

    assert status == "succeeded"
    assert label == "回传成功"
    assert error is None


def test_sap_review_status_marks_missing_review_as_pending() -> None:
    status, label, error, last_push_at = _status_from_review_and_log(None, None)

    assert status == "pending"
    assert label == "待回传"
    assert error is None
    assert last_push_at is None


def test_sap_review_status_distinguishes_system_pending_from_user_edit() -> None:
    pushed_at = datetime(2026, 5, 9, 9, 0, 0, tzinfo=timezone.utc)
    latest_log = SapPushLog(
        id="log-status-002",
        visit_id="visit-status-002",
        status="succeeded",
        sent_at=pushed_at,
        updated_at=pushed_at,
        created_at=pushed_at,
    )
    system_refreshed = SapConsultationReview(
        id="review-status-002",
        visit_id="visit-status-002",
        status="pending",
        blocks=[
            {
                "recording_id": "rec001",
                "generated_body": "●顾客主诉：系统刷新后的内容",
                "edited_body": None,
                "effective_body": "●顾客主诉：系统刷新后的内容",
            }
        ],
        updated_at=datetime(2026, 5, 9, 9, 30, 0, tzinfo=timezone.utc),
    )
    user_edited = SapConsultationReview(
        id="review-status-003",
        visit_id="visit-status-002",
        status="modified",
        blocks=[
            {
                "recording_id": "rec001",
                "generated_body": "●顾客主诉：系统生成内容",
                "edited_body": "●顾客主诉：人工编辑内容",
                "effective_body": "●顾客主诉：人工编辑内容",
            }
        ],
        updated_at=datetime(2026, 5, 9, 9, 30, 0, tzinfo=timezone.utc),
    )

    system_status, system_label, _error, _last_push_at = _status_from_review_and_log(system_refreshed, latest_log)
    edited_status, edited_label, _error, _last_push_at = _status_from_review_and_log(user_edited, latest_log)

    assert system_status == "pending"
    assert system_label == "待回传"
    assert edited_status == "modified_pending"
    assert edited_label == "已修改未回传"


def test_sap_review_extracts_success_consultation_no_from_response_body() -> None:
    push_log = SapPushLog(
        id="log-consultation-no-001",
        status="succeeded",
        response_items=[
            {
                "success": True,
                "http_status_code": 200,
                "gateway_code": 200,
                "business_status": "S",
                "business_message": "咨询单维护成功！",
                "response_body": {
                    "code": 200,
                    "msg": '{"STATU":"S","REMSG":"咨询单维护成功！","ZXDH":"3121178353"}',
                },
            }
        ],
    )

    assert _extract_consultation_no_from_push_log(push_log) == "3121178353"


def test_sap_review_extracts_retry_consultation_no_from_existing_order_message() -> None:
    push_log = SapPushLog(
        id="log-consultation-no-002",
        status="succeeded",
        response_items=[
            {
                "success": False,
                "http_status_code": 200,
                "gateway_code": 200,
                "business_status": "E",
                "business_message": "分诊单【2118339661-110】已有咨询单【3121178353】，不能再创建！",
            },
            {
                "success": True,
                "http_status_code": 200,
                "gateway_code": 200,
                "business_status": "S",
                "business_message": "咨询单维护成功！",
                "response_body": {"code": 200, "msg": '{"STATU":"S","REMSG":"咨询单维护成功！"}'},
                "retry_reason": "使用已有咨询单号 3121178353 改为修改模式回传",
            },
        ],
    )

    assert _extract_consultation_no_from_push_log(push_log) == "3121178353"


def test_build_consultation_text_uses_new_consultation_result_structure() -> None:
    result = {
        "consultation_result": {
            "chief_complaint_and_indications": {
                "primary_demands": ["下巴后缩", "希望更立体自然"],
                "standardized_indications": ["微整（Y4）｜下巴塑形（SYZ4001）｜下巴（BW4001）"],
            },
            "customer_profile_summary": {
                "tags": [
                    {"category": "治疗项目", "value": "无医美史"},
                    {"category": "倾向回访方式", "value": "微信"},
                ]
            },
            "deal_factors": {
                "budget": "5000-8000",
                "concerns": ["担心不自然", "担心超预算"],
                "decision_factors": ["价格", "效果"],
            },
            "recommended_plan": {
                "items": [
                    {"plan": "玻尿酸下巴填充", "acceptance": "犹豫"},
                ]
            },
            "seed_plan": {
                "items": [
                    {"plan": "水光针维护皮肤状态", "acceptance": "未明确回应"},
                ]
            },
            "deal_outcome": {
                "status": "未成交",
                "summary": "客户还需再考虑。",
                "loss_reasons": ["仍需比较价格"],
            },
        },
    }

    text = build_consultation_text("兰四秀", result)

    assert "●备注人员：兰四秀" in text
    assert "●接诊人员：" not in text
    assert "●顾客主诉：①下巴后缩；\n ②希望更立体自然" in text
    assert "●本次预算：5000-8000" in text
    assert "●顾客顾虑：①担心不自然；\n ②担心超预算" in text
    assert "●推荐方案：①玻尿酸下巴填充（认可程度：犹豫）" in text
    assert "●种草方案：①水光针维护皮肤状态（认可程度：未明确回应）" in text
    assert "●未成交原因：" not in text
    assert "●总结信息：\n1、客户基础信息：" in text
    assert "从消费基础看，既往医美经历相对空白；本次已出现5000-8000的预算或金额线索" in text
    assert "2、需求与动机分析：客户这次的需求主线比较清楚，主要集中在下巴后缩、希望更立体自然" in text
    assert "真正需要被化解的是担心不自然、担心超预算" in text
    assert "3、面诊与设计方案：本次由兰四秀承接咨询" in text
    assert "客户反馈中，玻尿酸下巴填充犹豫" in text
    assert "4、报价与成交策略：" in text
    assert "本次没有当场成交，主要卡点落在仍需比较价格" in text
    assert "从接受度看，玻尿酸下巴填充犹豫" in text
    assert "7、老带新提及：" in text
    assert "一、客户基础信息" not in text
    assert "1. 人口属性" not in text
    assert "建议通过微信延续沟通" in text


def test_build_consultation_text_wraps_multi_item_sap_fields() -> None:
    result = {
        "consultation_result": {
            "chief_complaint_and_indications": {
                "primary_demands": [
                    "调整眶外C线/眉尾轮廓，希望面部轮廓更自然协调",
                    "改善鼻基底/中面部衔接，希望恢复平整自然",
                ],
            },
            "deal_factors": {
                "concerns": ["担心风险、副作用或安全性"],
            },
            "recommended_plan": {
                "items": [
                    {
                        "plan": "先进行咬肌注射（减法），1.5-2个月后再做玻尿酸面部填充（加法），每侧一次一支，避免移位。",
                        "dosage": "每侧1支",
                        "course_or_frequency": "咬肌后1.5-2个月填充",
                        "treatment_steps": ["先注射咬肌", "1.5-2个月后再进行填充"],
                        "implementation_notes": "控制单侧单支剂量，避免移位",
                        "acceptance": "接受",
                    },
                    {"plan": "玻尿酸填充塑形", "acceptance": "未明确回应"},
                ],
            },
        },
    }

    text = build_consultation_text("李珍玉", result)

    assert (
        "●顾客主诉：①调整眶外C线/眉尾轮廓，希望面部轮廓更自然协调；\n"
        " ②改善鼻基底/中面部衔接，希望恢复平整自然"
    ) in text
    assert "●顾客顾虑：①担心风险、副作用或安全性" in text
    assert (
        "●推荐方案：①先进行咬肌注射（减法），1.5-2个月后再做玻尿酸面部填充（加法），每侧一次一支，避免移位。"
        "（用量：每侧1支；疗程：咬肌后1.5-2个月填充；步骤：先注射咬肌；1.5-2个月后再进行填充；要点：控制单侧单支剂量，避免移位）（认可程度：接受）；\n"
        " ②玻尿酸填充塑形（认可程度：未明确回应）"
    ) in text


def test_extract_existing_sap_preview_text_wraps_multi_item_fields() -> None:
    result = {
        "sap_consultation_preview": {
            "payloads": [
                {
                    "text": (
                        "●备注人员：李珍玉\n"
                        "●顾客主诉：调整眶外C线/眉尾轮廓；改善鼻基底/中面部衔接\n"
                        "●本次预算：无\n"
                        "●顾客顾虑：担心风险；担心安全性\n"
                        "●推荐方案：咬肌注射（用量：每侧1支；疗程：1.5-2个月）（认可程度：接受）；玻尿酸填充塑形（认可程度：未明确回应）\n"
                        "●种草方案：水光针维护；光子嫩肤提亮"
                    )
                }
            ]
        }
    }

    text = _extract_sap_preview_text(result)

    assert "●顾客主诉：①调整眶外C线/眉尾轮廓；\n ②改善鼻基底/中面部衔接" in text
    assert "●顾客顾虑：①担心风险；\n ②担心安全性" in text
    assert (
        "●推荐方案：①咬肌注射（用量：每侧1支；疗程：1.5-2个月）（认可程度：接受）；\n"
        " ②玻尿酸填充塑形（认可程度：未明确回应）"
    ) in text
    assert "●种草方案：①水光针维护；\n ②光子嫩肤提亮" in text


def test_build_consultation_text_prefers_model_sap_summary_materials() -> None:
    result = {
        "consultation_result": {
            "chief_complaint_and_indications": {
                "primary_demands": ["下巴后缩"],
            },
            "deal_factors": {
                "budget": "5000-8000",
            },
        },
        "sap_summary_materials": {
            "sections": [
                {
                    "name": "客户基础信息",
                    "content": "客户为新客，既往未做医美，整体更需要先建立基础信任。",
                    "covered_points": ["人口属性", "经济能力与消费历史"],
                },
                {
                    "name": "需求与动机分析",
                    "content": "客户主要想改善下巴轮廓，追求自然立体，当前阻力集中在是否自然和预算匹配。",
                    "covered_points": ["核心诉求", "决策顾虑"],
                },
            ]
        },
    }

    text = build_consultation_text("兰四秀", result)

    assert "1、客户基础信息：客户为新客，既往未做医美，整体更需要先建立基础信任。" in text
    assert "2、需求与动机分析：客户主要想改善下巴轮廓，追求自然立体，当前阻力集中在是否自然和预算匹配。" in text
    assert "本次预算或金额线索为5000-8000" not in text


def test_unlinked_sap_preview_uses_recording_staff_as_remark_person() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(id="staff001", name="李玲玉")
                recording = Recording(
                    id="rec001",
                    staff_id=staff.id,
                    file_name="audio_001.mp3",
                    file_path="uploads/recordings/audio_001.mp3",
                    status="analyzed",
                    created_at=datetime(2026, 4, 16, 9, 30, 0, tzinfo=timezone.utc),
                )
                task = AnalysisTask(
                    id="task001",
                    file_name="recording_rec001.json",
                    file_path="uploads/analysis_input/recording_rec001.json",
                    status="done",
                    result={
                        "consultation_result": {
                            "chief_complaint_and_indications": {
                                "primary_demands": ["改善鼻部线条"],
                            },
                            "deal_factors": {"concerns": ["担心价格"]},
                            "recommended_plan": {"items": [{"plan": "鼻部玻尿酸塑形"}]},
                            "deal_outcome": {"status": "未明确"},
                        },
                    },
                )
                db.add_all([staff, recording, task])
                await db.commit()

                preview = await generate_sap_consultation_payloads(
                    db,
                    recording.id,
                    allow_unlinked_preview=True,
                )

                text = preview["payloads"][0]["text"]
                assert preview["advisor_name"] == "李玲玉"
                assert "●备注人员：李玲玉" in text
                assert "●接诊人员：" not in text
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_build_consultation_text_formats_inline_model_sap_summary_points() -> None:
    result = {
        "consultation_result": {
            "chief_complaint_and_indications": {
                "primary_demands": ["改善鼻基底凹陷和上唇前凸感"],
            },
        },
        "sap_summary_materials": {
            "summary": (
                "1、客户背景：客户有既往太阳穴填充史，本次主要关注面中部凹陷和侧面比例。 "
                "2、决策画像：客户担心填充后显脸大，也在意医生操作安全。"
                "3、方案反馈：客户对鼻基底支撑未明确回应，对额颞区填充仍有犹豫。 "
                "4、成交与跟进：本次未成交，后续建议先以小范围支撑作为体验切入。"
            ),
            "sections": [],
        },
    }

    text = build_consultation_text("胡湘莲", result)
    summary_text = text.split("●总结信息：\n", 1)[1]

    assert "1、客户背景：客户有既往太阳穴填充史，本次主要关注面中部凹陷和侧面比例。\n2、决策画像：" in summary_text
    assert "也在意医生操作安全。\n3、方案反馈：" in summary_text
    assert "仍有犹豫。\n4、成交与跟进：" in summary_text
    assert "比例。 2、决策画像" not in summary_text


def test_build_consultation_text_defaults_budget_and_blocks_to_none_marker() -> None:
    result = {
        "consultation_result": {
            "chief_complaint_and_indications": {
                "primary_demands": ["面部松弛，希望更紧致。"],
            },
            "recommended_plan": {
                "items": [],
            },
        },
    }

    text = build_consultation_text("张三", result)

    assert "●本次预算：无" in text
    assert "●顾客顾虑：无" in text
    assert "●推荐方案：无" in text
    assert "●种草方案：无" in text
    assert "●总结信息：" in text


def test_build_consultation_text_only_includes_loss_reason_when_visit_order_final_not_deal() -> None:
    result = {
        "consultation_result": {
            "chief_complaint_and_indications": {
                "primary_demands": ["希望改善眼袋"],
            },
            "deal_outcome": {
                "status": "未成交",
                "loss_reasons": ["仍需比较价格", "需要与家人商量"],
            },
        }
    }
    visit_order = VisitOrder(dzdh="DZ001", dzseg="110", jcsta="N", jcsta_txt="未成交")

    text = build_consultation_text("张三", result, visit_order=visit_order)

    assert "●未成交原因：①仍需比较价格；\n ②需要与家人商量" in text


def test_build_consultation_text_summary_includes_transcript_clues() -> None:
    result = {
        "consultation_result": {
            "chief_complaint_and_indications": {
                "primary_demands": ["希望改善眼袋"],
            },
        }
    }
    text = build_consultation_text(
        "张三",
        result,
        transcript_utterances=[
            {"speaker_role": "customer", "text": "我单身，以前没做过医美项目。"},
            {"speaker_role": "customer", "text": "后续你加我微信联系我就可以，我主要担心恢复期会影响上班。"},
        ],
    )

    assert "1、客户基础信息：这位客户当前为单身状态" in text
    assert "从消费基础看，既往医美经历相对空白" in text
    assert "2、需求与动机分析：客户这次的需求主线比较清楚，主要集中在希望改善眼袋" in text
    assert "真正需要被化解的是恢复期对工作或日常安排的影响" in text
    assert "6、后续跟进规划：建议通过微信持续跟进" in text
    assert "建议通过微信延续沟通" in text
    assert "1. 人口属性" not in text


def test_build_consultation_text_summary_is_natural_and_avoids_stage_field_language() -> None:
    result = {
        "consultation_result": {
            "chief_complaint_and_indications": {
                "primary_demands": ["希望改善面部松弛", "关注恢复期"],
            },
            "customer_profile_summary": {
                "tags": [
                    {"category": "治疗项目", "value": "无医美史"},
                    {"category": "倾向回访方式", "value": "微信"},
                    {"category": "健康风险/禁忌", "value": "无风险禁忌"},
                ]
            },
            "deal_factors": {
                "decision_factors": ["恢复期", "效果"],
            },
            "deal_outcome": {
                "loss_reasons": ["时间安排受限"],
            },
        }
    }
    visit_order = VisitOrder(
        dzdh="DZ001",
        dzseg="110",
        jgks_txt="皮肤科",
        dztyp_txt="初诊",
        kusta_dq_txt="建档未上门",
    )

    text = build_consultation_text(
        "张三",
        result,
        visit_order=visit_order,
        transcript_utterances=[
            {"speaker_role": "customer", "text": "我47岁，之前没做过医美，加我微信就行，我上班怕恢复期太久。"}
        ],
    )

    assert "1、客户基础信息：这位客户年龄47岁" in text
    assert "从消费基础看，既往医美经历相对空白" in text
    assert "2、需求与动机分析：客户这次的需求主线比较清楚，主要集中在希望改善面部松弛、关注恢复期" in text
    assert "影响决策的因素包括恢复期、效果" in text
    assert "真正需要被化解的是恢复期对工作或日常安排的影响" in text
    assert "客户目前处于" not in text
    assert "当前属于" not in text
    assert "6、后续跟进规划：建议通过微信持续跟进" in text
    assert "下一步建议重点回应时间安排受限" in text
    assert "建议通过微信延续沟通" in text


def test_build_consultation_text_does_not_extract_age_from_effect_range() -> None:
    result = {
        "consultation_result": {
            "chief_complaint_and_indications": {
                "primary_demands": ["希望改善泪沟和中面部凹陷"],
            },
        }
    }

    text = build_consultation_text(
        "张三",
        result,
        transcript_utterances=[
            {
                "speaker_role": "customer",
                "text": "对他打完之后显得瞬间就是老了5~10岁了，不知道就完全见不了人了。",
            }
        ],
    )

    assert "年龄10岁" not in text
    assert "客户自述年龄约10岁" not in text


def test_merge_analysis_results_drops_sparse_main_fact_when_strong_result_exists() -> None:
    strong_result = {
        "customer_primary_demands": {
            "items": [
                {"demand": "改善泪沟/眼周凹陷，希望恢复平整自然"},
            ]
        },
        "standardized_indications": {
            "items": [
                {
                    "department_code": "Y1",
                    "indication_code": "SYZ1019",
                    "body_part_code": "BW1005",
                }
            ]
        },
    }
    sparse_result = {
        "customer_primary_demands": {
            "inference_note": "低内容量医美业务场景兜底：录音有效内容较少但存在真实医美咨询信号",
            "items": [
                {"demand": "面部两侧八字明显，希望改善凹陷"},
            ],
        },
        "standardized_indications": {
            "inference_note": "低内容量医美业务场景兜底：录音有效内容较少但存在真实医美咨询信号",
            "items": [
                {
                    "department_code": "Y1",
                    "indication_code": "SYZ1006",
                    "body_part_code": "BW1002",
                }
            ],
        },
    }

    merged = _merge_analysis_results([strong_result, sparse_result])

    primary_demands = merged["consultation_result"]["chief_complaint_and_indications"]["primary_demands"]
    indication_items = merged["standardized_indications"]["items"]
    assert primary_demands == ["改善泪沟/眼周凹陷，希望恢复平整自然"]
    assert [(item["indication_code"], item["body_part_code"]) for item in indication_items] == [
        ("SYZ1019", "BW1005")
    ]


def test_merge_analysis_results_uses_timeline_for_final_deal_and_plan_response() -> None:
    early_result = {
        "consultation_result": {
            "deal_factors": {"budget": "8000左右", "concerns": ["需要回去考虑价格"]},
            "recommended_plan": {
                "items": [{"plan": "玻尿酸填充法令纹", "acceptance": "犹豫"}],
            },
            "deal_outcome": {
                "status": "未成交",
                "amount": "8000左右",
                "loss_reasons": ["需要回去考虑价格"],
            },
        }
    }
    later_result = {
        "consultation_result": {
            "deal_factors": {"budget": "9000套餐"},
            "recommended_plan": {
                "items": [{"plan": "玻尿酸填充法令纹", "acceptance": "接受"}],
            },
            "deal_outcome": {
                "status": "已成交",
                "deal_items": ["玻尿酸填充法令纹"],
                "amount": "9000套餐",
            },
        }
    }

    merged = _merge_analysis_results([early_result, later_result])

    consultation_result = merged["consultation_result"]
    assert consultation_result["deal_outcome"]["status"] == "已成交"
    assert consultation_result["deal_outcome"]["amount"] == "9000套餐"
    assert consultation_result["recommended_plan"]["items"] == [
        {"plan": "玻尿酸填充法令纹", "acceptance": "接受"}
    ]
    assert merged["visit_level_synthesis"]["source"] == "deterministic_timeline"


def test_merge_analysis_results_dedupes_semantically_similar_recommendation_plans() -> None:
    early_result = {
        "consultation_result": {
            "recommended_plan": {
                "items": [
                    {"plan": "深层支撑+肉毒提升方案", "acceptance": "未接受，当下未选择"},
                    {"plan": "肉毒/除皱瘦脸", "acceptance": "犹豫"},
                ],
            },
            "deal_outcome": {"status": "未成交"},
        }
    }
    later_result = {
        "consultation_result": {
            "recommended_plan": {
                "items": [
                    {"plan": "后续可补充肉毒或深层支撑加强效果", "acceptance": "未明确回应"},
                    {"plan": "玻尿酸填充塑形", "acceptance": "犹豫"},
                ],
            },
            "deal_outcome": {"status": "未明确"},
        }
    }

    merged = _merge_analysis_results([early_result, later_result])

    assert merged["consultation_result"]["recommended_plan"]["items"] == [
        {"plan": "肉毒/除皱瘦脸", "acceptance": "犹豫"},
        {"plan": "深层支撑+肉毒提升方案", "acceptance": "未接受，当下未选择"},
        {"plan": "玻尿酸填充塑形", "acceptance": "犹豫"},
    ]


def test_build_consultation_text_dedupes_recommendation_names_in_sap_summary() -> None:
    result = {
        "consultation_result": {
            "chief_complaint_and_indications": {
                "primary_demands": ["感觉脸有点松，想改善松垂和提升感"],
            },
            "recommended_plan": {
                "items": [
                    {"plan": "深层支撑+肉毒提升方案（认可程度：未接受，当下未选择）"},
                    {"plan": "后续可补充肉毒或深层支撑加强效果", "acceptance": "未明确回应"},
                ],
            },
            "deal_outcome": {"status": "未明确"},
        }
    }

    text = build_consultation_text("胡倩雯", result)

    recommendation_line = next(line for line in text.splitlines() if line.startswith("●推荐方案："))
    assert recommendation_line == "●推荐方案：①深层支撑+肉毒提升方案（认可程度：未接受，当下未选择）"
    assert "后续可补充肉毒或深层支撑加强效果" not in text
    assert "深层支撑+肉毒提升方案（认可程度：" not in text.split("●总结信息：", 1)[1]


def test_visit_result_fusion_uses_structured_analysis_without_process_evaluation(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_chat_completion(system_prompt: str, user_prompt: str, **kwargs) -> str:
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        assert "consultation_process_evaluation" not in user_prompt
        assert "consultation_evaluation" not in user_prompt
        assert "完整原文不应发送" not in user_prompt
        assert kwargs["temperature"] == 0.1
        assert kwargs["max_tokens"] == 6000
        return json.dumps(
            {
                "consultation_result": {
                    "chief_complaint_and_indications": {
                        "primary_demands": ["融合后的核心主诉"],
                    },
                    "deal_factors": {"budget": "9000套餐", "concerns": ["担心恢复期"]},
                    "recommended_plan": {
                        "items": [{"plan": "眼周抗衰联合注射", "acceptance": "接受"}],
                    },
                    "deal_outcome": {
                        "status": "已成交",
                        "deal_items": ["眼周抗衰联合注射"],
                        "amount": "9000套餐",
                    },
                    "customer_profile_summary": {"tags": [{"category": "消费意愿", "value": "明确"}]},
                },
                "standardized_indications": {
                    "items": [
                        {"department_code": "Y3", "indication_code": "SYZ3001", "body_part_code": "BW3001"},
                        {"department_code": "Y9", "indication_code": "BAD", "body_part_code": "BAD"},
                    ]
                },
                "sap_summary_materials": {"summary": "融合总结", "sections": []},
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("smart_badge_api.sap_consultation.chat_completion", fake_chat_completion)

    contexts = [
        {
            "recording": Recording(id="rec001", file_name="consultant.mp3"),
            "result": {
                "consultation_result": {
                    "chief_complaint_and_indications": {"primary_demands": ["改善法令纹"]},
                    "deal_outcome": {"status": "未明确"},
                },
                "standardized_indications": {
                    "items": [
                        {"department_code": "Y2", "indication_code": "SYZ2001", "body_part_code": "BW2005"},
                    ]
                },
                "consultation_process_evaluation": {"overall_summary": "过程评价内容不应发送"},
            },
            "transcript_full_text": "完整原文不应发送",
        },
        {
            "recording": Recording(id="rec002", file_name="doctor.mp3"),
            "result": {
                "consultation_result": {
                    "chief_complaint_and_indications": {"primary_demands": ["眼下轻度松弛，想显年轻"]},
                    "deal_outcome": {"status": "已成交", "amount": "9000套餐"},
                },
                "standardized_indications": {
                    "items": [
                        {"department_code": "Y3", "indication_code": "SYZ3001", "body_part_code": "BW3001"},
                    ]
                },
                "consultation_evaluation": {"overall_summary": "面诊评价内容不应发送"},
            },
            "transcript_full_text": "完整原文不应发送",
        },
    ]
    visit_order = VisitOrder(id="vo001", dzdh="DZ001", dzseg="110", ninam="主客户", kunr="KH001")

    result = asyncio.run(_synthesize_visit_analysis_results(contexts, visit_order))

    assert result["visit_level_synthesis"]["source"] == "llm_result_fusion"
    assert result["consultation_result"]["chief_complaint_and_indications"]["primary_demands"] == ["融合后的核心主诉"]
    assert result["consultation_result"]["deal_outcome"]["status"] == "已成交"
    assert result["sap_summary_materials"]["summary"] == "融合总结"
    assert [
        (item["department_code"], item["indication_code"], item["body_part_code"])
        for item in result["standardized_indications"]["items"]
    ] == [("Y3", "SYZ3001", "BW3001")]
    assert "allowed_standardized_indications" in captured["user_prompt"]


def test_visit_result_fusion_dedupes_llm_recommendation_output(monkeypatch) -> None:
    def fake_chat_completion(*args, **kwargs) -> str:
        return json.dumps(
            {
                "consultation_result": {
                    "recommended_plan": {
                        "items": [
                            {"plan": "深层支撑+肉毒提升方案（认可程度：未接受，当下未选择）"},
                            {"plan": "后续可补充肉毒或深层支撑加强效果", "acceptance": "未明确回应"},
                        ]
                    },
                    "deal_outcome": {"status": "未明确"},
                },
                "standardized_indications": {"items": []},
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("smart_badge_api.sap_consultation.chat_completion", fake_chat_completion)

    contexts = [
        {
            "recording": Recording(id="rec001", file_name="consultant.mp3"),
            "result": {"consultation_result": {"recommended_plan": {"items": []}}},
        },
        {
            "recording": Recording(id="rec002", file_name="doctor.mp3"),
            "result": {"consultation_result": {"recommended_plan": {"items": []}}},
        },
    ]

    result = asyncio.run(_synthesize_visit_analysis_results(contexts, VisitOrder(id="vo001", dzdh="DZ001")))

    assert result["consultation_result"]["recommended_plan"]["items"] == [
        {"plan": "深层支撑+肉毒提升方案", "acceptance": "未接受，当下未选择"}
    ]


def test_generate_sap_consultation_payloads_creates_payload_for_each_linked_visit(monkeypatch) -> None:
    monkeypatch.setenv("SAP_RFC_OVERRIDE_KUNR", "")
    monkeypatch.setenv("SAP_RFC_OVERRIDE_USER", "")
    monkeypatch.setenv("SAP_RFC_OVERRIDE_ADVXC", "")
    monkeypatch.setenv("SAP_RFC_OVERRIDE_ZXDH", "")
    get_settings.cache_clear()

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                customer_primary = Customer(id="cust001", name="主客户", external_customer_code="KH001")
                customer_companion = Customer(id="cust002", name="同行客户", external_customer_code="KH002")
                staff = Staff(id="staff001", name="李玲玉")
                visit_primary = Visit(
                    id="visit001",
                    customer_id=customer_primary.id,
                    external_visit_order_no="DZ001",
                    external_visit_order_seg="110",
                )
                visit_companion = Visit(
                    id="visit002",
                    customer_id=customer_companion.id,
                    external_visit_order_no="DZ002",
                    external_visit_order_seg="110",
                )
                recording = Recording(
                    id="rec001",
                    visit_id=visit_primary.id,
                    file_name="audio_001.mp3",
                    file_path="uploads/recordings/audio_001.mp3",
                    status="analyzed",
                    staff_id=staff.id,
                    created_at=datetime(2026, 4, 16, 9, 30, 0, tzinfo=timezone.utc),
                )
                link_primary = RecordingVisitLink(
                    recording_id=recording.id,
                    visit_id=visit_primary.id,
                    is_primary=True,
                )
                link_companion = RecordingVisitLink(
                    recording_id=recording.id,
                    visit_id=visit_companion.id,
                    is_primary=False,
                )
                order_primary = VisitOrder(
                    id="vo001",
                    dzdh="DZ001",
                    dzseg="110",
                    jgbm="6101",
                    kunr="KH001",
                    ninam="主客户",
                    advxc="ADV001",
                    advxc_long="兰四秀",
                    fzdh="DZ001-110",
                    crtdt="20260416",
                    crttm="173000",
                )
                order_companion = VisitOrder(
                    id="vo002",
                    dzdh="DZ002",
                    dzseg="110",
                    jgbm="6101",
                    kunr="KH002",
                    ninam="同行客户",
                    advxc="ADV001",
                    advxc_long="兰四秀",
                    fzdh="DZ002-110",
                    crtdt="20260416",
                    crttm="173000",
                )
                task = AnalysisTask(
                    id="task001",
                    file_name="recording_rec001.json",
                    file_path="uploads/analysis_input/recording_rec001.json",
                    status="done",
                    result={
                        "customer_primary_demands": {"summary": "面部松弛，希望提升紧致度。"},
                        "customer_concerns": {"items": [{"type": "价格", "content": "担心预算不够"}]},
                        "staff_recommendations": {"summary": "超声炮联合水光。"},
                        "consultation_evaluation": {"overall_summary": "沟通完整。"},
                        "consultation_result": {
                            "chief_complaint_and_indications": {
                                "primary_demands": ["面部松弛，希望提升紧致度。"] ,
                                "standardized_indications": ["皮肤（Y3）｜松弛下垂（SYZ3001）｜面部（BW3001）"],
                            },
                            "customer_profile_summary": {
                                "tags": [{"category": "倾向回访方式", "value": "微信"}]
                            },
                            "deal_factors": {
                                "concerns": ["担心预算不够"],
                            },
                            "recommended_plan": {
                                "items": [{"plan": "超声炮联合水光", "acceptance": "未明确回应"}]
                            },
                            "deal_outcome": {
                                "status": "未明确",
                                "summary": "对话中未明确体现最终成交结果。",
                            },
                        },
                        "consultation_process_evaluation": {"overall_summary": "沟通完整。"},
                        "standardized_indications": {
                            "items": [
                                {"department_code": "Y3", "indication_code": "SYZ3001", "body_part_code": "BW3001"},
                            ]
                        },
                    },
                )
                scoped_primary = RecordingVisitAnalysis(
                    id="rva001",
                    recording_id=recording.id,
                    visit_id=visit_primary.id,
                    mapping_status="confirmed",
                    analysis_status="done",
                    analysis_result=task.result,
                    sap_ready_at=datetime(2026, 4, 16, 10, 0, 0, tzinfo=timezone.utc),
                )
                scoped_companion = RecordingVisitAnalysis(
                    id="rva002",
                    recording_id=recording.id,
                    visit_id=visit_companion.id,
                    mapping_status="confirmed",
                    analysis_status="done",
                    analysis_result=task.result,
                    sap_ready_at=datetime(2026, 4, 16, 10, 0, 0, tzinfo=timezone.utc),
                )
                db.add_all(
                    [
                        customer_primary,
                        customer_companion,
                        staff,
                        visit_primary,
                        visit_companion,
                        recording,
                        link_primary,
                        link_companion,
                        order_primary,
                        order_companion,
                        task,
                        scoped_primary,
                        scoped_companion,
                    ]
                )
                await db.commit()

                preview = await generate_sap_consultation_payloads(db, recording.id)

                assert preview["visit_order_no"] == "DZ001"
                assert preview["customer_name"] == "主客户"
                assert preview["target_count"] == 2
                assert [item["visit_id"] for item in preview["targets"]] == ["visit001", "visit002"]
                assert [item["visit_order_no"] for item in preview["targets"]] == ["DZ001", "DZ002"]
                assert [item["customer_code"] for item in preview["targets"]] == ["KH001", "KH002"]
                assert [item["is_primary"] for item in preview["targets"]] == [True, False]
                assert len(preview["payloads"]) == 2
                assert preview["payloads"][0]["zxxx"]["kunr"] == "KH001"
                assert preview["payloads"][1]["zxxx"]["kunr"] == "KH002"
                assert preview["payloads"][0]["zxxx"]["fzdh"] == "DZ001-110"
                assert preview["payloads"][1]["zxxx"]["fzdh"] == "DZ002-110"
                assert "●备注人员：李玲玉" in preview["payloads"][0]["text"]
                assert "●接诊人员：" not in preview["payloads"][0]["text"]
                assert "●顾客主诉：" in preview["payloads"][0]["text"]
                assert "●本次预算：" in preview["payloads"][0]["text"]
                assert "●顾客顾虑：" in preview["payloads"][0]["text"]
                assert "●推荐方案：" in preview["payloads"][0]["text"]
                assert "●总结信息：" in preview["payloads"][0]["text"]
        finally:
            await engine.dispose()

    try:
        asyncio.run(scenario())
    finally:
        get_settings.cache_clear()


def test_build_consultation_text_uses_empty_demand_and_price_quote_fallback() -> None:
    result = {
        "customer_primary_demands": {
            "items": [],
            "summary": "\u5bf9\u8bdd\u4e2d\u672a\u8bc6\u522b\u51fa\u53ef\u6807\u51c6\u5316\u7684\u9002\u5e94\u75c7",
        },
        "standardized_indications": {
            "items": [],
            "summary": "\u5bf9\u8bdd\u4e2d\u672a\u8bc6\u522b\u51fa\u53ef\u6807\u51c6\u5316\u7684\u9002\u5e94\u75c7",
        },
        "consultation_result": {
            "chief_complaint_and_indications": {
                "summary": "\u5bf9\u8bdd\u4e2d\u672a\u8bc6\u522b\u51fa\u53ef\u6807\u51c6\u5316\u7684\u9002\u5e94\u75c7",
                "primary_demands": [],
                "standardized_indications": [],
            },
            "deal_factors": {},
            "recommended_plan": {"items": []},
        },
    }
    transcript = (
        "\u6211\u4eec\u4fdd\u5229\u7684\u6c34\u6ef4\u578b\u554a\uff0c\u6211\u4eec\u6d3b\u52a8\u4e0b\u6765\u5c31\u662f69800"
        "\uff0c\u5982\u679c\u662f\u5706\u5f62\u7684\u8bdd\uff0c\u6211\u4eec\u5c31\u662f46800\u3002"
        "\u8fd9\u4e2a\u7231\u601d\u7f8e\u7684\u8bdd5\u4e07\u5427\u3002"
        "\u6bcd\u63d0\u74e612\u4e078\u661f\u94bb14\u4e07\u5427\u3002"
    )

    text = build_consultation_text("\u5f20\u5bd2", result, transcript_full_text=transcript)

    assert "\u25cf\u987e\u5ba2\u4e3b\u8bc9\uff1a\u65e0" in text
    assert "\u5bf9\u8bdd\u4e2d\u672a\u8bc6\u522b\u51fa\u53ef\u6807\u51c6\u5316\u7684\u9002\u5e94\u75c7" not in text
    assert "\u25cf\u63a8\u8350\u65b9\u6848\uff1a\u2460\u80f8\u5047\u4f53/\u9686\u80f8\u65b9\u6848\u62a5\u4ef7" in text
    assert "69800" in text
    assert "46800" in text
    assert "5\u4e07" in text


def test_generate_sap_consultation_payloads_merges_multiple_recordings_for_same_visit(monkeypatch) -> None:
    monkeypatch.setenv("SAP_RFC_OVERRIDE_KUNR", "")
    monkeypatch.setenv("SAP_RFC_OVERRIDE_USER", "")
    monkeypatch.setenv("SAP_RFC_OVERRIDE_ADVXC", "")
    monkeypatch.setenv("SAP_RFC_OVERRIDE_ZXDH", "")
    def fake_chat_completion(*args, **kwargs) -> str:
        return json.dumps(
            {
                "standardized_indications": {
                    "items": [
                        {"department_code": "Y3", "indication_code": "SYZ3001", "body_part_code": "BW3001"},
                    ],
                },
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("smart_badge_api.sap_consultation.chat_completion", fake_chat_completion)
    get_settings.cache_clear()

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                customer = Customer(id="cust001", name="主客户", external_customer_code="KH001")
                staff_one = Staff(id="staff001", name="李玲玉")
                staff_two = Staff(id="staff002", name="王医生")
                visit = Visit(
                    id="visit001",
                    customer_id=customer.id,
                    external_visit_order_no="DZ001",
                    external_visit_order_seg="110",
                )
                order = VisitOrder(
                    id="vo001",
                    dzdh="DZ001",
                    dzseg="110",
                    jgbm="6101",
                    kunr="KH001",
                    ninam="主客户",
                    advxc="ADV001",
                    advxc_long="兰四秀",
                    fzdh="DZ001-110",
                    crtdt="20260416",
                    crttm="173000",
                )
                recording_one = Recording(
                    id="rec001",
                    visit_id=visit.id,
                    file_name="consultant.mp3",
                    file_path="uploads/recordings/consultant.mp3",
                    status="analyzed",
                    staff_id=staff_one.id,
                    created_at=datetime(2026, 4, 16, 9, 30, 0, tzinfo=timezone.utc),
                )
                recording_two = Recording(
                    id="rec002",
                    visit_id=visit.id,
                    file_name="doctor.mp3",
                    file_path="uploads/recordings/doctor.mp3",
                    status="analyzed",
                    staff_id=staff_two.id,
                    created_at=datetime(2026, 4, 16, 9, 45, 0, tzinfo=timezone.utc),
                )
                link_one = RecordingVisitLink(recording_id=recording_one.id, visit_id=visit.id, is_primary=True)
                link_two = RecordingVisitLink(recording_id=recording_two.id, visit_id=visit.id, is_primary=True)
                task_one = AnalysisTask(
                    id="task001",
                    file_name="recording_rec001.json",
                    file_path="uploads/analysis_input/recording_rec001.json",
                    status="done",
                    result={
                        "consultation_result": {
                            "chief_complaint_and_indications": {
                                "primary_demands": ["改善法令纹"],
                            },
                            "deal_factors": {"concerns": ["担心恢复期"]},
                            "recommended_plan": {"items": [{"plan": "玻尿酸填充法令纹"}]},
                            "deal_outcome": {"status": "未明确"},
                        },
                        "standardized_indications": {
                            "items": [
                                {"department_code": "Y2", "indication_code": "SYZ2001", "body_part_code": "BW2005"},
                            ]
                        },
                    },
                    completed_at=datetime(2026, 4, 16, 10, 0, 0, tzinfo=timezone.utc),
                )
                task_two = AnalysisTask(
                    id="task002",
                    file_name="recording_rec002.json",
                    file_path="uploads/analysis_input/recording_rec002.json",
                    status="done",
                    result={
                        "consultation_result": {
                            "chief_complaint_and_indications": {
                                "primary_demands": ["眼下轻度松弛，想显年轻"],
                            },
                            "deal_factors": {"budget": "1万左右"},
                            "recommended_plan": {"items": [{"plan": "眼周抗衰联合注射"}]},
                            "deal_outcome": {
                                "status": "未成交",
                                "loss_reasons": ["需要回去考虑价格"],
                            },
                        },
                        "standardized_indications": {
                            "items": [
                                {"department_code": "Y3", "indication_code": "SYZ3001", "body_part_code": "BW3001"},
                            ]
                        },
                    },
                    completed_at=datetime(2026, 4, 16, 10, 5, 0, tzinfo=timezone.utc),
                )
                db.add_all([customer, staff_one, staff_two, visit, order, recording_one, recording_two, link_one, link_two, task_one, task_two])
                await db.commit()

                preview = await generate_sap_consultation_payloads(db, recording_one.id)

                assert preview["target_count"] == 1
                assert preview["recording_count"] == 2
                assert preview["targets"][0]["recording_count"] == 2
                assert len(preview["payloads"]) == 1
                payload = preview["payloads"][0]
                assert "●备注人员：李玲玉" in payload["text"]
                assert "●备注人员：王医生" in payload["text"]
                assert "●接诊人员：" not in payload["text"]
                assert payload["text"].count("●备注人员：") == 2
                assert payload["text"].index("●备注人员：李玲玉") < payload["text"].index("●备注人员：王医生")
                assert "改善法令纹" in payload["text"]
                assert "眼下轻度松弛，想显年轻" in payload["text"]
                assert "玻尿酸填充法令纹" in payload["text"]
                assert "眼周抗衰联合注射" in payload["text"]
                assert "●本次预算：无" in payload["text"]
                assert "●本次预算：1万左右" in payload["text"]
                assert payload["TAB_SYZ"] == [
                    {"CCKS": "Y3", "CCSYZ": "SYZ3001", "CCBW": "BW3001"},
                ]
        finally:
            await engine.dispose()

    try:
        asyncio.run(scenario())
    finally:
        get_settings.cache_clear()


def test_generate_sap_consultation_payloads_refreshes_stale_review_with_new_recording(monkeypatch) -> None:
    monkeypatch.setenv("SAP_RFC_OVERRIDE_KUNR", "")
    monkeypatch.setenv("SAP_RFC_OVERRIDE_USER", "")
    monkeypatch.setenv("SAP_RFC_OVERRIDE_ADVXC", "")
    monkeypatch.setenv("SAP_RFC_OVERRIDE_ZXDH", "")

    def fake_chat_completion(*args, **kwargs) -> str:
        return json.dumps(
            {
                "standardized_indications": {
                    "items": [
                        {"department_code": "Y3", "indication_code": "SYZ3001", "body_part_code": "BW3001"},
                    ],
                },
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("smart_badge_api.sap_consultation.chat_completion", fake_chat_completion)
    get_settings.cache_clear()

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                customer = Customer(id="cust001", name="Customer", external_customer_code="KH001")
                staff_one = Staff(id="staff001", name="Alice")
                staff_two = Staff(id="staff002", name="Bob")
                visit = Visit(
                    id="visit001",
                    customer_id=customer.id,
                    external_visit_order_no="DZ001",
                    external_visit_order_seg="110",
                )
                order = VisitOrder(
                    id="vo001",
                    dzdh="DZ001",
                    dzseg="110",
                    jgbm="6101",
                    kunr="KH001",
                    ninam="Customer",
                    advxc="ADV001",
                    advxc_long="Advisor",
                    fzdh="DZ001-110",
                    crtdt="20260416",
                    crttm="173000",
                )
                recording_one = Recording(
                    id="rec001",
                    visit_id=visit.id,
                    file_name="alice.mp3",
                    file_path="uploads/recordings/alice.mp3",
                    status="analyzed",
                    staff_id=staff_one.id,
                    created_at=datetime(2026, 4, 16, 9, 30, 0, tzinfo=timezone.utc),
                )
                recording_two = Recording(
                    id="rec002",
                    visit_id=visit.id,
                    file_name="bob.mp3",
                    file_path="uploads/recordings/bob.mp3",
                    status="analyzed",
                    staff_id=staff_two.id,
                    created_at=datetime(2026, 4, 16, 9, 45, 0, tzinfo=timezone.utc),
                )
                task_one = AnalysisTask(
                    id="task001",
                    file_name="recording_rec001.json",
                    file_path="uploads/analysis_input/recording_rec001.json",
                    status="done",
                    result={
                        "consultation_result": {
                            "chief_complaint_and_indications": {"primary_demands": ["A GENERATED DEMAND"]},
                            "recommended_plan": {"items": [{"plan": "A GENERATED PLAN"}]},
                            "deal_outcome": {"status": "未明确"},
                        },
                        "standardized_indications": {"items": []},
                    },
                    completed_at=datetime(2026, 4, 16, 10, 0, 0, tzinfo=timezone.utc),
                )
                task_two = AnalysisTask(
                    id="task002",
                    file_name="recording_rec002.json",
                    file_path="uploads/analysis_input/recording_rec002.json",
                    status="done",
                    result={
                        "consultation_result": {
                            "chief_complaint_and_indications": {"primary_demands": ["B GENERATED DEMAND"]},
                            "deal_factors": {"budget": "B BUDGET"},
                            "recommended_plan": {"items": [{"plan": "B GENERATED PLAN"}]},
                            "deal_outcome": {"status": "未明确"},
                        },
                        "standardized_indications": {
                            "items": [
                                {"department_code": "Y3", "indication_code": "SYZ3001", "body_part_code": "BW3001"},
                            ]
                        },
                    },
                    completed_at=datetime(2026, 4, 16, 10, 5, 0, tzinfo=timezone.utc),
                )
                edited_body = "●顾客主诉：A EDITED DEMAND\n●本次预算：A EDITED BUDGET\n●顾客顾虑：无\n●推荐方案：A EDITED PLAN"
                review = SapConsultationReview(
                    id="review001",
                    visit_id=visit.id,
                    visit_order_no="DZ001",
                    visit_order_seg="110",
                    recording_ids=[recording_one.id],
                    blocks=[
                        {
                            "recording_id": recording_one.id,
                            "file_name": recording_one.file_name,
                            "staff_id": staff_one.id,
                            "staff_name": staff_one.name,
                            "locked_header": "●备注人员：Alice",
                            "generated_body": "●顾客主诉：A GENERATED DEMAND",
                            "edited_body": edited_body,
                            "effective_body": edited_body,
                            "sort_index": 1,
                        }
                    ],
                    generated_text="●备注人员：Alice\n●顾客主诉：A GENERATED DEMAND",
                    effective_text=f"●备注人员：Alice\n{edited_body}",
                    status="succeeded",
                )
                db.add_all(
                    [
                        customer,
                        staff_one,
                        staff_two,
                        visit,
                        order,
                        recording_one,
                        recording_two,
                        RecordingVisitLink(recording_id=recording_one.id, visit_id=visit.id, is_primary=True),
                        RecordingVisitLink(recording_id=recording_two.id, visit_id=visit.id, is_primary=True),
                        task_one,
                        task_two,
                        review,
                    ]
                )
                await db.commit()

                preview = await generate_sap_consultation_payloads(db, recording_two.id, target_visit_id=visit.id)

                text = preview["payloads"][0]["text"]
                assert "A EDITED DEMAND" in text
                assert "A EDITED PLAN" in text
                assert "A GENERATED DEMAND" not in text
                assert "B GENERATED DEMAND" in text
                assert "B GENERATED PLAN" in text
                assert text.index("●备注人员：Alice") < text.index("●备注人员：Bob")

                refreshed = await db.get(SapConsultationReview, "review001")
                assert refreshed is not None
                assert refreshed.recording_ids == ["rec001", "rec002"]
                assert refreshed.effective_text == text
        finally:
            await engine.dispose()

    try:
        asyncio.run(scenario())
    finally:
        get_settings.cache_clear()
