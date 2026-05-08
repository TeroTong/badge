from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.analysis.consultation_evaluation import normalize_consultation_dimension_name
from smart_badge_api.db.models import AnalysisTask, Recording, RiskRecord, RiskRule, Visit
from smart_badge_api.db.risk_defaults import ensure_risk_rule_defaults


def extract_recording_id(file_name: str) -> str | None:
    if file_name.startswith("recording_") and file_name.endswith(".json"):
        return file_name.removeprefix("recording_").removesuffix(".json")
    return None


@dataclass
class RiskHit:
    summary: str
    hit_excerpt: str
    matched_dimension_name: str | None = None
    matched_keywords: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _truncate(value: str | None, limit: int = 180) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _recording_excerpt(recording: Recording | None, fallback: str | None = None) -> str:
    if fallback and fallback.strip():
        return _truncate(fallback)

    if recording and recording.transcript:
        utterances = recording.transcript.utterances or []
        pieces = [
            str(item.get("text") or "").strip()
            for item in utterances
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        if pieces:
            return _truncate(" ".join(pieces[:3]))
        if recording.transcript.full_text:
            return _truncate(recording.transcript.full_text)

    if recording and recording.transcript_text:
        return _truncate(recording.transcript_text)

    return ""


def _match_overall_score(rule: RiskRule, task: AnalysisTask, recording: Recording | None) -> RiskHit | None:
    threshold = _coerce_float(rule.match_config.get("threshold"))
    score = _coerce_float((task.result or {}).get("consultation_evaluation", {}).get("overall_score"))
    if score is None:
        score = _coerce_float(task.overall_score)
    if threshold is None or score is None or score >= threshold:
        return None

    return RiskHit(
        summary=f"综合评分 {score:.1f} 低于预警阈值 {threshold:.1f}",
        hit_excerpt=_recording_excerpt(recording, (task.result or {}).get("summary")),
        evidence={"overall_score": score, "threshold": threshold},
    )


def _match_dimension_score(rule: RiskRule, task: AnalysisTask, recording: Recording | None) -> RiskHit | None:
    config = rule.match_config or {}
    threshold = _coerce_float(config.get("threshold"))
    dimension_names = config.get("dimension_names") or []
    if isinstance(dimension_names, str):
        dimension_names = [dimension_names]
    normalized_names = {
        normalize_consultation_dimension_name(item).strip().lower()
        for item in dimension_names
        if str(item).strip()
    }
    if threshold is None or not normalized_names:
        return None

    dimensions = (task.result or {}).get("consultation_evaluation", {}).get("dimensions") or []
    for item in dimensions:
        if not isinstance(item, dict):
            continue
        name = normalize_consultation_dimension_name(item.get("name")).strip()
        score = _coerce_float(item.get("score"))
        if not name or score is None or name.lower() not in normalized_names or score >= threshold:
            continue
        comment = str(item.get("comment") or "").strip()
        return RiskHit(
            summary=f"{name} 得分 {score:.1f} 低于阈值 {threshold:.1f}",
            hit_excerpt=_recording_excerpt(recording, comment),
            matched_dimension_name=name,
            evidence={"dimension_name": name, "dimension_score": score, "threshold": threshold, "comment": comment},
        )

    return None


def _match_concern_keyword(rule: RiskRule, task: AnalysisTask, recording: Recording | None) -> RiskHit | None:
    config = rule.match_config or {}
    raw_keywords = config.get("keywords") or []
    if isinstance(raw_keywords, str):
        raw_keywords = [raw_keywords]
    keywords = [str(item).strip() for item in raw_keywords if str(item).strip()]
    if not keywords:
        return None

    concerns = (task.result or {}).get("customer_concerns", {}).get("items") or []
    lowered_keywords = [item.lower() for item in keywords]
    matched_items: list[dict[str, Any]] = []
    matched_keywords: set[str] = set()

    for item in concerns:
        if not isinstance(item, dict):
            continue
        text = " ".join(
            filter(
                None,
                [
                    str(item.get("type") or "").strip(),
                    str(item.get("content") or "").strip(),
                    str(item.get("evidence") or "").strip(),
                ],
            )
        )
        lowered_text = text.lower()
        current_hits = [keyword for keyword in lowered_keywords if keyword in lowered_text]
        if not current_hits:
            continue
        matched_items.append(item)
        matched_keywords.update(current_hits)

    if not matched_items:
        return None

    first_item = matched_items[0]
    excerpt = str(first_item.get("content") or first_item.get("evidence") or "").strip()
    return RiskHit(
        summary=f"客户顾虑中命中关键词：{'、'.join(sorted(matched_keywords))}",
        hit_excerpt=_recording_excerpt(recording, excerpt),
        matched_keywords=sorted(matched_keywords),
        evidence={"matched_items": matched_items[:3], "keywords": sorted(matched_keywords)},
    )


def match_rule(rule: RiskRule, task: AnalysisTask, recording: Recording | None) -> RiskHit | None:
    if rule.match_type == "overall_score_below":
        return _match_overall_score(rule, task, recording)
    if rule.match_type == "dimension_score_below":
        return _match_dimension_score(rule, task, recording)
    if rule.match_type == "concern_keyword":
        return _match_concern_keyword(rule, task, recording)
    return None


async def _load_recording_map(db: AsyncSession, tasks: list[AnalysisTask]) -> dict[str, Recording]:
    recording_ids = [recording_id for task in tasks if (recording_id := extract_recording_id(task.file_name))]
    if not recording_ids:
        return {}

    rows = (
        await db.execute(
            select(Recording)
            .where(Recording.id.in_(recording_ids))
            .options(
                selectinload(Recording.staff),
                selectinload(Recording.transcript),
                selectinload(Recording.visit).selectinload(Visit.customer),
            )
        )
    ).scalars().all()
    return {item.id: item for item in rows}


async def sync_risk_records_for_tasks(db: AsyncSession, task_ids: list[str] | None = None) -> int:
    await ensure_risk_rule_defaults(db)

    rules = (
        await db.execute(select(RiskRule).where(RiskRule.is_active.is_(True)).order_by(RiskRule.created_at.asc()))
    ).scalars().all()
    if not rules:
        return 0

    stmt = select(AnalysisTask).where(AnalysisTask.status == "done", AnalysisTask.result.is_not(None))
    if task_ids:
        stmt = stmt.where(AnalysisTask.id.in_(task_ids))
    tasks = list((await db.execute(stmt)).scalars().all())
    if not tasks:
        return 0

    relevant_task_ids = [item.id for item in tasks]
    relevant_rule_ids = [item.id for item in rules]
    existing_pairs = set(
        (
            row.rule_id,
            row.task_id,
        )
        for row in (
            await db.execute(
                select(RiskRecord.rule_id, RiskRecord.task_id).where(
                    RiskRecord.task_id.in_(relevant_task_ids),
                    RiskRecord.rule_id.in_(relevant_rule_ids),
                )
            )
        ).all()
    )
    recording_map = await _load_recording_map(db, tasks)
    created = 0

    for task in tasks:
        recording = recording_map.get(extract_recording_id(task.file_name) or "")
        visit = recording.visit if recording else None
        customer = visit.customer if visit and visit.customer else None
        staff = recording.staff if recording and recording.staff else None

        for rule in rules:
            if (rule.id, task.id) in existing_pairs:
                continue
            hit = match_rule(rule, task, recording)
            if hit is None:
                continue

            db.add(
                RiskRecord(
                    rule_id=rule.id,
                    task_id=task.id,
                    recording_id=recording.id if recording else None,
                    visit_id=visit.id if visit else None,
                    customer_id=customer.id if customer else None,
                    staff_id=staff.id if staff else None,
                    source_type="recording" if recording else "uploaded_json",
                    rule_name=rule.name,
                    risk_label=rule.risk_label or rule.name,
                    severity=rule.severity,
                    matched_dimension_name=hit.matched_dimension_name,
                    matched_keywords=hit.matched_keywords,
                    overall_score=_coerce_float(task.overall_score),
                    summary=hit.summary,
                    hit_excerpt=hit.hit_excerpt,
                    evidence=hit.evidence,
                )
            )
            existing_pairs.add((rule.id, task.id))
            created += 1

    if created:
        await db.commit()

    return created


async def purge_risk_records_for_rule(db: AsyncSession, rule_id: str) -> None:
    await db.execute(delete(RiskRecord).where(RiskRecord.rule_id == rule_id))
    await db.commit()
