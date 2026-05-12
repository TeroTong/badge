from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiofiles
import httpx
from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.analysis.consultation_evaluation import (
    rebuild_consultation_evaluation,
    rebuild_consultation_process_evaluation,
)
from smart_badge_api.analysis.pipeline import analyze_transcript, sanitize_analysis_result_with_raw
from smart_badge_api.analysis.prompt_builder import build_system_prompt
from smart_badge_api.api.analysis_normalization import normalize_analysis_result
from smart_badge_api.api.audit import append_audit_log
from smart_badge_api.asr.audio_preprocessing import diagnose_audio_quality
from smart_badge_api.asr.service import transcribe_audio_file
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import AnalysisTask, AuditLog, Device, Recording, RecordingVisitLink, Staff, Transcript
from smart_badge_api.db.session import _session_factory
from smart_badge_api.device_binding import load_device_staff_history, resolve_device_staff_binding
from smart_badge_api.sap_consultation import attach_unlinked_sap_preview_to_result
from smart_badge_api.dingtalk import (
    DingTalkApiError,
    DingTalkConfigError,
    dvi_get_audio_download_url,
    dvi_list_audio_files,
)
from smart_badge_api.dingtalk_iot import iot_list_audio_files, is_iot_hospital_code
from smart_badge_api.dingtalk_audio_quality import (
    DingTalkAudioQualityDecision as _QualityDecision,
    duration_ms_to_seconds,
    pre_asr_quality_decision,
)
from smart_badge_api.periodic_locks import DINGTALK_AUDIO_SYNC_LOCK_ID, periodic_advisory_lock
from smart_badge_api.recording_analysis_service import build_analysis_payload_from_utterances
from smart_badge_api.visit_order_card_recording_link import try_auto_link_visit_card_recording
from smart_badge_api.visit_order_sync import retry_visit_order_sync, sync_visit_orders_for_recording

logger = logging.getLogger("smart_badge.dingtalk_audio_sync")

AUDIO_SYNC_AUDIT_MODULE = "录音管理"
AUDIO_SYNC_AUDIT_ACTION = "钉钉音频同步"
AUDIO_SYNC_OPERATOR = "系统钉钉同步"
AUDIO_SYNC_IP = "dingtalk-sync"
_pipeline_semaphore: asyncio.Semaphore | None = None
_pipeline_semaphore_limit: int | None = None


async def _sync_visit_orders_for_recording_context(db: AsyncSession, recording_id: str) -> None:
    try:
        recording = await db.get(Recording, recording_id)
        if recording is None:
            return
        result = await retry_visit_order_sync(
            lambda: sync_visit_orders_for_recording(db, recording),
            label=f"dingtalk-recording-context:{recording_id}",
            attempts=3,
            initial_delay_seconds=1.0,
        )
        if result.new_count or result.updated_count:
            logger.info(
                "synced visit orders for DingTalk recording context recording_id=%s new=%d updated=%d",
                recording_id,
                result.new_count,
                result.updated_count,
            )
    except Exception:
        logger.exception("failed to sync visit orders for DingTalk recording context recording_id=%s", recording_id)


