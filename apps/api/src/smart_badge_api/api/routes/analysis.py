"""录音分析结果查看 API。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.analysis_access import build_analysis_artifact_access, task_is_visible
from smart_badge_api.api.analysis_normalization import normalize_analysis_result
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.api.hospital_scope import normalize_hospital_code, recording_hospital_condition
from smart_badge_api.analysis.pipeline import sanitize_analysis_result_with_raw
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import Recording, Transcript, User
from smart_badge_api.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analysis", tags=["analysis"])

_ANALYSIS_RESULT_LIST_CACHE_TTL_SECONDS = 60.0
_analysis_result_list_cache: dict[str, object] = {
    "expires_at": 0.0,
    "items": None,
    "source_key": None,
}
_SAP_CONSULTATION_PREVIEW_RESULT_KEY = "sap_consultation_preview"
# Per-file summary memo, keyed by (path_str, mtime_ns, size, source_key). Survives the
# coarse list-cache expiry so a cache miss only re-processes files that
# actually changed on disk.
_analysis_summary_memo: dict[tuple[object, ...], dict] = {}
_analysis_result_list_lock = asyncio.Lock()


def _results_dir() -> Path:
    return get_settings().results_path


def _raw_dir() -> Path:
    return get_settings().upload_path


def _parse_filename_time(filename: str) -> str | None:
    """从文件名中解析时间。格式: 20260210T151720874560Z_xxx.result.json"""
    try:
        ts_part = filename.split("_")[0]  # 20260210T151720874560Z
        # 解析为 ISO 格式
        dt = datetime(
            year=int(ts_part[0:4]),
            month=int(ts_part[4:6]),
            day=int(ts_part[6:8]),
            hour=int(ts_part[9:11]),
            minute=int(ts_part[11:13]),
            second=int(ts_part[13:15]),
            tzinfo=timezone.utc,
        )
        return dt.isoformat()
    except (ValueError, IndexError):
        return None


def _get_file_id(filename: str) -> str:
    """从文件名提取 file_id。"""
    return filename.replace(".result.json", "")


def _extract_recording_id(file_id: str) -> str | None:
    if not file_id.startswith("recording_"):
        return None
    recording_id = file_id[len("recording_"):]
    if len(recording_id) == 12 and recording_id.isalnum():
        return recording_id
    return None


def _load_raw_data(file_id: str) -> dict | None:
    raw_dir = _raw_dir()
    candidates = (
        raw_dir / f"{file_id}.json",
        raw_dir / "analysis_input" / f"{file_id}.json",
        raw_dir / "dingtalk_staging" / "analysis_input" / f"{file_id}.json",
    )
    for raw_path in candidates:
        if not raw_path.exists():
            continue
        with open(raw_path, encoding="utf-8") as f:
            return json.load(f)
    return None


async def _load_recording_meta(file_ids: list[str], db: AsyncSession) -> dict[str, dict[str, str | int | None]]:
    recording_ids = {recording_id for file_id in file_ids if (recording_id := _extract_recording_id(file_id))}
    if not recording_ids:
        return {}

    rows = (
        await db.execute(
            select(Recording.id, Recording.created_at, Recording.duration_seconds, Recording.file_name).where(Recording.id.in_(recording_ids))
        )
    ).all()
    return {
        row.id: {
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "duration_seconds": row.duration_seconds,
            "file_name": row.file_name,
        }
        for row in rows
    }


async def _load_transcript_context(recording_id: str | None, db: AsyncSession) -> dict | None:
    if not recording_id:
        return None
    transcript = (
        await db.execute(
            select(Transcript).where(Transcript.recording_id == recording_id)
        )
    ).scalar_one_or_none()
    if transcript is None:
        return None
    return {
        "id": transcript.id,
        "recording_id": transcript.recording_id,
        "status": transcript.status,
        "utterances": transcript.utterances or [],
        "duration_ms": transcript.duration_ms,
        "created_at": transcript.created_at.isoformat() if transcript.created_at else None,
        "completed_at": transcript.completed_at.isoformat() if transcript.completed_at else None,
    }


def _build_summary(
    file_id: str,
    result_data: dict,
    raw_data: dict | None,
    recording_meta: dict[str, str | int | None] | None = None,
) -> dict:
    """构建列表用的摘要信息。"""
    evaluation = result_data.get("consultation_evaluation", {})
    consultation_result = result_data.get("consultation_result", {})
    process_evaluation = result_data.get("consultation_process_evaluation", {})
    demands = result_data.get("customer_demands", {})
    concerns = result_data.get("customer_concerns", {})
    profile = result_data.get("customer_profile", {})
    primary_demands = result_data.get("customer_primary_demands", {})
    recommendations = result_data.get("staff_recommendations", {})
    standardized_indications = result_data.get("standardized_indications", {})
    consultation_result_chief = consultation_result.get("chief_complaint_and_indications", {}) if isinstance(consultation_result, dict) else {}
    consultation_result_profile = consultation_result.get("customer_profile_summary", {}) if isinstance(consultation_result, dict) else {}
    consultation_result_factors = consultation_result.get("deal_factors", {}) if isinstance(consultation_result, dict) else {}
    consultation_result_plan = consultation_result.get("recommended_plan", {}) if isinstance(consultation_result, dict) else {}

    process_issue_count = 0
    if isinstance(process_evaluation, dict):
        for section in process_evaluation.get("sections", []):
            if not isinstance(section, dict):
                continue
            for checkpoint in section.get("checkpoints", []):
                if not isinstance(checkpoint, dict):
                    continue
                process_issue_count += len(checkpoint.get("issues", []) or [])

    # 从原始文件获取元信息
    duration_ms = 0
    segment_count = 0
    audio_start = None
    audio_end = None
    if raw_data:
        segments = raw_data.get("payload", {}).get("transcribeResult", [])
        segment_count = len(segments)
        if segments:
            duration_ms = segments[-1].get("end", 0)
        audio_start = raw_data.get("payload", {}).get("audioStartTime")
        audio_end = raw_data.get("payload", {}).get("audioEndTime")

    recorded_at = None
    if recording_meta and recording_meta.get("created_at"):
        recorded_at = str(recording_meta["created_at"])
        if not duration_ms and recording_meta.get("duration_seconds"):
            duration_ms = int(recording_meta["duration_seconds"] or 0) * 1000
        if not audio_start:
            audio_start = recorded_at
        if not audio_end and duration_ms:
            audio_end = (datetime.fromisoformat(recorded_at) + timedelta(milliseconds=duration_ms)).isoformat()

    if not recorded_at:
        recorded_at = _parse_filename_time(file_id)

    focus_areas = [fa.get("area", "") for fa in demands.get("focus_areas", []) if isinstance(fa, dict)]
    if not focus_areas:
        focus_areas = [
            str(item.get("body_part") or "")
            for item in primary_demands.get("items", [])
            if isinstance(item, dict) and str(item.get("body_part") or "")
        ]
    if not focus_areas:
        focus_areas = [
            str(item.get("body_part_name") or "")
            for item in standardized_indications.get("items", [])
            if isinstance(item, dict) and str(item.get("body_part_name") or "")
        ]

    indication_names = [
        str(item.get("indication_name") or "")
        for item in standardized_indications.get("items", [])
        if isinstance(item, dict) and str(item.get("indication_name") or "")
    ]

    tags = [item for item in profile.get("tags", []) if isinstance(item, dict)]
    weight_1_tag_count = sum(1 for item in tags if item.get("weight_level") == 1)
    consumption_intent = result_data.get("consumption_intent", {}) if isinstance(result_data.get("consumption_intent"), dict) else {}
    consumption_intent_present = bool(
        consumption_intent.get("budget")
        or consumption_intent.get("decision_factors")
        or consumption_intent.get("evidence")
    )

    # Count total issues across all evaluation dimensions
    eval_issue_count = 0
    eval_dimensions = evaluation.get("dimensions", [])
    for dim in eval_dimensions:
        if isinstance(dim, dict):
            eval_issue_count += len(dim.get("issues", []))
    eval_issue_count = process_issue_count or eval_issue_count

    analysis_version = (
        "new"
        if any(
            isinstance(result_data.get(key), dict)
            for key in ("customer_primary_demands", "staff_recommendations", "standardized_indications")
        )
        else "legacy"
    )

    preferred_overall_score = (
        process_evaluation.get("overall_score")
        if isinstance(process_evaluation, dict) and isinstance(process_evaluation.get("overall_score"), (int, float))
        else evaluation.get("overall_score", 0)
    )

    return {
        "file_id": file_id,
        "recorded_at": recorded_at,
        "audio_start_time": audio_start,
        "audio_end_time": audio_end,
        "duration_ms": duration_ms,
        "duration_display": _format_duration(duration_ms),
        "segment_count": segment_count,
        "overall_score": preferred_overall_score,
        "eval_issue_count": eval_issue_count,
        "overall_summary": (
            process_evaluation.get("overall_summary")
            if isinstance(process_evaluation, dict) and process_evaluation.get("overall_summary")
            else evaluation.get("overall_summary", "")
        ),
        "dialogue_type": demands.get("expectation", {}).get("dialogue_type", ""),
        "primary_demand_summary": (
            consultation_result_chief.get("summary")
            or primary_demands.get("summary")
            or None
        ),
        "focus_areas": focus_areas,
        "recommendation_count": len(consultation_result_plan.get("items", []) or recommendations.get("items", [])),
        "standardized_indication_count": len(standardized_indications.get("items", [])),
        "indication_names": indication_names,
        "concern_count": len(consultation_result_factors.get("concerns", []) or concerns.get("items", [])),
        "tag_count": int(consultation_result_profile.get("extracted_tag_count") or len(tags)),
        "weight_1_tag_count": weight_1_tag_count,
        "consumption_intent_present": consumption_intent_present,
        "inference_note": primary_demands.get("inference_note") or demands.get("inference_note"),
        "analysis_version": analysis_version,
        "recording_file_name": (recording_meta or {}).get("file_name") or None,
    }


def _format_duration(ms: int) -> str:
    total_sec = ms // 1000
    minutes = total_sec // 60
    seconds = total_sec % 60
    return f"{minutes}:{seconds:02d}"


def clear_analysis_result_list_cache() -> None:
    _analysis_result_list_cache["expires_at"] = 0.0
    _analysis_result_list_cache["items"] = None
    _analysis_result_list_cache["source_key"] = None
    _analysis_summary_memo.clear()


def _clone_analysis_summaries(items: list[dict]) -> list[dict]:
    return [dict(item) for item in items]


async def _load_cached_analysis_result_summaries(db: AsyncSession) -> list[dict]:
    now = time.monotonic()
    results_dir = _results_dir()
    raw_dir = _raw_dir()
    source_key = (str(results_dir.resolve()), str(raw_dir.resolve()))
    cached_items = _analysis_result_list_cache.get("items")
    cached_expires_at = float(_analysis_result_list_cache.get("expires_at") or 0.0)
    if (
        cached_items is not None
        and cached_expires_at > now
        and _analysis_result_list_cache.get("source_key") == source_key
    ):
        return _clone_analysis_summaries(cached_items)  # type: ignore[arg-type]

    # Single-flight: avoid stampedes of expensive disk+sanitize work when many
    # concurrent requests land during a cache miss.
    async with _analysis_result_list_lock:
        now = time.monotonic()
        cached_items = _analysis_result_list_cache.get("items")
        cached_expires_at = float(_analysis_result_list_cache.get("expires_at") or 0.0)
        if (
            cached_items is not None
            and cached_expires_at > now
            and _analysis_result_list_cache.get("source_key") == source_key
        ):
            return _clone_analysis_summaries(cached_items)  # type: ignore[arg-type]

        if not results_dir.exists():
            _analysis_result_list_cache["items"] = []
            _analysis_result_list_cache["source_key"] = source_key
            _analysis_result_list_cache["expires_at"] = now + _ANALYSIS_RESULT_LIST_CACHE_TTL_SECONDS
            return []

        result_files = sorted(results_dir.glob("*.result.json"))
        file_ids = [_get_file_id(fp.name) for fp in result_files]
        recording_meta_map = await _load_recording_meta(file_ids, db)

        items: list[dict] = []
        seen_keys: set[tuple[object, ...]] = set()
        for idx, fp in enumerate(result_files):
            # Yield to the event loop periodically so other requests / warm-ups
            # are not starved during the (CPU-heavy) sanitize loop.
            if idx and idx % 20 == 0:
                await asyncio.sleep(0)
            try:
                stat = fp.stat()
                memo_key = (str(fp), stat.st_mtime_ns, stat.st_size, source_key)
                seen_keys.add(memo_key)
                file_id = _get_file_id(fp.name)
                recording_id = _extract_recording_id(file_id)
                meta = recording_meta_map.get(recording_id) if recording_id else None
                # Include meta identity in the key so cache invalidates if the
                # underlying recording metadata gets refreshed.
                full_key = memo_key + ((meta or {}).get("_cache_token"),)
                cached = _analysis_summary_memo.get(full_key)  # type: ignore[arg-type]
                if cached is not None:
                    items.append(cached)
                    continue

                with open(fp, encoding="utf-8") as f:
                    result_data = json.load(f)
                # Match detail-page semantics: load raw_data and sanitize so
                # tag counts / segment counts / duration fallbacks are
                # consistent between list and detail views.
                raw_data = _load_raw_data(file_id)
                if raw_data:
                    sanitize_analysis_result_with_raw(result_data, raw=raw_data)
                result_data = normalize_analysis_result(result_data) or {}
                summary = _build_summary(file_id, result_data, raw_data, meta)
                _analysis_summary_memo[full_key] = summary  # type: ignore[index]
                items.append(summary)
            except Exception as e:
                logger.warning("Failed to load %s: %s", fp.name, e)

        # Evict memo entries for files that no longer exist (best-effort).
        if len(_analysis_summary_memo) > len(seen_keys) * 2 + 50:
            stale = [
                k for k in _analysis_summary_memo
                if (k[0], k[1], k[2]) not in seen_keys
            ]
            for k in stale:
                _analysis_summary_memo.pop(k, None)

        _analysis_result_list_cache["items"] = _clone_analysis_summaries(items)
        _analysis_result_list_cache["source_key"] = source_key
        _analysis_result_list_cache["expires_at"] = now + _ANALYSIS_RESULT_LIST_CACHE_TTL_SECONDS
        return items


@router.get("/results")
async def list_results(
    sort_by: str = Query("time", pattern="^(time|tags|issues)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    min_score: float | None = Query(None, ge=0, le=10),
    max_score: float | None = Query(None, ge=0, le=10),
    hospital_code: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取所有分析结果列表。"""
    access = await build_analysis_artifact_access(db, current_user)
    hospital_code = hospital_code if isinstance(hospital_code, str) else None
    requested_hospital_code = normalize_hospital_code(hospital_code)
    hospital_recording_ids: set[str] | None = None
    if requested_hospital_code:
        hospital_recording_ids = set(
            (
                await db.execute(
                    select(Recording.id).where(
                        recording_hospital_condition(requested_hospital_code),
                        Recording.status != "filtered",
                    )
                )
            ).scalars().all()
        )
    items = []
    for summary in await _load_cached_analysis_result_summaries(db):
        file_id = str(summary.get("file_id") or "")
        if not task_is_visible(f"{file_id}.json", access):
            continue
        recording_id = _extract_recording_id(file_id)
        if hospital_recording_ids is not None and recording_id not in hospital_recording_ids:
            continue

        # 分数过滤
        if min_score is not None and summary["overall_score"] < min_score:
            continue
        if max_score is not None and summary["overall_score"] > max_score:
            continue

        items.append(summary)

    # 排序
    if sort_by == "tags":
        items.sort(
            key=lambda x: (x["weight_1_tag_count"], x["tag_count"], x["consumption_intent_present"]),
            reverse=(sort_order == "desc"),
        )
    elif sort_by == "issues":
        items.sort(key=lambda x: x["eval_issue_count"], reverse=(sort_order == "desc"))
    else:
        items.sort(key=lambda x: x["recorded_at"] or "", reverse=(sort_order == "desc"))

    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size

    return {"items": items[start:end], "total": total, "page": page, "page_size": page_size}


