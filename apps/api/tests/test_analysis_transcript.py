import json

from smart_badge_api.analysis import pipeline
from smart_badge_api.analysis.transcript import format_dialogue, normalize_role, prepare_transcript


def test_normalize_role_supports_archive_business_roles() -> None:
    assert normalize_role("consultant") == "咨询师"
    assert normalize_role("badge_owner") == "工牌本人"
    assert normalize_role("staff_peer") == "员工同事"
    assert normalize_role("primary_customer") == "主客户"
    assert normalize_role("visitor_companion") == "同行人"


def test_format_dialogue_uses_precise_speaker_labels() -> None:
    dialogue = format_dialogue(
        [
            {
                "role": "badge_owner",
                "speaker_label": "兰四秀（工牌本人）",
                "begin": 0,
                "end": 2500,
                "text": "今天我先帮您了解一下需求。",
            },
            {
                "role": "primary_customer",
                "speaker_label": "主客户",
                "begin": 2600,
                "end": 5200,
                "text": "我主要想做腰腹吸脂。",
            },
            {
                "role": "unknown",
                "speaker_label": "SPEAKER_03",
                "begin": 5300,
                "end": 7000,
                "text": "我是陪她一起来的。",
            },
        ]
    )

    assert "[00:00-00:02] 兰四秀（工牌本人）: 今天我先帮您了解一下需求。" in dialogue
    assert "[00:02-00:05] 主客户: 我主要想做腰腹吸脂。" in dialogue
    assert "[00:05-00:07] 其他在场人员: 我是陪她一起来的。" in dialogue


