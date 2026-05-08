from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from smart_badge_api.analysis.pipeline import analyze_transcript
from smart_badge_api.analysis.prompt_builder import build_system_prompt
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.session import _session_factory
from smart_badge_api.dingtalk_audio_sync import (
    _ensure_recording_stub_from_manifest,
    _post_analysis_quality_decision,
    _sync_recording_analysis_task,
    _sync_recording_transcript,
)
from smart_badge_api.recording_analysis_service import (
    build_analysis_segment,
    refine_utterances_for_analysis,
)


TZ = ZoneInfo("Asia/Shanghai")
TARGET_DATES = {
    item.strip()
    for item in os.getenv("POST_ASR_RERUN_DATES", "2026-04-23,2026-04-24").split(",")
    if item.strip()
}
MAX_CONCURRENCY = max(int(os.getenv("POST_ASR_RERUN_CONCURRENCY", "2")), 1)
SOURCE_STAGE_ROOT = os.getenv("POST_ASR_SOURCE_STAGE_ROOT", "").strip()
SUMMARY_PATH = os.getenv("POST_ASR_RERUN_SUMMARY", "").strip()
STAMP = datetime.now(TZ).strftime("%Y%m%dT%H%M%S%z")

logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass(slots=True)
class Target:
    key: str
    manifest_path: Path
    manifest: dict
    created_at: datetime


@dataclass(slots=True)
class WorkResult:
    ok: bool
    key: str
    status: str
    segments: int = 0
    seeded_transcript: bool = False
    error: str | None = None


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _coerce_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: object) -> datetime | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(TZ)
    except ValueError:
        return None


def _target_created_at(manifest: dict) -> datetime | None:
    return _parse_dt(manifest.get("remoteCreatedAt")) or _parse_dt(manifest.get("createdAt"))


def _stage_key(manifest: dict, fallback: str) -> str:
    key = _clean_text(manifest.get("stageKey"))
    if key:
        return key
    device_code = _clean_text(manifest.get("deviceCode"))
    file_id = _clean_text(manifest.get("fileId"))
    if device_code and file_id:
        return f"{device_code}__{file_id}"
    return fallback


