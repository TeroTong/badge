from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.asr.domain_terms import _TEXT_REPLACEMENTS
from smart_badge_api.db.models import Hotword, HotwordGroup

AUTO_HOTWORD_GROUP_NAME = "ASR自动挖词候选"
LEGACY_AUTO_HOTWORD_GROUP_NAME = "ASR自动挖词"
AUTO_HOTWORD_GROUP_TYPE = "industry"
AUTO_HOTWORD_SOURCE_LABEL = "ASR自动挖词候选（待审核）"
LEGACY_AUTO_HOTWORD_SOURCE_LABEL = "ASR自动挖词"
AUTO_HOTWORD_LIBRARY_SCOPE = "public"
AUTO_HOTWORD_GROUP_ACTIVE = False
AUTO_HOTWORD_WORD_ACTIVE = False

_AUTO_HOTWORD_IGNORE = {
    "设计师",
    "医生",
    "院长",
    "面诊",
    "咨询",
    "方案",
    "预算",
    "价格",
    "顾客",
    "客户",
    "求美者",
    "大概讲",
}


@dataclass(slots=True)
class AutoHotwordCandidate:
    term: str
    weight: int
    evidence_score: int
    direct_correction_count: int = 0
    fuzzy_candidate_count: int = 0
    known_term_count: int = 0


def _normalize_term(value: object) -> str:
    return str(value or "").strip()


_NON_CANONICAL_TERMS = {
    source.strip()
    for source, target in _TEXT_REPLACEMENTS
    if source.strip() and source.strip() != target.strip()
} | {"轻度体积"}


def _is_valid_auto_hotword(term: str) -> bool:
    cleaned = _normalize_term(term)
    if not cleaned:
        return False
    if cleaned in _AUTO_HOTWORD_IGNORE:
        return False
    if cleaned in _NON_CANONICAL_TERMS:
        return False
    if len(cleaned) > 30:
        return False
    if cleaned.isdigit():
        return False
    return True


def _score_to_weight(score: int) -> int:
    if score >= 28:
        return 100
    if score >= 20:
        return 80
    if score >= 14:
        return 60
    if score >= 8:
        return 40
    return 20


def build_auto_hotword_candidates(
    report: dict[str, Any],
    *,
    min_fuzzy_count: int = 2,
) -> list[AutoHotwordCandidate]:
    known_counts: dict[str, int] = {}
    for item in report.get("known_term_hits") or []:
        term = _normalize_term(item.get("term"))
        if not term:
            continue
        known_counts[term] = int(item.get("count") or 0)

    direct_counts: Counter[str] = Counter()
    for item in report.get("direct_corrections") or []:
        term = _normalize_term(item.get("suggested"))
        if not term:
            continue
        direct_counts[term] += int(item.get("count") or 0)

    fuzzy_counts: Counter[str] = Counter()
    for item in report.get("fuzzy_candidates") or []:
        term = _normalize_term(item.get("suggested"))
        if not term:
            continue
        count = int(item.get("count") or 0)
        if count < min_fuzzy_count:
            continue
        fuzzy_counts[term] += count

    candidates: list[AutoHotwordCandidate] = []
    for term in sorted(set(direct_counts) | set(fuzzy_counts)):
        if not _is_valid_auto_hotword(term):
            continue
        direct_count = direct_counts[term]
        fuzzy_count = fuzzy_counts[term]
        known_count = known_counts.get(term, 0)
        if direct_count <= 0 and fuzzy_count < min_fuzzy_count:
            continue

        evidence_score = direct_count * 6 + fuzzy_count * 2 + min(known_count, 10)
        candidates.append(
            AutoHotwordCandidate(
                term=term,
                weight=_score_to_weight(evidence_score),
                evidence_score=evidence_score,
                direct_correction_count=direct_count,
                fuzzy_candidate_count=fuzzy_count,
                known_term_count=known_count,
            )
        )

    return sorted(
        candidates,
        key=lambda item: (-item.evidence_score, -item.weight, item.term),
    )


async def upsert_auto_hotword_candidates(
    db: AsyncSession,
    candidates: list[AutoHotwordCandidate],
) -> dict[str, Any]:
    result = await db.execute(
        select(HotwordGroup)
        .where(
            or_(
                HotwordGroup.name == AUTO_HOTWORD_GROUP_NAME,
                HotwordGroup.name == LEGACY_AUTO_HOTWORD_GROUP_NAME,
                HotwordGroup.source_label == LEGACY_AUTO_HOTWORD_SOURCE_LABEL,
                HotwordGroup.source_label == AUTO_HOTWORD_SOURCE_LABEL,
            )
        )
        .options(selectinload(HotwordGroup.words))
        .order_by(HotwordGroup.updated_at.desc(), HotwordGroup.created_at.desc())
    )
    groups = list(result.scalars().all())
    group = next((item for item in groups if item.name == AUTO_HOTWORD_GROUP_NAME), None) or (groups[0] if groups else None)
    created_group = False
    if group is None:
        group = HotwordGroup(
            name=AUTO_HOTWORD_GROUP_NAME,
            group_type=AUTO_HOTWORD_GROUP_TYPE,
            library_scope=AUTO_HOTWORD_LIBRARY_SCOPE,
            source_label=AUTO_HOTWORD_SOURCE_LABEL,
            is_active=AUTO_HOTWORD_GROUP_ACTIVE,
        )
        db.add(group)
        await db.flush()
        created_group = True
    else:
        group.name = AUTO_HOTWORD_GROUP_NAME
        group.group_type = AUTO_HOTWORD_GROUP_TYPE
        group.library_scope = AUTO_HOTWORD_LIBRARY_SCOPE
        group.source_label = AUTO_HOTWORD_SOURCE_LABEL
        group.is_active = AUTO_HOTWORD_GROUP_ACTIVE

    merged_group_count = 0
    for other_group in groups:
        if other_group.id == group.id:
            continue
        other_group.is_active = False
        other_group.source_label = AUTO_HOTWORD_SOURCE_LABEL
        merged_group_count += 1

    existing = (
        {
            word.word.strip().casefold(): word
            for word in group.words
        }
        if not created_group
        else {}
    )

    inserted = 0
    updated = 0
    unchanged = 0
    deactivated = 0
    candidate_keys = {candidate.term.casefold() for candidate in candidates}
    for candidate in candidates:
        key = candidate.term.casefold()
        current = existing.get(key)
        if current is None:
            current = Hotword(
                group_id=group.id,
                word=candidate.term,
                weight=candidate.weight,
                is_active=AUTO_HOTWORD_WORD_ACTIVE,
            )
            db.add(current)
            existing[key] = current
            inserted += 1
            continue

        next_weight = max(int(current.weight or 0), candidate.weight)
        changed = False
        if current.word != candidate.term:
            current.word = candidate.term
            changed = True
        if next_weight != current.weight:
            current.weight = next_weight
            changed = True
        if current.is_active != AUTO_HOTWORD_WORD_ACTIVE:
            current.is_active = AUTO_HOTWORD_WORD_ACTIVE
            changed = True
        if changed:
            updated += 1
        else:
            unchanged += 1

    for key, current in existing.items():
        if key in candidate_keys or not current.is_active:
            continue
        current.is_active = False
        deactivated += 1

    await db.commit()
    await db.refresh(group, ["words"])

    return {
        "group_id": group.id,
        "group_name": group.name,
        "created_group": created_group,
        "candidate_count": len(candidates),
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "deactivated": deactivated,
        "merged_group_count": merged_group_count,
        "candidate_mode": "pending_review",
        "is_active": bool(group.is_active),
        "terms": [asdict(item) for item in candidates],
    }
