"""ASR correction hotword candidate selection.

This module keeps the Agent correction prompt data-driven without putting the
entire database hotword library into every LLM call.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


_AUTO_MINED_MARKERS = ("ASR自动挖词", "自动挖词候选")
_HIGH_VALUE_GROUP_MARKERS = (
    "材料",
    "品牌",
    "项目",
    "治疗",
    "医疗",
    "部位",
    "竞品",
    "brand",
    "material",
    "project",
    "treatment",
    "medical",
    "body",
    "competitor",
)
_BRAND_GROUP_MARKERS = ("材料", "品牌", "brand", "material")
_PROJECT_GROUP_MARKERS = ("项目", "治疗", "医疗", "project", "treatment", "medical")
_BODY_GROUP_MARKERS = ("部位", "body")

_INJECTION_CONTEXT_TERMS = (
    "注射",
    "玻尿酸",
    "胶原",
    "胶原蛋白",
    "再生",
    "材料",
    "支撑",
    "填充",
    "一支",
    "两支",
    "每边",
    "鼻基底",
    "苹果肌",
    "泪沟",
    "卧蚕",
    "下巴",
    "颞区",
    "童颜针",
    "肉毒",
)
_PROJECT_CONTEXT_TERMS = ("项目", "治疗", "方案", "做", "改善", "修复", "祛", "去", "打", "疗程")
_BODY_CONTEXT_TERMS = ("部位", "面部", "鼻", "眼", "唇", "嘴", "下巴", "颞", "额", "胸", "腰", "腿", "皮肤")

_CANONICAL_ALIAS_HINTS = {
    # These aliases are only enabled when the canonical term exists in the
    # configured hotword library. The correction still requires local context.
    "瑞德喜": (
        "瑞1",
        "瑞一",
        "瑞的一",
        "瑞的1",
        "瑞的仪",
        "瑞仪",
        "瑞义",
        "瑞德仪",
        "瑞得喜",
        "瑞地喜",
        "瑞蓝1",
        "瑞蓝1号",
        "read的1",
        "read的仪",
        "为的仪",
        "为的一",
    ),
    "濡白天使": ("鲁白天使", "鲁班天使", "鲁板天使", "濡白", "鲁白", "鲁班", "鲁板"),
    "艾拉斯提": ("艾拉斯的", "艾拉丝提", "艾拉斯体", "艾拉斯蒂"),
    "贝丽菲尔": ("贝利菲尔", "贝丽菲儿", "贝利菲儿"),
    "乔雅登": ("乔亚登", "乔雅顿"),
    "海薇": ("海微", "海威", "海派", "海妹"),
    "双美": ("双酶", "双镁", "双美玻尿酸", "双美的玻尿酸"),
}


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _compact_text(value: object) -> str:
    return re.sub(r"\s+", "", _clean_text(value))


def _group_descriptor(entry: dict[str, Any]) -> str:
    return f"{entry.get('group_name') or ''} {entry.get('group_type') or ''} {entry.get('source_label') or ''}".casefold()


def _is_auto_mined(entry: dict[str, Any]) -> bool:
    descriptor = _group_descriptor(entry)
    return any(marker.casefold() in descriptor for marker in _AUTO_MINED_MARKERS)


def _is_high_value_group(entry: dict[str, Any]) -> bool:
    descriptor = _group_descriptor(entry)
    return any(marker.casefold() in descriptor for marker in _HIGH_VALUE_GROUP_MARKERS)


def _is_brand_group(entry: dict[str, Any]) -> bool:
    descriptor = _group_descriptor(entry)
    return any(marker.casefold() in descriptor for marker in _BRAND_GROUP_MARKERS)


def _is_project_group(entry: dict[str, Any]) -> bool:
    descriptor = _group_descriptor(entry)
    return any(marker.casefold() in descriptor for marker in _PROJECT_GROUP_MARKERS)


def _is_body_group(entry: dict[str, Any]) -> bool:
    descriptor = _group_descriptor(entry)
    return any(marker.casefold() in descriptor for marker in _BODY_GROUP_MARKERS)


def _weight(entry: dict[str, Any]) -> int:
    try:
        return int(entry.get("weight") or 0)
    except (TypeError, ValueError):
        return 0


def normalize_asr_correction_hotwords(value: object) -> list[dict[str, Any]]:
    """Normalize hotword payload passed through staff_context."""

    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if isinstance(raw, str):
            entry = {"term": raw}
        elif isinstance(raw, dict):
            entry = dict(raw)
        else:
            continue
        term = _compact_text(entry.get("term") or entry.get("word") or entry.get("name"))
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        entry["term"] = term
        normalized.append(entry)
    return normalized


def _context_score(dialogue: str, entry: dict[str, Any]) -> tuple[int, str]:
    if _is_brand_group(entry) and any(term in dialogue for term in _INJECTION_CONTEXT_TERMS):
        return 22, "material_or_brand_context"
    if _is_project_group(entry) and any(term in dialogue for term in _PROJECT_CONTEXT_TERMS):
        return 16, "project_context"
    if _is_body_group(entry) and any(term in dialogue for term in _BODY_CONTEXT_TERMS):
        return 14, "body_part_context"
    return 0, ""


def _max_substring_overlap(term: str, dialogue: str) -> float:
    if len(term) < 3:
        return 0.0
    best = 0
    for size in range(min(len(term), 6), 1, -1):
        for start in range(0, len(term) - size + 1):
            if term[start : start + size] in dialogue:
                return size / len(term)
            best = max(best, 0)
    return float(best)


def _best_latin_similarity(term: str, dialogue: str) -> float:
    # Handles fragments such as "read的1" against Chinese canonical terms poorly,
    # but catches romanized / mixed hotwords when configured.
    if not re.search(r"[A-Za-z0-9]", term):
        return 0.0
    candidates = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,12}", dialogue)
    if not candidates:
        return 0.0
    compact_term = term.casefold()
    return max(SequenceMatcher(None, compact_term, item.casefold()).ratio() for item in candidates[:2000])


def _alias_candidates(dialogue: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_term = {entry["term"]: entry for entry in entries}
    candidates: list[dict[str, Any]] = []
    for canonical, aliases in _CANONICAL_ALIAS_HINTS.items():
        entry = by_term.get(canonical)
        if not entry:
            continue
        matched = [alias for alias in aliases if alias in dialogue]
        if not matched:
            continue
        candidates.append(
            {
                "term": canonical,
                "group_name": entry.get("group_name") or "",
                "group_type": entry.get("group_type") or "",
                "weight": _weight(entry),
                "reason": "known_alias_from_configured_hotword",
                "matched_fragments": matched[:5],
                "match_kind": "alias",
                "confidence_hint": "high",
            }
        )
    return candidates


def select_asr_correction_hotword_candidates(
    dialogue: str,
    hotwords: object,
    *,
    max_candidates: int = 60,
) -> list[dict[str, Any]]:
    """Select a compact set of hotword hints for ASR correction.

    The output is intended for LLM prompt context. It contains only candidates
    related to this transcript and excludes auto-mined/noisy groups.
    """

    compact_dialogue = _compact_text(dialogue)
    if not compact_dialogue:
        return []
    entries = [
        entry
        for entry in normalize_asr_correction_hotwords(hotwords)
        if not _is_auto_mined(entry) and _is_high_value_group(entry)
    ]
    if not entries:
        return []

    scored: list[tuple[int, str, dict[str, Any]]] = []
    for entry in entries:
        term = entry["term"]
        score = min(_weight(entry), 100)
        reasons: list[str] = []
        matched_fragments: list[str] = []
        textual_signal = False

        if term in compact_dialogue:
            score += 120
            reasons.append("exact_hotword_in_transcript")
            matched_fragments.append(term)
            textual_signal = True
        else:
            overlap = _max_substring_overlap(term, compact_dialogue)
            if overlap >= 0.66:
                score += int(overlap * 70)
                reasons.append("partial_character_overlap")
                textual_signal = True
            latin_similarity = _best_latin_similarity(term, compact_dialogue)
            if latin_similarity >= 0.82:
                score += int(latin_similarity * 55)
                reasons.append("mixed_latin_similarity")
                textual_signal = True

        context_points, context_reason = _context_score(compact_dialogue, entry)
        if context_points and textual_signal:
            score += context_points
            reasons.append(context_reason)

        # Keep only a small number of high-priority references available without
        # a direct textual signal. These are lower-confidence guardrails and are
        # capped after ranking to avoid polluting the correction prompt.
        if not reasons and _weight(entry) >= 80:
            context_points, context_reason = _context_score(compact_dialogue, entry)
            if context_points:
                score = min(score, 75) + context_points
                reasons.append(f"context_reference_{context_reason}")

        if not reasons:
            continue
        if score < 90:
            continue
        scored.append(
            (
                score,
                term,
                {
                    "term": term,
                    "group_name": entry.get("group_name") or "",
                    "group_type": entry.get("group_type") or "",
                    "weight": _weight(entry),
                    "reason": ",".join(dict.fromkeys(reasons)),
                    "matched_fragments": matched_fragments[:5],
                    "match_kind": "textual" if textual_signal else "context_reference",
                    "confidence_hint": "high" if score >= 150 else ("medium" if score >= 95 else "low"),
                },
            )
        )

    alias_items = _alias_candidates(compact_dialogue, entries)
    for item in alias_items:
        scored.append((220 + int(item.get("weight") or 0), item["term"], item))

    deduped: dict[str, tuple[int, dict[str, Any]]] = {}
    for score, term, item in scored:
        current = deduped.get(term)
        if current is None or score > current[0]:
            deduped[term] = (score, item)

    ordered = sorted(deduped.values(), key=lambda pair: (-pair[0], pair[1]["term"]))
    strong = [item for _score, item in ordered if item.get("match_kind") != "context_reference"]
    context_only = [item for _score, item in ordered if item.get("match_kind") == "context_reference"]
    context_limit = min(12, max(max_candidates - len(strong), 0))
    return [*strong[:max_candidates], *context_only[:context_limit]][:max_candidates]
