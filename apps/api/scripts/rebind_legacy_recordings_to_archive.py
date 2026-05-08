from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from smart_badge_api.api.analysis_normalization import normalize_analysis_result
from smart_badge_api.asr.service import execute_segmentation
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import AnalysisTask, Recording, RecordingVisitLink, Transcript, _new_id
from smart_badge_api.db.session import _session_factory
from smart_badge_api.visit_linking import sync_recording_visit_links


@dataclass(slots=True)
class ArchiveManifest:
    stage_key: str
    staged_file_name: str
    audio_path: Path
    transcript_path: Path
    analysis_result_path: Path | None
    analysis_input_path: Path | None
    staff_id: str | None
    device_id: str | None
    device_code: str | None
    remote_created_at: datetime
    duration_seconds: int | None
    file_size: int | None
    status: str | None
    updated_at: datetime | None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebind legacy linked recordings to dingtalk archive recordings.")
    parser.add_argument("--apply", action="store_true", help="Persist changes. Without this flag, run in dry-run mode.")
    return parser.parse_args()


def _parse_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_archive_manifests(root: Path) -> dict[tuple[datetime, str | None], ArchiveManifest]:
    manifests: dict[tuple[datetime, str | None], ArchiveManifest] = {}
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        remote_created_at = _parse_datetime(payload.get("remoteCreatedAt"))
        staged_file_name = str(payload.get("stagedFileName") or "").strip()
        transcript_path = Path(str(payload.get("transcriptPath") or ""))
        if remote_created_at is None or not staged_file_name or not transcript_path.is_file():
            continue
        key = (remote_created_at, str(payload.get("staffId") or "").strip() or None)
        manifests[key] = ArchiveManifest(
            stage_key=str(payload.get("stageKey") or path.stem),
            staged_file_name=staged_file_name,
            audio_path=Path(str(payload.get("audioPath") or "")),
            transcript_path=transcript_path,
            analysis_result_path=Path(str(payload.get("analysisResultPath") or "")) if payload.get("analysisResultPath") else None,
            analysis_input_path=Path(str(payload.get("analysisInputPath") or "")) if payload.get("analysisInputPath") else None,
            staff_id=str(payload.get("staffId") or "").strip() or None,
            device_id=str(payload.get("deviceId") or "").strip() or None,
            device_code=str(payload.get("deviceCode") or "").strip() or None,
            remote_created_at=remote_created_at,
            duration_seconds=payload.get("durationSeconds"),
            file_size=payload.get("fileSize"),
            status=str(payload.get("status") or "").strip() or None,
            updated_at=_parse_datetime(payload.get("updatedAt")),
        )
    return manifests


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _copy_if_exists(src: Path | None, dest: Path) -> bool:
    if src is None or not src.is_file():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return True


async def _ensure_archive_recording(
    legacy_recording: Recording,
    manifest: ArchiveManifest,
    *,
    apply_changes: bool,
) -> tuple[str, str, bool]:
    settings = get_settings()
    transcript_payload = _read_json(manifest.transcript_path) or {}
    utterances = transcript_payload.get("utterances") if isinstance(transcript_payload.get("utterances"), list) else []
    full_text = str(transcript_payload.get("fullText") or "").strip()
    duration_ms = transcript_payload.get("durationMs")
    asr_provider = str(transcript_payload.get("asrProvider") or "archive_import").strip() or "archive_import"
    analysis_result = normalize_analysis_result(_read_json(manifest.analysis_result_path)) if manifest.analysis_result_path else None
    consultation_evaluation = analysis_result.get("consultation_evaluation") if isinstance(analysis_result, dict) else None
    overall_score = None
    if isinstance(consultation_evaluation, dict) and consultation_evaluation.get("overall_score") is not None:
        try:
            overall_score = float(consultation_evaluation["overall_score"])
        except (TypeError, ValueError):
            overall_score = None

    async with _session_factory() as db:
        existing = (
            await db.execute(
                select(Recording)
                .where(Recording.file_name == manifest.staged_file_name)
                .options(selectinload(Recording.transcript), selectinload(Recording.visit_links))
                .order_by(Recording.created_at.desc())
            )
        ).scalars().first()

        created = False
        if existing is None:
            existing = Recording(
                id=_new_id(),
                file_name=manifest.staged_file_name,
                file_path=settings.make_relative_path(manifest.audio_path),
                file_size=manifest.file_size,
                duration_seconds=manifest.duration_seconds,
                status="uploaded",
                staff_id=manifest.staff_id or legacy_recording.staff_id,
                device_id=manifest.device_id or manifest.device_code or legacy_recording.device_id,
                created_at=manifest.remote_created_at,
                updated_at=manifest.updated_at or manifest.remote_created_at,
            )
            db.add(existing)
            await db.flush()
            created = True
        else:
            existing.file_path = settings.make_relative_path(manifest.audio_path)
            existing.file_size = manifest.file_size
            existing.duration_seconds = manifest.duration_seconds
            existing.staff_id = manifest.staff_id or existing.staff_id or legacy_recording.staff_id
            existing.device_id = manifest.device_id or manifest.device_code or existing.device_id or legacy_recording.device_id
            existing.updated_at = manifest.updated_at or existing.updated_at

        transcript = (
            await db.execute(select(Transcript).where(Transcript.recording_id == existing.id))
        ).scalar_one_or_none()
        if transcript is None:
            transcript = Transcript(recording_id=existing.id)
            db.add(transcript)
        transcript.asr_provider = asr_provider
        transcript.asr_task_id = manifest.stage_key
        transcript.status = "completed"
        transcript.full_text = full_text
        transcript.utterances = utterances
        transcript.duration_ms = duration_ms
        transcript.error_message = None
        transcript.completed_at = manifest.updated_at or manifest.remote_created_at

        existing.transcript_text = full_text
        existing.transcript_segments = utterances
        existing.status = "analyzed" if analysis_result else "transcribed"

        analysis_file_name = f"recording_{existing.id}.json"
        task = (
            await db.execute(
                select(AnalysisTask)
                .where(AnalysisTask.file_name == analysis_file_name)
                .order_by(AnalysisTask.created_at.desc())
            )
        ).scalars().first()
        if task is None:
            task = AnalysisTask(
                file_name=analysis_file_name,
                file_path=settings.make_relative_path(settings.upload_path / "analysis_input" / analysis_file_name),
            )
            db.add(task)

        task.status = "done" if analysis_result else "pending"
        task.progress = 100 if analysis_result else 0
        task.error_message = None
        task.result = analysis_result
        task.duration_ms = duration_ms
        task.segment_count = len(utterances)
        task.overall_score = overall_score
        task.completed_at = manifest.updated_at or manifest.remote_created_at if analysis_result else None

        if apply_changes:
            input_dest = settings.upload_path / "analysis_input" / analysis_file_name
            result_dest = settings.results_path / f"recording_{existing.id}.result.json"
            _copy_if_exists(manifest.analysis_input_path, input_dest)
            if analysis_result:
                if not result_dest.parent.exists():
                    result_dest.parent.mkdir(parents=True, exist_ok=True)
                if manifest.analysis_result_path and manifest.analysis_result_path.is_file():
                    shutil.copyfile(manifest.analysis_result_path, result_dest)
                else:
                    result_dest.write_text(json.dumps(analysis_result, ensure_ascii=False), encoding="utf-8")
            await db.commit()
        else:
            await db.rollback()

        recording_id = existing.id
        recording_file_name = existing.file_name

    if apply_changes:
        await execute_segmentation(recording_id)

    return recording_id, recording_file_name, created