async def _recording_has_completed_analysis_task(db: AsyncSession, recording_id: str) -> bool:
    task_id = (
        await db.execute(
            select(AnalysisTask.id)
            .where(
                AnalysisTask.file_name == f"recording_{recording_id}.json",
                AnalysisTask.status == "done",
                AnalysisTask.result.is_not(None),
                cast(AnalysisTask.result, String) != "null",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return task_id is not None

SEMANTIC_SPEAKER_ROLES = {"consultant", "doctor", "customer"}
STAFF_SPEAKER_ROLES = {"consultant", "doctor"}
INTERNAL_DISCUSSION_KEYWORDS = {
    "排班",
    "复盘",
    "考勤",
    "会议",
    "开会",
    "同事",
    "员工",
    "培训",
    "审批",
    "报销",
    "业绩",
    "招聘",
    "群里",
    "内部流程",
    "值班",
    "打卡",
}
CUSTOMER_CONSULTATION_KEYWORDS = {
    "咨询",
    "项目",
    "效果",
    "恢复期",
    "预算",
    "价格",
    "多少钱",
    "想做",
    "想了解",
    "改善",
    "诉求",
    "皮肤",
    "法令纹",
    "面诊",
    "顾虑",
    "方案",
    "材料",
    "支撑力",
    "注射",
    "打针",
    "填充",
    "塑形",
    "鼻部",
    "鼻尖",
    "鼻梁",
    "鼻背",
    "鼻综合",
    "耳朵",
    "丰耳",
    "精灵耳",
    "艾拉斯提",
    "艾拉提斯",
    "艾提",
    "减龄",
    "减盐",
}
POST_ANALYSIS_MEDICAL_BUSINESS_KEYWORDS = {
    *CUSTOMER_CONSULTATION_KEYWORDS,
    "医美",
    "医生",
    "治疗",
    "手术",
    "微创",
    "注射",
    "打针",
    "瘦脸",
    "瘦脸针",
    "除皱",
    "肉毒",
    "玻尿酸",
    "水光",
    "美白",
    "抗衰",
    "提升",
    "填充",
    "脂肪",
    "吸脂",
    "双眼皮",
    "眼袋",
    "泪沟",
    "鼻基底",
    "鼻子",
    "痘",
    "痘印",
    "疤痕",
    "瘢痕",
    "脱毛",
}
POST_ANALYSIS_NON_BUSINESS_NEGATION_CUES = {
    "没有医美项目",
    "无医美项目",
    "不是医美项目",
    "不是来做医美",
    "不咨询医美",
    "不做医美",
}

_background_pipeline_tasks: set[asyncio.Task] = set()

# 钉钉录音流水线：有界队列 + 固定消费者池，避免无界 create_task 导致内存/连接堆积。
_pipeline_queue: asyncio.Queue[str] | None = None
_pipeline_consumers: list[asyncio.Task] = []
_pipeline_consumers_started: bool = False
_pipeline_queue_lock: asyncio.Lock | None = None


def _get_pipeline_queue_lock() -> asyncio.Lock:
    global _pipeline_queue_lock
    if _pipeline_queue_lock is None:
        _pipeline_queue_lock = asyncio.Lock()
    return _pipeline_queue_lock


@dataclass(slots=True)
class DingTalkAudioSyncItem:
    device_code: str
    file_name: str
    status: str
    message: str
    file_id: str | None = None
    stage_key: str | None = None


@dataclass(slots=True)
class DingTalkAudioSyncResult:
    imported: int = 0
    skipped: int = 0
    filtered: int = 0
    failed: int = 0
    queued: int = 0
    items: list[DingTalkAudioSyncItem] = field(default_factory=list)


@dataclass(slots=True)
class _StagePaths:
    root: Path
    audio_dir: Path
    transcript_dir: Path
    analysis_input_dir: Path
    result_dir: Path
    manifest_dir: Path


def _ensure_stage_paths() -> _StagePaths:
    root = get_settings().dingtalk_audio_stage_path
    audio_dir = root / "audio"
    transcript_dir = root / "transcripts"
    analysis_input_dir = root / "analysis_input"
    result_dir = root / "results"
    manifest_dir = root / "manifests"
    for path in (root, audio_dir, transcript_dir, analysis_input_dir, result_dir, manifest_dir):
        path.mkdir(parents=True, exist_ok=True)
    return _StagePaths(
        root=root,
        audio_dir=audio_dir,
        transcript_dir=transcript_dir,
        analysis_input_dir=analysis_input_dir,
        result_dir=result_dir,
        manifest_dir=manifest_dir,
    )


def _clean_text(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def _resolve_staff_hospital_code(db: AsyncSession, staff_id: str | None) -> str | None:
    if not staff_id:
        return None
    return (
        await db.execute(
            select(Staff.hospital_code)
            .where(Staff.id == staff_id)
            .limit(1)
        )
    ).scalar_one_or_none()


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, dict) and "value" in value:
        return _coerce_int(value["value"])
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
    return None


def _coerce_remote_timestamp(value: object) -> datetime | None:
    timestamp_ms = _coerce_int(value)
    if timestamp_ms is None or timestamp_ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    remote_value = _coerce_remote_timestamp(value)
    if remote_value is not None:
        return remote_value
    text = _clean_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _probe_audio_duration_seconds(audio_path: Path) -> int | None:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        logger.warning("ffprobe not found, skipping duration probe for %s", audio_path.name)
        return None
    except subprocess.CalledProcessError:
        logger.warning("ffprobe failed, skipping duration probe for %s", audio_path.name)
        return None

    text = result.stdout.strip()
    try:
        duration_seconds = float(text or 0.0)
    except ValueError:
        logger.warning("ffprobe returned invalid duration for %s", audio_path.name)
        return None

    if duration_seconds <= 0:
        return None
    return max(int(round(duration_seconds)), 1)


def _build_audio_quality_diagnostic_payload(
    audio_path: Path,
    *,
    duration_seconds: int | None,
) -> dict[str, Any] | None:
    if not get_settings().asr_audio_quality_diagnostics_enabled:
        return None
    try:
        return asdict(diagnose_audio_quality(audio_path, duration_seconds=duration_seconds))
    except Exception as exc:
        logger.warning("failed to build audio quality diagnostic for %s: %s", audio_path.name, exc)
        return None


def _safe_part(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("_") or "unknown"


def _stage_key(device_code: str, file_id: str) -> str:
    return f"{_safe_part(device_code)}__{_safe_part(file_id)}"


def _manifest_path(paths: _StagePaths, stage_key: str) -> Path:
    return paths.manifest_dir / f"{stage_key}.json"


def _read_manifest(paths: _StagePaths, stage_key: str) -> dict[str, Any] | None:
    path = _manifest_path(paths, stage_key)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest(paths: _StagePaths, manifest: dict[str, Any]) -> None:
    stage_key = str(manifest["stageKey"])
    manifest["updatedAt"] = datetime.now(timezone.utc).isoformat()
    _manifest_path(paths, stage_key).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _relative_path_for_audio(audio_path: Path) -> str:
    return get_settings().make_relative_path(audio_path)


def _resolve_stage_manifest_file_path(raw_value: object) -> Path | None:
    raw_path = _clean_text(raw_value)
    if not raw_path:
        return None

    settings = get_settings()
    original = Path(raw_path)
    candidates: list[Path] = []

    if original.is_absolute():
        candidates.append(original)
    else:
        candidates.append(settings.upload_path / original)
        candidates.append(settings.resolve_path(original))

    legacy_upload_root = Path("/app/uploads")
    legacy_results_root = Path("/app/results")
    if original.is_absolute():
        try:
            candidates.append(settings.upload_path / original.relative_to(legacy_upload_root))
        except ValueError:
            pass
        try:
            candidates.append(settings.results_path / original.relative_to(legacy_results_root))
        except ValueError:
            pass

    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)

    for candidate in deduped:
        if candidate.exists():
            return candidate.resolve()

    return deduped[0].resolve(strict=False) if deduped else None


async def _ensure_recording_stub_from_manifest(
    db: AsyncSession,
    manifest: dict[str, Any],
    *,
    status: str | None = None,
) -> Recording | None:
    audio_path = _resolve_stage_manifest_file_path(manifest.get("audioPath"))
    staged_file_name = _clean_text(manifest.get("stagedFileName")) or (audio_path.name if audio_path.name else None)
    if not staged_file_name or not audio_path:
        return None

    manifest["audioPath"] = str(audio_path)

    file_path = _relative_path_for_audio(audio_path)
    created_at = (
        _coerce_datetime(manifest.get("remoteCreatedAt"))
        or _coerce_datetime(manifest.get("createdAt"))
        or datetime.now(timezone.utc)
    )
    updated_at = _coerce_datetime(manifest.get("updatedAt")) or created_at
    duration_seconds = _coerce_int(manifest.get("durationSeconds"))
    if duration_seconds is None:
        duration_ms = _coerce_int(manifest.get("durationMs"))
        if duration_ms is not None and duration_ms > 0:
            duration_seconds = max(duration_ms // 1000, 1)

    resolved_staff_id = _clean_text(manifest.get("staffId"))
    if resolved_staff_id and await db.get(Staff, resolved_staff_id) is None:
        resolved_staff_id = None

    resolved_device_id = _clean_text(manifest.get("deviceId"))
    if resolved_device_id and await db.get(Device, resolved_device_id) is None:
        resolved_device_id = None
    if resolved_device_id is None:
        resolved_device_code = _clean_text(manifest.get("deviceCode"))
        if resolved_device_code:
            resolved_device_id = (
                await db.execute(select(Device.id).where(Device.device_code == resolved_device_code).limit(1))
            ).scalar_one_or_none()

    existing = (
        await db.execute(
            select(Recording)
            .where(or_(Recording.file_name == staged_file_name, Recording.file_path == file_path))
            .order_by(Recording.created_at.desc())
        )
    ).scalars().first()

    if existing is None:
        existing = Recording(
            file_name=staged_file_name,
            file_path=file_path,
            file_size=_coerce_int(manifest.get("fileSize")),
            duration_seconds=duration_seconds,
            status=status or "uploaded",
            staff_id=resolved_staff_id,
            device_id=resolved_device_id,
            created_at=created_at,
            updated_at=updated_at,
        )
        db.add(existing)
        await db.flush()
        return existing

    incoming_status = (_clean_text(status) or "").lower()
    existing_status = (_clean_text(existing.status) or "").lower()
    incoming_error = _clean_text(manifest.get("errorMessage"))
    analysis_result_path = _resolve_stage_manifest_file_path(manifest.get("analysisResultPath"))
    if incoming_status == "failed" and analysis_result_path and analysis_result_path.is_file():
        incoming_status = "analyzed"
        status = "analyzed"
    if (
        incoming_status
        and incoming_status not in {"analyzed", "filtered"}
        and existing_status != "filtered"
        and await _recording_has_completed_analysis_task(db, existing.id)
    ):
        # A later DingTalk rescan can see stale or incomplete manifest state.
        # The database analysis task is the source of truth once it has a result.
        incoming_status = "analyzed"
        status = "analyzed"
    if incoming_status == "failed" and existing_status in {"analyzed", "filtered"}:
        return existing
    if (
        incoming_status == "failed"
        and "录音文件不存在" in incoming_error
        and existing_status in {"analyzed", "filtered", "transcribed"}
        and not audio_path.is_file()
    ):
        # Test/prod may share one DB while keeping separate local audio folders.
        # A missing local staged file must not downgrade an already resolved record.
        return existing

    existing.file_path = file_path
    existing.file_size = _coerce_int(manifest.get("fileSize"))
    existing.duration_seconds = duration_seconds
    existing.staff_id = resolved_staff_id or existing.staff_id
    existing.device_id = resolved_device_id or existing.device_id
    existing.updated_at = updated_at
    if status:
        existing.status = status
    await db.flush()
    return existing


async def _sync_recording_transcript(
    db: AsyncSession,
    recording: Recording,
    *,
    manifest: dict[str, Any],
    utterances: list[dict],
    full_text: str,
    duration_ms: int,
    provider: str,
) -> None:
    transcript = (
        await db.execute(select(Transcript).where(Transcript.recording_id == recording.id))
    ).scalar_one_or_none()
    if transcript is None:
        transcript = Transcript(recording_id=recording.id)
        db.add(transcript)

    completed_at = _coerce_datetime(manifest.get("updatedAt")) or datetime.now(timezone.utc)
    transcript.asr_provider = provider
    transcript.asr_task_id = _clean_text(manifest.get("stageKey"))
    transcript.status = "completed"
    transcript.full_text = full_text
    transcript.utterances = utterances
    transcript.duration_ms = duration_ms
    transcript.error_message = None
    transcript.completed_at = completed_at

    recording.transcript_text = full_text
    recording.transcript_segments = utterances
    await db.flush()


def _extract_overall_score(result_dict: dict[str, Any] | None) -> float | None:
    if not isinstance(result_dict, dict):
        return None
    evaluation = result_dict.get("consultation_evaluation")
    if not isinstance(evaluation, dict):
        return None
    raw_score = evaluation.get("overall_score")
    if not isinstance(raw_score, (int, float)):
        return None
    return float(raw_score)


def _prepare_analysis_result_for_persistence(
    result_dict: dict[str, Any] | None,
    *,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(result_dict, dict):
        return result_dict

    prepared = deepcopy(result_dict)
    if isinstance(raw, dict):
        sanitize_analysis_result_with_raw(prepared, raw=raw)

    normalized = normalize_analysis_result(prepared) or prepared
    refreshed = dict(normalized)
    refreshed["consultation_evaluation"] = rebuild_consultation_evaluation(refreshed)
    refreshed["consultation_process_evaluation"] = rebuild_consultation_process_evaluation(refreshed)
    return refreshed


async def _sync_recording_analysis_task(
    db: AsyncSession,
    recording: Recording,
    *,
    result_dict: dict[str, Any] | None,
    raw: dict[str, Any] | None = None,
    duration_ms: int,
    utterance_count: int,
) -> None:
    settings = get_settings()
    analysis_file_name = f"recording_{recording.id}.json"
    result_dict = _prepare_analysis_result_for_persistence(result_dict, raw=raw)
    if result_dict:
        try:
            result_dict = await attach_unlinked_sap_preview_to_result(db, recording.id, result_dict) or result_dict
        except Exception as exc:
            logger.warning("failed to attach SAP preview to synced analysis result recording_id=%s: %s", recording.id, exc)
    if isinstance(raw, dict):
        analysis_input_path = settings.upload_path / "analysis_input" / analysis_file_name
        analysis_input_path.parent.mkdir(parents=True, exist_ok=True)
        analysis_input_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
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

    completed_at = datetime.now(timezone.utc)
    task.status = "done" if result_dict else "pending"
    task.progress = 100 if result_dict else 0
    task.error_message = None
    task.result = result_dict
    task.duration_ms = duration_ms
    task.segment_count = utterance_count
    task.overall_score = _extract_overall_score(result_dict)
    task.completed_at = completed_at if result_dict else None
    if result_dict:
        result_path = settings.results_path / f"recording_{recording.id}.result.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result_dict, ensure_ascii=False), encoding="utf-8")
    await db.flush()


def _load_json_object(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


async def _reconcile_completed_manifest_to_recording(
    db: AsyncSession,
    manifest: dict[str, Any],
) -> bool:
    status = (_clean_text(manifest.get("status")) or "").lower()
    if status not in {"transcribed", "analyzed"}:
        return False

    transcript_path = _resolve_stage_manifest_file_path(manifest.get("transcriptPath"))
    if transcript_path is None or not transcript_path.is_file():
        return False

    result_path = _resolve_stage_manifest_file_path(manifest.get("analysisResultPath"))
    if status == "analyzed" and (result_path is None or not result_path.is_file()):
        return False

    try:
        utterances, full_text, duration_ms, provider = _load_transcript_document(transcript_path)
    except (OSError, ValueError, TypeError):
        logger.exception("failed to load transcript while reconciling staged manifest stage_key=%s", manifest.get("stageKey"))
        return False

    recording = await _ensure_recording_stub_from_manifest(db, manifest, status=status)
    if recording is None:
        return False

    resolved_provider = provider or get_settings().asr_provider
    await _sync_recording_transcript(
        db,
        recording,
        manifest=manifest,
        utterances=utterances,
        full_text=full_text,
        duration_ms=duration_ms or _coerce_int(manifest.get("durationMs")) or 0,
        provider=resolved_provider,
    )

    if status == "analyzed":
        result_dict = _load_json_object(result_path)
        if result_dict is None:
            return False
        analysis_input_path = _resolve_stage_manifest_file_path(manifest.get("analysisInputPath"))
        raw_payload = _load_json_object(analysis_input_path)
        await _sync_recording_analysis_task(
            db,
            recording,
            result_dict=result_dict,
            raw=raw_payload,
            duration_ms=duration_ms or _coerce_int(manifest.get("durationMs")) or 0,
            utterance_count=len(utterances),
        )
        recording.status = "analyzed"
    else:
        recording.status = "transcribed"

    await try_auto_link_visit_card_recording(db, recording)
    await db.flush()
    return True


def clear_staged_device_staff_assignments(device_code: str) -> int:
    normalized_code = _clean_text(device_code)
    if not normalized_code:
        return 0

    paths = _ensure_stage_paths()
    updated = 0
    for manifest_path in paths.manifest_dir.glob("*.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(manifest, dict):
            continue
        if _clean_text(manifest.get("deviceCode")) != normalized_code:
            continue
        manifest["staffId"] = None
        manifest["staffName"] = ""
        manifest["staffRole"] = "consultant"
        _write_manifest(paths, manifest)
        updated += 1
    return updated


def _normalized_suffix(file_name: str | None) -> str:
    suffix = Path(file_name or "").suffix.lower()
    return suffix or ".mp3"


def _build_staged_audio_name(device_code: str, file_id: str, remote_file_name: str | None) -> str:
    return f"dingtalk_{_safe_part(device_code)}_{_safe_part(file_id)}{_normalized_suffix(remote_file_name)}"


def _pre_asr_quality_decision(duration_seconds: int | None) -> _QualityDecision:
    return pre_asr_quality_decision(duration_seconds)


def _normalize_speaker_label(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    return text


def _keyword_hit_count(text: str, keywords: set[str]) -> int:
    content = text.strip()
    if not content:
        return 0
    return sum(1 for keyword in keywords if keyword in content)


def _has_consultation_dialogue_signal(
    *,
    utterance_count: int,
    text_length: int,
    consultation_keyword_hits: int,
    internal_keyword_hits: int,
) -> bool:
    # Tencent diarization or local role resolution may occasionally collapse a
    # real consultation into a single speaker. In that case, use the transcript
    # semantics as a fallback so we do not hard-filter genuine consultations.
    if utterance_count < 6:
        return False
    if text_length < 100:
        return False
    if consultation_keyword_hits < 3:
        return False
    if internal_keyword_hits >= consultation_keyword_hits:
        return False
    return True


def _post_asr_quality_decision(utterances: list[dict], full_text: str) -> _QualityDecision:
    settings = get_settings()
    valid_utterances = [
        item
        for item in utterances
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    utterance_count = len(valid_utterances)
    if settings.dingtalk_audio_min_utterance_count > 0 and utterance_count < settings.dingtalk_audio_min_utterance_count:
        return _QualityDecision(
            False,
            f"有效发言仅 {utterance_count} 条，低于最小要求 {settings.dingtalk_audio_min_utterance_count} 条",
            "post_asr",
        )

    text_length = len(full_text.strip())
    if settings.dingtalk_audio_min_transcript_chars > 0 and text_length < settings.dingtalk_audio_min_transcript_chars:
        return _QualityDecision(
            False,
            f"转写文本长度仅 {text_length} 字，低于最小要求 {settings.dingtalk_audio_min_transcript_chars} 字",
            "post_asr",
        )

    internal_keyword_hits = _keyword_hit_count(full_text, INTERNAL_DISCUSSION_KEYWORDS)
    consultation_keyword_hits = _keyword_hit_count(full_text, CUSTOMER_CONSULTATION_KEYWORDS)
    consultation_dialogue_override = _has_consultation_dialogue_signal(
        utterance_count=utterance_count,
        text_length=text_length,
        consultation_keyword_hits=consultation_keyword_hits,
        internal_keyword_hits=internal_keyword_hits,
    )

    if settings.dingtalk_audio_require_multi_speaker:
        speakers = {
            _normalize_speaker_label(item.get("speaker"))
            for item in valid_utterances
            if str(item.get("speaker") or "").strip()
        }
        normalized_speakers = {
            speaker
            for speaker in speakers
            if speaker not in {"unknown", "unk", "speaker"}
        }
        if len(normalized_speakers) < 2 and not consultation_dialogue_override:
            return _QualityDecision(
                False,
                "转写结果未识别到明确的双人沟通，已按非客户沟通录音过滤",
                "post_asr",
            )

    normalized_speakers = {
        _normalize_speaker_label(item.get("speaker"))
        for item in valid_utterances
        if str(item.get("speaker") or "").strip()
    }
    semantic_roles = normalized_speakers & SEMANTIC_SPEAKER_ROLES
    if settings.dingtalk_audio_require_customer_role and semantic_roles:
        if (
            "customer" not in semantic_roles or not (semantic_roles & STAFF_SPEAKER_ROLES)
        ) and not consultation_dialogue_override:
            return _QualityDecision(
                False,
                "转写结果未识别到客户与接诊人员的有效沟通，已按非客户沟通录音过滤",
                "post_asr",
            )

    if internal_keyword_hits >= max(settings.dingtalk_audio_internal_keyword_threshold, 1):
        if consultation_keyword_hits == 0 or internal_keyword_hits >= consultation_keyword_hits + 2:
            return _QualityDecision(
                False,
                f"转写内容更像内部沟通，命中 {internal_keyword_hits} 个内部沟通关键词，已过滤",
                "post_asr",
            )

    return _QualityDecision(True)


def _standardized_indication_count(result_dict: dict[str, Any]) -> int:
    standardized = result_dict.get("standardized_indications")
    if isinstance(standardized, dict):
        items = standardized.get("items")
        if isinstance(items, list) and items:
            return len([item for item in items if isinstance(item, dict)])

    consultation_result = result_dict.get("consultation_result")
    if isinstance(consultation_result, dict):
        chief = consultation_result.get("chief_complaint_and_indications")
        if isinstance(chief, dict):
            items = chief.get("standardized_indications")
            if isinstance(items, list) and items:
                return len([item for item in items if str(item or "").strip()])
    return 0


def _result_has_non_empty_items(value: object) -> bool:
    def is_positive_text(raw: object) -> bool:
        text = _clean_text(raw)
        if not text:
            return False
        negative_markers = ("未识别", "未提及", "无明确", "暂无", "无相关", "未获取", "不明确")
        return not any(marker in text for marker in negative_markers) and text not in {"无", "无。", "-", "未知"}

    if isinstance(value, list):
        return any(
            bool(item)
            and (
                not isinstance(item, dict)
                or any(is_positive_text(nested_value) for nested_value in item.values() if not isinstance(nested_value, (dict, list)))
            )
            for item in value
        )
    if isinstance(value, dict):
        for key in ("items", "demands", "plans", "concerns", "factors", "tags"):
            if _result_has_non_empty_items(value.get(key)):
                return True
        return False
    return is_positive_text(value)


def _post_analysis_business_signal_reasons(
    result_dict: dict[str, Any],
    *,
    full_text: str = "",
    has_visit_link: bool = False,
) -> list[str]:
    reasons: list[str] = []
    if has_visit_link:
        reasons.append("已关联到诊单")

    field_checks = (
        ("customer_primary_demands", "已提取客户主诉"),
        ("customer_demands", "已提取客户需求画像"),
        ("staff_recommendations", "已提取推荐方案"),
        ("recommended_solutions", "已提取推荐方案"),
        ("decision_factors", "已提取成交影响因素"),
        ("deal_summary", "已提取成交/跟进信息"),
        ("sap_summary_materials", "已生成SAP咨询素材"),
    )
    for key, label in field_checks:
        if _result_has_non_empty_items(result_dict.get(key)):
            reasons.append(label)

    compact_text = re.sub(r"\s+", "", full_text)
    has_non_business_negation = any(cue in compact_text for cue in POST_ANALYSIS_NON_BUSINESS_NEGATION_CUES)
    keyword_hits = 0 if has_non_business_negation else _keyword_hit_count(full_text, POST_ANALYSIS_MEDICAL_BUSINESS_KEYWORDS)
    if keyword_hits >= 3:
        reasons.append(f"转写命中 {keyword_hits} 个医美业务关键词")

    deduped: list[str] = []
    for reason in reasons:
        if reason not in deduped:
            deduped.append(reason)
    return deduped


async def _manifest_has_visit_link(manifest: dict[str, Any]) -> bool:
    async with _session_factory() as db:
        recording = await _ensure_recording_stub_from_manifest(db, manifest)
        if recording is None:
            return False
        linked_id = (
            await db.execute(
                select(RecordingVisitLink.id)
                .where(RecordingVisitLink.recording_id == recording.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        await db.rollback()
        return linked_id is not None


def _post_analysis_quality_decision(
    result_dict: dict[str, Any],
    *,
    full_text: str = "",
    has_visit_link: bool = False,
) -> _QualityDecision:
    if _standardized_indication_count(result_dict) <= 0:
        signal_reasons = _post_analysis_business_signal_reasons(
            result_dict,
            full_text=full_text,
            has_visit_link=has_visit_link,
        )
        if signal_reasons:
            return _QualityDecision(
                True,
                "分析结果暂未映射到标准适应症，但存在业务信号，已保留待补充："
                + "、".join(signal_reasons),
                "post_analysis",
            )
        return _QualityDecision(
            False,
            "分析结果未识别到医美适应症，已按无效或非医美咨询录音过滤",
            "post_analysis",
        )
    return _QualityDecision(True)


def _analysis_payload_from_utterances(
    utterances: list[dict],
    *,
    manifest: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    manifest = manifest or {}
    payload, _segment_count, duration_ms = build_analysis_payload_from_utterances(
        utterances,
        staff_id=_clean_text(manifest.get("staffId")),
        staff_name=_clean_text(manifest.get("staffName")),
        staff_role=_clean_text(manifest.get("staffRole")),
    )
    return payload, duration_ms


def _build_transcript_document(
    manifest: dict[str, Any],
    *,
    utterances: list[dict],
    full_text: str,
    duration_ms: int,
    provider: str,
) -> dict[str, Any]:
    document = {
        "stageKey": manifest["stageKey"],
        "deviceCode": manifest["deviceCode"],
        "fileId": manifest["fileId"],
        "remoteFileName": manifest.get("remoteFileName"),
        "audioPath": manifest.get("audioPath"),
        "audioQualityDiagnostic": manifest.get("audioQualityDiagnostic"),
        "asrProvider": provider,
        "durationMs": duration_ms,
        "fullText": full_text,
        "utterances": utterances,
    }
    return document


def _load_transcript_document(path: Path) -> tuple[list[dict[str, Any]], str, int | None, str | None]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"转写文件格式无效：{path}")
    raw_utterances = payload.get("utterances")
    utterances = [item for item in raw_utterances if isinstance(item, dict)] if isinstance(raw_utterances, list) else []
    full_text = _clean_text(payload.get("fullText")) or ""
    duration_ms = _coerce_int(payload.get("durationMs"))
    provider = _clean_text(payload.get("asrProvider"))
    return utterances, full_text, duration_ms, provider


def _run_analysis_sync(file_path: str, system_prompt: str) -> dict[str, Any]:
    return analyze_transcript(file_path, system_prompt=system_prompt).model_dump()


async def _download_file(url: str, dest: Path) -> int:
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            async with aiofiles.open(dest, "wb") as handle:
                async for chunk in response.aiter_bytes():
                    if chunk:
                        await handle.write(chunk)
    return dest.stat().st_size


async def _list_all_audio_files_for_device(
    device_code: str,
    *,
    start_timestamp: int | None,
    end_timestamp: int | None,
    use_iot: bool = False,
) -> list[dict[str, Any]]:
    if use_iot:
        return await iot_list_audio_files(
            device_no=device_code,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )

    result: list[dict[str, Any]] = []
    next_token = ""
    for _ in range(50):
        page = await dvi_list_audio_files(
            device_code,
            max_results=get_settings().dingtalk_audio_sync_page_size,
            next_token=next_token,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
        items = page.get("result") or []
        result.extend(item for item in items if isinstance(item, dict))
        next_token = _clean_text(page.get("nextToken")) or ""
        if not next_token:
            break
    return result


async def _resolve_audio_download_url(remote_item: dict[str, Any], file_id: str) -> str | None:
    direct_url = _clean_text(remote_item.get("downloadUrl") or remote_item.get("fileUrl"))
    if direct_url:
        return direct_url

    download_payload = await dvi_get_audio_download_url(file_id)
    return _clean_text(
        download_payload.get("url")
        or download_payload.get("downloadUrl")
        or (download_payload.get("result") or {}).get("url")
        or (download_payload.get("result") or {}).get("downloadUrl")
    )


async def _mark_manifest_filtered(
    paths: _StagePaths,
    manifest: dict[str, Any],
    decision: _QualityDecision,
) -> None:
    manifest["status"] = "filtered"
    manifest["qualityStage"] = decision.stage
    manifest["qualityReason"] = decision.reason
    manifest.pop("errorMessage", None)
    _write_manifest(paths, manifest)
    async with _session_factory() as db:
        recording = await _ensure_recording_stub_from_manifest(db, manifest, status="filtered")
        if recording is not None:
            await try_auto_link_visit_card_recording(db, recording)
            await db.commit()


async def execute_dingtalk_recording_pipeline(stage_key: str) -> None:
    paths = _ensure_stage_paths()
    manifest = _read_manifest(paths, stage_key)
    if manifest is None:
        logger.warning("stage manifest %s not found", stage_key)
        return

    try:
        audio_path = _resolve_stage_manifest_file_path(manifest.get("audioPath"))
        if audio_path is None:
            raise FileNotFoundError(f"未找到录音文件路径：{manifest.get('audioPath')}")
        if str(manifest.get("audioPath")) != str(audio_path):
            manifest["audioPath"] = str(audio_path)
            _write_manifest(paths, manifest)
        if not audio_path.is_file():
            raise FileNotFoundError(f"录音文件不存在：{audio_path}")

        duration_seconds = _coerce_int(manifest.get("durationSeconds"))
        if duration_seconds is None:
            probed_duration_seconds = _probe_audio_duration_seconds(audio_path)
            if probed_duration_seconds is not None:
                duration_seconds = probed_duration_seconds
                manifest["durationSeconds"] = duration_seconds
                if _coerce_int(manifest.get("durationMs")) is None:
                    manifest["durationMs"] = duration_seconds * 1000
                _write_manifest(paths, manifest)

        diagnostic_payload = _build_audio_quality_diagnostic_payload(
            audio_path,
            duration_seconds=duration_seconds,
        )
        if diagnostic_payload is not None:
            manifest["audioQualityDiagnostic"] = diagnostic_payload
            if _coerce_int(manifest.get("durationMs")) is None and diagnostic_payload.get("duration_ms"):
                manifest["durationMs"] = diagnostic_payload["duration_ms"]
            if _coerce_int(manifest.get("durationSeconds")) is None and diagnostic_payload.get("duration_ms"):
                manifest["durationSeconds"] = max(int(diagnostic_payload["duration_ms"]) // 1000, 1)
            if duration_seconds is None and diagnostic_payload.get("duration_ms"):
                duration_seconds = max(int(diagnostic_payload["duration_ms"]) // 1000, 1)
            _write_manifest(paths, manifest)

        pre_decision = _pre_asr_quality_decision(duration_seconds)
        if not pre_decision.passed:
            await _mark_manifest_filtered(paths, manifest, pre_decision)
            return

        transcript_path = _resolve_stage_manifest_file_path(manifest.get("transcriptPath"))
        analysis_result_path = _resolve_stage_manifest_file_path(manifest.get("analysisResultPath"))
        should_resume_from_transcript = bool(
            transcript_path and transcript_path.is_file() and not (analysis_result_path and analysis_result_path.is_file())
        )

        if should_resume_from_transcript:
            utterances, full_text, duration_ms, transcript_provider = _load_transcript_document(transcript_path)
            provider = transcript_provider or get_settings().asr_provider
            manifest["transcriptPath"] = str(transcript_path)
            manifest["status"] = "transcribed"
            manifest["fullTextLength"] = len(full_text)
            manifest["utteranceCount"] = len(utterances)
            if duration_ms is not None:
                manifest["durationMs"] = duration_ms
                if _coerce_int(manifest.get("durationSeconds")) is None and duration_ms > 0:
                    manifest["durationSeconds"] = max(duration_ms // 1000, 1)
            manifest.pop("errorMessage", None)
            manifest.pop("qualityReason", None)
            manifest.pop("qualityStage", None)
            _write_manifest(paths, manifest)
            async with _session_factory() as db:
                recording = await _ensure_recording_stub_from_manifest(db, manifest, status="transcribed")
                if recording is not None:
                    await _sync_recording_transcript(
                        db,
                        recording,
                        manifest=manifest,
                        utterances=utterances,
                        full_text=full_text,
                        duration_ms=duration_ms or 0,
                        provider=provider,
                    )
                    await try_auto_link_visit_card_recording(db, recording)
                    await db.commit()
        else:
            manifest["status"] = "transcribing"
            manifest.pop("errorMessage", None)
            manifest.pop("qualityReason", None)
            manifest.pop("qualityStage", None)
            _write_manifest(paths, manifest)
            async with _session_factory() as db:
                recording = await _ensure_recording_stub_from_manifest(db, manifest, status="transcribing")
                if recording is not None:
                    await try_auto_link_visit_card_recording(db, recording)
                    await db.commit()

            provider = get_settings().asr_provider
            utterances, full_text, duration_ms = await transcribe_audio_file(
                audio_path,
                duration_seconds=duration_seconds,
                provider=provider,
                staff_id=_clean_text(manifest.get("staffId")),
                staff_name=_clean_text(manifest.get("staffName")),
                staff_role=_clean_text(manifest.get("staffRole")),
                source_id=stage_key,
            )

            transcript_path = paths.transcript_dir / f"{stage_key}.transcript.json"
            transcript_document = _build_transcript_document(
                manifest,
                utterances=utterances,
                full_text=full_text,
                duration_ms=duration_ms,
                provider=provider,
            )
            transcript_path.write_text(
                json.dumps(transcript_document, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            manifest["transcriptPath"] = str(transcript_path)
            manifest["status"] = "transcribed"
            manifest["fullTextLength"] = len(full_text)
            manifest["utteranceCount"] = len(utterances)
            manifest["durationMs"] = duration_ms
            manifest.pop("errorMessage", None)
            _write_manifest(paths, manifest)
            async with _session_factory() as db:
                recording = await _ensure_recording_stub_from_manifest(db, manifest, status="transcribed")
                if recording is not None:
                    await _sync_recording_transcript(
                        db,
                        recording,
                        manifest=manifest,
                        utterances=utterances,
                        full_text=full_text,
                        duration_ms=duration_ms,
                        provider=provider,
                    )
                    await try_auto_link_visit_card_recording(db, recording)
                    await db.commit()

        post_decision = _post_asr_quality_decision(utterances, full_text)
        if not post_decision.passed:
            await _mark_manifest_filtered(paths, manifest, post_decision)
            return

        if not get_settings().dingtalk_audio_auto_analyze:
            return

        payload, normalized_duration_ms = _analysis_payload_from_utterances(
            utterances,
            manifest=manifest,
        )
        analysis_input_path = paths.analysis_input_dir / f"{stage_key}.json"
        analysis_input_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

        manifest["analysisInputPath"] = str(analysis_input_path)
        manifest["status"] = "analyzing"
        manifest["durationMs"] = normalized_duration_ms or duration_ms
        manifest.pop("errorMessage", None)
        _write_manifest(paths, manifest)

        async with _session_factory() as db:
            hospital_code = await _resolve_staff_hospital_code(db, _clean_text(manifest.get("staffId")))
            system_prompt = await build_system_prompt(db, hospital_code=hospital_code)

        loop = asyncio.get_running_loop()
        result_dict = await loop.run_in_executor(
            None,
            _run_analysis_sync,
            str(analysis_input_path),
            system_prompt,
        )
        sanitize_analysis_result_with_raw(result_dict, raw=payload)

        result_path = paths.result_dir / f"{stage_key}.result.json"
        result_path.write_text(
            json.dumps(result_dict, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        manifest["analysisResultPath"] = str(result_path)
        has_visit_link = await _manifest_has_visit_link(manifest)
        post_analysis_decision = _post_analysis_quality_decision(
            result_dict,
            full_text=full_text,
            has_visit_link=has_visit_link,
        )
        if not post_analysis_decision.passed:
            await _mark_manifest_filtered(paths, manifest, post_analysis_decision)
            return

        manifest["status"] = "analyzed"
        if post_analysis_decision.reason:
            manifest["qualityStage"] = post_analysis_decision.stage
            manifest["qualityReason"] = post_analysis_decision.reason
        else:
            manifest.pop("qualityReason", None)
            manifest.pop("qualityStage", None)
        manifest.pop("errorMessage", None)
        _write_manifest(paths, manifest)
        async with _session_factory() as db:
            recording = await _ensure_recording_stub_from_manifest(db, manifest, status="analyzed")
            if recording is not None:
                await _sync_recording_transcript(
                    db,
                    recording,
                    manifest=manifest,
                    utterances=utterances,
                    full_text=full_text,
                    duration_ms=duration_ms,
                    provider=provider,
                )
                await _sync_recording_analysis_task(
                    db,
                    recording,
                    result_dict=result_dict,
                    raw=payload,
                    duration_ms=normalized_duration_ms or duration_ms,
                    utterance_count=len(utterances),
                )
                await try_auto_link_visit_card_recording(db, recording)
                await db.commit()
    except Exception as exc:
        logger.exception("failed to process staged DingTalk audio %s: %s", stage_key, exc)
        manifest["status"] = "failed"
        manifest["errorMessage"] = str(exc)
        _write_manifest(paths, manifest)
        async with _session_factory() as db:
            recording = await _ensure_recording_stub_from_manifest(db, manifest, status="failed")
            if recording is not None:
                await try_auto_link_visit_card_recording(db, recording)
                await db.commit()


def _get_pipeline_semaphore() -> asyncio.Semaphore:
    global _pipeline_semaphore, _pipeline_semaphore_limit

    limit = max(get_settings().dingtalk_audio_pipeline_workers, 1)
    if _pipeline_semaphore is None or _pipeline_semaphore_limit != limit:
        _pipeline_semaphore = asyncio.Semaphore(limit)
        _pipeline_semaphore_limit = limit
    return _pipeline_semaphore


async def _run_dingtalk_recording_pipeline_guarded(stage_key: str) -> None:
    async with _get_pipeline_semaphore():
        await execute_dingtalk_recording_pipeline(stage_key)


async def _pipeline_consumer_loop(worker_index: int) -> None:
    queue = _pipeline_queue
    if queue is None:
        return
    logger.info("dingtalk pipeline consumer #%d started", worker_index)
    try:
        while True:
            stage_key = await queue.get()
            try:
                await execute_dingtalk_recording_pipeline(stage_key)
            except Exception:
                logger.exception("dingtalk pipeline consumer #%d failed for stage_key=%s", worker_index, stage_key)
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        logger.info("dingtalk pipeline consumer #%d cancelled", worker_index)
        raise


async def start_dingtalk_pipeline_workers() -> None:
    """在 FastAPI lifespan 启动期间调用：初始化有界队列与固定消费者池。"""
    global _pipeline_queue, _pipeline_consumers, _pipeline_consumers_started
    async with _get_pipeline_queue_lock():
        if _pipeline_consumers_started:
            return
        settings = get_settings()
        worker_count = max(settings.dingtalk_audio_pipeline_workers, 1)
        # 队列容量 = 工作者数 * 64，给突发同步留缓冲，但不允许无限堆积。
        queue_size = max(worker_count * 64, 64)
        _pipeline_queue = asyncio.Queue(maxsize=queue_size)
        _pipeline_consumers = [
            asyncio.create_task(_pipeline_consumer_loop(i + 1), name=f"dingtalk-pipeline-{i+1}")
            for i in range(worker_count)
        ]
        _pipeline_consumers_started = True
        logger.info(
            "dingtalk pipeline workers started workers=%d queue_size=%d",
            worker_count,
            queue_size,
        )


async def stop_dingtalk_pipeline_workers(timeout: float = 10.0) -> None:
    """FastAPI lifespan 关闭时调用：尽量排空队列，再取消消费者。"""
    global _pipeline_queue, _pipeline_consumers, _pipeline_consumers_started
    async with _get_pipeline_queue_lock():
        if not _pipeline_consumers_started:
            return
        consumers = list(_pipeline_consumers)
        queue = _pipeline_queue
        _pipeline_consumers_started = False

    if queue is not None:
        try:
            await asyncio.wait_for(queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("dingtalk pipeline queue did not drain within %.1fs, forcing cancel", timeout)

    for task in consumers:
        task.cancel()
    for task in consumers:
        with suppress(asyncio.CancelledError, BaseException):
            await task

    _pipeline_consumers.clear()
    _pipeline_queue = None
    logger.info("dingtalk pipeline workers stopped")


async def dispatch_dingtalk_recording_pipeline(stage_key: str) -> None:
    """投递一个 stage 到流水线队列。若 worker pool 未启动，则回退到旧的临时 task 模式（兼容测试/脚本）。"""
    queue = _pipeline_queue
    if queue is None or not _pipeline_consumers_started:
        # 回退路径（脚本、测试、worker 启动前）：单 task 并被 semaphore 守护。
        task = asyncio.create_task(_run_dingtalk_recording_pipeline_guarded(stage_key))
        _background_pipeline_tasks.add(task)
        task.add_done_callback(_background_pipeline_tasks.discard)
        return

    try:
        # 非阻塞优先，避免在调用方等待过久；若已满则 await 阻塞，把背压传回上游 sync_dingtalk_audio_files。
        queue.put_nowait(stage_key)
    except asyncio.QueueFull:
        logger.warning("dingtalk pipeline queue full (size=%d), applying backpressure for stage_key=%s", queue.maxsize, stage_key)
        await queue.put(stage_key)


async def sync_dingtalk_audio_files(
    db: AsyncSession,
    *,
    device_codes: list[str] | None = None,
    lookback_minutes: int | None = None,
    start_timestamp: int | None = None,
    end_timestamp: int | None = None,
    run_pipeline_inline: bool = False,
) -> DingTalkAudioSyncResult:
    result = DingTalkAudioSyncResult()
    settings = get_settings()
    paths = _ensure_stage_paths()

    stmt = select(Device).where(Device.is_active.is_(True))
    if device_codes:
        stmt = stmt.where(Device.device_code.in_(device_codes))
    else:
        stmt = stmt.where(Device.staff_id.is_not(None))

    devices = (await db.execute(stmt.order_by(Device.updated_at.desc(), Device.created_at.desc()))).scalars().all()
    if not devices:
        return result

    device_snapshots = [
        {
            "id": device.id,
            "device_code": _clean_text(device.device_code),
            "hospital_code": _clean_text(device.hospital_code),
        }
        for device in devices
        if _clean_text(device.device_code)
    ]
    history_by_code = await load_device_staff_history(
        db,
        [item["device_code"] for item in device_snapshots],
    )

    if start_timestamp is None or end_timestamp is None:
        now = datetime.now(timezone.utc)
        resolved_end = int(now.timestamp() * 1000)
        resolved_lookback = lookback_minutes if lookback_minutes is not None else settings.dingtalk_audio_sync_lookback_minutes
        resolved_start = int((now - timedelta(minutes=max(resolved_lookback, 1))).timestamp() * 1000)
        start_timestamp = resolved_start if start_timestamp is None else start_timestamp
        end_timestamp = resolved_end if end_timestamp is None else end_timestamp

    for device in device_snapshots:
        device_code = device["device_code"]
        try:
            remote_items = await _list_all_audio_files_for_device(
                device_code,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                use_iot=is_iot_hospital_code(device.get("hospital_code")),
            )
        except (DingTalkConfigError, DingTalkApiError) as exc:
            message = str(exc)
            logger.warning("failed to list DingTalk audio for %s: %s", device_code, message)
            result.failed += 1
            result.items.append(
                DingTalkAudioSyncItem(
                    device_code=device_code,
                    file_name="",
                    status="error",
                    message=message,
                )
            )
            continue

        device_audio_dir = paths.audio_dir / _safe_part(device_code)
        device_audio_dir.mkdir(parents=True, exist_ok=True)

        for remote_item in remote_items:
            file_id = _clean_text(remote_item.get("fileId"))
            remote_file_name = _clean_text(remote_item.get("fileName"))
            if not file_id:
                result.failed += 1
                result.items.append(
                    DingTalkAudioSyncItem(
                        device_code=device_code,
                        file_name=remote_file_name or "",
                        status="error",
                        message="钉钉音频缺少 fileId",
                    )
                )
                continue

            current_stage_key = _stage_key(device_code, file_id)
            existing_manifest_path = _manifest_path(paths, current_stage_key)
            existing_manifest = _read_manifest(paths, current_stage_key) if existing_manifest_path.exists() else None
            existing_audio_path = (
                _resolve_stage_manifest_file_path(existing_manifest.get("audioPath"))
                if isinstance(existing_manifest, dict)
                else None
            )
            should_redownload_missing_audio = (
                isinstance(existing_manifest, dict)
                and (_clean_text(existing_manifest.get("status")) or "").lower() == "failed"
                and "录音文件不存在" in _clean_text(existing_manifest.get("errorMessage"))
                and not (existing_audio_path and existing_audio_path.is_file())
            )
            if existing_manifest_path.exists() and not should_redownload_missing_audio:
                if isinstance(existing_manifest, dict):
                    try:
                        reconciled = await _reconcile_completed_manifest_to_recording(db, existing_manifest)
                        if reconciled:
                            await db.commit()
                    except Exception:
                        await db.rollback()
                        logger.exception("failed to reconcile existing DingTalk manifest stage_key=%s", current_stage_key)
                result.skipped += 1
                result.items.append(
                    DingTalkAudioSyncItem(
                        device_code=device_code,
                        file_name=_build_staged_audio_name(device_code, file_id, remote_file_name),
                        status="skipped",
                        message="该音频已暂存",
                        file_id=file_id,
                        stage_key=current_stage_key,
                    )
                )
                continue

            staged_name = _build_staged_audio_name(device_code, file_id, remote_file_name)
            dest: Path | None = None
            try:
                dest = device_audio_dir / staged_name
                duration_ms = _coerce_int(remote_item.get("duration"))
                duration_seconds = duration_ms_to_seconds(duration_ms)
                created_at = _coerce_remote_timestamp(remote_item.get("createTime")) or datetime.now(timezone.utc)
                resolved_staff = resolve_device_staff_binding(
                    history_by_code,
                    device_code=device_code,
                    occurred_at=created_at,
                )

                manifest = {
                    "stageKey": current_stage_key,
                    "deviceCode": device_code,
                    "deviceId": device["id"],
                    "staffId": (resolved_staff or {}).get("staff_id"),
                    "staffName": (resolved_staff or {}).get("staff_name") or "",
                    "staffRole": (resolved_staff or {}).get("staff_role") or "consultant",
                    "fileId": file_id,
                    "remoteFileName": remote_file_name,
                    "stagedFileName": staged_name,
                    "audioPath": str(dest),
                    "fileSize": _coerce_int(remote_item.get("fileSize")),
                    "durationMs": duration_ms,
                    "durationSeconds": duration_seconds,
                    "remoteCreatedAt": created_at.isoformat(),
                    "remoteProvider": _clean_text(remote_item.get("remoteProvider") or remote_item.get("source")) or "dvi",
                    "status": "downloaded",
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                }

                pre_decision = _pre_asr_quality_decision(duration_seconds)
                if not pre_decision.passed:
                    manifest["status"] = "filtered"
                    manifest["qualityStage"] = pre_decision.stage
                    manifest["qualityReason"] = pre_decision.reason
                    _write_manifest(paths, manifest)
                    recording = await _ensure_recording_stub_from_manifest(db, manifest, status="filtered")
                    recording_id = recording.id if recording is not None else None
                    await db.commit()
                    if recording_id is not None:
                        await _sync_visit_orders_for_recording_context(db, recording_id)

                    result.imported += 1
                    result.filtered += 1
                    result.items.append(
                        DingTalkAudioSyncItem(
                            device_code=device_code,
                            file_name=staged_name,
                            status="filtered",
                            message=pre_decision.reason or "录音未通过 ASR 前质检，已直接过滤",
                            file_id=file_id,
                            stage_key=current_stage_key,
                        )
                    )
                    continue

                download_url = await _resolve_audio_download_url(remote_item, file_id)
                if not download_url:
                    raise RuntimeError("钉钉/IOT 未返回可用下载地址")

                file_size = await _download_file(download_url, dest)
                manifest["fileSize"] = file_size or manifest["fileSize"]
                _write_manifest(paths, manifest)
                recording = await _ensure_recording_stub_from_manifest(db, manifest, status="uploaded")
                recording_id = recording.id if recording is not None else None
                if recording is not None:
                    await try_auto_link_visit_card_recording(db, recording)
                await db.commit()
                if recording_id is not None:
                    await _sync_visit_orders_for_recording_context(db, recording_id)

                if run_pipeline_inline:
                    await execute_dingtalk_recording_pipeline(current_stage_key)
                    processed_manifest = _read_manifest(paths, current_stage_key)
                    if processed_manifest and processed_manifest.get("status") == "filtered":
                        result.filtered += 1
                else:
                    await dispatch_dingtalk_recording_pipeline(current_stage_key)
                    result.queued += 1

                result.imported += 1
                result.items.append(
                    DingTalkAudioSyncItem(
                        device_code=device_code,
                        file_name=staged_name,
                        status="imported",
                        message="音频已暂存，后续处理结果将写入暂存区，不影响当前系统引用文件",
                        file_id=file_id,
                        stage_key=current_stage_key,
                    )
                )
            except Exception as exc:
                if isinstance(dest, Path):
                    dest.unlink(missing_ok=True)
                logger.exception("failed to stage DingTalk audio %s for %s", file_id, device_code)
                result.failed += 1
                result.items.append(
                    DingTalkAudioSyncItem(
                        device_code=device_code,
                        file_name=staged_name,
                        status="error",
                        message=str(exc),
                        file_id=file_id,
                        stage_key=current_stage_key,
                    )
                )

    return result


def _build_audit_content(
    *,
    status: str,
    result: DingTalkAudioSyncResult | None = None,
    error_message: str | None = None,
) -> str:
    payload: dict[str, Any] = {"status": status}
    if result is not None:
        payload["summary"] = (
            f"暂存 {result.imported} 条，排重 {result.skipped} 条，过滤 {result.filtered} 条，失败 {result.failed} 条"
        )
        payload["imported"] = result.imported
        payload["skipped"] = result.skipped
        payload["filtered"] = result.filtered
        payload["failed"] = result.failed
        payload["queued"] = result.queued
        payload["items"] = [
            {
                "device_code": item.device_code,
                "file_name": item.file_name,
                "status": item.status,
                "message": item.message,
                "file_id": item.file_id,
                "stage_key": item.stage_key,
            }
            for item in result.items[:20]
        ]
    if error_message:
        payload["error_message"] = error_message[:500]
    return json.dumps(payload, ensure_ascii=False)


async def _write_audit_log(status: str, result: DingTalkAudioSyncResult | None = None, error: str | None = None) -> None:
    try:
        async with _session_factory() as db:
            await append_audit_log(
                db,
                operator_name=AUDIO_SYNC_OPERATOR,
                ip_address=AUDIO_SYNC_IP,
                module_name=AUDIO_SYNC_AUDIT_MODULE,
                action_name=AUDIO_SYNC_AUDIT_ACTION,
                content=_build_audit_content(status=status, result=result, error_message=error),
            )
    except Exception:
        logger.exception("failed to write DingTalk audio sync audit log")


async def periodic_dingtalk_audio_sync(
    stop_event: asyncio.Event,
    *,
    interval_seconds: int | None = None,
    lookback_minutes: int | None = None,
) -> None:
    settings = get_settings()
    resolved_interval = interval_seconds if interval_seconds is not None else settings.dingtalk_audio_sync_interval_seconds
    resolved_lookback = lookback_minutes if lookback_minutes is not None else settings.dingtalk_audio_sync_lookback_minutes

    logger.info(
        "starting DingTalk audio sync loop interval_seconds=%d lookback_minutes=%d stage_root=%s",
        resolved_interval,
        resolved_lookback,
        settings.dingtalk_audio_stage_path,
    )

    while not stop_event.is_set():
        try:
            async with periodic_advisory_lock("dingtalk_audio_sync", DINGTALK_AUDIO_SYNC_LOCK_ID) as acquired:
                if not acquired:
                    pass
                else:
                    async with _session_factory() as db:
                        sync_result = await sync_dingtalk_audio_files(
                            db,
                            lookback_minutes=resolved_lookback,
                            run_pipeline_inline=False,
                        )

                    filtered_count = 0
                    for item in sync_result.items:
                        if item.stage_key is None:
                            continue
                        manifest = _read_manifest(_ensure_stage_paths(), item.stage_key)
                        if manifest and manifest.get("status") == "filtered":
                            filtered_count += 1
                    sync_result.filtered = max(sync_result.filtered, filtered_count)

                    await _write_audit_log("success" if sync_result.failed == 0 else "partial", sync_result)
        except Exception as exc:
            logger.exception("DingTalk audio sync loop failed: %s", exc)
            await _write_audit_log("failed", error=str(exc))

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(resolved_interval, 1))
        except asyncio.TimeoutError:
            continue

    logger.info("DingTalk audio sync loop stopped")


async def get_dingtalk_audio_sync_status_snapshot(
    db: AsyncSession,
    *,
    task: asyncio.Task | None,
    enabled: bool,
    started_at: datetime | None,
    note: str | None,
) -> dict[str, Any]:
    running = bool(task and not task.done())
    resolved_note = note

    if task and task.done() and not task.cancelled():
        exc = task.exception()
        if exc is not None:
            resolved_note = f"钉钉音频同步服务异常退出：{type(exc).__name__}: {exc}"

    latest_log = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.module_name == AUDIO_SYNC_AUDIT_MODULE,
                AuditLog.action_name == AUDIO_SYNC_AUDIT_ACTION,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    last_sync_at = None
    last_sync_summary = None
    last_sync_status = None
    if latest_log is not None:
        last_sync_at = latest_log.created_at
        try:
            payload = json.loads(latest_log.content)
            last_sync_summary = payload.get("summary")
            last_sync_status = payload.get("status")
        except (TypeError, ValueError):
            pass

    return {
        "enabled": enabled,
        "running": running,
        "started_at": started_at,
        "note": resolved_note,
        "stage_root": str(get_settings().dingtalk_audio_stage_path),
        "last_sync_at": last_sync_at,
        "last_sync_status": last_sync_status,
        "last_sync_summary": last_sync_summary,
    }