def test_prepare_transcript_supports_archive_utterance_shape(tmp_path) -> None:
    transcript_path = tmp_path / "archive_transcript.json"
    transcript_path.write_text(
        json.dumps(
            {
                "stageKey": "demo",
                "utterances": [
                    {
                        "speaker": "consultant",
                        "speaker_role": "consultant",
                        "speaker_display_label": "李文军（工牌本人）",
                        "begin_ms": 0,
                        "end_ms": 4200,
                        "text": "今天主要想了解哪方面？",
                    },
                    {
                        "speaker": "customer",
                        "speaker_role": "customer",
                        "speaker_display_label": "主客户",
                        "begin_ms": 4300,
                        "end_ms": 9800,
                        "text": "想咨询鼻部塑形方案。",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    dialogue, raw = prepare_transcript(transcript_path)

    assert raw["stageKey"] == "demo"
    assert "[00:00-00:04] 咨询师（李文军（工牌本人））: 今天主要想了解哪方面？" in dialogue
    assert "[00:04-00:09] 主客户: 想咨询鼻部塑形方案。" in dialogue


def test_analyze_transcript_backfills_primary_demands_and_indications(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample.json"
    transcript_path.write_text(
        json.dumps(
            {
                "payload": {
                    "transcribeResult": [
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 0,
                            "end": 4000,
                            "text": "你好，我是今天接待您的美学顾问，今天主要是想了解哪方面呢？",
                        },
                        {
                            "role": "primary_customer",
                            "speaker_label": "客户",
                            "begin": 4100,
                            "end": 9000,
                            "text": "主要想做水光针，也想了解面部抗衰。",
                        },
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 9100,
                            "end": 16000,
                            "text": "你现在面部明显有些松弛下垂，法令纹也会比较明显。",
                        },
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {"summary": "", "items": []},
            "standardized_indications": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    demand_texts = [item.demand for item in result.customer_primary_demands.items]
    assert "想做水光针" in demand_texts
    assert "希望做面部抗衰" in demand_texts
    assert "改善面部松弛下垂" not in demand_texts
    assert all(item.indication_code != "SYZ3001" for item in result.standardized_indications.items)
    assert result.consultation_result.chief_complaint_and_indications.primary_demands


def test_sanitize_backfills_indications_for_eye_oily_skin_and_body_sculpting() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 60_000,
                    "end": 76_000,
                    "text": "我就是想改善单眼皮，眼睛看久了有点没精神。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 80_000,
                    "end": 96_000,
                    "text": "脸上主要想收缩毛孔，也想补水，T区出油比较多。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 110_000,
                    "end": 130_000,
                    "text": "你的斜方肌比较明显，可以考虑斜方肌肉毒做瘦肩。",
                },
            ]
        }
    }
    result = {
        "customer_primary_demands": {"summary": "", "items": []},
        "standardized_indications": {"summary": "", "items": []},
        "staff_recommendations": {
            "summary": "建议斜方肌肉毒瘦肩",
            "items": [
                {
                    "recommendation": "斜方肌肉毒瘦肩",
                    "body_part": "身体",
                    "evidence": "[01:50] 你的斜方肌比较明显，可以考虑斜方肌肉毒做瘦肩。",
                }
            ],
        },
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    indications = {
        (item["indication_name"], item["body_part_name"])
        for item in result["standardized_indications"]["items"]
    }
    assert ("双眼皮", "眼部") in indications
    assert ("毛孔", "面部") in indications
    assert ("干燥", "面部") in indications
    assert ("油脂旺盛", "面部") in indications
    assert ("塑美", "身体") in indications


def test_sanitize_backfills_double_eyelid_from_single_eyelid_consultation() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "speaker_label": "咨询师",
                    "begin": 60,
                    "end": 60_000,
                    "text": "我们这边是专门做美杜莎眼型设计的，可以帮你看一下眼睛的问题。",
                },
                {
                    "speaker_label": "员工同事",
                    "begin": 60_540,
                    "end": 120_000,
                    "text": "看久了就感觉眼是个单眼皮，对，是个单眼皮，之前也去面诊过其他医生。",
                },
            ]
        }
    }
    result = {
        "customer_primary_demands": {
            "summary": "对话中未识别到客户明确表达的核心诉求。",
            "items": [],
        },
        "standardized_indications": {
            "summary": "对话中未识别出可标准化的适应症",
            "items": [],
        },
        "staff_recommendations": {"summary": "", "items": []},
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    indications = result["standardized_indications"]["items"]
    assert [(item["indication_name"], item["body_part_name"]) for item in indications] == [("双眼皮", "眼部")]


def test_customer_profile_age_evidence_repaired_to_direct_answer() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 89_000,
                    "end": 104_000,
                    "text": "而且你现在呃，今年多大，年龄68 68岁，年龄稍微大一点点。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 951_000,
                    "end": 988_000,
                    "text": "嗯就前一周不要喝酒那些就可以了，因为像您这个年龄的话，他不像30岁，他可能只有一点点松那种。",
                },
            ]
        }
    }
    result = {
        "customer_profile": {
            "tags": [
                {
                    "category": "出生日期",
                    "value": "30岁",
                    "weight_level": 1,
                    "evidence": "[15:51] 嗯就前一周不要喝酒那些就可以了，因为像您这个年龄的话，他不像30岁。",
                }
            ]
        }
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    assert result["customer_profile"]["age"] == "68岁"
    assert result["customer_profile"]["age_evidence"] == "[01:29] 而且你现在呃，今年多大，年龄68 68岁，年龄稍微大一点点。"
    assert all(tag["value"] != "30岁" for tag in result["customer_profile"]["tags"])


def test_birthdate_statement_rejects_age_examples() -> None:
    assert pipeline._looks_like_birthdate_statement(
        "嗯就前一周不要喝酒那些就可以了，因为像您这个年龄的话，他不像30岁，他可能只有一点点松那种。",
        "30岁",
    ) is False
    assert pipeline._looks_like_birthdate_statement(
        "而且你现在呃，今年多大，年龄68 68岁，年龄稍微大一点点。",
        "68岁",
    ) is False
    assert pipeline._extract_supported_age("而且你现在呃，今年多大，年龄68 68岁，年龄稍微大一点点。") == "68岁"


def test_extract_supported_age_rejects_life_timeline_anchor() -> None:
    assert pipeline._extract_supported_age("我从20岁到大学第二年我就进入了这个行业。") is None


def test_sanitize_removes_chatty_eye_and_scar_false_primary_demands() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 0,
                    "end": 8_000,
                    "text": "你之前有打过哪些位置。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 8_000,
                    "end": 25_000,
                    "text": "我的卧蚕泪沟太阳穴都打过，现在融完后整个眼眶子全空了，泪沟也凹了。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 25_000,
                    "end": 42_000,
                    "text": "鼻基底其实也空了，两个八字纹明显，想恢复平整一点。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 672_000,
                    "end": 682_000,
                    "text": "我不想那种自然双眼皮，我想把内眼角调整一点。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 701_000,
                    "end": 706_000,
                    "text": "闭上可能因为我是疤痕角啊。",
                },
                {
                    "role": "doctor",
                    "speaker_label": "医生",
                    "begin": 1_384_000,
                    "end": 1_396_000,
                    "text": "用瑞德喜填充鼻基底及内侧苹果肌，可以改善这两条八字纹。",
                },
            ]
        }
    }
    result = {
        "customer_primary_demands": {
            "summary": "改善单眼皮，想让眼睛更有神；手部小疤痕，希望去除",
            "items": [
                {
                    "priority": 1,
                    "demand": "改善单眼皮，想让眼睛更有神",
                    "body_part": "眼部",
                    "evidence": "[11:12] 我不想那种自然双眼皮，我想把内眼角调整一点。",
                },
                {
                    "priority": 2,
                    "demand": "手部小疤痕，希望去除",
                    "body_part": "身体",
                    "evidence": "[11:41] 闭上可能因为我是疤痕角啊。",
                },
            ],
        },
        "standardized_indications": {
            "summary": "识别出3项适应症：双眼皮（眼部）；疤痕（身体）；面部填充（面部）",
            "items": [
                {
                    "department_code": "Y1",
                    "department_name": "外科",
                    "indication_code": "SYZ1002",
                    "indication_name": "双眼皮",
                    "body_part_code": "BW1001",
                    "body_part_name": "眼部",
                    "evidence": "[11:12] 我不想那种自然双眼皮，我想把内眼角调整一点。",
                },
                {
                    "department_code": "Y3",
                    "department_name": "皮肤",
                    "indication_code": "SYZ3021",
                    "indication_name": "疤痕",
                    "body_part_code": "BW3004",
                    "body_part_name": "身体",
                    "evidence": "[11:41] 闭上可能因为我是疤痕角啊。",
                },
                {
                    "department_code": "Y1",
                    "department_name": "外科",
                    "indication_code": "SYZ1019",
                    "indication_name": "面部填充",
                    "body_part_code": "BW1005",
                    "body_part_name": "面部",
                    "evidence": "[23:04] 用瑞德喜填充鼻基底及内侧苹果肌，可以改善这两条八字纹。",
                },
            ],
        },
        "staff_recommendations": {
            "items": [
                {
                    "recommendation": "瑞德喜填充鼻基底及内侧苹果肌",
                    "body_part": "面部",
                    "evidence": "[23:04] 用瑞德喜填充鼻基底及内侧苹果肌，可以改善这两条八字纹。",
                }
            ]
        },
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    demand_text = "；".join(item["demand"] for item in result["customer_primary_demands"]["items"])
    assert "单眼皮" not in demand_text
    assert "手部小疤痕" not in demand_text
    assert "泪沟" in demand_text or "眼周" in demand_text
    indication_names = {item["indication_name"] for item in result["standardized_indications"]["items"]}
    assert "双眼皮" not in indication_names
    assert "疤痕" not in indication_names
    assert "面部填充" in indication_names


def test_sanitize_corrects_nasal_base_filler_and_negated_eyelid_indications() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 117_000,
                    "end": 126_000,
                    "text": "我面部本来比较平整，现在两个泪沟凹了，鼻翼基底这里也空了。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 149_000,
                    "end": 155_000,
                    "text": "那个专门做鼻子的机构叫什么来着？",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 672_000,
                    "end": 682_000,
                    "text": "我不想那种自然双眼皮，我对双眼皮都不想，我想把内眼角调整一点。",
                },
                {
                    "role": "doctor",
                    "speaker_label": "医生",
                    "begin": 1_384_000,
                    "end": 1_396_000,
                    "text": "用德国瑞德喜填充内侧苹果肌和鼻基底，可以改善这两条八字纹。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 1_839_000,
                    "end": 1_846_000,
                    "text": "那我打卧蚕能不能用福曼再复配嗨体和玻尿酸？",
                },
            ]
        }
    }
    result = {
        "customer_primary_demands": {
            "summary": "咨询鼻部塑形，希望鼻型更自然协调；改善泪沟/眼周凹陷，希望恢复平整自然",
            "items": [
                {
                    "priority": 1,
                    "demand": "咨询鼻部塑形，希望鼻型更自然协调",
                    "body_part": "鼻部",
                    "evidence": "[01:57] 我面部本来比较平整，现在两个泪沟凹了，鼻翼基底这里也空了。",
                },
                {
                    "priority": 2,
                    "demand": "改善泪沟/眼周凹陷，希望恢复平整自然",
                    "body_part": "眼部",
                    "evidence": "[01:57] 我面部本来比较平整，现在两个泪沟凹了，鼻翼基底这里也空了。",
                },
            ],
        },
        "standardized_indications": {
            "summary": "识别出2项适应症：双眼皮（眼部）；鼻综合（鼻部）",
            "items": [
                {
                    "department_name": "外科",
                    "indication_name": "双眼皮",
                    "body_part_name": "眼部",
                    "evidence": "[11:12] 我不想那种自然双眼皮，我对双眼皮都不想，我想把内眼角调整一点。",
                },
                {
                    "department_name": "外科",
                    "indication_name": "鼻综合",
                    "body_part_name": "鼻部",
                    "evidence": "[02:29] 那个专门做鼻子的机构叫什么来着？",
                },
            ],
        },
        "staff_recommendations": {
            "summary": "瑞德喜填充鼻基底及内侧苹果肌改善八字纹；泪沟嗨体+玻尿酸复配",
            "items": [
                {
                    "recommendation": "瑞德喜填充鼻基底及内侧苹果肌改善八字纹",
                    "body_part": "鼻基底/内侧苹果肌",
                    "evidence": "[23:04] 用德国瑞德喜填充内侧苹果肌和鼻基底，可以改善这两条八字纹。",
                },
                {
                    "recommendation": "泪沟嗨体+玻尿酸复配",
                    "body_part": "眼部",
                    "evidence": "[30:39] 那我打卧蚕能不能用福曼再复配嗨体和玻尿酸？",
                },
            ],
        },
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    demand_text = "；".join(item["demand"] for item in result["customer_primary_demands"]["items"])
    assert "鼻部塑形" not in demand_text
    assert "鼻基底/中面部衔接" in demand_text
    indications = {
        (item["department_name"], item["indication_name"], item["body_part_name"])
        for item in result["standardized_indications"]["items"]
    }
    assert ("外科", "双眼皮", "眼部") not in indications
    assert ("外科", "鼻综合", "鼻部") not in indications
    assert ("外科", "面部填充", "面部") in indications
    assert ("微创", "塑美", "眼部（D）") in indications


def test_customer_profile_age_rejects_consultant_self_demo_age() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 48_000,
                    "end": 64_000,
                    "text": "你看，我的脸很垮，我就想怎么改善一下，然后眼睛也很老态，脸也很垮，看你们会有什么样的方案。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "李文军",
                    "begin": 138_000,
                    "end": 154_000,
                    "text": "我不是00后，我31岁了，但是我是全脸都做了，做医美的话现在都是求自然。",
                },
            ]
        }
    }
    result = {
        "customer_profile": {
            "age": "31岁",
            "age_evidence": "[02:18] 李文军：我不是00后，我31岁了，但是我是全脸都做了，做医美的话现在都是求自然。",
            "tags": [],
        }
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    assert "age" not in result["customer_profile"]
    assert "age_evidence" not in result["customer_profile"]


def test_customer_primary_demand_prefers_opening_customer_need_over_consultant_self_example() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "badge_owner",
                    "speaker_label": "李文军",
                    "begin": 40_000,
                    "end": 47_000,
                    "text": "这次过来主要想了解什么，哪里想改善？",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 48_000,
                    "end": 64_000,
                    "text": "你看，我的脸很垮，我就想怎么改善一下，然后眼睛也很老态，脸也很垮，看你们会有什么样的方案。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "李文军",
                    "begin": 155_000,
                    "end": 169_000,
                    "text": "看不出来啊，就相当于我是全脸都做完了，抗衰我做到了极致了啊。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "李文军",
                    "begin": 209_000,
                    "end": 219_000,
                    "text": "我脸上鼻子一万，鼻基底花了四五万吧。",
                },
            ]
        }
    }
    result = {
        "customer_primary_demands": {
            "summary": "全脸抗衰做到极致",
            "items": [
                {
                    "priority": 1,
                    "demand": "希望全脸抗衰做到极致",
                    "body_part": "面部",
                    "evidence": "[02:35] 李文军：看不出来啊，就相当于我是全脸都做完了，抗衰我做到了极致了啊。",
                }
            ],
        },
        "standardized_indications": {"items": []},
        "staff_recommendations": {"items": []},
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    items = result["customer_primary_demands"]["items"]
    assert [item["demand"] for item in items] == ["脸部下垮、眼睛显老态，希望整体改善"]
    assert items[0]["evidence"] == "[00:48] 你看，我的脸很垮，我就想怎么改善一下，然后眼睛也很老态，脸也很垮，看你们会有什么样的方案。"


def test_customer_primary_demand_rejects_mechanism_explanation_as_complaint() -> None:
    assert (
        pipeline._looks_like_primary_demand_evidence(
            "[05:41] 原因是我们随着年龄增长，口周的骨量流失造成的胶原蛋白流失造成的这个口周凹陷。",
            demand="改善口周衔接和干瘪感，希望更自然",
        )
        is False
    )


def test_customer_profile_age_removes_stale_life_timeline_anchor() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 2_555_000,
                    "end": 2_560_000,
                    "text": "我从20岁到大学第二年我就进入了这个行业。",
                }
            ]
        }
    }
    result = {
        "customer_profile": {
            "age": "20岁",
            "age_evidence": "[42:35] 我从20岁到大学第二年我就进入了这个行业。",
            "tags": [],
        }
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    assert "age" not in result["customer_profile"]
    assert "age_evidence" not in result["customer_profile"]


def test_customer_primary_demands_dedupes_eye_bag_tear_trough_fatigue() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 10_000,
                    "end": 18_000,
                    "text": "我主要想改善眼袋和泪沟，看起来不要那么疲惫。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 20_000,
                    "end": 26_000,
                    "text": "另外脸上的痘印也想改善一下。",
                },
            ]
        }
    }
    result = {
        "customer_primary_demands": {
            "summary": "改善眼袋与泪沟，消除疲惫感、避免凹凸不平；改善面部痘印；改善眼袋泪沟疲态",
            "items": [
                {
                    "priority": 1,
                    "demand": "改善眼袋与泪沟，消除疲惫感、避免凹凸不平",
                    "body_part": "眼部",
                    "evidence": "[00:10] 我主要想改善眼袋和泪沟，看起来不要那么疲惫。",
                },
                {
                    "priority": 2,
                    "demand": "改善面部痘印",
                    "body_part": "面部",
                    "evidence": "[00:20] 另外脸上的痘印也想改善一下。",
                },
                {
                    "priority": 3,
                    "demand": "改善眼袋泪沟疲态",
                    "body_part": "眼部",
                    "evidence": "[00:10] 我主要想改善眼袋和泪沟，看起来不要那么疲惫。",
                },
            ],
        }
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    demand_texts = [item["demand"] for item in result["customer_primary_demands"]["items"]]
    assert demand_texts == ["改善眼袋与泪沟，消除疲惫感、避免凹凸不平", "改善面部痘印"]
    assert result["customer_primary_demands"]["summary"] == "改善眼袋与泪沟，消除疲惫感、避免凹凸不平；改善面部痘印"


def test_sanitize_removes_wrinkle_primary_demand_from_simple_mention_without_intent() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 0,
                    "end": 4_000,
                    "text": "今天主要想了解哪方面？",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 5_000,
                    "end": 12_000,
                    "text": "脸上有一点皱纹，不过我这次主要还是先看看鼻子。",
                },
            ]
        }
    }
    result = {
        "customer_primary_demands": {
            "summary": "改善面部纹路和细纹",
            "items": [
                {
                    "priority": 1,
                    "demand": "改善面部纹路和细纹",
                    "body_part": "面部",
                    "evidence": "[00:05] 脸上有一点皱纹，不过我这次主要还是先看看鼻子。",
                }
            ],
        }
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    demand_texts = [item["demand"] for item in result["customer_primary_demands"]["items"]]
    assert "改善面部纹路和细纹" not in demand_texts


def test_sanitize_keeps_wrinkle_primary_demand_with_explicit_intent() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 0,
                    "end": 8_000,
                    "text": "我第一次做医美，主要想改善法令纹。",
                }
            ]
        }
    }
    result = {
        "customer_primary_demands": {
            "summary": "改善面部纹路和细纹",
            "items": [
                {
                    "priority": 1,
                    "demand": "改善面部纹路和细纹",
                    "body_part": "面部",
                    "evidence": "[00:00] 我第一次做医美，主要想改善法令纹。",
                }
            ],
        }
    }

    pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert result["customer_primary_demands"]["items"][0]["demand"] == "解决法令纹问题"