@router.get("/results/{file_id}")
async def get_result(
    file_id: str,
    include_transcript: bool = Query(False, description="Include transcript utterances for evidence context"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取单个分析结果详情。"""
    # 安全校验：file_id 只允许字母数字和下划线
    if not all(c.isalnum() or c in ("_", "T", "Z") for c in file_id):
        raise HTTPException(status_code=400, detail="无效的文件 ID")

    result_path = _results_dir() / f"{file_id}.result.json"
    if not result_path.exists():
        raise HTTPException(status_code=404, detail="分析结果未找到")
    access = await build_analysis_artifact_access(db, current_user)
    if not task_is_visible(f"{file_id}.json", access):
        raise HTTPException(status_code=404, detail="分析结果未找到")

    with open(result_path, encoding="utf-8") as f:
        result_data = json.load(f)

    raw_data = _load_raw_data(file_id)
    if raw_data:
        sanitize_analysis_result_with_raw(result_data, raw=raw_data)
    result_data = normalize_analysis_result(result_data) or {}

    recording_id = _extract_recording_id(file_id)
    recording_meta_map = await _load_recording_meta([file_id], db)
    summary = _build_summary(file_id, result_data, raw_data, recording_meta_map.get(recording_id) if recording_id else None)
    transcript_context = await _load_transcript_context(recording_id, db) if include_transcript else None

    return {
        **summary,
        "transcript": transcript_context,
        "customer_primary_demands": result_data.get("customer_primary_demands"),
        "staff_recommendations": result_data.get("staff_recommendations"),
        "standardized_indications": result_data.get("standardized_indications"),
        "customer_demands": result_data.get("customer_demands"),
        "customer_concerns": result_data.get("customer_concerns"),
        "customer_profile": result_data.get("customer_profile"),
        "consumption_intent": result_data.get("consumption_intent"),
        "consultation_evaluation": result_data.get("consultation_evaluation"),
        "consultation_result": result_data.get("consultation_result"),
        "consultation_process_evaluation": result_data.get("consultation_process_evaluation"),
        _SAP_CONSULTATION_PREVIEW_RESULT_KEY: result_data.get(_SAP_CONSULTATION_PREVIEW_RESULT_KEY),
    }
