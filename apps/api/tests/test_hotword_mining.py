from __future__ import annotations

from smart_badge_api.asr.hotword_mining import build_auto_hotword_candidates


def test_build_auto_hotword_candidates_prefers_domain_terms_and_filters_generic_noise() -> None:
    report = {
        "known_term_hits": [
            {"term": "黄金微针", "count": 5},
            {"term": "设计师", "count": 20},
            {"term": "朗润PRO", "count": 4},
        ],
        "direct_corrections": [
            {"raw_term": "黄金位置", "suggested": "黄金微针", "count": 2},
            {"raw_term": "瑞兰", "suggested": "瑞蓝", "count": 1},
        ],
        "fuzzy_candidates": [
            {"raw_term": "学设计师", "suggested": "设计师", "count": 18},
            {"raw_term": "黄金微", "suggested": "黄金微针", "count": 5},
            {"raw_term": "朗润P", "suggested": "朗润PRO", "count": 3},
            {"raw_term": "乳白天", "suggested": "乳白天使", "count": 2},
            {"raw_term": "轻度体", "suggested": "轻度体积", "count": 2},
        ],
    }

    candidates = build_auto_hotword_candidates(report)
    terms = {item.term for item in candidates}

    assert "黄金微针" in terms
    assert "瑞蓝" in terms
    assert "朗润PRO" in terms
    assert "设计师" not in terms
    assert "乳白天使" not in terms
    assert "轻度体积" not in terms


def test_build_auto_hotword_candidates_assigns_higher_weight_to_stronger_evidence() -> None:
    report = {
        "known_term_hits": [
            {"term": "黄金微针", "count": 6},
            {"term": "瑞蓝", "count": 1},
        ],
        "direct_corrections": [
            {"raw_term": "黄金位置", "suggested": "黄金微针", "count": 2},
            {"raw_term": "瑞兰", "suggested": "瑞蓝", "count": 1},
        ],
        "fuzzy_candidates": [
            {"raw_term": "黄金微", "suggested": "黄金微针", "count": 5},
            {"raw_term": "金微针", "suggested": "黄金微针", "count": 5},
        ],
    }

    candidates = {item.term: item for item in build_auto_hotword_candidates(report)}

    assert candidates["黄金微针"].weight > candidates["瑞蓝"].weight
    assert candidates["黄金微针"].evidence_score > candidates["瑞蓝"].evidence_score
