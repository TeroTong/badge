from __future__ import annotations

import asyncio
import json
import logging
import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from smart_badge_api.analysis.pipeline import (
    _backfill_customer_profile_tags,
    _backfill_consumption_intent,
    _backfill_customer_concerns,
    _backfill_staff_recommendations,
    _backfill_consultation_result_outcome,
    _backfill_first_consultation_item,
    _clear_stale_first_item_summary,
    _compute_inference_note,
    _sanitize_customer_concerns,
    _sanitize_customer_primary_demands,
    _sanitize_customer_profile_tags,
    _sanitize_consumption_intent,
    _sanitize_standardized_indications,
    analyze_transcript,
)
import smart_badge_api.analysis.pipeline as pipeline_module
from smart_badge_api.analysis.prompt_builder import build_system_prompt
from smart_badge_api.analysis.schemas import AnalysisResult
from smart_badge_api.analysis.transcript import prepare_transcript
from smart_badge_api.analysis.consultation_evaluation import (
    rebuild_consultation_evaluation,
    rebuild_consultation_process_evaluation,
)
from smart_badge_api.core.config import get_settings
from smart_badge_api.api.analysis_normalization import (
    normalize_analysis_result,
    normalize_standardized_indications_payload,
)
from smart_badge_api.db.models import AnalysisTask, Recording
from smart_badge_api.db.session import _session_factory

settings = get_settings()

# 全量重跑只基于已有 transcript，不重新调用腾讯云 ASR。
# 这里把分析超时压到更合理的范围，并限制并发，避免串行跑太久。
settings.llm_timeout_seconds = min(settings.llm_timeout_seconds, 20.0)
MAX_CONCURRENCY = 6

logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

_ORIGINAL_CALL_LLM_JSON = pipeline_module._call_llm_json


def _call_llm_json_once(*, system_prompt: str, user_prompt: str, max_tokens: int = 4096, attempts: int = 3) -> dict:
    return _ORIGINAL_CALL_LLM_JSON(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=max_tokens,
        attempts=1,
    )


pipeline_module._call_llm_json = _call_llm_json_once


@dataclass(slots=True)
class WorkResult:
    ok: bool
    name: str
    error: str | None = None
    mode: str = "fresh"


def _extract_recording_id(file_path: str | None) -> str | None:
    stem = Path(file_path or "").stem
    if stem.startswith("recording_"):
        rid = stem.removeprefix("recording_")
        return rid or None
    return None


def _result_path_for_task(file_path: str) -> Path:
    return settings.results_path / f"{Path(file_path).stem}.result.json"


def _analyze_to_dict(path: Path, system_prompt: str) -> dict:
    result = analyze_transcript(path, system_prompt=system_prompt)
    return result.model_dump(mode="json")


def _rebuild_from_existing(path: Path, existing_result: dict | None) -> dict:
    dialogue, raw = prepare_transcript(path)
    result_dict = deepcopy(existing_result or {})
    if not isinstance(result_dict, dict):
        result_dict = {}

    note = _compute_inference_note(raw)
    if note:
        for key in (
            "customer_primary_demands",
            "standardized_indications",
            "customer_demands",
            "customer_concerns",
            "customer_profile",
        ):
            if key in result_dict and isinstance(result_dict[key], dict):
                result_dict[key]["inference_note"] = note

    demand_sanitize_changed = _sanitize_customer_primary_demands(result_dict, raw=raw)
    indication_sanitize_changed = _sanitize_standardized_indications(result_dict, raw=raw)
    _backfill_first_consultation_item(result_dict, raw=raw)
    _backfill_customer_profile_tags(result_dict, raw=raw)
    _backfill_consumption_intent(result_dict, raw=raw)
    _backfill_customer_concerns(result_dict, raw=raw)
    _sanitize_customer_profile_tags(result_dict, raw=raw)
    _sanitize_consumption_intent(result_dict, raw=raw)
    _sanitize_customer_concerns(result_dict, raw=raw)
    _backfill_staff_recommendations(result_dict, raw=raw)
    _backfill_consultation_result_outcome(result_dict, raw=raw)

    standardized_indications = result_dict.get("standardized_indications")
    if isinstance(standardized_indications, dict):
        result_dict["standardized_indications"] = normalize_standardized_indications_payload(
            standardized_indications
        )

    result_dict["consultation_evaluation"] = rebuild_consultation_evaluation(
        result_dict,
        dialogue=dialogue,
    )
    result_dict["consultation_process_evaluation"] = rebuild_consultation_process_evaluation(
        result_dict,
        dialogue=dialogue,
    )

    if demand_sanitize_changed or indication_sanitize_changed:
        _clear_stale_first_item_summary(result_dict)

    normalized = normalize_analysis_result(result_dict)
    if isinstance(normalized, dict):
        result_dict = normalized
    return AnalysisResult.model_validate(result_dict).model_dump(mode="json")