def test_sanitize_treats_nasolabial_fold_topic_answer_as_primary_demand_not_base() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 49_000,
                    "end": 60_000,
                    "text": "于女士是吧？这次过来主要想了解什么项目啊？",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 60_000,
                    "end": 63_000,
                    "text": "就是我的法令纹哦。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 69_000,
                    "end": 71_000,
                    "text": "玻尿酸玻尿酸哈，",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 104_000,
                    "end": 106_000,
                    "text": "他的鼻基底嗯。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 115_000,
                    "end": 125_000,
                    "text": "常规情况下，我们解决法令纹这个地方的话，首先要解决深层的鼻基底凹陷。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 398_000,
                    "end": 405_000,
                    "text": "玻尿酸，你可能一年打个两次，对吧？",
                },
            ]
        }
    }
    result = {
        "customer_primary_demands": {
            "summary": "改善面部纹路和细纹；改善鼻基底凹陷",
            "items": [
                {
                    "priority": 1,
                    "demand": "改善面部纹路和细纹",
                    "body_part": "面部",
                    "evidence": "[00:49] 于女士是吧？这次过来主要想了解什么项目啊？\n[01:00] 就是我的法令纹哦。",
                },
                {
                    "priority": 2,
                    "demand": "鼻基底比较凹陷，希望改善面中支撑",
                    "body_part": "鼻基底/面中",
                    "evidence": "[01:55] 常规情况下，我们解决法令纹这个地方的话，首先要解决深层的鼻基底凹陷。",
                },
            ],
        },
        "standardized_indications": {
            "summary": "识别出2项适应症：纹路（面部）；面部填充（面部）",
            "items": [
                {
                    "department_code": "Y3",
                    "department_name": "皮肤",
                    "indication_code": "SYZ3002",
                    "indication_name": "纹路",
                    "body_part_code": "BW3001",
                    "body_part_name": "面部",
                    "evidence": "[00:49] 于女士是吧？这次过来主要想了解什么项目啊？\n[01:00] 就是我的法令纹哦。",
                },
                {
                    "department_code": "Y1",
                    "department_name": "外科",
                    "indication_code": "SYZ1019",
                    "indication_name": "面部填充",
                    "body_part_code": "BW1005",
                    "body_part_name": "面部",
                    "evidence": "[01:55] 常规情况下，我们解决法令纹这个地方的话，首先要解决深层的鼻基底凹陷。",
                },
            ],
        },
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    assert [item["demand"] for item in result["customer_primary_demands"]["items"]] == ["解决法令纹问题"]
    assert result["customer_primary_demands"]["summary"] == "解决法令纹问题"
    assert [
        (item["indication_name"], item["body_part_name"])
        for item in result["standardized_indications"]["items"]
    ] == [("纹路", "面部")]
    assert result["standardized_indications"]["summary"] == "识别出1项适应症：纹路（面部）"


def test_sanitize_removes_third_party_story_from_demands_indications_and_tags() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 0,
                    "end": 18_000,
                    "text": "我有个老乡之前打过玻尿酸，后来鼻子不太满意，脸上很多痘痘毛孔，从来没做过医美的时候特别害怕。",
                }
            ]
        }
    }
    evidence = "[00:00] 我有个老乡之前打过玻尿酸，后来鼻子不太满意，脸上很多痘痘毛孔，从来没做过医美的时候特别害怕。"
    result = {
        "customer_primary_demands": {
            "summary": "改善毛孔粗大",
            "items": [
                {
                    "priority": 1,
                    "demand": "改善毛孔粗大",
                    "body_part": "面部",
                    "evidence": evidence,
                }
            ],
        },
        "standardized_indications": {
            "summary": "识别出2项适应症：痤疮（面部）；毛孔（面部）",
            "items": [
                {
                    "department_code": "Y3",
                    "department_name": "皮肤",
                    "indication_code": "SYZ3007",
                    "indication_name": "痤疮",
                    "body_part_code": "BW3001",
                    "body_part_name": "面部",
                    "evidence": evidence,
                },
                {
                    "department_code": "Y3",
                    "department_name": "皮肤",
                    "indication_code": "SYZ3004",
                    "indication_name": "毛孔",
                    "body_part_code": "BW3001",
                    "body_part_name": "面部",
                    "evidence": evidence,
                },
            ],
        },
        "customer_profile": {
            "tags": [
                {"category": "治疗项目", "value": "无医美史", "evidence": evidence},
                {"category": "历史用的设备/原材料名称", "value": "玻尿酸", "evidence": evidence},
                {"category": "负面项目/设备/原材料", "value": "玻尿酸", "evidence": evidence},
            ]
        },
        "staff_recommendations": {"summary": "", "items": []},
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    assert result["customer_primary_demands"]["items"] == []
    assert result["standardized_indications"]["items"] == []
    assert result["customer_profile"]["tags"] == []


def test_sanitize_keeps_customer_self_demand_in_mixed_third_party_context() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 0,
                    "end": 16_000,
                    "text": "我朋友以前做了眼袋不太满意，所以我自己现在更想改善眼袋和泪沟，不想看起来这么疲惫。",
                }
            ]
        }
    }
    evidence = "[00:00] 我朋友以前做了眼袋不太满意，所以我自己现在更想改善眼袋和泪沟，不想看起来这么疲惫。"
    result = {
        "customer_primary_demands": {
            "summary": "改善眼袋泪沟疲态",
            "items": [
                {
                    "priority": 1,
                    "demand": "改善眼袋泪沟疲态",
                    "body_part": "眼部",
                    "evidence": evidence,
                }
            ],
        },
        "standardized_indications": {
            "summary": "识别出1项适应症：眼袋（眼部）",
            "items": [
                {
                    "department_code": "Y1",
                    "department_name": "外科",
                    "indication_code": "SYZ1001",
                    "indication_name": "眼袋",
                    "body_part_code": "BW1001",
                    "body_part_name": "眼部",
                    "evidence": evidence,
                }
            ],
        },
        "staff_recommendations": {"summary": "", "items": []},
    }

    pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert [item["demand"] for item in result["customer_primary_demands"]["items"]] == ["改善眼袋泪沟疲态"]
    assert [
        (item["indication_name"], item["body_part_name"])
        for item in result["standardized_indications"]["items"]
    ] == [("眼袋", "眼部")]


def test_customer_profile_health_no_risk_skips_question_only_evidence() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "doctor",
                    "speaker_label": "医生",
                    "begin": 507_680,
                    "end": 508_700,
                    "text": "你有没有高血压。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "张鑫（工牌本人）",
                    "begin": 508_715,
                    "end": 511_300,
                    "text": "那些没有没有没有高血压。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "张鑫（工牌本人）",
                    "begin": 519_560,
                    "end": 523_000,
                    "text": "我没有高血压，就是胆固醇有点高。",
                },
            ]
        }
    }
    result = {
        "customer_profile": {
            "tags": [
                {
                    "category": "健康风险/禁忌",
                    "value": "无风险禁忌",
                    "weight_level": 1,
                    "evidence": "[08:27] 你有没有高血压。",
                }
            ]
        }
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    assert result["customer_profile"]["tags"] == []


def test_customer_profile_health_positive_tag_rejects_negative_answer() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 0,
                    "end": 3000,
                    "text": "我没有高血压，就是胆固醇有点高。",
                }
            ]
        }
    }
    result = {
        "customer_profile": {
            "tags": [
                {
                    "category": "健康风险/禁忌",
                    "value": "高血压",
                    "weight_level": 1,
                    "evidence": "[00:00] 我没有高血压，就是胆固醇有点高。",
                }
            ]
        }
    }

    pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    tags = result["customer_profile"]["tags"]
    assert all(tag["value"] != "高血压" for tag in tags)
    assert any(tag["value"] == "无风险禁忌" for tag in tags)


def test_customer_profile_health_no_risk_rejects_financial_negation_context() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "badge_owner",
                    "speaker_label": "张鑫（工牌本人）",
                    "begin": 1_980_000,
                    "end": 1_983_000,
                    "text": "你自己是没有存款那些的。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 1_983_100,
                    "end": 1_984_000,
                    "text": "对。",
                },
            ]
        }
    }
    result = {
        "customer_profile": {
            "tags": [
                {
                    "category": "健康风险/禁忌",
                    "value": "无风险禁忌",
                    "weight_level": 1,
                    "evidence": "[33:00] 你自己是没有存款那些的。\n[33:03] 对。",
                }
            ]
        }
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    assert result["customer_profile"]["tags"] == []


def test_customer_profile_treatment_history_normalizes_specific_procedure_to_broad_category() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 29_810,
                    "end": 36_000,
                    "text": "我是我我看到是头几年我我来做过那个提眉那个手术，哎。",
                },
                {
                    "role": "doctor",
                    "speaker_label": "医生",
                    "begin": 950_000,
                    "end": 960_000,
                    "text": "做拉皮又贵，恢复期又长，而且别人一看就看得出来你是做了拉皮的。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "张鑫（工牌本人）",
                    "begin": 1_120_000,
                    "end": 1_130_000,
                    "text": "我跟我同年的一个男同事，他就做了眼袋。",
                }
            ]
        }
    }
    result = {
        "customer_profile": {
            "tags": [
                {
                    "category": "治疗项目",
                    "value": "提眉",
                    "weight_level": 1,
                    "evidence": "[00:29] 我是我我看到是头几年我我来做过那个提眉那个手术，哎。",
                }
            ]
        }
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    tags = result["customer_profile"]["tags"]
    assert {
        "category": "治疗项目",
        "value": "手术类",
        "weight_level": 1,
        "evidence": "[00:29] 我是我我看到是头几年我我来做过那个提眉那个手术，哎。",
    } in tags
    assert [tag["value"] for tag in tags if tag["category"] == "治疗项目"] == ["手术类"]


def test_customer_profile_treatment_history_rejects_future_suggestion_and_effect_request() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "badge_owner",
                    "speaker_label": "张鑫（工牌本人）",
                    "begin": 1_305_000,
                    "end": 1_309_000,
                    "text": "你建议我是注射还是做手术呢你吗。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 1_309_100,
                    "end": 1_310_000,
                    "text": "对。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 3_653_000,
                    "end": 3_657_000,
                    "text": "我的意思是就是看不出来那个手术痕迹看不出来手术痕迹。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 3_886_000,
                    "end": 3_898_000,
                    "text": "我下巴就是打过的，我下巴之前有点点后缩。",
                },
            ]
        }
    }
    result = {
        "customer_profile": {
            "tags": [
                {
                    "category": "治疗项目",
                    "value": "注射类",
                    "weight_level": 1,
                    "evidence": "[21:45] 你建议我是注射还是做手术呢你吗。\n[21:49] 对。",
                },
                {
                    "category": "治疗项目",
                    "value": "手术类",
                    "weight_level": 1,
                    "evidence": "[60:53] 我的意思是就是看不出来那个手术痕迹看不出来手术痕迹。",
                },
                {
                    "category": "治疗历史",
                    "value": "正畸",
                    "evidence": "[03:52] 现在已经做了正畸。",
                },
                {
                    "category": "治疗历史",
                    "value": "注射类",
                    "evidence": "[64:46] 我下巴就是打过的，我下巴之前有点点后缩。",
                },
            ]
        }
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    assert result["customer_profile"]["tags"] == [
        {
            "category": "治疗项目",
            "value": "注射类",
            "weight_level": 1,
            "evidence": "[64:46] 我下巴就是打过的，我下巴之前有点点后缩。",
        }
    ]


def test_customer_profile_backfills_injection_history_from_brief_question_answer() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
            {
                "speaker": "consultant",
                "role": "badge_owner",
                "speaker_label": "钟露（工牌本人）",
                "begin": 950_000,
                "end": 960_000,
                "text": "这个方案是100单位乐提葆，主要做除皱和提升。",
            },
            {
                "speaker": "speaker_2",
                "role": "staff_peer",
                "speaker_label": "员工同事",
                "begin": 965_490,
                "end": 966_280,
                "text": "打过没有。",
            },
            {
                "speaker": "consultant",
                "role": "badge_owner",
                "speaker_label": "钟露（工牌本人）",
                "begin": 966_330,
                "end": 969_070,
                "text": "嗯，几年前打过一次，哎，对。",
            },
            ]
        }
    }
    result = {"customer_profile": {"tags": []}}

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    assert {
        "category": "治疗项目",
        "value": "注射类",
        "weight_level": 1,
        "evidence": "[16:05] 打过没有。\n[16:06] 嗯，几年前打过一次，哎，对。",
    } in result["customer_profile"]["tags"]


