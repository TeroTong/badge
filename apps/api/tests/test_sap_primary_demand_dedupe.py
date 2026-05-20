from smart_badge_api.sap_consultation import build_consultation_text


def test_build_consultation_text_semantically_dedupes_primary_demands() -> None:
    result = {
        "consultation_result": {
            "chief_complaint_and_indications": {
                "primary_demands": [
                    "改善面部上半部轮廓外扩问题",
                    "自述面部轮廓上半部有外扩问题，希望改善轮廓",
                    "改善眶外C线/额颞交界（眼窝外侧）区域轮廓",
                    "关注眶外C线/额颞交界区域（眼窝外侧）改善",
                    "改善颊部轻度凹陷",
                    "自述颊部有轻度凹陷，希望改善",
                ],
            }
        }
    }

    text = build_consultation_text("李宇晴", result)
    demand_block = text.split("●本次预算", 1)[0]

    assert "⑥" not in demand_block
    assert demand_block.count("眶外C线") == 1
    assert demand_block.count("颊部") == 1
