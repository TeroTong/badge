from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.analysis.consultation_evaluation import (
    extract_preferred_overall_score,
    rebuild_consultation_evaluation,
    rebuild_consultation_process_evaluation,
)
from smart_badge_api.api.analysis_normalization import normalize_analysis_result
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import AnalysisTask, Customer, Recording, RecordingVisitLink, Visit, VisitOrder
from smart_badge_api.tag_catalog_reference import (
    BIRTHDATE_TAG_CATEGORY,
    canonicalize_profile_tag_category,
    canonicalize_profile_tag_value,
    is_valid_profile_tag_value,
    load_tag_catalog_definitions,
)

logger = logging.getLogger(__name__)


_EMPTY_SYNC_TAG_VALUES = frozenset({"", "未明确", "未提及", "未知", "N/A", "-", "n/a"})
_CUSTOMER_ARCHIVE_SYNC_SOURCE = "customer_archive_sync"
_CUSTOMER_HISTORY_SYNC_SOURCE = "customer_history_sync"
_CUSTOMER_ARCHIVE_SYNC_EVIDENCE = "已从绑定客户档案同步"
_CUSTOMER_HISTORY_SYNC_EVIDENCE = "已从客户历史标签同步"
_SYNC_SEEN_AT_FIELD = "_sync_seen_at"
_SYNC_GENERATED_SOURCES = frozenset({_CUSTOMER_ARCHIVE_SYNC_SOURCE, _CUSTOMER_HISTORY_SYNC_SOURCE})


def _analysis_file_name(recording_id: str) -> str:
    return f"recording_{recording_id}.json"


def _extract_recording_id(file_name: str | None) -> str | None:
    text = str(file_name or "").strip()
    if not text.startswith("recording_") or not text.endswith(".json"):
        return None
    recording_id = text.removeprefix("recording_").removesuffix(".json")
    return recording_id or None


def _result_path(recording_id: str) -> Path:
    settings = get_settings()
    settings.results_path.mkdir(parents=True, exist_ok=True)
    return settings.results_path / f"recording_{recording_id}.result.json"


