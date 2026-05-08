from smart_badge_api.analysis.pipeline import sanitize_analysis_result_with_raw
from smart_badge_api.api.analysis_normalization import normalize_analysis_result


def _tag_pairs(result: dict) -> set[tuple[str, str]]:
    return {
        (str(item.get("category") or ""), str(item.get("value") or ""))
        for item in result["customer_profile"]["tags"]
    }


def test_backfills_bellafill_prior_injection_history_tags() -> None:
    result = {
        "customer_profile": {"tags": []},
        "consultation_result": {"customer_profile_summary": {"summary": "本次录音暂未提取出明确画像标签。"}},
    }
    raw = {
        "payload": {
            "transcribeResult": [
                {
                    "role": "badge_owner",
                    "speaker_label": "李珍玉（工牌本人）",
                    "text": "鼻子，鼻子做过什么吗？",
                    "begin": 0,
                    "end": 2855,
                },
                {
                    "role": "badge_owner",
                    "speaker_label": "李珍玉（工牌本人）",
                    "text": "嗯，鼻梁倒是有高度，所以你当时是打的贝利菲尔。菲利菲尔，你感觉撑起来了吗？",
                    "begin": 14880,
                    "end": 27330,
                },
                {
                    "role": "primary_customer",
                    "speaker_label": "主客户",
                    "text": "好像没有完全的，",
                    "begin": 29860,
                    "end": 31393,
                },
            ]
        }
    }

    assert sanitize_analysis_result_with_raw(result, raw=raw)
    normalized = normalize_analysis_result(result) or result

    assert ("治疗项目", "注射类") in _tag_pairs(normalized)
    assert ("历史用的设备/原材料名称", "贝丽菲尔") in _tag_pairs(normalized)
    summary = normalized["consultation_result"]["customer_profile_summary"]
    assert summary["extracted_tag_count"] >= 2
    assert ("历史用的设备/原材料名称", "贝丽菲尔") in {
        (item["category"], item["value"]) for item in summary["tags"]
    }