def _load_targets(stage_root: Path) -> list[Target]:
    targets: list[Target] = []
    for manifest_path in sorted((stage_root / "manifests").glob("*.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(manifest, dict):
            continue
        created_at = _target_created_at(manifest)
        if created_at is None or created_at.strftime("%Y-%m-%d") not in TARGET_DATES:
            continue
        targets.append(
            Target(
                key=_stage_key(manifest, manifest_path.stem),
                manifest_path=manifest_path,
                manifest=manifest,
                created_at=created_at,
            )
        )
    return sorted(targets, key=lambda item: (item.created_at, item.key))


def _backup(path: Path) -> None:
    if path.is_file():
        backup_path = path.with_name(f"{path.name}.bak.{STAMP}")
        shutil.copy2(path, backup_path)


def _write_json(path: Path, payload: dict, *, indent: int | None = 2, backup: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup:
        _backup(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")


def _local_transcript_path(stage_root: Path, key: str) -> Path:
    return stage_root / "transcripts" / f"{key}.transcript.json"


def _seed_transcript_from_source(stage_root: Path, target: Target) -> bool:
    if not SOURCE_STAGE_ROOT:
        return False
    source_stage_root = Path(SOURCE_STAGE_ROOT)
    source_path = source_stage_root / "transcripts" / f"{target.key}.transcript.json"
    if not source_path.is_file():
        return False

    payload = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return False

    manifest = target.manifest
    payload["stageKey"] = target.key
    payload["deviceCode"] = manifest.get("deviceCode") or manifest.get("sn") or payload.get("deviceCode")
    payload["fileId"] = manifest.get("fileId") or payload.get("fileId")
    payload["remoteFileName"] = manifest.get("remoteFileName") or payload.get("remoteFileName")
    payload["audioPath"] = manifest.get("audioPath") or payload.get("audioPath")
    if manifest.get("audioQualityDiagnostic"):
        payload["audioQualityDiagnostic"] = manifest.get("audioQualityDiagnostic")

    local_path = _local_transcript_path(stage_root, target.key)
    _write_json(local_path, payload, indent=2, backup=False)
    return True


def _load_transcript(stage_root: Path, target: Target) -> tuple[Path, dict, bool]:
    transcript_path = _local_transcript_path(stage_root, target.key)
    seeded = False
    if not transcript_path.is_file():
        seeded = _seed_transcript_from_source(stage_root, target)
    if not transcript_path.is_file():
        raise FileNotFoundError(f"transcript not found: {transcript_path}")
    payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid transcript: {transcript_path}")
    return transcript_path, payload, seeded


def _build_payload_from_transcript(
    transcript: dict,
    manifest: dict,
) -> tuple[dict, list[dict], int, int]:
    raw_utterances = transcript.get("utterances")
    utterances = [item for item in raw_utterances if isinstance(item, dict)] if isinstance(raw_utterances, list) else []
    refined = refine_utterances_for_analysis(
        utterances,
        staff_id=_clean_text(manifest.get("staffId")),
        staff_name=_clean_text(manifest.get("staffName")),
        staff_role=_clean_text(manifest.get("staffRole")),
    )
    segments = []
    for item in refined:
        segment = build_analysis_segment(item)
        if segment is not None:
            segments.append(segment)
    segments.sort(key=lambda value: (value["begin"], value["end"]))
    duration_ms = segments[-1]["end"] if segments else (_coerce_int(transcript.get("durationMs")) or 0)
    return {"payload": {"transcribeResult": segments}}, refined, len(segments), duration_ms


def _full_text_from_transcript(transcript: dict, utterances: list[dict]) -> str:
    full_text = _clean_text(transcript.get("fullText"))
    if full_text:
        return full_text
    return " ".join(_clean_text(item.get("text")) for item in utterances if isinstance(item, dict)).strip()


def _run_analysis(input_path: Path, system_prompt: str) -> dict:
    return analyze_transcript(input_path, system_prompt=system_prompt).model_dump(mode="json")


async def _process_target(
    stage_root: Path,
    target: Target,
    system_prompt: str,
    sem: asyncio.Semaphore,
) -> WorkResult:
    async with sem:
        try:
            manifest = dict(target.manifest)
            transcript_path, transcript, seeded = _load_transcript(stage_root, target)
            payload, refined_utterances, segment_count, duration_ms = _build_payload_from_transcript(
                transcript,
                manifest,
            )
            if segment_count <= 0:
                raise ValueError("analysis input has no valid segments")

            full_text = _full_text_from_transcript(transcript, refined_utterances)
            provider = _clean_text(transcript.get("asrProvider")) or get_settings().asr_provider

            transcript["utterances"] = refined_utterances
            transcript["fullText"] = full_text
            transcript["durationMs"] = duration_ms or transcript.get("durationMs")
            transcript["asrProvider"] = provider
            _write_json(transcript_path, transcript, indent=2, backup=not seeded)

            analysis_input_path = stage_root / "analysis_input" / f"{target.key}.json"
            _write_json(analysis_input_path, payload, indent=None)

            result_dict = await asyncio.to_thread(_run_analysis, analysis_input_path, system_prompt)
            result_path = stage_root / "results" / f"{target.key}.result.json"
            _write_json(result_path, result_dict, indent=2)

            quality_decision = _post_analysis_quality_decision(result_dict)
            status = "analyzed" if quality_decision.passed else "filtered"
            now = datetime.now(timezone.utc).isoformat()
            manifest.update(
                {
                    "stageKey": target.key,
                    "status": status,
                    "updatedAt": now,
                    "transcriptPath": str(transcript_path),
                    "analysisInputPath": str(analysis_input_path),
                    "analysisResultPath": str(result_path),
                    "fullTextLength": len(full_text),
                    "utteranceCount": len(refined_utterances),
                    "durationMs": duration_ms or manifest.get("durationMs"),
                }
            )
            if quality_decision.passed:
                manifest.pop("qualityStage", None)
                manifest.pop("qualityReason", None)
            else:
                manifest["qualityStage"] = quality_decision.stage
                manifest["qualityReason"] = quality_decision.reason
            manifest.pop("errorMessage", None)
            _write_json(target.manifest_path, manifest, indent=2)

            async with _session_factory() as db:
                recording = await _ensure_recording_stub_from_manifest(db, manifest, status=status)
                if recording is not None:
                    await _sync_recording_transcript(
                        db,
                        recording,
                        manifest=manifest,
                        utterances=refined_utterances,
                        full_text=full_text,
                        duration_ms=duration_ms,
                        provider=provider,
                    )
                    recording_input_path = get_settings().upload_path / "analysis_input" / f"recording_{recording.id}.json"
                    _write_json(recording_input_path, payload, indent=None)
                    recording_result_path = get_settings().results_path / f"recording_{recording.id}.result.json"
                    _write_json(recording_result_path, result_dict, indent=2)
                    await _sync_recording_analysis_task(
                        db,
                        recording,
                        result_dict=result_dict,
                        duration_ms=duration_ms,
                        utterance_count=segment_count,
                    )
                await db.commit()

            return WorkResult(True, target.key, status, segment_count, seeded)
        except Exception as exc:
            return WorkResult(False, target.key, "failed", error=str(exc))


async def main() -> None:
    settings = get_settings()
    stage_root = settings.dingtalk_audio_stage_path
    targets = _load_targets(stage_root)
    print(
        f"[post-asr] stage_root={stage_root} dates={sorted(TARGET_DATES)} "
        f"targets={len(targets)} concurrency={MAX_CONCURRENCY}"
    )
    async with _session_factory() as db:
        system_prompt = await build_system_prompt(db)

    summary: dict[str, object] = {
        "stage_root": str(stage_root),
        "dates": sorted(TARGET_DATES),
        "total": len(targets),
        "success": 0,
        "failed": 0,
        "filtered": 0,
        "seeded_transcripts": 0,
        "items": [],
    }
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    tasks = [_process_target(stage_root, target, system_prompt, sem) for target in targets]
    for index, future in enumerate(asyncio.as_completed(tasks), start=1):
        result = await future
        item = {
            "key": result.key,
            "ok": result.ok,
            "status": result.status,
            "segments": result.segments,
            "seededTranscript": result.seeded_transcript,
            "error": result.error,
        }
        summary["items"].append(item)
        if result.ok:
            summary["success"] = int(summary["success"]) + 1
            if result.status == "filtered":
                summary["filtered"] = int(summary["filtered"]) + 1
            if result.seeded_transcript:
                summary["seeded_transcripts"] = int(summary["seeded_transcripts"]) + 1
        else:
            summary["failed"] = int(summary["failed"]) + 1
        print(
            f"[post-asr] {index}/{len(targets)} {result.key} "
            f"ok={result.ok} status={result.status} segments={result.segments} error={result.error or ''}"
        )

    out = Path(SUMMARY_PATH) if SUMMARY_PATH else stage_root / f"post_asr_rerun_summary_{STAMP}.json"
    _write_json(out, summary, indent=2, backup=False)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[post-asr] summary written to {out}")


if __name__ == "__main__":
    asyncio.run(main())