def _collect_profile_tags(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    customer_profile = result.get("customer_profile")
    if not isinstance(customer_profile, dict):
        return []
    return [item for item in customer_profile.get("tags") or [] if isinstance(item, dict)]


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _task_seen_at(task: AnalysisTask) -> str:
    timestamp = task.completed_at or task.created_at
    return timestamp.isoformat() if timestamp else ""


def _is_generated_sync_tag(item: dict[str, Any]) -> bool:
    return _clean_text(item.get("source")) in _SYNC_GENERATED_SOURCES


def _profile_weight_by_category() -> dict[str, int]:
    return {
        item.name: int(item.weight_level)
        for item in load_tag_catalog_definitions()
        if item.weight_level is not None
    }


def _normalize_existing_profile_tags(
    tags: list[dict[str, Any]],
    *,
    include_generated_sync_tags: bool = True,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in tags:
        if not include_generated_sync_tags and _is_generated_sync_tag(item):
            continue
        category = canonicalize_profile_tag_category(item.get("category"))
        value = canonicalize_profile_tag_value(category, item.get("value")) if category else None
        if not category or not value or value in _EMPTY_SYNC_TAG_VALUES:
            continue
        if not is_valid_profile_tag_value(category, value):
            continue
        normalized.append({**item, "category": category, "value": value})
    return normalized

async def _resolve_customer_archive_birthdate(
    db: AsyncSession,
    customer: Customer | None,
) -> str | None:
    if customer is None:
        return None

    visit_order_birthdays = (
        await db.execute(
            select(VisitOrder.customer_birthday)
            .join(Visit, Visit.external_visit_order_no == VisitOrder.dzdh)
            .where(
                Visit.customer_id == customer.id,
                VisitOrder.customer_birthday.is_not(None),
                VisitOrder.customer_birthday != "",
            )
            .order_by(
                Visit.visit_date.desc().nullslast(),
                Visit.created_at.desc().nullslast(),
                VisitOrder.crtdt.desc().nullslast(),
                VisitOrder.crttm.desc().nullslast(),
            )
        )
    ).scalars().all()
    for raw_value in visit_order_birthdays:
        normalized = canonicalize_profile_tag_value(BIRTHDATE_TAG_CATEGORY, raw_value)
        if normalized and normalized not in _EMPTY_SYNC_TAG_VALUES:
            return normalized
    return None


async def _build_customer_archive_tags(
    db: AsyncSession,
    customer: Customer | None,
) -> list[dict[str, Any]]:
    if customer is None:
        return []

    weight_by_category = _profile_weight_by_category()
    synced_tags: list[dict[str, Any]] = []
    archive_birthdate = await _resolve_customer_archive_birthdate(db, customer)
    if archive_birthdate:
        synced_tags.append(
            {
                "category": BIRTHDATE_TAG_CATEGORY,
                "value": archive_birthdate,
                "weight_level": weight_by_category.get(BIRTHDATE_TAG_CATEGORY),
                "evidence": _CUSTOMER_ARCHIVE_SYNC_EVIDENCE,
                "source": _CUSTOMER_ARCHIVE_SYNC_SOURCE,
            }
        )

    return synced_tags


def _build_history_sync_tags(tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    weight_by_category = _profile_weight_by_category()
    latest_by_category: dict[str, tuple[str, dict[str, Any]]] = {}
    for item in tags:
        category = canonicalize_profile_tag_category(item.get("category"))
        value = canonicalize_profile_tag_value(category, item.get("value")) if category else None
        if not category or not value or value in _EMPTY_SYNC_TAG_VALUES:
            continue
        if not is_valid_profile_tag_value(category, value):
            continue
        seen_at = _clean_text(item.get(_SYNC_SEEN_AT_FIELD))
        payload = {
            "category": category,
            "value": value,
            "weight_level": int(item.get("weight_level")) if isinstance(item.get("weight_level"), int) else weight_by_category.get(category),
            "evidence": _CUSTOMER_HISTORY_SYNC_EVIDENCE,
            "source": _CUSTOMER_HISTORY_SYNC_SOURCE,
        }
        existing = latest_by_category.get(category)
        if existing is None or seen_at >= existing[0]:
            latest_by_category[category] = (seen_at, payload)
    return [payload for _, payload in latest_by_category.values()]


def _merge_missing_profile_tags(
    current_tags: list[dict[str, Any]],
    supplemental_tags: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized_current = _normalize_existing_profile_tags(current_tags, include_generated_sync_tags=False)
    existing_categories = {
        str(item.get("category"))
        for item in normalized_current
        if str(item.get("value") or "").strip()
    }
    merged = list(normalized_current)
    for item in supplemental_tags:
        category = canonicalize_profile_tag_category(item.get("category"))
        value = canonicalize_profile_tag_value(category, item.get("value")) if category else None
        if not category or not value or value in _EMPTY_SYNC_TAG_VALUES:
            continue
        if not is_valid_profile_tag_value(category, value):
            continue
        payload = {**item, "category": category, "value": value}
        if _clean_text(item.get("source")) == _CUSTOMER_ARCHIVE_SYNC_SOURCE and category == BIRTHDATE_TAG_CATEGORY:
            insert_at = next(
                (
                    index
                    for index, existing_item in enumerate(merged)
                    if str(existing_item.get("category") or "").strip() == category
                ),
                len(merged),
            )
            merged = [existing_item for existing_item in merged if str(existing_item.get("category") or "").strip() != category]
            merged.insert(insert_at if insert_at <= len(merged) else len(merged), payload)
            existing_categories.add(category)
            continue
        if category in existing_categories:
            continue
        merged.append(payload)
        existing_categories.add(category)

    return merged


async def _latest_done_tasks_by_recording(
    db: AsyncSession,
    recording_ids: list[str],
) -> dict[str, AnalysisTask]:
    if not recording_ids:
        return {}

    file_names = [_analysis_file_name(recording_id) for recording_id in recording_ids]
    tasks = (
        await db.execute(
            select(AnalysisTask)
            .where(
                AnalysisTask.status == "done",
                AnalysisTask.file_name.in_(file_names),
            )
            .order_by(AnalysisTask.completed_at.desc(), AnalysisTask.created_at.desc())
        )
    ).scalars().all()

    latest: dict[str, AnalysisTask] = {}
    for task in tasks:
        recording_id = _extract_recording_id(task.file_name)
        if recording_id and recording_id not in latest:
            latest[recording_id] = task
    return latest


def _write_result_artifact_if_exists(recording_id: str, result: dict[str, Any]) -> None:
    result_path = _result_path(recording_id)
    if not result_path.exists():
        return
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


async def refresh_recording_profile_score(
    db: AsyncSession,
    recording_id: str,
    *,
    supplemental_profile_tags: list[dict[str, Any]] | None = None,
) -> str | None:
    task = (
        await db.execute(
            select(AnalysisTask)
            .where(
                AnalysisTask.status == "done",
                AnalysisTask.file_name == _analysis_file_name(recording_id),
            )
            .order_by(AnalysisTask.completed_at.desc(), AnalysisTask.created_at.desc())
        )
    ).scalars().first()
    if task is None or not isinstance(task.result, dict):
        return None

    normalized_result = normalize_analysis_result(task.result) or {}
    current_tags = _collect_profile_tags(normalized_result)
    merged_tags = _merge_missing_profile_tags(current_tags, supplemental_profile_tags or [])

    updated_result = dict(normalized_result)
    customer_profile = dict(updated_result.get("customer_profile") or {})
    customer_profile["tags"] = merged_tags
    updated_result["customer_profile"] = customer_profile
    updated_result = normalize_analysis_result(updated_result) or updated_result
    updated_result["consultation_evaluation"] = rebuild_consultation_evaluation(
        updated_result,
        historical_profile_tags=[],
    )
    updated_result["consultation_process_evaluation"] = rebuild_consultation_process_evaluation(updated_result)
    try:
        from smart_badge_api.sap_consultation import attach_unlinked_sap_preview_to_result

        updated_result = await attach_unlinked_sap_preview_to_result(db, recording_id, updated_result) or updated_result
    except Exception as exc:
        logger.warning("failed to refresh SAP preview after profile score sync recording_id=%s: %s", recording_id, exc)
    normalized_overall_score = extract_preferred_overall_score(updated_result)

    if task.result != updated_result or task.overall_score != normalized_overall_score:
        task.result = updated_result
        task.overall_score = normalized_overall_score
        _write_result_artifact_if_exists(recording_id, updated_result)
        return task.id
    return None


async def refresh_customer_profile_scores(
    db: AsyncSession,
    customer_id: str,
) -> list[str]:
    customer = (
        await db.execute(select(Customer).where(Customer.id == customer_id))
    ).scalar_one_or_none()
    visit_ids = (
        await db.execute(select(Visit.id).where(Visit.customer_id == customer_id))
    ).scalars().all()
    if not visit_ids:
        return []

    linked_recording_ids = (
        await db.execute(
            select(RecordingVisitLink.recording_id)
            .where(RecordingVisitLink.visit_id.in_(visit_ids))
            .distinct()
        )
    ).scalars().all()
    primary_recording_ids = (
        await db.execute(select(Recording.id).where(Recording.visit_id.in_(visit_ids)))
    ).scalars().all()
    recording_ids = list(dict.fromkeys([*linked_recording_ids, *primary_recording_ids]))
    tasks_by_recording = await _latest_done_tasks_by_recording(db, recording_ids)
    if not tasks_by_recording:
        return []

    normalized_result_by_recording = {
        recording_id: normalize_analysis_result(task.result) or {}
        for recording_id, task in tasks_by_recording.items()
        if isinstance(task.result, dict)
    }
    tags_by_recording = {
        recording_id: [
            {**tag, _SYNC_SEEN_AT_FIELD: _task_seen_at(tasks_by_recording[recording_id])}
            for tag in _normalize_existing_profile_tags(
                _collect_profile_tags(result),
                include_generated_sync_tags=False,
            )
        ]
        for recording_id, result in normalized_result_by_recording.items()
    }
    customer_archive_tags = await _build_customer_archive_tags(db, customer)

    updated_task_ids: list[str] = []
    for recording_id in tasks_by_recording:
        historical_tags: list[dict[str, Any]] = []
        for other_recording_id, tags in tags_by_recording.items():
            if other_recording_id == recording_id:
                continue
            historical_tags.extend(tags)
        supplemental_tags = [
            *customer_archive_tags,
            *_build_history_sync_tags(historical_tags),
        ]
        updated_task_id = await refresh_recording_profile_score(
            db,
            recording_id,
            supplemental_profile_tags=supplemental_tags,
        )
        if updated_task_id:
            updated_task_ids.append(updated_task_id)

    return updated_task_ids


async def refresh_recording_profile_scores_for_current_context(
    db: AsyncSession,
    recording_id: str,
) -> list[str]:
    recording = (
        await db.execute(select(Recording).where(Recording.id == recording_id))
    ).scalar_one_or_none()
    if recording is None:
        return []

    customer_id = None
    if recording.visit_id:
        customer_id = (
            await db.execute(select(Visit.customer_id).where(Visit.id == recording.visit_id))
        ).scalar_one_or_none()

    if customer_id:
        return await refresh_customer_profile_scores(db, customer_id)

    updated_task_id = await refresh_recording_profile_score(db, recording_id, supplemental_profile_tags=[])
    return [updated_task_id] if updated_task_id else []