def test_customer_profile_does_not_treat_ambiguous_hit_as_injection_without_context() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "speaker": "speaker_2",
                    "role": "staff_peer",
                    "speaker_label": "员工同事",
                    "begin": 965_490,
                    "end": 966_280,
                    "text": "打过没有。",
                },
                {
                    "speaker": "consultant",
                    "role": "badge_owner",
                    "speaker_label": "钟露（工牌本人）",
                    "begin": 966_330,
                    "end": 969_070,
                    "text": "嗯，几年前打过一次，哎，对。",
                },
            ]
        }
    }
    result = {"customer_profile": {"tags": []}}

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is False
    assert result["customer_profile"]["tags"] == []


def test_customer_profile_backfills_energy_history_from_laser_context() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "speaker": "consultant",
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 0,
                    "end": 3000,
                    "text": "我们刚才说的是点阵激光，主要看你之前有没有做过。",
                },
                {
                    "speaker": "speaker_2",
                    "role": "staff_peer",
                    "speaker_label": "员工同事",
                    "begin": 3100,
                    "end": 3900,
                    "text": "打过没有。",
                },
                {
                    "speaker": "consultant",
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 4000,
                    "end": 6000,
                    "text": "嗯，几年前做过一次点阵激光，哎，对。",
                },
            ]
        }
    }
    result = {"customer_profile": {"tags": []}}

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    tag_pairs = {(item["category"], item["value"]) for item in result["customer_profile"]["tags"]}
    assert ("治疗项目", "光电类") in tag_pairs
    assert ("治疗项目", "注射类") not in tag_pairs


def test_customer_profile_backfills_price_sensitivity_from_discount_question() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 468_390,
                    "end": 476_000,
                    "text": "如果直接在店里面买，有没有那个活动啊，他跟我说的是999三个部位啊。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 534_795,
                    "end": 540_000,
                    "text": "这个100单位的1600不能一分钱都少不了。",
                },
            ]
        }
    }
    result = {"customer_profile": {"tags": []}}

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    assert {
        "category": "价格敏感度",
        "value": "高",
        "weight_level": 2,
        "evidence": "[08:54] 这个100单位的1600不能一分钱都少不了。",
    } in result["customer_profile"]["tags"]


def test_customer_profile_does_not_backfill_price_sensitivity_from_staff_price_intro_only() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 0,
                    "end": 4000,
                    "text": "这个已经是特价活动了，1999已经是活动价。",
                }
            ]
        }
    }
    result = {"customer_profile": {"tags": []}}

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is False
    assert result["customer_profile"]["tags"] == []


def test_customer_profile_sanitizes_weak_profile_tags_from_0420_094902_like_dialogue() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 2_070,
                    "end": 4_000,
                    "text": "就用我太多了。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 20_000,
                    "end": 28_000,
                    "text": "你已经做过一次了，做过一次的双眼皮是通过手术把你里面单眼皮的，把你天生的单眼皮变成双眼皮。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "赵婷玲（工牌本人）",
                    "begin": 3_585_240,
                    "end": 3_606_970,
                    "text": "我顾客年龄比你大50多岁的一个姐，切了眉之后，纹绣师都没发现她眉毛是切过的。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "赵婷玲（工牌本人）",
                    "begin": 3_819_015,
                    "end": 3_826_820,
                    "text": "高血压，糖尿病有没有加致传染病，有没有焦虑或者抑郁。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 4_030_000,
                    "end": 4_034_000,
                    "text": "我是成都的。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "赵婷玲（工牌本人）",
                    "begin": 4_099_330,
                    "end": 4_112_360,
                    "text": "反正我说我走哪儿都不像本地人，反正他们都说像外地人。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "赵婷玲（工牌本人）",
                    "begin": 4_230_380,
                    "end": 4_232_760,
                    "text": "身份证号年龄是36哈。",
                },
            ]
        }
    }
    result = {
        "customer_profile": {
            "age": "50多岁",
            "age_evidence": "[59:45] 我顾客年龄比你大50多岁的一个姐，切了眉之后。",
            "tags": [
                {"category": "价格敏感度", "value": "高", "weight_level": 2, "evidence": "[00:02] 就用我太多了。"},
                {"category": "治疗项目", "value": "手术类", "weight_level": 1, "evidence": "[59:45] 能用微创的，能做无创的我们就做无创。"},
                {"category": "健康风险/禁忌", "value": "传染性疾病", "weight_level": 1, "evidence": "[63:39] 高血压，糖尿病有没有加致传染病，有没有焦虑或者抑郁。"},
                {"category": "常驻城市", "value": "本地", "weight_level": 1, "evidence": "[67:10] 我是成都的。"},
                {"category": "常驻城市", "value": "外地", "weight_level": 1, "evidence": "[68:19] 我走哪儿都不像本地人，反正他们都说像外地人。"},
            ],
        }
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    assert result["customer_profile"]["age"] == "36岁"
    assert result["customer_profile"]["age_evidence"] == "[70:30] 身份证号年龄是36哈。"
    tag_pairs = {(item["category"], item["value"]) for item in result["customer_profile"]["tags"]}
    assert ("治疗项目", "手术类") in tag_pairs
    assert ("常驻城市", "本地") in tag_pairs
    assert ("价格敏感度", "高") not in tag_pairs
    assert ("健康风险/禁忌", "传染性疾病") not in tag_pairs
    assert ("常驻城市", "外地") not in tag_pairs


def test_customer_profile_backfills_negative_project_from_prior_eyelid_regret() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 35_140,
                    "end": 42_000,
                    "text": "双眼皮本来是单眼皮的，反是单眼皮做过，现在看的是那双呢。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 67_480,
                    "end": 69_000,
                    "text": "3年前做出来的时候满意吗？",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 69_435,
                    "end": 73_000,
                    "text": "那的时候真没有，因为我也后悔。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 84_130,
                    "end": 88_000,
                    "text": "当时做出来就不满意，那都3年了。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 198_950,
                    "end": 208_000,
                    "text": "你已经做过一次了，做过一次的双眼皮是通过手术把你天生的单眼皮变成双眼皮。",
                },
            ]
        }
    }
    result = {
        "customer_profile": {
            "tags": [
                {"category": "治疗项目", "value": "手术类", "weight_level": 1, "evidence": "[00:35] 双眼皮本来是单眼皮的，反是单眼皮做过，现在看的是那双呢。"},
                {"category": "负面项目/设备/原材料", "value": "无", "weight_level": 1, "evidence": "[01:09] 那的时候真没有，因为我也后悔。"},
            ]
        }
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    tag_pairs = {(item["category"], item["value"]) for item in result["customer_profile"]["tags"]}
    assert ("治疗项目", "手术类") in tag_pairs
    assert ("负面项目/设备/原材料", "双眼皮") in tag_pairs
    assert ("负面项目/设备/原材料", "无") not in tag_pairs


def test_customer_profile_backfills_negative_project_from_prior_filler_dissatisfaction() -> None:
    text = (
        "我想了解一下鼻子，嗯嗯，我先说一下我自己想法嘛，可以，就是我的鼻子，我觉得我鼻背的高度还可以，"
        "但是鼻三根呢，整体会比较低，我这个位置我之前打了一点玻尿酸，尝试了一下，然后我最不满意的是我的就是"
        "拧开看一下我的鼻孔，其实有点闭尖，有点卡，嗯，然后想整体调一下，你玻尿酸之前打了多久了，你玻尿酸"
        "打了很久了，有一两年了。"
    )
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 40_000,
                    "end": 94_000,
                    "text": text,
                }
            ]
        }
    }
    result = {
        "customer_profile": {
            "tags": [
                {"category": "治疗项目", "value": "注射类", "weight_level": 1, "evidence": f"[00:40] {text}"},
                {"category": "历史用的设备/原材料名称", "value": "玻尿酸", "weight_level": 1, "evidence": f"[00:40] {text}"},
                {"category": "负面项目/设备/原材料", "value": "无", "weight_level": 1, "evidence": f"[00:40] {text}"},
            ]
        }
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    tag_pairs = {(item["category"], item["value"]) for item in result["customer_profile"]["tags"]}
    assert ("历史用的设备/原材料名称", "玻尿酸") in tag_pairs
    assert ("负面项目/设备/原材料", "玻尿酸") in tag_pairs
    assert ("负面项目/设备/原材料", "无") not in tag_pairs


def test_customer_profile_does_not_treat_first_surgery_as_no_medical_history() -> None:
    filler_text = "我这个位置，我之前打了一点玻尿酸，尝试了一下，然后我最不满意的是我的鼻孔，想整体调一下。"
    surgery_text = "我说实话，我第一次做这个手术，嗯，我觉得肋骨好像看他们在床上几天了。"
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 51_000,
                    "end": 63_000,
                    "text": filler_text,
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 669_000,
                    "end": 688_000,
                    "text": surgery_text,
                },
            ]
        }
    }
    result = {
        "customer_profile": {
            "tags": [
                {"category": "治疗项目", "value": "无医美史", "weight_level": 1, "evidence": f"[11:09] {surgery_text}"},
                {"category": "历史用的设备/原材料名称", "value": "无", "weight_level": 1, "evidence": f"[11:09] {surgery_text}"},
                {"category": "负面项目/设备/原材料", "value": "无", "weight_level": 1, "evidence": f"[11:09] {surgery_text}"},
            ]
        }
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    tag_pairs = {(item["category"], item["value"]) for item in result["customer_profile"]["tags"]}
    assert ("治疗项目", "无医美史") not in tag_pairs
    assert ("治疗项目", "注射类") in tag_pairs
    assert ("历史用的设备/原材料名称", "玻尿酸") in tag_pairs
    assert ("负面项目/设备/原材料", "玻尿酸") in tag_pairs
    assert ("历史用的设备/原材料名称", "无") not in tag_pairs
    assert ("负面项目/设备/原材料", "无") not in tag_pairs


def test_sanitize_removes_negated_or_generic_indications() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 422_000,
                    "end": 428_000,
                    "text": "但你还好，你没啥泪沟，然后卧蚕还好看吗？",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 1_628_000,
                    "end": 1_635_000,
                    "text": "春季本身皮肤相对要比较敏感一些。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 1_653_000,
                    "end": 1_660_000,
                    "text": "加价购可以选择美睫美甲水光或者箱子四选一。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 1_836_000,
                    "end": 1_840_000,
                    "text": "3支润诺威面中鼻基底，然后一支润致。",
                },
            ]
        }
    }
    result = {
        "standardized_indications": {
            "items": [
                {"department_name": "外科", "indication_name": "眼袋", "body_part_name": "眼部", "evidence": "[07:02] 但你还好，你没啥泪沟，然后卧蚕还好看吗？"},
                {"department_name": "皮肤", "indication_name": "敏感", "body_part_name": "面部", "evidence": "[27:08] 春季本身皮肤相对要比较敏感一些。"},
                {"department_name": "皮肤", "indication_name": "干燥", "body_part_name": "面部", "evidence": "[27:33] 加价购可以选择美睫美甲水光或者箱子四选一。"},
                {"department_name": "外科", "indication_name": "鼻综合", "body_part_name": "鼻部", "evidence": "[30:36] 3支润诺威面中鼻基底，然后一支润致。"},
            ]
        }
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    assert result["standardized_indications"]["items"] == []


def test_sanitize_keeps_direct_dryness_and_rhinoplasty_indications() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 0,
                    "end": 3000,
                    "text": "我最近皮肤干，想做水光补水。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 4000,
                    "end": 8000,
                    "text": "我想咨询鼻综合，主要想改善鼻头和山根。",
                },
            ]
        }
    }
    result = {
        "standardized_indications": {
            "items": [
                {"department_name": "皮肤", "indication_name": "干燥", "body_part_name": "面部", "evidence": "[00:00] 我最近皮肤干，想做水光补水。"},
                {"department_name": "外科", "indication_name": "鼻综合", "body_part_name": "鼻部", "evidence": "[00:04] 我想咨询鼻综合，主要想改善鼻头和山根。"},
            ]
        }
    }

    pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    indications = {(item["indication_name"], item["body_part_name"]) for item in result["standardized_indications"]["items"]}
    assert ("干燥", "面部") in indications
    assert ("鼻综合", "鼻部") in indications


