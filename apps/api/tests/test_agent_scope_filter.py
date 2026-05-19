from smart_badge_api.analysis.agent_pipeline import (
    _dialogue_with_scope_filter,
    _scope_segment_should_ignore,
)


def _dialogue(line_count: int = 12) -> str:
    return "\n".join(f"L{index:04d} 00:00-00:01 speaker_{index % 2}: 第{index}句" for index in range(1, line_count + 1))


def test_scope_filter_removes_clear_staff_chat_range():
    scoped, debug = _dialogue_with_scope_filter(
        _dialogue(),
        {
            "segments": [
                {
                    "start_line_id": "L0001",
                    "end_line_id": "L0003",
                    "scope_type": "staff_chat",
                    "business_relevance": "ignore",
                    "current_visit_relevant": False,
                }
            ]
        },
    )

    assert "L0001" not in scoped
    assert "L0003" not in scoped
    assert "L0004" in scoped
    assert debug["removed_line_count"] == 3


def test_scope_filter_keeps_quote_or_payment_even_if_relevance_is_wrong():
    source = _dialogue()
    scoped, debug = _dialogue_with_scope_filter(
        source,
        {
            "segments": [
                {
                    "start_line_id": "L0002",
                    "end_line_id": "L0004",
                    "scope_type": "quote_or_payment",
                    "business_relevance": "ignore",
                    "current_visit_relevant": False,
                }
            ]
        },
    )

    assert scoped == source
    assert debug["removed_line_count"] == 0


def test_scope_filter_keeps_current_visit_boundary_types():
    for scope_type in [
        "current_customer_consultation",
        "accompanying_customer_consultation",
        "doctor_face_to_face",
        "post_deal_care",
        "future_seed_or_cross_department",
        "unclear",
        "unknown",
    ]:
        assert not _scope_segment_should_ignore(
            {
                "scope_type": scope_type,
                "business_relevance": "ignore",
                "current_visit_relevant": False,
            }
        )


def test_scope_filter_keeps_current_visit_relevant_even_for_staff_chat_label():
    assert not _scope_segment_should_ignore(
        {
            "scope_type": "staff_chat",
            "business_relevance": "ignore",
            "current_visit_relevant": True,
        }
    )
