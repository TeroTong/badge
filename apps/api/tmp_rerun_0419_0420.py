from __future__ import annotations
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from sqlalchemy import select

from smart_badge_api.analysis.pipeline import analyze_transcript
from smart_badge_api.analysis.prompt_builder import build_system_prompt
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import AnalysisTask, Recording
from smart_badge_api.db.session import _session_factory
from tmp_batch_rerun_new_analysis import _rebuild_from_existing

settings = get_settings()
TARGET_PREFIXES = ('0419_', '0420_')
TARGET_START_UTC = datetime(2026, 4, 18, 16, 0, 0, tzinfo=timezone.utc)
TARGET_END_UTC = datetime(2026, 4, 20, 16, 0, 0, tzinfo=timezone.utc)


def _result_path_for_task(file_path: str) -> Path:
    return settings.results_path / f"{Path(file_path).stem}.result.json"


def _normalize_archive_path(raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else (Path('/opt/badge/apps/api') / path)


async def main() -> None:
    async with _session_factory() as db:
        prompt = await build_system_prompt(db)
        recordings = list((await db.execute(
            select(Recording.id, Recording.file_name)
            .where(Recording.created_at >= TARGET_START_UTC, Recording.created_at < TARGET_END_UTC)
            .order_by(Recording.created_at.asc())
        )).all())

    db_summary = {"total": 0, "success": 0, "fallback": 0, "failed": 0, "items": []}
    for recording_id, file_name in recordings:
        db_summary["total"] += 1
        async with _session_factory() as db:
            task = (await db.execute(
                select(AnalysisTask)
                .where(AnalysisTask.file_name == f'recording_{recording_id}.json')
                .order_by(AnalysisTask.created_at.desc())
                .limit(1)
            )).scalar_one_or_none()
            recording = await db.get(Recording, recording_id)
            if task is None or recording is None:
                db_summary["failed"] += 1
                db_summary["items"].append({"file": file_name, "status": "missing_task"})
                continue
            task_id = task.id
            existing_result = dict(task.result or {}) if isinstance(task.result, dict) else {}
            transcript_path = settings.resolve_file_path(task.file_path)

        try:
            result_dict = analyze_transcript(transcript_path, system_prompt=prompt).model_dump(mode='json')
            mode = 'fresh'
        except Exception:
            result_dict = _rebuild_from_existing(transcript_path, existing_result)
            mode = 'fallback'

        result_path = _result_path_for_task(str(transcript_path))
        result_path.write_text(json.dumps(result_dict, ensure_ascii=False, indent=2), encoding='utf-8')
        score = result_dict.get('consultation_process_evaluation', {}).get('overall_score')
        if not isinstance(score, (int, float)):
            score = result_dict.get('consultation_evaluation', {}).get('overall_score')

        async with _session_factory() as db:
            task = await db.get(AnalysisTask, task_id)
            recording = await db.get(Recording, recording_id)
            if task is None:
                raise RuntimeError(f'missing task {task_id}')
            task.status = 'done'
            task.progress = 100
            task.error_message = None
            task.result = result_dict
            task.overall_score = float(score) if isinstance(score, (int, float)) else None
            task.completed_at = datetime.now(timezone.utc)
            if recording is not None:
                recording.status = 'analyzed'
            await db.commit()

        db_summary['success'] += 1
        if mode == 'fallback':
            db_summary['fallback'] += 1
        db_summary['items'].append({"file": file_name, "status": mode})
        print(f"[DB] {file_name}: {mode}", flush=True)

    manifest_by_file_id: dict[str, dict] = {}
    for manifest_path in (settings.dingtalk_audio_stage_path / 'manifests').glob('*.json'):
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        except Exception:
            continue
        file_id = str(manifest.get('fileId') or '').strip()
        if file_id:
            manifest_by_file_id[file_id] = manifest

    archive_root = settings.dingtalk_audio_stage_path / 'archive'
    archive_files: list[Path] = []
    for prefix in TARGET_PREFIXES:
        archive_files.extend(sorted(archive_root.glob(f'*/202604/{prefix}*.json')))
    archive_summary = {"total": len(set(archive_files)), "success": 0, "fallback": 0, "failed": 0, "items": []}
    for archive_meta in sorted(set(archive_files)):
        meta = json.loads(archive_meta.read_text(encoding='utf-8'))
        file_name = archive_meta.with_suffix('.mp3').name
        file_id = str(meta.get('fileId') or '').strip()
        manifest = manifest_by_file_id.get(file_id, {})
        transcript_path = _normalize_archive_path(manifest.get('transcriptPath'))
        stage_key = transcript_path.name.replace('.transcript.json', '') if transcript_path else None
        if transcript_path is None or not transcript_path.exists() or not stage_key:
            archive_summary['failed'] += 1
            archive_summary['items'].append({"file": file_name, "status": "missing_transcript"})
            continue
        result_path = settings.dingtalk_audio_stage_path / 'results' / f'{stage_key}.result.json'
        existing_result = {}
        if result_path.is_file():
            try:
                existing_result = json.loads(result_path.read_text(encoding='utf-8'))
            except Exception:
                existing_result = {}
        try:
            result_dict = analyze_transcript(transcript_path, system_prompt=prompt).model_dump(mode='json')
            mode = 'fresh'
        except Exception:
            result_dict = _rebuild_from_existing(transcript_path, existing_result)
            mode = 'fallback'
        result_path.write_text(json.dumps(result_dict, ensure_ascii=False, indent=2), encoding='utf-8')
        manifest_path = settings.dingtalk_audio_stage_path / 'manifests' / f'{stage_key}.json'
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            manifest['analysisResultPath'] = str(result_path)
            manifest['status'] = 'analyzed'
            manifest.pop('errorMessage', None)
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        archive_summary['success'] += 1
        if mode == 'fallback':
            archive_summary['fallback'] += 1
        archive_summary['items'].append({"file": file_name, "status": mode})
        print(f"[ARCHIVE] {file_name}: {mode}", flush=True)

    summary = {"db": db_summary, "archive": archive_summary}
    out = Path('/opt/badge/rerun_0419_0420_summary.json')
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f'summary written to {out}', flush=True)

asyncio.run(main())