async def main() -> None:
    args = _parse_args()
    settings = get_settings()
    manifest_root = settings.dingtalk_audio_stage_path / "manifests"
    manifest_map = _load_archive_manifests(manifest_root)

    async with _session_factory() as db:
        legacy_recordings = (
            await db.execute(
                select(Recording)
                .where(Recording.file_name.like("audio_%"))
                .options(
                    selectinload(Recording.visit_links).selectinload(RecordingVisitLink.visit),
                    selectinload(Recording.staff),
                )
                .order_by(Recording.file_name.asc())
            )
        ).scalars().all()
        legacy_recordings = [recording for recording in legacy_recordings if recording.visit_links]

    summary: dict[str, Any] = {
        "dry_run": not args.apply,
        "legacy_linked_recordings": len(legacy_recordings),
        "matched": 0,
        "migrated": 0,
        "created_new_recordings": 0,
        "reused_existing_archive_recordings": 0,
        "unmatched": [],
        "items": [],
    }

    for legacy in legacy_recordings:
        key = (legacy.created_at, legacy.staff_id)
        manifest = manifest_map.get(key)
        if manifest is None:
            summary["unmatched"].append(
                {
                    "legacy_recording_id": legacy.id,
                    "legacy_file_name": legacy.file_name,
                    "created_at": legacy.created_at.isoformat() if legacy.created_at else None,
                    "staff_id": legacy.staff_id,
                }
            )
            continue

        summary["matched"] += 1
        archive_recording_id, archive_file_name, created = await _ensure_archive_recording(
            legacy,
            manifest,
            apply_changes=args.apply,
        )

        linked_visit_ids = [link.visit_id for link in legacy.visit_links if link.visit_id]
        primary_visit_id = legacy.visit_id or next((link.visit_id for link in legacy.visit_links if link.is_primary and link.visit_id), None)

        if args.apply:
            async with _session_factory() as db:
                old_recording = (
                    await db.execute(
                        select(Recording)
                        .where(Recording.id == legacy.id)
                        .options(selectinload(Recording.visit_links))
                    )
                ).scalar_one()
                new_recording = (
                    await db.execute(
                        select(Recording)
                        .where(Recording.id == archive_recording_id)
                        .options(selectinload(Recording.visit_links))
                    )
                ).scalar_one()
                await sync_recording_visit_links(db, new_recording, linked_visit_ids, primary_visit_id=primary_visit_id, source="archive_rebind")
                await sync_recording_visit_links(db, old_recording, [], primary_visit_id=None, source="archive_rebind")
                await db.commit()

        summary["migrated"] += 1
        if created:
            summary["created_new_recordings"] += 1
        else:
            summary["reused_existing_archive_recordings"] += 1

        summary["items"].append(
            {
                "legacy_recording_id": legacy.id,
                "legacy_file_name": legacy.file_name,
                "legacy_visit_ids": linked_visit_ids,
                "archive_recording_id": archive_recording_id,
                "archive_file_name": archive_file_name,
                "manifest_stage_key": manifest.stage_key,
                "created_archive_recording": created,
            }
        )

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
