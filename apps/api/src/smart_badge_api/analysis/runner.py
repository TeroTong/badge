"""后台分析任务执行器。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update

from smart_badge_api.analysis.customer_profile_score_sync import refresh_recording_profile_scores_for_current_context
from smart_badge_api.analysis.production import analyze_transcript_for_production
from smart_badge_api.analysis.prompt_builder import build_system_prompt
from smart_badge_api.analysis.transcript import load_transcript
from smart_badge_api.api.ws_hub import task_hub
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import AnalysisTask, Recording, Staff
from smart_badge_api.db.session import _session_factory
from smart_badge_api.risk.service import sync_risk_records_for_tasks
from smart_badge_api.sap_consultation import attach_unlinked_sap_preview_to_result

logger = logging.getLogger(__name__)
_VISIT_SCOPED_ANALYSIS_STEM_PATTERN = re.compile(r"^recording_(?P<recording_id>[^_]+)_visit_(?P<visit_id>[^_]+)$")


def _run_analysis_sync(
    file_path: str,
    system_prompt: str | None = None,
    staff_context: dict[str, str] | None = None,
) -> dict:
    return analyze_transcript_for_production(
        file_path,
        system_prompt=system_prompt,
        staff_context=staff_context,
    )


def _result_path(file_path: str) -> Path:
    result_dir = get_settings().results_path
    result_dir.mkdir(parents=True, exist_ok=True)
    return result_dir / f"{Path(file_path).stem}.result.json"


async def _resolve_recording_hospital_code(db, recording_id: str | None) -> str | None:
    if not recording_id:
        return None
    return (
        await db.execute(
            select(Staff.hospital_code)
            .join(Recording, Recording.staff_id == Staff.id)
            .where(Recording.id == recording_id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _resolve_recording_staff_context(db, recording_id: str | None, file_path: str) -> dict[str, str]:
    context = {
        "file_name": Path(file_path).name,
        "staff_name": "",
        "staff_role": "",
        "hospital_code": "",
    }
    if not recording_id:
        return context
    row = (
        await db.execute(
            select(Recording.file_name, Staff.name, Staff.role, Staff.hospital_code)
            .join(Staff, Recording.staff_id == Staff.id, isouter=True)
            .where(Recording.id == recording_id)
            .limit(1)
        )
    ).first()
    if row is None:
        return context
    file_name, staff_name, staff_role, hospital_code = row
    context.update(
        {
            "file_name": file_name or context["file_name"],
            "staff_name": staff_name or "",
            "staff_role": staff_role or "",
            "hospital_code": hospital_code or "",
        }
    )
    return context


async def _broadcast_task_progress(task_id: str, **fields) -> None:
    payload: dict[str, str | int | float | None] = {"task_id": task_id}
    for key in ("status", "progress", "error_message", "overall_score"):
        if key in fields:
            payload[key] = fields[key]
    if "completed_at" in fields:
        completed_at = fields["completed_at"]
        payload["completed_at"] = completed_at.isoformat() if completed_at else None
    await task_hub.broadcast("task_progress", payload)


async def _claim_task(task_id: str) -> str | None:
    async with _session_factory() as db:
        result = await db.execute(
            update(AnalysisTask)
            .where(AnalysisTask.id == task_id, AnalysisTask.status == "pending")
            .values(status="running", progress=10, error_message=None)
        )
        if result.rowcount != 1:
            await db.rollback()
            return None

        task = await db.get(AnalysisTask, task_id)
        await db.commit()

    if task is None:
        return None

    await _broadcast_task_progress(task_id, status="running", progress=10)
    return task.file_path


async def _update_task(task_id: str, **fields) -> None:
    async with _session_factory() as db:
        task = await db.get(AnalysisTask, task_id)
        if task is None:
            return

        for key, value in fields.items():
            setattr(task, key, value)
        await db.commit()

    await _broadcast_task_progress(task_id, **fields)


async def _update_recording_status(recording_id: str | None, status: str) -> None:
    if not recording_id:
        return
    async with _session_factory() as db:
        recording = await db.get(Recording, recording_id)
        if recording is None:
            return
        recording.status = status
        await db.commit()


def _parse_analysis_file_stem(file_path: str | None) -> tuple[str | None, str | None]:
    recording_file_name = Path(file_path or "").stem
    scoped_match = _VISIT_SCOPED_ANALYSIS_STEM_PATTERN.match(recording_file_name)
    if scoped_match:
        return scoped_match.group("recording_id"), scoped_match.group("visit_id")
    if not recording_file_name.startswith("recording_"):
        return None, None
    recording_id = recording_file_name.removeprefix("recording_")
    return recording_id or None, None


def _extract_recording_id_from_analysis_file_path(file_path: str | None) -> str | None:
    recording_id, _visit_id = _parse_analysis_file_stem(file_path)
    return recording_id


async def _sync_visit_scoped_analysis_result(
    recording_id: str | None,
    visit_id: str | None,
    task_id: str,
) -> None:
    if not recording_id or not visit_id:
        return
    async with _session_factory() as db:
        from sqlalchemy import select

        from smart_badge_api.db.models import RecordingVisitAnalysis
        from smart_badge_api.recording_multi_customer import sync_visit_analysis_task_result

        analysis = (
            await db.execute(
                select(RecordingVisitAnalysis).where(
                    RecordingVisitAnalysis.recording_id == recording_id,
                    RecordingVisitAnalysis.visit_id == visit_id,
                    RecordingVisitAnalysis.analysis_task_id == task_id,
                )
            )
        ).scalar_one_or_none()
        if analysis is None:
            return
        await sync_visit_analysis_task_result(db, analysis)
        await db.commit()


async def execute_analysis(task_id: str) -> None:
    """执行单个分析任务。"""
    file_path = await _claim_task(task_id)
    if file_path is None:
        logger.info("Task %s skipped because it was already claimed or no longer pending", task_id)
        return
    recording_id, visit_id = _parse_analysis_file_stem(file_path)
    if visit_id is None:
        await _update_recording_status(recording_id, "analyzing")

    try:
        # 将数据库中存储的路径解析为绝对路径
        resolved_path = str(get_settings().resolve_file_path(file_path))

        async with _session_factory() as db:
            hospital_code = await _resolve_recording_hospital_code(db, recording_id)
            system_prompt = await build_system_prompt(db, hospital_code=hospital_code)
            staff_context = await _resolve_recording_staff_context(db, recording_id, resolved_path)

        source_path = Path(resolved_path)
        if not source_path.exists():
            raise FileNotFoundError(f"Transcript file not found: {source_path}")

        raw = load_transcript(resolved_path)
        segments = raw.get("payload", {}).get("transcribeResult", [])
        segment_count = len(segments)
        duration_ms = 0
        if segments:
            duration_ms = max(segment.get("end", 0) for segment in segments) - min(
                segment.get("begin", 0) for segment in segments
            )

        await _update_task(task_id, progress=20, segment_count=segment_count, duration_ms=duration_ms)

        loop = asyncio.get_running_loop()
        result_dict = await loop.run_in_executor(None, _run_analysis_sync, resolved_path, system_prompt, staff_context)
        if visit_id is None and recording_id:
            try:
                async with _session_factory() as db:
                    result_dict = await attach_unlinked_sap_preview_to_result(db, recording_id, result_dict) or result_dict
            except Exception as exc:
                logger.warning("failed to attach SAP preview to analysis result task_id=%s: %s", task_id, exc)

        raw_process_score = result_dict.get("consultation_process_evaluation", {}).get("overall_score")
        if isinstance(raw_process_score, (int, float)):
            overall_score = float(raw_process_score)
        else:
            raw_overall_score = result_dict.get("consultation_evaluation", {}).get("overall_score")
            overall_score = float(raw_overall_score) if isinstance(raw_overall_score, (int, float)) else None
        # Count total issues for the new evaluation format
        eval_dims = result_dict.get("consultation_evaluation", {}).get("dimensions", [])
        issue_count = sum(len(d.get("issues", [])) for d in eval_dims if isinstance(d, dict))
        result_path = _result_path(resolved_path)
        await asyncio.to_thread(
            result_path.write_text,
            json.dumps(result_dict, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        await _update_task(
            task_id,
            status="done",
            progress=100,
            result=result_dict,
            overall_score=overall_score,
            completed_at=datetime.now(timezone.utc),
        )
        if visit_id is None:
            await _update_recording_status(recording_id, "analyzed")
            async with _session_factory() as db:
                recording_file_name = Path(resolved_path).stem
                if recording_file_name.startswith("recording_"):
                    recording_id = recording_file_name.removeprefix("recording_")
                    await refresh_recording_profile_scores_for_current_context(db, recording_id)
                    await db.commit()
                await sync_risk_records_for_tasks(db, [task_id])
        else:
            await _sync_visit_scoped_analysis_result(recording_id, visit_id, task_id)
        logger.info("Task %s completed, issue_count=%d", task_id, issue_count)
    except Exception as exc:
        logger.exception("Task %s failed: %s", task_id, exc)
        await _update_task(
            task_id,
            status="failed",
            progress=0,
            error_message=str(exc),
            completed_at=datetime.now(timezone.utc),
        )
        if visit_id is None:
            await _update_recording_status(recording_id, "failed")
        else:
            await _sync_visit_scoped_analysis_result(recording_id, visit_id, task_id)
