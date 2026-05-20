from smart_badge_api.analysis.staged_pipeline import _dedupe_demands, _dedupe_primary_demands_payload


def test_semantic_dedupe_merges_repeated_contour_primary_demands() -> None:
    items = [
        {"demand": "改善面部上半部轮廓外扩问题", "body_part": "面部上半部轮廓"},
        {"demand": "自述面部轮廓上半部有外扩问题，希望改善轮廓", "body_part": "面部上半部轮廓"},
        {"demand": "改善眶外C线/额颞交界（眼窝外侧）区域轮廓", "body_part": "眶外C线/额颞交界"},
        {"demand": "关注眶外C线/额颞交界区域（眼窝外侧）改善", "body_part": "眶外C线（眼窝外侧）"},
        {"demand": "改善侧面外轮廓线条，使侧面不明显外扩", "body_part": "眶外C线/外轮廓线"},
        {"demand": "自述侧面该区域明显，希望改善侧面轮廓", "body_part": "眶外C线/外轮廓线"},
        {"demand": "改善颊部轻度凹陷", "body_part": "颊部"},
        {"demand": "自述颊部有轻度凹陷，希望改善", "body_part": "颊部"},
        {"demand": "因上镜需求，希望改善面部凹陷与整体轮廓表现", "body_part": "面部整体轮廓"},
        {"demand": "因上镜需求，希望改善面部凹陷与轮廓表现", "body_part": "面部整体轮廓"},
        {"demand": "近期变瘦后感觉面部凹陷明显，希望填充改善", "body_part": "面部凹陷区域（未明确具体点位）"},
    ]

    deduped = _dedupe_demands(items)

    assert [item["demand"] for item in deduped] == [
        "改善面部上半部轮廓外扩问题",
        "改善眶外C线/额颞交界（眼窝外侧）区域轮廓",
        "改善侧面外轮廓线条，使侧面不明显外扩",
        "改善颊部轻度凹陷",
        "因上镜需求，希望改善面部凹陷与整体轮廓表现",
    ]


def test_primary_demands_payload_is_reindexed_after_semantic_dedupe() -> None:
    payload = {
        "summary": "改善颊部轻度凹陷；自述颊部有轻度凹陷，希望改善",
        "items": [
            {"priority": 1, "demand": "改善颊部轻度凹陷", "body_part": "颊部"},
            {"priority": 2, "demand": "自述颊部有轻度凹陷，希望改善", "body_part": "颊部"},
        ],
    }

    deduped = _dedupe_primary_demands_payload(payload)

    assert deduped["summary"] == "改善颊部轻度凹陷"
    assert deduped["items"] == [
        {"priority": 1, "demand": "改善颊部轻度凹陷", "body_part": "颊部", "evidence": None}
    ]