async def _process_db_task(
    task_id: str,
    system_prompt: str,
    sem: asyncio.Semaphore,
) -> WorkResult:
    async with sem:
        async with _session_factory() as db:
            task = await db.get(AnalysisTask, task_id)
            if task is None:
                return WorkResult(False, task_id, "analysis task missing")
            file_path = task.file_path
            file_name = task.file_name
            existing_result = deepcopy(task.result or {})

        try:
            resolved = settings.resolve_file_path(file_path)
            if not resolved.exists():
                raise FileNotFoundError(f"transcript not found: {resolved}")

            try:
                result_dict = await asyncio.to_thread(_analyze_to_dict, resolved, system_prompt)
                mode = "fresh"
            except Exception:
                result_dict = await asyncio.to_thread(_rebuild_from_existing, resolved, existing_result)
                mode = "fallback"
            result_path = _result_path_for_task(str(resolved))
            result_path.write_text(json.dumps(result_dict, ensure_ascii=False, indent=2), encoding="utf-8")

            score = result_dict.get("consultation_process_evaluation", {}).get("overall_score")
            if not isinstance(score, (int, float)):
                score = result_dict.get("consultation_evaluation", {}).get("overall_score")

            completed_at = datetime.now(timezone.utc)
            async with _session_factory() as db:
                fresh = await db.get(AnalysisTask, task_id)
                if fresh is None:
                    raise RuntimeError(f"task missing after analysis: {task_id}")
                fresh.status = "done"
                fresh.progress = 100
                fresh.error_message = None
                fresh.result = result_dict
                fresh.overall_score = float(score) if isinstance(score, (int, float)) else None
                fresh.completed_at = completed_at
                rid = _extract_recording_id(fresh.file_path)
                if rid:
                    recording = await db.get(Recording, rid)
                    if recording is not None:
                        recording.status = "analyzed"
                await db.commit()
            return WorkResult(True, file_name, mode=mode)
        except Exception as exc:
            async with _session_factory() as db:
                fresh = await db.get(AnalysisTask, task_id)
                if fresh is not None:
                    fresh.status = "failed"
                    fresh.progress = 0
                    fresh.error_message = str(exc)
                    fresh.completed_at = datetime.now(timezone.utc)
                    rid = _extract_recording_id(fresh.file_path)
                    if rid:
                        recording = await db.get(Recording, rid)
                        if recording is not None:
                            recording.status = "failed"
                    await db.commit()
            return WorkResult(False, file_name, str(exc), mode="failed")


async def rerun_db_tasks(system_prompt: str) -> dict:
    summary = {"total": 0, "success": 0, "failed": 0, "fallback_success": 0, "failed_items": []}
    async with _session_factory() as db:
        task_ids = list(
            (
                await db.execute(
                    select(AnalysisTask.id).order_by(AnalysisTask.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
    summary["total"] = len(task_ids)
    print(f"[DB] rerun {len(task_ids)} analysis tasks with concurrency={MAX_CONCURRENCY}")

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    coros = [_process_db_task(task_id, system_prompt, sem) for task_id in task_ids]
    for idx, future in enumerate(asyncio.as_completed(coros), start=1):
        result = await future
        if result.ok:
            summary["success"] += 1
            if result.mode == "fallback":
                summary["fallback_success"] += 1
        else:
            summary["failed"] += 1
            summary["failed_items"].append(f"{result.name}:{result.error}")
        if idx % 5 == 0 or idx == len(task_ids):
            print(
                f"[DB] {idx}/{len(task_ids)} done "
                f"(success={summary['success']} fallback={summary['fallback_success']} failed={summary['failed']})"
            )
    return summary


async def _process_archive_transcript(
    transcript_path: Path,
    system_prompt: str,
    sem: asyncio.Semaphore,
) -> WorkResult:
    async with sem:
        try:
            result_path = (
                settings.dingtalk_audio_stage_path
                / "results"
                / transcript_path.name.replace(".transcript.json", ".result.json")
            )
            existing_result = {}
            if result_path.is_file():
                try:
                    existing_result = json.loads(result_path.read_text(encoding="utf-8"))
                except Exception:
                    existing_result = {}
            try:
                result_dict = await asyncio.to_thread(_analyze_to_dict, transcript_path, system_prompt)
                mode = "fresh"
            except Exception:
                result_dict = await asyncio.to_thread(_rebuild_from_existing, transcript_path, existing_result)
                mode = "fallback"
            result_path = (
                settings.dingtalk_audio_stage_path
                / "results"
                / transcript_path.name.replace(".transcript.json", ".result.json")
            )
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(json.dumps(result_dict, ensure_ascii=False, indent=2), encoding="utf-8")
            return WorkResult(True, transcript_path.name, mode=mode)
        except Exception as exc:
            return WorkResult(False, transcript_path.name, str(exc), mode="failed")


async def rerun_archive_results(system_prompt: str) -> dict:
    transcript_dir = settings.dingtalk_audio_stage_path / "transcripts"
    files = sorted(transcript_dir.glob("*.transcript.json"))
    summary = {"total": len(files), "success": 0, "failed": 0, "fallback_success": 0, "failed_items": []}
    print(f"[ARCHIVE] rerun {len(files)} archive transcripts with concurrency={MAX_CONCURRENCY}")

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    coros = [_process_archive_transcript(path, system_prompt, sem) for path in files]
    for idx, future in enumerate(asyncio.as_completed(coros), start=1):
        result = await future
        if result.ok:
            summary["success"] += 1
            if result.mode == "fallback":
                summary["fallback_success"] += 1
        else:
            summary["failed"] += 1
            summary["failed_items"].append(f"{result.name}:{result.error}")
        if idx % 5 == 0 or idx == len(files):
            print(
                f"[ARCHIVE] {idx}/{len(files)} done "
                f"(success={summary['success']} fallback={summary['fallback_success']} failed={summary['failed']})"
            )
    return summary


async def main() -> None:
    async with _session_factory() as db:
        system_prompt = await build_system_prompt(db)

    mode = os.getenv("BATCH_RERUN_MODE", "all").strip().lower()
    summary = {}
    if mode in {"all", "db"}:
        summary["db"] = await rerun_db_tasks(system_prompt)
    if mode in {"all", "archive"}:
        summary["archive"] = await rerun_archive_results(system_prompt)
    out = Path("/opt/batch_rerun_new_analysis_summary.json")
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"summary written to {out}")


if __name__ == "__main__":
    asyncio.run(main())