def test_sanitize_backfills_indications_from_staff_recommendations() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 0,
                    "end": 8000,
                    "text": "你这个太阳穴有点凹，可以通过玻尿酸填充让外轮廓更流畅。",
                }
            ]
        }
    }
    result = {
        "customer_primary_demands": {"summary": "", "items": []},
        "standardized_indications": {"summary": "对话中未识别出可标准化的适应症", "items": []},
        "staff_recommendations": {
            "summary": "玻尿酸填充塑形",
            "items": [
                {
                    "recommendation": "玻尿酸填充塑形",
                    "product_or_solution": "玻尿酸填充塑形",
                    "body_part": "面部",
                    "evidence": "[00:00] 你这个太阳穴有点凹，可以通过玻尿酸填充让外轮廓更流畅。",
                }
            ],
        },
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    indication_names = {
        (item["indication_name"], item["body_part_name"])
        for item in result["standardized_indications"]["items"]
    }
    assert ("面部填充", "面部") in indication_names


def test_sanitize_rejects_staff_only_weak_plan_context_as_primary_demand() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 0,
                    "end": 6000,
                    "text": "这样的话你花钱可能得不少，要瘦成这种，就是整体得把一点就行。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 7000,
                    "end": 10000,
                    "text": "就会脸会看起来小一些。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 36000,
                    "end": 71000,
                    "text": "如果说打提升的话，100单位的话，现在做的比较多的是英伦大提升，可以选择乐提葆或者保妥适。",
                },
            ]
        }
    }
    result = {
        "customer_primary_demands": {"summary": "", "items": []},
        "standardized_indications": {
            "summary": "识别出1项适应症：面部除皱（面部）",
            "items": [
                {
                    "department_code": "Y1",
                    "department_name": "外科",
                    "indication_code": "SYZ1017",
                    "indication_name": "面部除皱",
                    "body_part_code": "BW1005",
                    "body_part_name": "面部",
                    "evidence": "[00:36] 如果说打提升的话，100单位的话，现在做的比较多的是英伦大提升，可以选择乐提葆或者保妥适。",
                }
            ],
        },
        "staff_recommendations": {
            "summary": "液态提升",
            "items": [
                {
                    "recommendation": "液态提升",
                    "product_or_solution": "100单位肉毒素",
                    "body_part": "面部",
                    "evidence": "[00:36] 如果说打提升的话，100单位的话，现在做的比较多的是英伦大提升，可以选择乐提葆或者保妥适。",
                    "customer_response": "接受",
                    "demand_priority": [1, 2],
                }
            ],
        },
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    assert result["customer_primary_demands"]["items"] == []
    assert result["standardized_indications"]["items"] == []
    assert result["staff_recommendations"]["items"] == []


def test_main_fact_floor_requires_sparse_medical_business_scene() -> None:
    normal_volume_segments = []
    for index in range(40):
        is_customer = index % 2 == 0
        normal_volume_segments.append(
            {
                "role": "primary_customer" if is_customer else "badge_owner",
                "speaker_label": "主客户" if is_customer else "咨询师",
                "begin": index * 10_000,
                "end": index * 10_000 + 8_000,
                "text": (
                    "我主要是在了解整体方案和恢复情况，想再比较一下价格和效果，"
                    "目前还没有明确决定具体项目，需要继续听医生怎么设计，也会关注恢复期、自然度、维持时间和后续护理安排。"
                    "我会反复确认风险、预算、医生经验、材料品牌和术后护理，不会只听一句建议就马上决定。"
                    if is_customer
                    else "建议可以看玻尿酸填充太阳穴和苹果肌，费用、恢复期和医生方案都可以再详细沟通，后面还要结合面诊检查和材料选择来确认。"
                    "这类方案需要看脸型、凹陷程度、预算范围和顾客接受度，不是只凭一个关键词就下结论。"
                ),
            }
        )

    assert pipeline._is_sparse_effective_consultation(normal_volume_segments) is False
    assert (
        pipeline._allows_main_fact_floor(
            normal_volume_segments,
            staff_recommendations_payload={"items": [{"recommendation": "玻尿酸填充太阳穴"}]},
        )
        is False
    )

    sparse_medical_segments = [
        {
            "role": "badge_owner",
            "speaker_label": "咨询师",
            "begin": 0,
            "end": 8_000,
            "text": "建议做玻尿酸填充太阳穴，费用和方案一会儿给你算。",
        },
        {
            "role": "primary_customer",
            "speaker_label": "主客户",
            "begin": 9_000,
            "end": 12_000,
            "text": "可以。",
        },
    ]

    assert pipeline._is_sparse_effective_consultation(sparse_medical_segments) is True
    assert pipeline._allows_main_fact_floor(sparse_medical_segments) is True


def test_sanitize_drops_face_wrinkle_indication_from_prior_botulinum_history() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 1_413_590,
                    "end": 1_420_790,
                    "text": "拉出来了之后，拍照上镜一些。然后前段时间我这个脸特别宽，就有点像那种国字脸。然后打了瘦脸针之后，现在好很多了。",
                }
            ]
        }
    }
    result = {
        "standardized_indications": {
            "summary": "识别出1项适应症：面部除皱（面部）",
            "items": [
                {
                    "department_code": "Y1",
                    "department_name": "外科",
                    "indication_code": "SYZ1017",
                    "indication_name": "面部除皱",
                    "body_part_code": "BW1005",
                    "body_part_name": "面部",
                    "evidence": "[23:33] 拉出来了之后，拍照上镜一些。然后前段时间我这个脸特别宽，就有点像那种国字脸。然后打了瘦脸针之后，现在好很多了。",
                }
            ],
        },
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed is True
    assert result["standardized_indications"]["items"] == []


def test_analyze_transcript_does_not_override_existing_first_item(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample.json"
    transcript_path.write_text(
        json.dumps(
            {
                "payload": {
                    "transcribeResult": [
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 0,
                            "end": 4000,
                            "text": "今天主要是想了解哪方面呢？",
                        },
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 4100,
                            "end": 9000,
                            "text": "我想改善鼻型。",
                        },
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {
                "summary": "想改善鼻型",
                "items": [
                    {
                        "priority": 1,
                        "demand": "改善鼻型",
                        "body_part": "鼻部",
                        "evidence": "[00:04] 我想改善鼻型。",
                    }
                ],
            },
            "standardized_indications": {
                "summary": "识别出1项适应症：鼻综合（鼻部）",
                "items": [
                    {
                        "department_code": "Y1",
                        "department_name": "外科",
                        "indication_code": "SYZ1006",
                        "indication_name": "鼻综合",
                        "body_part_code": "BW1002",
                        "body_part_name": "鼻部",
                        "evidence": "[00:04] 我想改善鼻型。",
                    }
                ],
            },
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    assert [item.demand for item in result.customer_primary_demands.items] == ["改善鼻型"]
    assert [item.indication_code for item in result.standardized_indications.items] == ["SYZ1006"]


def test_analyze_transcript_fallback_keeps_single_staff_supported_indication(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_multi_body.json"
    transcript_path.write_text(
        json.dumps(
            {
                "payload": {
                    "transcribeResult": [
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 0,
                            "end": 3000,
                            "text": "今天主要是想了解哪方面呢？",
                        },
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 3100,
                            "end": 12000,
                            "text": "你现在面中和眼尾都有点松弛下垂，整体有往下走的状态。",
                        },
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {"summary": "", "items": []},
            "standardized_indications": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    items = result.standardized_indications.items
    assert len(items) == 1
    assert items[0].indication_name == "松弛下垂"
    assert items[0].body_part_name == "面部"


def test_analyze_transcript_backfills_nose_consultation_indications_from_primary_demand(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_nose_consultation.json"
    transcript_path.write_text(
        json.dumps(
            {
                "stageKey": "demo-nose",
                "utterances": [
                    {
                        "speaker": "consultant",
                        "speaker_role": "consultant",
                        "speaker_display_label": "咨询师",
                        "begin_ms": 0,
                        "end_ms": 4000,
                        "text": "今天主要想咨询哪方面？",
                    },
                    {
                        "speaker": "customer",
                        "speaker_role": "customer",
                        "speaker_display_label": "主客户",
                        "begin_ms": 4100,
                        "end_ms": 9000,
                        "text": "主要想咨询鼻部塑形方案。",
                    },
                    {
                        "speaker": "doctor",
                        "speaker_role": "doctor",
                        "speaker_display_label": "医生",
                        "begin_ms": 9100,
                        "end_ms": 17000,
                        "text": "你这种鼻头和山根基础，更适合做鼻综合，膨体会更稳一些。",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {
                "summary": "咨询鼻部塑形方案",
                "items": [
                    {
                        "priority": 1,
                        "demand": "咨询鼻部塑形方案",
                        "body_part": "鼻部",
                        "evidence": "[00:04] 主要想咨询鼻部塑形方案。",
                    }
                ],
            },
            "standardized_indications": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    assert len(result.standardized_indications.items) == 1
    item = result.standardized_indications.items[0]
    assert item.department_code == "Y1"
    assert item.indication_code == "SYZ1006"
    assert item.body_part_code == "BW1002"
    assert "鼻综合" in result.consultation_result.chief_complaint_and_indications.standardized_indications[0]


def test_analyze_transcript_does_not_treat_staff_probing_question_as_confirmed_indication(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_staff_probe.json"
    transcript_path.write_text(
        json.dumps(
            {
                "stageKey": "demo-probe",
                "utterances": [
                    {
                        "speaker": "consultant",
                        "speaker_role": "consultant",
                        "speaker_display_label": "咨询师",
                        "begin_ms": 0,
                        "end_ms": 5000,
                        "text": "有有没有什么想法，就是你自己是想要眼睛双眼皮变宽一点呢，还是想要改善你现在眼皮下垂的这个情况。",
                    },
                    {
                        "speaker": "customer",
                        "speaker_role": "customer",
                        "speaker_display_label": "主客户",
                        "begin_ms": 5100,
                        "end_ms": 11000,
                        "text": "我就是不懂，我本来就双眼皮，但是现在这个双眼皮效果好吗？",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {"summary": "", "items": []},
            "standardized_indications": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    demand_texts = [item.demand for item in result.customer_primary_demands.items]
    indication_codes = [item.indication_code for item in result.standardized_indications.items]

    assert "改善面部松弛下垂" not in demand_texts
    assert "SYZ3001" not in indication_codes


def test_analyze_transcript_does_not_treat_scar_constitution_as_scar_indication(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_scar_constitution.json"
    transcript_path.write_text(
        json.dumps(
            {
                "stageKey": "demo-scar-constitution",
                "utterances": [
                    {
                        "speaker": "customer",
                        "speaker_role": "customer",
                        "speaker_display_label": "主客户",
                        "begin_ms": 0,
                        "end_ms": 7000,
                        "text": "然后我是疤痕体质，之前做完之后恢复得会慢一些。",
                    },
                    {
                        "speaker": "consultant",
                        "speaker_role": "consultant",
                        "speaker_display_label": "咨询师",
                        "begin_ms": 7100,
                        "end_ms": 14000,
                        "text": "因为你本来就疤痕体质，所以如果能非手术解决，我们尽量不做手术。",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {"summary": "", "items": []},
            "standardized_indications": {
                "summary": "识别出1项适应症：疤痕（面部）",
                "items": [
                    {
                        "department_code": "Y3",
                        "department_name": "皮肤",
                        "indication_code": "SYZ3021",
                        "indication_name": "疤痕",
                        "body_part_code": "BW3001",
                        "body_part_name": "面部",
                        "evidence": "[00:00] 然后我是疤痕体质，之前做完之后恢复得会慢一些。",
                    }
                ],
            },
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    indication_codes = [item.indication_code for item in result.standardized_indications.items]
    health_tags = {(item.category, item.value) for item in result.customer_profile.tags}

    assert "SYZ3021" not in indication_codes
    assert ("健康风险/禁忌", "疤痕体质") in health_tags


def test_analyze_transcript_does_not_treat_customer_question_about_recommended_project_as_confirmed_indication(
    tmp_path, monkeypatch
) -> None:
    transcript_path = tmp_path / "sample_question_only_project.json"
    transcript_path.write_text(
        json.dumps(
            {
                "stageKey": "demo-question-only",
                "utterances": [
                    {
                        "speaker": "customer",
                        "speaker_role": "customer",
                        "speaker_display_label": "主客户",
                        "begin_ms": 0,
                        "end_ms": 4000,
                        "text": "我主要是想改善法令纹和面部纹路。",
                    },
                    {
                        "speaker": "consultant",
                        "speaker_role": "consultant",
                        "speaker_display_label": "咨询师",
                        "begin_ms": 4100,
                        "end_ms": 12000,
                        "text": "你这种情况也可以看提眉、双眼皮或者射频微针这些方案。",
                    },
                    {
                        "speaker": "customer",
                        "speaker_role": "customer",
                        "speaker_display_label": "主客户",
                        "begin_ms": 12100,
                        "end_ms": 18000,
                        "text": "那提眉好吗？双眼皮效果好吗？射频微针做一次的话怎么样？",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {
                "summary": "改善法令纹和面部纹路",
                "items": [
                    {
                        "priority": 1,
                        "demand": "改善法令纹和面部纹路",
                        "body_part": "面部",
                        "evidence": "[00:00] 我主要是想改善法令纹和面部纹路。",
                    }
                ],
            },
            "standardized_indications": {
                "summary": "识别出4项适应症：提眉（眼部）；双眼皮（眼部）；纹路（面部）；紧致淡纹（面部）",
                "items": [
                    {
                        "department_code": "Y1",
                        "department_name": "外科",
                        "indication_code": "SYZ1005",
                        "indication_name": "提眉",
                        "body_part_code": "BW1001",
                        "body_part_name": "眼部",
                        "evidence": "[00:12] 那提眉好吗？双眼皮效果好吗？射频微针做一次的话怎么样？",
                    },
                    {
                        "department_code": "Y1",
                        "department_name": "外科",
                        "indication_code": "SYZ1002",
                        "indication_name": "双眼皮",
                        "body_part_code": "BW1001",
                        "body_part_name": "眼部",
                        "evidence": "[00:12] 那提眉好吗？双眼皮效果好吗？射频微针做一次的话怎么样？",
                    },
                    {
                        "department_code": "Y3",
                        "department_name": "皮肤",
                        "indication_code": "SYZ3002",
                        "indication_name": "纹路",
                        "body_part_code": "BW3001",
                        "body_part_name": "面部",
                        "evidence": "[00:00] 我主要是想改善法令纹和面部纹路。",
                    },
                    {
                        "department_code": "Y2",
                        "department_name": "微创",
                        "indication_code": "SYZ2002",
                        "indication_name": "紧致淡纹",
                        "body_part_code": "BW2009",
                        "body_part_name": "面部",
                        "evidence": "[00:12] 那提眉好吗？双眼皮效果好吗？射频微针做一次的话怎么样？",
                    },
                ],
            },
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    indication_codes = [item.indication_code for item in result.standardized_indications.items]
    assert indication_codes == ["SYZ3002"]


def test_analyze_transcript_does_not_treat_staff_explanation_as_customer_primary_demand(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_staff_style_primary_demand.json"
    transcript_path.write_text(
        json.dumps(
            {
                "stageKey": "demo-staff-style-demand",
                "utterances": [
                    {
                        "speaker": "customer",
                        "speaker_role": "customer",
                        "speaker_display_label": "主客户",
                        "begin_ms": 0,
                        "end_ms": 9000,
                        "text": "你这个地方是松的，而且很厚，你要想改善眼睛只能把你皮肤往上提一点点。",
                    },
                    {
                        "speaker": "customer",
                        "speaker_role": "customer",
                        "speaker_display_label": "主客户",
                        "begin_ms": 9100,
                        "end_ms": 13000,
                        "text": "我主要还是想改善法令纹和面部纹路。",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {
                "summary": "改善面部松弛下垂；改善法令纹和面部纹路",
                "items": [
                    {
                        "priority": 1,
                        "demand": "改善面部松弛下垂",
                        "body_part": "面部",
                        "evidence": "[00:00] 你这个地方是松的，而且很厚，你要想改善眼睛只能把你皮肤往上提一点点。",
                    },
                    {
                        "priority": 2,
                        "demand": "改善法令纹和面部纹路",
                        "body_part": "面部",
                        "evidence": "[00:09] 我主要还是想改善法令纹和面部纹路。",
                    },
                ],
            },
            "standardized_indications": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    demand_texts = [item.demand for item in result.customer_primary_demands.items]
    assert demand_texts == ["改善法令纹和面部纹路"]


def test_analyze_transcript_ignores_mislabeled_staff_observation_segment(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_mislabeled_staff_observation.json"
    transcript_path.write_text(
        json.dumps(
            {
                "stageKey": "demo-mislabeled-staff-observation",
                "utterances": [
                    {
                        "speaker": "customer",
                        "speaker_role": "customer",
                        "speaker_business_role": "staff_peer",
                        "speaker_display_label": "员工同事",
                        "begin_ms": 0,
                        "end_ms": 8000,
                        "text": "以前的眼睛虽然是双眼皮，但是它是平的，现在眼睛有点三角眼了。",
                    },
                    {
                        "speaker": "customer",
                        "speaker_role": "customer",
                        "speaker_business_role": "staff_peer",
                        "speaker_display_label": "员工同事",
                        "begin_ms": 8100,
                        "end_ms": 12000,
                        "text": "我怕做了手术过后会发炎，也怕有后遗症。",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {
                "summary": "改善面部松弛下垂",
                "items": [
                    {
                        "priority": 1,
                        "demand": "改善面部松弛下垂",
                        "body_part": "面部",
                        "evidence": "[00:00] 以前的眼睛虽然是双眼皮，但是它是平的，现在眼睛有点三角眼了。",
                    }
                ],
            },
            "standardized_indications": {
                "summary": "识别出1项适应症：松弛下垂（面部）",
                "items": [
                    {
                        "department_code": "Y3",
                        "department_name": "皮肤",
                        "indication_code": "SYZ3001",
                        "indication_name": "松弛下垂",
                        "body_part_code": "BW3001",
                        "body_part_name": "面部",
                        "evidence": "[00:00] 以前的眼睛虽然是双眼皮，但是它是平的，现在眼睛有点三角眼了。",
                    }
                ],
            },
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {
                "summary": "担心风险、副作用或安全性",
                "items": [
                    {
                        "type": "风险类",
                        "content": "担心风险、副作用或安全性",
                        "evidence": "[00:08] 我怕做了手术过后会发炎，也怕有后遗症。",
                    }
                ],
            },
            "customer_profile": {"tags": []},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    assert result.customer_primary_demands.items == []
    assert result.standardized_indications.items == []
    assert result.customer_concerns.items


def test_analyze_transcript_does_not_treat_staff_demo_sentence_as_double_eyelid_indication(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_staff_demo_double_eyelid.json"
    transcript_path.write_text(
        json.dumps(
            {
                "stageKey": "demo-staff-double-eyelid",
                "utterances": [
                    {
                        "speaker": "customer",
                        "speaker_role": "customer",
                        "speaker_business_role": "staff_peer",
                        "speaker_display_label": "员工同事",
                        "begin_ms": 0,
                        "end_ms": 5000,
                        "text": "之前的眼睛，你说你先生的双眼皮的样子。",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {"summary": "", "items": []},
            "standardized_indications": {
                "summary": "识别出1项适应症：双眼皮（眼部）",
                "items": [
                    {
                        "department_code": "Y1",
                        "department_name": "外科",
                        "indication_code": "SYZ1002",
                        "indication_name": "双眼皮",
                        "body_part_code": "BW1001",
                        "body_part_name": "眼部",
                        "evidence": "[00:00] 之前的眼睛，你说你先生的双眼皮的样子。",
                    }
                ],
            },
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    assert result.standardized_indications.items == []


def test_analyze_transcript_does_not_treat_staff_checklist_question_as_positive_health_tag(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_health_checklist.json"
    transcript_path.write_text(
        json.dumps(
            {
                "stageKey": "demo-health-checklist",
                "utterances": [
                    {
                        "speaker": "customer",
                        "speaker_role": "customer",
                        "speaker_display_label": "主客户",
                        "begin_ms": 0,
                        "end_ms": 6000,
                        "text": "高血压，糖尿病有没有，传染病有没有，焦虑或者抑郁有没有？",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {"summary": "", "items": []},
            "standardized_indications": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {
                "tags": [
                    {"category": "健康风险/禁忌", "value": "精神类疾病", "evidence": "[00:00] 高血压，糖尿病有没有，传染病有没有，焦虑或者抑郁有没有？"},
                    {"category": "健康风险/禁忌", "value": "传染性疾病", "evidence": "[00:00] 高血压，糖尿病有没有，传染病有没有，焦虑或者抑郁有没有？"},
                    {"category": "健康风险/禁忌", "value": "高血压", "evidence": "[00:00] 高血压，糖尿病有没有，传染病有没有，焦虑或者抑郁有没有？"},
                    {"category": "健康风险/禁忌", "value": "糖尿病", "evidence": "[00:00] 高血压，糖尿病有没有，传染病有没有，焦虑或者抑郁有没有？"},
                ]
            },
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)
    assert result.customer_profile.tags == []


def test_analyze_transcript_backfills_high_confidence_profile_tags(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_profile.json"
    transcript_path.write_text(
        json.dumps(
            {
                "payload": {
                    "transcribeResult": [
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 0,
                            "end": 4000,
                            "text": "今天主要是想了解哪方面呢？",
                        },
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 4100,
                            "end": 11000,
                            "text": "以前没有做过医美项目，就是第一次。",
                        },
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 11100,
                            "end": 18000,
                            "text": "稍后我给你二维码，你加我微信就行，有问题随时联系我。",
                        },
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {"summary": "", "items": []},
            "standardized_indications": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    tag_pairs = {(item.category, item.value) for item in result.customer_profile.tags}
    assert ("治疗项目", "无医美史") in tag_pairs
    assert ("倾向回访方式", "微信") not in tag_pairs
    assert ("历史用的设备/原材料名称", "无") in tag_pairs
    assert ("负面项目/设备/原材料", "无") in tag_pairs
    assert result.consultation_result.customer_profile_summary.extracted_tag_count >= 3


def test_analyze_transcript_resets_concrete_history_device_when_no_prior_treatment_present(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_no_prior_conflicting_device.json"
    transcript_path.write_text(
        json.dumps(
            {
                "payload": {
                    "transcribeResult": [
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 0,
                            "end": 5000,
                            "text": "我以前没做过医美，就是第一次过来了解。",
                        }
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {"summary": "", "items": []},
            "standardized_indications": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {
                "tags": [
                    {"category": "治疗项目", "value": "无医美史", "evidence": "[00:00] 我以前没做过医美，就是第一次过来了解。"},
                    {"category": "历史用的设备/原材料名称", "value": "光子", "evidence": "[00:00] 我以前没做过医美，就是第一次过来了解。"},
                    {"category": "负面项目/设备/原材料", "value": "光电", "evidence": "[00:00] 我以前没做过医美，就是第一次过来了解。"},
                ]
            },
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    tag_pairs = {(item.category, item.value) for item in result.customer_profile.tags}
    assert ("治疗项目", "无医美史") in tag_pairs
    assert ("历史用的设备/原材料名称", "无") in tag_pairs
    assert ("负面项目/设备/原材料", "无") in tag_pairs
    assert ("历史用的设备/原材料名称", "光子") not in tag_pairs
    assert ("负面项目/设备/原材料", "光电") not in tag_pairs


def test_analyze_transcript_always_normalizes_legacy_result_shape(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_legacy_shape.json"
    transcript_path.write_text(
        json.dumps(
            {
                "payload": {
                    "transcribeResult": [
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 0,
                            "end": 3000,
                            "text": "今天主要是想了解哪方面呢？",
                        },
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 3100,
                            "end": 10000,
                            "text": "我第一次做医美，主要想改善法令纹。",
                        },
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {},
            "standardized_indications": {"summary": "", "items": []},
            "consumption_intent": {
                "budget": None,
                "willingness": None,
                "decision_factors": None,
                "evidence": None,
            },
            "staff_recommendations": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": "未出现可构成需求链路的内容。"},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    assert result.customer_primary_demands.summary == "解决法令纹问题"
    assert result.customer_demands.expectation.entry_state == "未出现可构成需求链路的内容。"
    assert result.consumption_intent.willingness == "未明确"


def test_analyze_transcript_backfills_more_profile_tags_from_clear_cues(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_profile_cues.json"
    transcript_path.write_text(
        json.dumps(
            {
                "payload": {
                    "transcribeResult": [
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 0,
                            "end": 2500,
                            "text": "今天主要想了解哪方面？",
                        },
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 2600,
                            "end": 7200,
                            "text": "我32岁，成都本地，第一次做医美，主要想改善眼袋。",
                        },
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 7300,
                            "end": 11000,
                            "text": "如果太贵我就回去跟老公商量一下，不过你先加我微信。",
                        },
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 11100,
                            "end": 15000,
                            "text": "可以，你加我微信，回去和老公商量好了再联系我。",
                        },
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {"summary": "", "items": []},
            "standardized_indications": {"summary": "", "items": []},
            "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
            "staff_recommendations": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    tag_pairs = {(item.category, item.value) for item in result.customer_profile.tags}
    assert result.customer_profile.age == "32岁"
    assert ("常驻城市", "本地") in tag_pairs
    assert ("治疗项目", "无医美史") in tag_pairs
    assert ("倾向回访方式", "微信") in tag_pairs
    assert ("决策主体", "伴侣") in tag_pairs
    assert ("价格敏感度", "高") in tag_pairs


def test_analyze_transcript_backfills_concerns_plan_and_outcome_from_clear_cues(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_business_cues.json"
    transcript_path.write_text(
        json.dumps(
            {
                "payload": {
                    "transcribeResult": [
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 0,
                            "end": 2200,
                            "text": "今天主要想了解哪方面？",
                        },
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 2300,
                            "end": 5600,
                            "text": "我想做嘴唇塑形，看起来更立体一点。",
                        },
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 5700,
                            "end": 9500,
                            "text": "我建议你可以考虑玻尿酸填充塑形，第一次从云润开始会更合适。",
                        },
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 9600,
                            "end": 13200,
                            "text": "有点贵，而且我怕恢复期影响上班，我回去再和男朋友商量一下。",
                        },
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {
                "summary": "改善唇形，做小圆唇",
                "items": [
                    {
                        "priority": 1,
                        "demand": "改善唇形，做小圆唇",
                        "body_part": "面部",
                        "evidence": "[00:02] 我想做嘴唇塑形，看起来更立体一点。",
                    }
                ],
            },
            "standardized_indications": {"summary": "", "items": []},
            "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
            "staff_recommendations": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
            "consultation_result": {
                "deal_outcome": {"status": "未明确", "summary": "", "deal_items": [], "amount": None, "loss_reasons": []}
            },
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    assert result.staff_recommendations.items
    assert any(item.recommendation == "玻尿酸填充塑形" for item in result.staff_recommendations.items)
    assert result.customer_concerns.items
    assert any(item.type == "价格类" for item in result.customer_concerns.items)
    assert "价格" not in result.consumption_intent.decision_factors
    assert "恢复期" not in result.consumption_intent.decision_factors
    assert result.consultation_result.deal_outcome.status == "未成交"
    assert "价格因素" in result.consultation_result.deal_outcome.loss_reasons


def test_analyze_transcript_backfills_recommendation_from_purchase_guidance(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_purchase_guidance.json"
    transcript_path.write_text(
        json.dumps(
            {
                "payload": {
                    "transcribeResult": [
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 0,
                            "end": 2000,
                            "text": "主要是想先体验下那个光泽。",
                        },
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 2100,
                            "end": 6200,
                            "text": "你脸上暗黄一点，光子嫩肤主要就是嫩肤提亮一下肤色。",
                        },
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 6300,
                            "end": 9200,
                            "text": "主要是想打光子，那你就买那个嘛，直接买就得了。",
                        },
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {
                "summary": "想让皮肤更有光泽、提亮肤色",
                "items": [
                    {
                        "priority": 1,
                        "demand": "想让皮肤更有光泽、提亮肤色",
                        "body_part": "面部",
                        "evidence": "[00:00] 主要是想先体验下那个光泽。",
                    }
                ],
            },
            "standardized_indications": {
                "summary": "识别出1项适应症：暗黄（面部）",
                "items": [
                    {
                        "department_name": "皮肤",
                        "indication_name": "暗黄",
                        "body_part_name": "面部",
                        "evidence": "[00:02] 光子嫩肤主要就是嫩肤提亮一下肤色。",
                    }
                ],
            },
            "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
            "staff_recommendations": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
            "consultation_result": {
                "deal_outcome": {"status": "未明确", "summary": "", "deal_items": [], "amount": None, "loss_reasons": []}
            },
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    assert result.staff_recommendations.items
    assert any(item.recommendation == "光子嫩肤" for item in result.staff_recommendations.items)


def test_sanitize_staff_recommendations_merges_ultrasound_and_rejects_negated_filler() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 0,
                    "end": 4000,
                    "text": "我的脸很垮，我就想怎么改善一下，让自己年轻一点。",
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 688000,
                    "end": 695000,
                    "text": "就是二代的嘛，你搞个性价比高的啊，价格说再高我给不了。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 705000,
                    "end": 708000,
                    "text": "所以超声炮的话，那个4999可以做。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "咨询师",
                    "begin": 887000,
                    "end": 890000,
                    "text": "我没说玻尿酸不能打哈，我从来没有说玻尿酸不能打。",
                },
            ]
        }
    }
    result = {
        "customer_primary_demands": {
            "items": [
                {
                    "priority": 1,
                    "demand": "脸部下垮，希望改善松弛感、看起来更年轻",
                    "body_part": "面部",
                    "evidence": "[00:00] 我的脸很垮，我就想怎么改善一下，让自己年轻一点。",
                }
            ]
        },
        "standardized_indications": {
            "items": [
                {
                    "department_name": "皮肤",
                    "indication_name": "松弛下垂",
                    "body_part_name": "面部",
                    "evidence": "[00:00] 我的脸很垮，我就想怎么改善一下，让自己年轻一点。",
                }
            ]
        },
        "staff_recommendations": {
            "summary": "二代超声类抗衰；热玛吉/超声抗衰；玻尿酸填充塑形",
            "items": [
                {
                    "recommendation": "二代超声类抗衰",
                    "product_or_solution": "超声炮",
                    "body_part": "面部",
                    "evidence": "[11:45] 超声炮的话，那个4999可以做。",
                    "customer_response": "未明确回应",
                    "demand_priority": [1],
                },
                {
                    "recommendation": "热玛吉/超声抗衰",
                    "product_or_solution": "热玛吉/超声抗衰",
                    "body_part": "面部",
                    "evidence": "[11:45] 所以超声炮的话，那个4999可以做。",
                    "customer_response": "未明确回应",
                    "demand_priority": [1],
                },
                {
                    "recommendation": "玻尿酸填充塑形",
                    "product_or_solution": "玻尿酸填充塑形",
                    "body_part": "面部",
                    "evidence": "[14:47] 我没说玻尿酸不能打哈，我从来没有说玻尿酸不能打。",
                    "customer_response": "未明确回应",
                    "demand_priority": [1],
                },
            ],
        },
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed
    items = result["staff_recommendations"]["items"]
    assert len(items) == 1
    assert items[0]["recommendation"] == "4999二代超声炮全脸提升紧致"
    assert "11:28" in items[0]["evidence"]
    assert "11:45" in items[0]["evidence"]
    assert "玻尿酸" not in result["staff_recommendations"]["summary"]


def test_sanitize_staff_recommendations_prioritizes_positive_plan_over_negated_material() -> None:
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "begin": 122800,
                    "end": 126000,
                    "text": "这有没有什么办法可以改善呢。",
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "李宇晴",
                    "begin": 175200,
                    "end": 183500,
                    "text": "嗯，再过个半年，估计这个泪沟又能打点胶原了。可别再在那个打玻尿酸了。玻尿酸不能打那个再好的玻尿酸，我们都不用来打那个。",
                },
            ]
        }
    }
    result = {
        "customer_primary_demands": {
            "items": [
                {
                    "priority": 1,
                    "demand": "改善泪沟凹陷",
                    "body_part": "眼部",
                    "evidence": "[02:02] 这有没有什么办法可以改善呢。",
                }
            ]
        },
        "standardized_indications": {
            "items": [
                {
                    "department_name": "皮肤",
                    "indication_name": "泪沟",
                    "body_part_name": "眼部",
                    "evidence": "[02:55] 嗯，再过个半年，估计这个泪沟又能打点胶原了。可别再在那个打玻尿酸了。",
                }
            ]
        },
        "staff_recommendations": {
            "summary": "暂不再打玻尿酸处理泪沟",
            "items": [
                {
                    "recommendation": "暂不再打玻尿酸处理泪沟",
                    "product_or_solution": "避免玻尿酸泪沟填充",
                    "body_part": "眼部",
                    "evidence": "[02:55] 嗯，再过个半年，估计这个泪沟又能打点胶原了。可别再在那个打玻尿酸了。玻尿酸不能打那个再好的玻尿酸，我们都不用来打那个。",
                    "customer_response": "未明确回应",
                    "demand_priority": [1],
                },
            ],
        },
        "consultation_result": {
            "recommended_plan": {
                "summary": "暂不再打玻尿酸处理泪沟",
                "items": [
                    {
                        "plan": "暂不再打玻尿酸处理泪沟",
                        "acceptance": "未明确回应",
                        "evidence": "[02:55] 嗯，再过个半年，估计这个泪沟又能打点胶原了。",
                    }
                ],
            }
        },
    }

    changed = pipeline.sanitize_analysis_result_with_raw(result, raw=raw)

    assert changed
    item = result["staff_recommendations"]["items"][0]
    assert item["recommendation"] == "半年后可考虑胶原/胶原蛋白改善泪沟"
    assert item["product_or_solution"] == "半年后可考虑胶原/胶原蛋白改善泪沟"
    assert result["staff_recommendations"]["summary"] == "半年后可考虑胶原/胶原蛋白改善泪沟"
    recommended_plan = result["consultation_result"]["recommended_plan"]
    assert recommended_plan["summary"] == "半年后可考虑胶原/胶原蛋白改善泪沟"
    assert recommended_plan["items"][0]["plan"] == "半年后可考虑胶原/胶原蛋白改善泪沟"


def test_analyze_transcript_downgrades_false_positive_deal_status_from_payment_flow_and_pending(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_false_positive_deal_status.json"
    transcript_path.write_text(
        json.dumps(
            {
                "payload": {
                    "transcribeResult": [
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 0,
                            "end": 4200,
                            "text": "如果今天做的话可以微信或支付宝扫，也可以刷卡，这边还送光子嫩肤。",
                        },
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 4300,
                            "end": 7600,
                            "text": "我先回去和家里商量一下，后面再决定。",
                        },
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {"summary": "", "items": []},
            "standardized_indications": {"summary": "", "items": []},
            "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
            "staff_recommendations": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
            "consultation_result": {
                "deal_outcome": {"status": "已成交", "summary": "", "deal_items": [], "amount": None, "loss_reasons": []}
            },
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    assert result.consultation_result.deal_outcome.status == "未成交"
    assert "仍需考虑或商量" in result.consultation_result.deal_outcome.loss_reasons


def test_analyze_transcript_downgrades_false_positive_deal_status_from_flow_explanation_only(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_payment_flow_only.json"
    transcript_path.write_text(
        json.dumps(
            {
                "payload": {
                    "transcribeResult": [
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 0,
                            "end": 4200,
                            "text": "如果今天做的话可以微信或支付宝扫，也可以刷卡，敷了麻药就能开始。",
                        },
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 4300,
                            "end": 6400,
                            "text": "我先听听方案。",
                        },
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {"summary": "", "items": []},
            "standardized_indications": {"summary": "", "items": []},
            "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
            "staff_recommendations": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
            "consultation_result": {
                "deal_outcome": {"status": "已成交", "summary": "", "deal_items": [], "amount": None, "loss_reasons": ["价格因素"]}
            },
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    assert result.consultation_result.deal_outcome.status == "未明确"
    assert result.consultation_result.deal_outcome.loss_reasons == []


def test_analyze_transcript_does_not_infer_profile_tags_from_staff_generic_intro(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_staff_generic_intro.json"
    transcript_path.write_text(
        json.dumps(
            {
                "payload": {
                    "transcribeResult": [
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 0,
                            "end": 3200,
                            "text": "第一次做医美的客户一般会先从基础水光开始，后面你加我微信我把方案发给你。",
                        },
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 3300,
                            "end": 6200,
                            "text": "我主要就是想了解一下鼻子怎么做会更自然。",
                        },
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {
                "summary": "咨询鼻部塑形方案",
                "items": [
                    {
                        "priority": 1,
                        "demand": "咨询鼻部塑形方案",
                        "body_part": "鼻部",
                        "evidence": "[00:03] 我主要就是想了解一下鼻子怎么做会更自然。",
                    }
                ],
            },
            "standardized_indications": {"summary": "", "items": []},
            "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
            "staff_recommendations": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    tag_pairs = {(item.category, item.value) for item in result.customer_profile.tags}
    assert ("治疗项目", "无医美史") not in tag_pairs
    assert ("倾向回访方式", "微信") not in tag_pairs


def test_analyze_transcript_accepts_staff_recap_after_customer_confirmation(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_staff_recap_confirmed.json"
    transcript_path.write_text(
        json.dumps(
            {
                "payload": {
                    "transcribeResult": [
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 0,
                            "end": 3200,
                            "text": "您之前没做过医美项目，对吧？",
                        },
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 3300,
                            "end": 5200,
                            "text": "对，是第一次。",
                        },
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 5300,
                            "end": 8200,
                            "text": "后面我加您微信，方便回访跟进，可以吧？",
                        },
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 8300,
                            "end": 9600,
                            "text": "可以。",
                        },
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 9700,
                            "end": 13600,
                            "text": "那您预算2万左右，主要担心恢复期，对吧？",
                        },
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 13700,
                            "end": 15000,
                            "text": "对。",
                        },
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {"summary": "", "items": []},
            "standardized_indications": {"summary": "", "items": []},
            "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
            "staff_recommendations": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    tag_pairs = {(item.category, item.value) for item in result.customer_profile.tags}
    assert ("治疗项目", "无医美史") in tag_pairs
    assert ("倾向回访方式", "微信") in tag_pairs
    assert result.consumption_intent.budget == "2万"
    assert "恢复期" not in result.consumption_intent.decision_factors
    assert any(item.type == "恢复类" for item in result.customer_concerns.items)


def test_analyze_transcript_ignores_third_party_no_prior_treatment_story(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_third_party_no_prior_treatment.json"
    transcript_path.write_text(
        json.dumps(
            {
                "payload": {
                    "transcribeResult": [
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 0,
                            "end": 12000,
                            "text": "我有个老乡从来没做过医美，她当时很害怕，后来做了之后今天还说想来微调一下。",
                        }
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {"summary": "", "items": []},
            "standardized_indications": {"summary": "", "items": []},
            "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
            "staff_recommendations": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    tag_pairs = {(item.category, item.value) for item in result.customer_profile.tags}
    assert ("治疗项目", "无医美史") not in tag_pairs


def test_analyze_transcript_removes_staff_led_profile_tags_without_customer_fact_basis(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_staff_led_tags.json"
    transcript_path.write_text(
        json.dumps(
            {
                "payload": {
                    "transcribeResult": [
                        {
                            "role": "badge_owner",
                            "speaker_label": "咨询师",
                            "begin": 0,
                            "end": 5000,
                            "text": "你更适合提眉，像你这种情况如果都没有做过我会先建议射频微针。",
                        },
                        {
                            "role": "primary_customer",
                            "speaker_label": "主客户",
                            "begin": 5100,
                            "end": 8200,
                            "text": "我先听听你的建议。",
                        },
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {"summary": "", "items": []},
            "standardized_indications": {"summary": "", "items": []},
            "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
            "staff_recommendations": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {
                "tags": [
                    {"category": "治疗项目", "value": "外科整形", "evidence": "[00:00] 你更适合提眉，像你这种情况如果都没有做过我会先建议射频微针。"},
                    {"category": "治疗项目", "value": "无医美史", "evidence": "[00:00] 你更适合提眉，像你这种情况如果都没有做过我会先建议射频微针。"},
                    {"category": "常驻城市", "value": "本地", "evidence": "[00:00] 你更适合提眉，像你这种情况如果都没有做过我会先建议射频微针。"},
                ]
            },
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    tag_pairs = {(item.category, item.value) for item in result.customer_profile.tags}
    assert ("治疗项目", "外科整形") not in tag_pairs
    assert ("治疗项目", "无医美史") not in tag_pairs
    assert ("常驻城市", "本地") not in tag_pairs


def test_analyze_transcript_accepts_mislabeled_customer_self_report_after_staff_question(tmp_path, monkeypatch) -> None:
    transcript_path = tmp_path / "sample_mislabeled_customer_self_report.json"
    transcript_path.write_text(
        json.dumps(
            {
                "utterances": [
                    {
                        "speaker": "customer",
                        "speaker_role": "customer",
                        "speaker_business_role": "staff_peer",
                        "speaker_display_label": "员工同事",
                        "begin_ms": 0,
                        "end_ms": 3000,
                        "text": "你现在的话主要有什么诉求？",
                    },
                    {
                        "speaker": "consultant",
                        "speaker_role": "consultant",
                        "speaker_business_role": "badge_owner",
                        "speaker_display_label": "赵婷玲（工牌本人）",
                        "begin_ms": 3100,
                        "end_ms": 6800,
                        "text": "我想改善眼皮下垂，之前做过一次双眼皮也一直不太满意。",
                    },
                    {
                        "speaker": "customer",
                        "speaker_role": "customer",
                        "speaker_business_role": "staff_peer",
                        "speaker_display_label": "员工同事",
                        "begin_ms": 6900,
                        "end_ms": 9800,
                        "text": "那你做出来的时候满意吗？",
                    },
                    {
                        "speaker": "consultant",
                        "speaker_role": "consultant",
                        "speaker_business_role": "badge_owner",
                        "speaker_display_label": "赵婷玲（工牌本人）",
                        "begin_ms": 9900,
                        "end_ms": 13200,
                        "text": "那时候真没有，我也后悔了很久。",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pipeline,
        "_analyze_single",
        lambda *args, **kwargs: {
            "customer_primary_demands": {"summary": "", "items": []},
            "standardized_indications": {"summary": "", "items": []},
            "consumption_intent": {"budget": None, "willingness": "未明确", "decision_factors": [], "evidence": []},
            "staff_recommendations": {"summary": "", "items": []},
            "customer_demands": {"focus_areas": [], "expectation": {"turning_points": []}},
            "customer_concerns": {"summary": "", "items": []},
            "customer_profile": {"tags": []},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_evaluation",
        lambda *args, **kwargs: {"overall_summary": "", "dimensions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "rebuild_consultation_process_evaluation",
        lambda *args, **kwargs: {
            "total_score": 0,
            "max_total_score": 9,
            "overall_score": 0,
            "overall_summary": "",
            "sections": [],
        },
    )

    result = pipeline.analyze_transcript(transcript_path)

    demand_texts = [item.demand for item in result.customer_primary_demands.items]
    assert any("下垂" in item for item in demand_texts)
    tag_pairs = {(item.category, item.value) for item in result.customer_profile.tags}
    assert ("治疗项目", "手术类") in tag_pairs
