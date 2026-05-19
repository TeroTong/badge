"""API routes for DingTalk smart badge (钉工牌) integration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import re
import shutil
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload

from smart_badge_api.analysis.consultation_evaluation import (
    extract_preferred_overall_score,
    rebuild_consultation_evaluation,
    rebuild_consultation_process_evaluation,
)
from smart_badge_api.analysis.pipeline import sanitize_analysis_result_with_raw
from smart_badge_api.api.analysis_normalization import normalize_analysis_result
from smart_badge_api.api.data_scope import build_permission_scope, resolve_visible_staff_ids_for_user
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.device_binding import (
    DeviceBindingOverlapError,
    bind_staff_to_device,
    clear_device_staff_history,
    get_device_by_code,
    load_device_staff_history,
    resolve_device_staff_binding,
)
from smart_badge_api.core.config import get_settings
from smart_badge_api.core.permissions import is_global_role
from smart_badge_api.device_battery_notifications import handle_device_battery_update
from smart_badge_api.dingtalk_audio_archive import (
    RemoteAudioItem,
    archive_audio_files,
    archive_audio_item,
    get_dingtalk_audio_archive_sync_status_snapshot,
    get_archive_root,
)
from smart_badge_api.dingtalk_audio_sync import (
    clear_staged_device_staff_assignments,
    get_dingtalk_audio_sync_status_snapshot,
    sync_dingtalk_audio_files,
)
from smart_badge_api.dingtalk import (
    DingTalkApiError,
    DingTalkConfigError,
    configure_corp_badge,
    create_badge_code,
    decode_badge_code,
    dvi_control_recording,
    dvi_get_audio_download_url,
    dvi_get_audio_file_info,
    dvi_list_audio_files,
    dvi_list_devices,
    dvi_list_recording_durations,
    dvi_list_teams,
    dvi_query_device_detail,
    dvi_query_device_status,
    dvi_update_device_binding,
    notify_badge_code_verify_result,
    update_badge_code,
)
from smart_badge_api.dingtalk_iot import (
    iot_list_audio_files,
    iot_list_devices,
    iot_query_device_statuses,
    is_iot_hospital_code,
)
from smart_badge_api.db.models import (
    AnalysisTask,
    Device,
    DeviceStaffBinding,
    PositionProfile,
    Recording,
    RecordingVisitLink,
    Staff,
    Transcript,
    User,
    Visit,
    _new_id,
)
from smart_badge_api.db.session import get_db
from smart_badge_api.recording_analysis_service import create_or_dispatch_recording_analysis
from smart_badge_api.sap_consultation import attach_unlinked_sap_preview_to_result
from smart_badge_api.schemas.pagination import PaginatedResponse, make_page_response
from smart_badge_api.visit_linking import ordered_recording_visit_links
from smart_badge_api.visit_order_sync import retry_visit_order_sync, sync_visit_orders_for_recording

router = APIRouter(prefix="/dingtalk", tags=["朗姿工牌"])
_TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DINGTALK_DEVICE_REMOTE_TIMEOUT_SECONDS = 10.0
logger = logging.getLogger(__name__)
_ARCHIVE_RECORDING_INDEX_CACHE_TTL_SECONDS = 60.0
_ARCHIVE_RECORDING_INDEX_STALE_TTL_SECONDS = 600.0
_ARCHIVE_RECORDING_PAYLOAD_CACHE_TTL_SECONDS = 120.0
_ARCHIVE_RECORDING_PAYLOAD_CACHE_MAX_ENTRIES = 512
_archive_recording_index_cache: dict[str, Any] = {
    "expires_at": 0.0,
    "stale_expires_at": 0.0,
    "cache_key": None,
    "value": None,
}
_archive_recording_index_cache_lock = threading.RLock()
_archive_recording_payload_cache: dict[str, tuple[float, str, dict[str, Any]]] = {}
_CANONICAL_SPLIT_RECORDING_FILE_RE = re.compile(r"^\d{4}_\d{6}_\d{6}(?:\.[A-Za-z0-9]+)?$", re.IGNORECASE)


async def _sync_visit_orders_for_recording_context(db: AsyncSession, recording: Recording) -> None:
    recording_id = recording.id
    try:
        result = await retry_visit_order_sync(
            lambda: sync_visit_orders_for_recording(db, recording),
            label=f"archive-recording-context:{recording_id}",
            attempts=3,
            initial_delay_seconds=1.0,
        )
        if result.new_count or result.updated_count:
            logger.info(
                "synced visit orders for archived recording context recording_id=%s new=%d updated=%d",
                recording_id,
                result.new_count,
                result.updated_count,
            )
    except Exception:
        logger.exception("failed to sync visit orders for archived recording context recording_id=%s", recording_id)


def _handle(exc: DingTalkConfigError | DingTalkApiError) -> HTTPException:
    if isinstance(exc, DingTalkConfigError):
        return HTTPException(status_code=501, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


def _clean_text(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _nested_value(value: object) -> object:
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def _normalize_device_status_value(value: object) -> str | None:
    normalized = _clean_text(_nested_value(value))
    return normalized.lower() if normalized else None


def _normalize_device_battery_value(value: object) -> int | None:
    return _coerce_int(_nested_value(value))


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    timestamp_ms = _coerce_int(value)
    if timestamp_ms is not None and timestamp_ms > 0:
        try:
            return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = _clean_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _device_visible_for_scope(device: Device | None, scope: Any) -> bool:
    if is_global_role(getattr(scope, "role", None)):
        return True
    hospital_code = _clean_text(getattr(scope, "hospital_code", None))
    if not hospital_code or device is None:
        return False
    return _clean_text(device.hospital_code) == hospital_code


def _isoformat_datetime(value: object) -> str | None:
    resolved = _coerce_datetime(value)
    if resolved is None:
        return None
    return resolved.isoformat()


def _preferred_archive_display_name(
    *,
    create_time: object,
    archive_file_name: str | None,
    staged_file_name: str | None,
    remote_file_name: str | None,
    fallback_file_id: str,
) -> str:
    if archive_file_name:
        return archive_file_name

    for candidate in (remote_file_name, staged_file_name):
        leaf_name = Path(candidate or "").name
        if leaf_name and _CANONICAL_SPLIT_RECORDING_FILE_RE.match(leaf_name):
            return leaf_name

    resolved_time = _coerce_datetime(create_time)
    suffix = Path(remote_file_name or staged_file_name or fallback_file_id).suffix.lower() or ".mp3"
    if resolved_time is not None:
        localized = resolved_time.astimezone(_TZ_SHANGHAI)
        return f"{localized.strftime('%m%d_%H%M%S')}{suffix}"

    return staged_file_name or remote_file_name or fallback_file_id


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_archive_analysis_raw_data(recording_id: str | None) -> dict[str, Any] | None:
    normalized_recording_id = _clean_text(recording_id)
    if not normalized_recording_id:
        return None

    settings = get_settings()
    file_id = f"recording_{normalized_recording_id}"
    candidates = (
        settings.upload_path / f"{file_id}.json",
        settings.upload_path / "analysis_input" / f"{file_id}.json",
        settings.upload_path / "dingtalk_staging" / "analysis_input" / f"{file_id}.json",
    )
    for raw_path in candidates:
        raw_payload = _read_json_file(raw_path)
        if raw_payload is not None:
            return raw_payload
    return None


def _copy_if_exists(src: Path | None, dest: Path) -> bool:
    if src is None or not src.is_file():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return True


def _refresh_archive_analysis_result(
    path: Path | None,
    payload: dict[str, Any] | None,
    *,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return payload

    original_payload = deepcopy(payload)
    working_payload = deepcopy(payload)
    if isinstance(raw, dict):
        sanitize_analysis_result_with_raw(working_payload, raw=raw)

    normalized = normalize_analysis_result(working_payload) or working_payload
    refreshed = dict(normalized)
    refreshed["consultation_evaluation"] = rebuild_consultation_evaluation(refreshed)
    refreshed["consultation_process_evaluation"] = rebuild_consultation_process_evaluation(refreshed)

    if path and refreshed != original_payload:
        try:
            path.write_text(json.dumps(refreshed, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    return refreshed


def _write_json_file(path: Path | None, payload: dict[str, Any] | None) -> None:
    if path is None or not isinstance(payload, dict):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _standardized_indication_count(result_dict: dict[str, Any] | None) -> int:
    if not isinstance(result_dict, dict):
        return 0

    standardized = result_dict.get("standardized_indications")
    if isinstance(standardized, dict):
        items = standardized.get("items")
        if isinstance(items, list):
            return len([item for item in items if isinstance(item, dict) and item])

    consultation_result = result_dict.get("consultation_result")
    if isinstance(consultation_result, dict):
        chief = consultation_result.get("chief_complaint_and_indications")
        if isinstance(chief, dict):
            items = chief.get("standardized_indications")
            if isinstance(items, list):
                return len([item for item in items if _clean_text(item)])
    return 0


def _clear_resolved_quality_fields(
    target: dict[str, Any],
    *,
    status_key: str = "pipeline_status",
    reason_key: str = "quality_reason",
    stage_key: str = "quality_stage",
) -> bool:
    changed = False
    if target.get(reason_key) is not None:
        target[reason_key] = None
        changed = True
    if target.get(stage_key) is not None:
        target[stage_key] = None
        changed = True
    if str(target.get(status_key) or "").strip().lower() == "filtered":
        target[status_key] = "analyzed"
        changed = True
    if target.get("error_message") is not None:
        target["error_message"] = None
        changed = True
    return changed


def _clear_manifest_quality_fields(manifest: dict[str, Any], manifest_path: Path | None = None) -> bool:
    changed = False
    if manifest.get("qualityReason") is not None:
        manifest.pop("qualityReason", None)
        changed = True
    if manifest.get("qualityStage") is not None:
        manifest.pop("qualityStage", None)
        changed = True
    if _clean_text(manifest.get("errorMessage")):
        manifest.pop("errorMessage", None)
        changed = True
    if _clean_text(manifest.get("status")) != "analyzed":
        manifest["status"] = "analyzed"
        changed = True
    if changed:
        manifest["updatedAt"] = datetime.now(timezone.utc).isoformat()
        _write_json_file(manifest_path, manifest)
    return changed


def _clear_manifest_quality_if_analysis_resolved(
    manifest: dict[str, Any] | None,
    manifest_path: Path | None = None,
) -> bool:
    if not isinstance(manifest, dict):
        return False
    analysis_path = _resolve_archive_analysis_result_path(None, manifest)
    analysis_payload = _refresh_archive_analysis_result(
        analysis_path,
        _read_json_file(analysis_path) if analysis_path else None,
    )
    if _standardized_indication_count(analysis_payload) <= 0:
        return False
    return _clear_manifest_quality_fields(manifest, manifest_path)


async def _resolve_archive_analysis_result(
    db: AsyncSession,
    *,
    summary: dict[str, Any],
    manifest: dict[str, Any] | None,
) -> dict[str, Any] | None:
    analysis_path = _resolve_archive_analysis_result_path(None, manifest)
    recording_id = _clean_text(summary.get("recording_id"))
    raw_data = _load_archive_analysis_raw_data(recording_id)

    refreshed_file_payload = _refresh_archive_analysis_result(
        analysis_path,
        _read_json_file(analysis_path) if analysis_path else None,
        raw=raw_data,
    )
    file_payload = refreshed_file_payload

    if not recording_id:
        return file_payload

    if isinstance(file_payload, dict):
        file_payload = (
            await attach_unlinked_sap_preview_to_result(db, recording_id, file_payload)
        ) or file_payload

    latest_task = (
        await db.execute(
            select(AnalysisTask)
            .where(
                AnalysisTask.file_name == f"recording_{recording_id}.json",
                AnalysisTask.status == "done",
                AnalysisTask.result.is_not(None),
            )
            .order_by(
                AnalysisTask.completed_at.desc(),
                AnalysisTask.updated_at.desc(),
                AnalysisTask.created_at.desc(),
            )
        )
    ).scalars().first()

    if latest_task is None or not isinstance(latest_task.result, dict):
        if file_payload != refreshed_file_payload:
            _write_json_file(analysis_path, file_payload)
            settings = get_settings()
            _write_json_file(settings.results_path / f"recording_{recording_id}.result.json", file_payload)
        return file_payload

    task_payload = _refresh_archive_analysis_result(None, latest_task.result, raw=raw_data)
    if task_payload is None:
        return file_payload
    task_payload = (
        await attach_unlinked_sap_preview_to_result(db, recording_id, task_payload)
    ) or task_payload

    if task_payload != latest_task.result:
        latest_task.result = task_payload
        latest_task.overall_score = extract_preferred_overall_score(task_payload)
        await db.commit()

    if task_payload != file_payload:
        _write_json_file(analysis_path, task_payload)
        settings = get_settings()
        _write_json_file(settings.results_path / f"recording_{recording_id}.result.json", task_payload)

    return task_payload


def _dingtalk_stage_root() -> Path:
    root = get_settings().dingtalk_audio_stage_path
    root.mkdir(parents=True, exist_ok=True)
    return root


def _archive_recording_id(device_code: str | None, file_id: str) -> str:
    seed = f"{device_code or ''}:{file_id}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def _resolve_archive_manifest_file_path(raw_value: Any) -> Path | None:
    raw_path = _clean_text(raw_value)
    if not raw_path:
        return None

    original = Path(raw_path)
    candidates: list[Path] = []

    if original.is_absolute():
        candidates.append(original)
    else:
        settings = get_settings()
        candidates.append(settings.upload_path / original)
        candidates.append(settings.resolve_path(original))

    settings = get_settings()
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


def _resolve_archive_recording_audio_path(
    archive_metadata: dict[str, Any] | None,
    manifest: dict[str, Any] | None,
) -> Path | None:
    for payload in (archive_metadata, manifest):
        if not isinstance(payload, dict):
            continue
        raw_path = _clean_text(payload.get("audioPath"))
        if not raw_path:
            continue
        path = _resolve_archive_manifest_file_path(raw_path)
        if path and path.is_file():
            return path
    return None


def _infer_archive_stage_key(
    archive_metadata: dict[str, Any] | None,
    manifest: dict[str, Any] | None,
) -> str | None:
    explicit_stage_key = _clean_text((manifest or {}).get("stageKey"))
    if explicit_stage_key:
        return explicit_stage_key

    device_code = _clean_text((manifest or {}).get("deviceCode")) or _clean_text((manifest or {}).get("sn")) or _clean_text((archive_metadata or {}).get("sn"))
    file_id = _clean_text((manifest or {}).get("fileId")) or _clean_text((archive_metadata or {}).get("fileId"))
    if device_code and file_id:
        return f"{device_code}__{file_id}"
    return None


def _resolve_archive_analysis_result_path(
    archive_metadata: dict[str, Any] | None,
    manifest: dict[str, Any] | None,
) -> Path | None:
    explicit_path = _resolve_archive_manifest_file_path((manifest or {}).get("analysisResultPath"))
    if explicit_path and explicit_path.is_file():
        return explicit_path

    stage_key = _infer_archive_stage_key(archive_metadata, manifest)
    if not stage_key:
        return explicit_path

    inferred_path = _dingtalk_stage_root() / "results" / f"{stage_key}.result.json"
    if inferred_path.is_file():
        return inferred_path.resolve()
    return explicit_path


def _resolve_effective_archive_pipeline_status(
    raw_status: str | None,
    *,
    has_transcript: bool,
    has_analysis: bool,
) -> str:
    normalized = _clean_text(raw_status) or ""
    normalized_status = normalized.lower()
    if normalized_status == "filtered":
        return normalized_status
    if has_analysis:
        return "analyzed"
    if has_transcript and normalized_status in {"analyzing", "transcribing"}:
        return "transcribed"
    if has_transcript and normalized_status == "failed":
        return "transcribed"
    if normalized_status == "failed":
        return normalized_status
    return normalized_status or "archived"


def _build_archive_recording_summary(
    archive_metadata: dict[str, Any],
    manifest: dict[str, Any] | None,
) -> dict[str, Any] | None:
    file_id = _clean_text(archive_metadata.get("fileId"))
    if not file_id:
        return None

    sn = _clean_text(archive_metadata.get("sn"))
    device_code = _clean_text((manifest or {}).get("deviceCode")) or sn
    stage_key = _clean_text((manifest or {}).get("stageKey"))

    archive_audio_path = _clean_text(archive_metadata.get("audioPath"))
    stage_audio_path = _clean_text((manifest or {}).get("audioPath"))
    preferred_audio_path = _resolve_archive_recording_audio_path(archive_metadata, manifest)

    transcript_path = _resolve_archive_manifest_file_path((manifest or {}).get("transcriptPath"))
    analysis_result_path = _resolve_archive_analysis_result_path(archive_metadata, manifest)
    has_transcript = bool(transcript_path and transcript_path.is_file())
    has_analysis = bool(analysis_result_path and analysis_result_path.is_file())

    archive_file_name = Path(archive_audio_path).name if archive_audio_path else None
    staged_file_name = _clean_text((manifest or {}).get("stagedFileName"))
    remote_file_name = _clean_text((manifest or {}).get("remoteFileName")) or _clean_text(archive_metadata.get("remoteFileName"))

    create_time = (
        _isoformat_datetime(archive_metadata.get("createTimeMs"))
        or _isoformat_datetime((manifest or {}).get("remoteCreatedAt"))
        or _isoformat_datetime((manifest or {}).get("createdAt"))
    )
    downloaded_at = _isoformat_datetime(archive_metadata.get("downloadedAt"))
    updated_at = _isoformat_datetime((manifest or {}).get("updatedAt"))
    pipeline_status = _resolve_effective_archive_pipeline_status(
        _clean_text((manifest or {}).get("status")) or _clean_text(archive_metadata.get("status")),
        has_transcript=has_transcript,
        has_analysis=has_analysis,
    )
    exposed_error_message = _clean_text((manifest or {}).get("errorMessage")) if pipeline_status == "failed" else None

    return {
        "id": _archive_recording_id(device_code, file_id),
        "stage_key": stage_key,
        "sn": sn,
        "device_code": device_code,
        "file_id": file_id,
        "display_file_name": _preferred_archive_display_name(
            create_time=create_time,
            archive_file_name=archive_file_name,
            staged_file_name=staged_file_name,
            remote_file_name=remote_file_name,
            fallback_file_id=file_id,
        ),
        "archive_file_name": archive_file_name,
        "staged_file_name": staged_file_name,
        "remote_file_name": remote_file_name,
        "audio_path": str(preferred_audio_path) if preferred_audio_path else None,
        "archive_audio_path": archive_audio_path,
        "stage_audio_path": stage_audio_path,
        "duration_ms": _coerce_int((manifest or {}).get("durationMs")) or _coerce_int(archive_metadata.get("durationMs")),
        "duration_seconds": _coerce_int((manifest or {}).get("durationSeconds")),
        "file_size": _coerce_int((manifest or {}).get("fileSize")) or _coerce_int(archive_metadata.get("fileSize")),
        "create_time": create_time,
        "downloaded_at": downloaded_at,
        "updated_at": updated_at,
        "staff_id": _clean_text((manifest or {}).get("staffId")),
        "staff_name": _clean_text((manifest or {}).get("staffName")),
        "staff_role": _clean_text((manifest or {}).get("staffRole")),
        "staff_hospital_code": _clean_text((manifest or {}).get("staffHospitalCode")) or _clean_text((manifest or {}).get("hospitalCode")),
        "staff_hospital_short_name": _clean_text((manifest or {}).get("staffHospitalShortName")) or _clean_text((manifest or {}).get("hospitalShortName")),
        "device_hospital_code": _clean_text((manifest or {}).get("deviceHospitalCode")),
        "device_hospital_short_name": _clean_text((manifest or {}).get("deviceHospitalShortName")),
        "pipeline_status": pipeline_status,
        "quality_stage": _clean_text((manifest or {}).get("qualityStage")) or _clean_text(archive_metadata.get("qualityStage")),
        "quality_reason": _clean_text((manifest or {}).get("qualityReason")) or _clean_text(archive_metadata.get("qualityReason")),
        "error_message": exposed_error_message,
        "utterance_count": _coerce_int((manifest or {}).get("utteranceCount")),
        "full_text_length": _coerce_int((manifest or {}).get("fullTextLength")),
        "has_transcript": has_transcript,
        "has_analysis": has_analysis,
    }


def _build_staged_archive_recording_summary(manifest: dict[str, Any]) -> dict[str, Any] | None:
    file_id = _clean_text(manifest.get("fileId"))
    if not file_id:
        return None

    device_code = _clean_text(manifest.get("deviceCode")) or _clean_text(manifest.get("sn"))
    stage_key = _clean_text(manifest.get("stageKey"))
    stage_audio_path = _clean_text(manifest.get("audioPath"))
    preferred_audio_path = _resolve_archive_recording_audio_path(None, manifest)

    transcript_path = _resolve_archive_manifest_file_path(manifest.get("transcriptPath"))
    analysis_result_path = _resolve_archive_analysis_result_path(None, manifest)
    has_transcript = bool(transcript_path and transcript_path.is_file())
    has_analysis = bool(analysis_result_path and analysis_result_path.is_file())

    staged_file_name = _clean_text(manifest.get("stagedFileName"))
    remote_file_name = _clean_text(manifest.get("remoteFileName"))
    create_time = _isoformat_datetime(manifest.get("remoteCreatedAt")) or _isoformat_datetime(manifest.get("createdAt"))
    downloaded_at = _isoformat_datetime(manifest.get("createdAt"))
    updated_at = _isoformat_datetime(manifest.get("updatedAt"))
    pipeline_status = _resolve_effective_archive_pipeline_status(
        _clean_text(manifest.get("status")) or "downloaded",
        has_transcript=has_transcript,
        has_analysis=has_analysis,
    )
    exposed_error_message = _clean_text(manifest.get("errorMessage")) if pipeline_status == "failed" else None

    return {
        "id": _archive_recording_id(device_code, file_id),
        "stage_key": stage_key,
        "sn": device_code,
        "device_code": device_code,
        "file_id": file_id,
        "display_file_name": _preferred_archive_display_name(
            create_time=create_time,
            archive_file_name=None,
            staged_file_name=staged_file_name,
            remote_file_name=remote_file_name,
            fallback_file_id=file_id,
        ),
        "archive_file_name": None,
        "staged_file_name": staged_file_name,
        "remote_file_name": remote_file_name,
        "audio_path": str(preferred_audio_path) if preferred_audio_path else None,
        "archive_audio_path": None,
        "stage_audio_path": stage_audio_path,
        "duration_ms": _coerce_int(manifest.get("durationMs")),
        "duration_seconds": _coerce_int(manifest.get("durationSeconds")),
        "file_size": _coerce_int(manifest.get("fileSize")),
        "create_time": create_time,
        "downloaded_at": downloaded_at,
        "updated_at": updated_at,
        "staff_id": _clean_text(manifest.get("staffId")),
        "staff_name": _clean_text(manifest.get("staffName")),
        "staff_role": _clean_text(manifest.get("staffRole")),
        "staff_hospital_code": _clean_text(manifest.get("staffHospitalCode")) or _clean_text(manifest.get("hospitalCode")),
        "staff_hospital_short_name": _clean_text(manifest.get("staffHospitalShortName")) or _clean_text(manifest.get("hospitalShortName")),
        "device_hospital_code": _clean_text(manifest.get("deviceHospitalCode")),
        "device_hospital_short_name": _clean_text(manifest.get("deviceHospitalShortName")),
        "pipeline_status": pipeline_status,
        "quality_stage": _clean_text(manifest.get("qualityStage")),
        "quality_reason": _clean_text(manifest.get("qualityReason")),
        "error_message": exposed_error_message,
        "utterance_count": _coerce_int(manifest.get("utteranceCount")),
        "full_text_length": _coerce_int(manifest.get("fullTextLength")),
        "has_transcript": has_transcript,
        "has_analysis": has_analysis,
    }


def clear_archive_recording_index_cache() -> None:
    with _archive_recording_index_cache_lock:
        _archive_recording_index_cache["expires_at"] = 0.0
        _archive_recording_index_cache["stale_expires_at"] = 0.0
        _archive_recording_index_cache["cache_key"] = None
        _archive_recording_index_cache["value"] = None
        _archive_recording_payload_cache.clear()


def _build_archive_recording_index_uncached() -> dict[str, dict[str, Any]]:
    stage_root = _dingtalk_stage_root()
    manifest_dir = stage_root / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    stage_manifests: list[tuple[str, Path, dict[str, Any]]] = []
    stage_by_pair: dict[tuple[str, str], tuple[str, Path, dict[str, Any]]] = {}
    stage_by_file_id: dict[str, list[tuple[str, Path, dict[str, Any]]]] = {}
    for manifest_path in manifest_dir.glob("*.json"):
        manifest = _read_json_file(manifest_path)
        if manifest is None:
            continue
        _clear_manifest_quality_if_analysis_resolved(manifest, manifest_path)
        manifest_key = manifest_path.name
        stage_manifests.append((manifest_key, manifest_path, manifest))
        file_id = _clean_text(manifest.get("fileId"))
        device_code = _clean_text(manifest.get("deviceCode"))
        if file_id:
            stage_by_file_id.setdefault(file_id, []).append((manifest_key, manifest_path, manifest))
        if file_id and device_code:
            stage_by_pair[(device_code, file_id)] = (manifest_key, manifest_path, manifest)

    archive_root = get_archive_root()
    index: dict[str, dict[str, Any]] = {}
    used_manifest_keys: set[str] = set()
    for meta_path in archive_root.rglob("*.json"):
        if meta_path.name.startswith("."):
            continue
        archive_metadata = _read_json_file(meta_path)
        if archive_metadata is None:
            continue

        file_id = _clean_text(archive_metadata.get("fileId"))
        sn = _clean_text(archive_metadata.get("sn"))
        if not file_id:
            continue

        manifest_key = None
        manifest_path = None
        manifest = None
        if sn:
            matched = stage_by_pair.get((sn, file_id))
            if matched is not None:
                manifest_key, manifest_path, manifest = matched
        if manifest is None:
            manifest_candidates = stage_by_file_id.get(file_id) or []
            if manifest_candidates:
                manifest_key, manifest_path, manifest = manifest_candidates[0]
        if manifest_key is not None:
            used_manifest_keys.add(manifest_key)

        summary = _build_archive_recording_summary(archive_metadata, manifest)
        if summary is None:
            continue
        if manifest_path is not None:
            summary["_manifest_path"] = str(manifest_path)
        item_id = str(summary["id"])
        index[item_id] = {
            "summary": summary,
            "archive_metadata": archive_metadata,
            "manifest": manifest,
            "manifest_path": manifest_path,
        }

    for manifest_key, manifest_path, manifest in stage_manifests:
        if manifest_key in used_manifest_keys:
            continue
        summary = _build_staged_archive_recording_summary(manifest)
        if summary is None:
            continue
        summary["_manifest_path"] = str(manifest_path)
        item_id = str(summary["id"])
        if item_id in index:
            continue
        index[item_id] = {
            "summary": summary,
            "archive_metadata": None,
            "manifest": manifest,
            "manifest_path": manifest_path,
        }

    return index


def _archive_recording_index_cache_key() -> str:
    return f"{_dingtalk_stage_root().resolve()}::{get_archive_root().resolve()}"


def _load_archive_recording_index_cached(*, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    now = time.monotonic()
    cache_key = _archive_recording_index_cache_key()
    with _archive_recording_index_cache_lock:
        cached_value = _archive_recording_index_cache.get("value")
        cached_expires_at = float(_archive_recording_index_cache.get("expires_at") or 0.0)
        stale_expires_at = float(_archive_recording_index_cache.get("stale_expires_at") or 0.0)
        if (
            not force_refresh
            and cached_value is not None
            and _archive_recording_index_cache.get("cache_key") == cache_key
            and (cached_expires_at > now or stale_expires_at > now)
        ):
            return cached_value

    index = _build_archive_recording_index_uncached()
    now = time.monotonic()
    with _archive_recording_index_cache_lock:
        _archive_recording_index_cache["value"] = index
        _archive_recording_index_cache["cache_key"] = cache_key
        _archive_recording_index_cache["expires_at"] = now + _ARCHIVE_RECORDING_INDEX_CACHE_TTL_SECONDS
        _archive_recording_index_cache["stale_expires_at"] = now + _ARCHIVE_RECORDING_INDEX_STALE_TTL_SECONDS
    return index


def _load_archive_recording_index(*, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    return deepcopy(_load_archive_recording_index_cached(force_refresh=force_refresh))


def _load_archive_recording_payload(item_id: str, *, force_refresh: bool = False) -> dict[str, Any] | None:
    now = time.monotonic()
    cache_key = _archive_recording_index_cache_key()
    if not force_refresh:
        with _archive_recording_index_cache_lock:
            cached = _archive_recording_payload_cache.get(item_id)
            if cached is not None:
                expires_at, cached_cache_key, cached_payload = cached
                if expires_at > now and cached_cache_key == cache_key:
                    return deepcopy(cached_payload)
                _archive_recording_payload_cache.pop(item_id, None)

    payload = _load_archive_recording_index_cached(force_refresh=force_refresh).get(item_id)
    if payload is None:
        return None

    with _archive_recording_index_cache_lock:
        _archive_recording_payload_cache[item_id] = (
            time.monotonic() + _ARCHIVE_RECORDING_PAYLOAD_CACHE_TTL_SECONDS,
            cache_key,
            deepcopy(payload),
        )
        while len(_archive_recording_payload_cache) > _ARCHIVE_RECORDING_PAYLOAD_CACHE_MAX_ENTRIES:
            oldest_key = next(iter(_archive_recording_payload_cache))
            _archive_recording_payload_cache.pop(oldest_key, None)
    return deepcopy(payload)


def warm_archive_recording_index_cache(*, force_refresh: bool = False) -> int:
    return len(_load_archive_recording_index_cached(force_refresh=force_refresh))


def _build_archive_analysis_summary(
    summary: dict[str, Any],
    transcript: dict[str, Any] | None,
    analysis_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(analysis_result, dict):
        return None

    evaluation = analysis_result.get("consultation_evaluation")
    demands = analysis_result.get("customer_demands")
    concerns = analysis_result.get("customer_concerns")
    profile = analysis_result.get("customer_profile")
    recommendations = analysis_result.get("staff_recommendations")
    primary_demands = analysis_result.get("customer_primary_demands")
    standardized_indications = analysis_result.get("standardized_indications")

    transcript_payload = transcript or {}
    utterances = transcript_payload.get("utterances") if isinstance(transcript_payload, dict) else []
    duration_ms = _coerce_int(transcript_payload.get("durationMs") if isinstance(transcript_payload, dict) else None) or _coerce_int(summary.get("duration_ms")) or 0

    focus_areas: list[str] = []
    if isinstance(demands, dict):
        focus_areas = [
            str(item.get("area") or "").strip()
            for item in (demands.get("focus_areas") or [])
            if isinstance(item, dict) and str(item.get("area") or "").strip()
        ]

    if not focus_areas and isinstance(primary_demands, dict):
        focus_areas = [
            str(item.get("body_part") or "").strip()
            for item in (primary_demands.get("items") or [])
            if isinstance(item, dict) and str(item.get("body_part") or "").strip()
        ]

    if not focus_areas and isinstance(standardized_indications, dict):
        focus_areas = [
            str(item.get("body_part_name") or "").strip()
            for item in (standardized_indications.get("items") or [])
            if isinstance(item, dict) and str(item.get("body_part_name") or "").strip()
        ]
    if focus_areas:
        deduped_focus_areas: list[str] = []
        seen_focus_areas: set[str] = set()
        for area in focus_areas:
            normalized_area = re.sub(r"[\s/／、，,；;]+", "", area)
            if normalized_area in seen_focus_areas:
                continue
            seen_focus_areas.add(normalized_area)
            deduped_focus_areas.append(area)
        focus_areas = deduped_focus_areas

    concern_items = concerns.get("items") if isinstance(concerns, dict) else []
    profile_tags = profile.get("tags") if isinstance(profile, dict) else []
    recommendation_items = recommendations.get("items") if isinstance(recommendations, dict) else []

    overall_score = None
    total_score = None
    max_total_score = None
    overall_summary = None
    dialogue_type = None
    process_evaluation = (
        analysis_result.get("consultation_process_evaluation")
        if isinstance(analysis_result, dict)
        else None
    )
    if isinstance(process_evaluation, dict):
        if isinstance(process_evaluation.get("total_score"), (int, float)):
            total_score = float(process_evaluation["total_score"])
        if isinstance(process_evaluation.get("max_total_score"), (int, float)):
            max_total_score = float(process_evaluation["max_total_score"])
        if isinstance(process_evaluation.get("overall_score"), (int, float)):
            overall_score = float(process_evaluation["overall_score"])
        overall_summary = _clean_text(process_evaluation.get("overall_summary")) or overall_summary
    if isinstance(evaluation, dict):
        if total_score is None and isinstance(evaluation.get("total_score"), (int, float)):
            total_score = float(evaluation["total_score"])
        if max_total_score is None and isinstance(evaluation.get("max_total_score"), (int, float)):
            max_total_score = float(evaluation["max_total_score"])
        if overall_score is None and isinstance(evaluation.get("overall_score"), (int, float)):
            overall_score = float(evaluation["overall_score"])
        overall_summary = overall_summary or _clean_text(evaluation.get("overall_summary"))
    if isinstance(demands, dict):
        expectation = demands.get("expectation")
        if isinstance(expectation, dict):
            dialogue_type = _clean_text(expectation.get("dialogue_type"))

    return {
        "recorded_at": summary.get("create_time"),
        "duration_ms": duration_ms,
        "duration_display": f"{duration_ms // 60000}:{(duration_ms // 1000) % 60:02d}",
        "segment_count": len(utterances) if isinstance(utterances, list) else 0,
        "overall_score": overall_score,
        "total_score": total_score,
        "max_total_score": max_total_score,
        "overall_summary": overall_summary,
        "dialogue_type": dialogue_type,
        "focus_areas": focus_areas,
        "concern_count": len(concern_items) if isinstance(concern_items, list) else 0,
        "tag_count": len(profile_tags) if isinstance(profile_tags, list) else 0,
        "recommendation_count": len(recommendation_items) if isinstance(recommendation_items, list) else 0,
    }


class DingtalkArchiveRecordingOut(BaseModel):
    id: str
    stage_key: str | None = None
    sn: str | None = None
    device_code: str | None = None
    file_id: str
    display_file_name: str
    archive_file_name: str | None = None
    staged_file_name: str | None = None
    remote_file_name: str | None = None
    audio_path: str | None = None
    archive_audio_path: str | None = None
    stage_audio_path: str | None = None
    duration_ms: int | None = None
    duration_seconds: int | None = None
    file_size: int | None = None
    create_time: str | None = None
    downloaded_at: str | None = None
    updated_at: str | None = None
    staff_id: str | None = None
    staff_name: str | None = None
    staff_role: str | None = None
    pipeline_status: str | None = None
    quality_stage: str | None = None
    quality_reason: str | None = None
    error_message: str | None = None
    recording_id: str | None = None
    is_split_hidden: bool = False
    visit_id: str | None = None
    linked_visit_ids: list[str] = Field(default_factory=list)
    linked_visit_order_refs: list[str] = Field(default_factory=list)
    has_visit_link: bool = False
    needs_visit_link: bool = False
    utterance_count: int | None = None
    full_text_length: int | None = None
    has_transcript: bool
    has_analysis: bool


class DingtalkArchiveRecordingDetailOut(DingtalkArchiveRecordingOut):
    manifest: dict[str, Any] | None = None
    archive_metadata: dict[str, Any] | None = None
    transcript: dict[str, Any] | None = None
    analysis_result: dict[str, Any] | None = None
    analysis_summary: dict[str, Any] | None = None


class DingtalkArchiveEnsureRecordingOut(BaseModel):
    item_id: str
    recording_id: str
    file_name: str
    created_new_recording: bool
    visit_id: str | None = None
    linked_visit_ids: list[str] = Field(default_factory=list)
    linked_visit_order_refs: list[str] = Field(default_factory=list)


def _build_visit_order_ref(visit: Visit | None) -> str | None:
    if visit is None:
        return None
    visit_order_no = _clean_text(visit.external_visit_order_no)
    visit_order_seg = _clean_text(visit.external_visit_order_seg)
    if visit_order_no:
        return f"{visit_order_no}-{visit_order_seg}" if visit_order_seg else visit_order_no
    return _clean_text(visit.id)


def _serialize_archive_recording_binding(recording: Recording) -> dict[str, Any]:
    linked_visits = [link.visit for link in ordered_recording_visit_links(recording) if link.visit is not None]
    return {
        "recording_id": recording.id,
        "file_name": recording.file_name,
        "visit_id": recording.visit_id,
        "linked_visit_ids": [visit.id for visit in linked_visits],
        "linked_visit_order_refs": [
            ref
            for ref in (_build_visit_order_ref(visit) for visit in linked_visits)
            if ref
        ],
        "linked_customer_names": [
            customer_name
            for customer_name in (
                _clean_text(visit.customer.name) if visit.customer else None
                for visit in linked_visits
            )
            if customer_name
        ],
    }


async def _attach_archive_recording_bindings(
    db: AsyncSession,
    items: list[dict[str, Any]],
    *,
    lightweight: bool = False,
) -> list[dict[str, Any]]:
    candidate_file_names = {
        file_name
        for item in items
        for file_name in (
            _clean_text(item.get("staged_file_name")),
            _clean_text(item.get("display_file_name")),
            _clean_text(item.get("archive_file_name")),
            _clean_text(item.get("remote_file_name")),
        )
        if file_name
    }
    if not candidate_file_names:
        for item in items:
            item["recording_id"] = None
            item["visit_id"] = None
            item["linked_visit_ids"] = []
            item["linked_visit_order_refs"] = []
            item["linked_customer_names"] = []
            item["is_split_hidden"] = False
            item["has_visit_link"] = False
            item["needs_visit_link"] = bool(item.get("has_transcript")) and str(item.get("pipeline_status") or "") not in {"filtered", "failed"}
        return items

    recording_options = [
        selectinload(Recording.staff),
        selectinload(Recording.visit).selectinload(Visit.customer),
        selectinload(Recording.visit_links).selectinload(RecordingVisitLink.visit).selectinload(Visit.customer),
    ]
    if lightweight:
        recording_options.append(
            load_only(
                Recording.id,
                Recording.visit_id,
                Recording.staff_id,
                Recording.file_name,
                Recording.status,
                Recording.created_at,
            )
        )
    else:
        recording_options.append(selectinload(Recording.transcript))

    recordings = (
        await db.execute(
            select(Recording)
            .where(Recording.file_name.in_(candidate_file_names))
            .options(*recording_options)
            .order_by(Recording.created_at.desc())
        )
    ).scalars().all()

    recording_ids = [recording.id for recording in recordings]
    split_hidden_parent_ids: set[str] = set()
    if recording_ids:
        split_hidden_parent_ids = {
            parent_id
            for (parent_id,) in (
                await db.execute(
                    select(Recording.split_parent_recording_id).where(
                        Recording.split_parent_recording_id.in_(recording_ids)
                    )
                )
            ).all()
            if parent_id
        }
    transcribed_recording_ids: set[str] = set()
    if lightweight and recording_ids:
        transcribed_recording_ids = {
            recording_id
            for (recording_id,) in (
                await db.execute(
                    select(Transcript.recording_id).where(
                        Transcript.recording_id.in_(recording_ids),
                        Transcript.status == "completed",
                    )
                )
            ).all()
        }
    analyzed_recording_ids: set[str] = set()
    latest_analysis_by_recording_id: dict[str, dict[str, Any]] = {}
    if recording_ids:
        task_file_names = {f"recording_{recording_id}.json" for recording_id in recording_ids}
        task_columns = (
            (AnalysisTask.file_name,)
            if lightweight
            else (AnalysisTask.file_name, AnalysisTask.result)
        )
        task_conditions = [
            AnalysisTask.file_name.in_(task_file_names),
            AnalysisTask.status == "done",
        ]
        if not lightweight:
            task_conditions.append(AnalysisTask.result.is_not(None))
        done_task_rows = (
            await db.execute(
                select(*task_columns)
                .where(*task_conditions)
                .order_by(
                    AnalysisTask.completed_at.desc(),
                    AnalysisTask.updated_at.desc(),
                    AnalysisTask.created_at.desc(),
                )
            )
        ).all()
        for row in done_task_rows:
            file_name = _clean_text(row.file_name)
            if file_name and file_name.startswith("recording_") and file_name.endswith(".json"):
                recording_id = file_name.removeprefix("recording_").removesuffix(".json")
                analyzed_recording_ids.add(recording_id)
                if (
                    not lightweight
                    and recording_id not in latest_analysis_by_recording_id
                    and isinstance(row.result, dict)
                ):
                    latest_analysis_by_recording_id[recording_id] = row.result

    recording_by_file_name: dict[str, Recording] = {}
    for recording in recordings:
        if recording.file_name not in recording_by_file_name:
            recording_by_file_name[recording.file_name] = recording

    scoped_staff_ids = {
        staff_id
        for staff_id in (_clean_text(item.get("staff_id")) for item in items)
        if staff_id
    }
    scoped_device_codes = {
        device_code
        for device_code in (
            _clean_text(item.get("device_code")) or _clean_text(item.get("sn"))
            for item in items
        )
        if device_code
    }
    for recording in recordings:
        if recording.staff_id:
            scoped_staff_ids.add(recording.staff_id)

    staff_by_id: dict[str, dict[str, str | None]] = {}
    staff_by_badge: dict[str, dict[str, str | None]] = {}
    staff_filters = []
    if scoped_staff_ids:
        staff_filters.append(Staff.id.in_(scoped_staff_ids))
    if scoped_device_codes:
        staff_filters.append(Staff.badge_id.in_(scoped_device_codes))
    if staff_filters:
        staff_rows = (
            await db.execute(
                select(
                    Staff.id,
                    Staff.name,
                    Staff.role,
                    Staff.permission_role,
                    Staff.badge_id,
                    Staff.hospital_code,
                    Staff.hospital_short_name,
                ).where(or_(*staff_filters))
            )
        ).all()
        for row in staff_rows:
            payload = {
                "staff_id": _clean_text(row.id),
                "staff_name": _clean_text(row.name),
                "staff_role": _clean_text(row.role),
                "staff_permission_role": _clean_text(row.permission_role),
                "staff_hospital_code": _clean_text(row.hospital_code),
                "staff_hospital_short_name": _clean_text(row.hospital_short_name),
            }
            if payload["staff_id"]:
                staff_by_id[payload["staff_id"]] = payload
            badge_id = _clean_text(row.badge_id)
            if badge_id:
                staff_by_badge[badge_id] = payload

    device_staff_by_code: dict[str, dict[str, str | None]] = {}
    if scoped_device_codes:
        history_by_code = await load_device_staff_history(db, scoped_device_codes)
        device_rows = (
            await db.execute(
                select(
                    Device.device_code.label("device_code"),
                    Device.staff_id.label("staff_id"),
                    Device.hospital_code.label("device_hospital_code"),
                    Device.hospital_short_name.label("device_hospital_short_name"),
                    Staff.name.label("staff_name"),
                    Staff.role.label("staff_role"),
                    Staff.permission_role.label("staff_permission_role"),
                    Staff.hospital_code.label("staff_hospital_code"),
                    Staff.hospital_short_name.label("staff_hospital_short_name"),
                )
                .select_from(Device)
                .join(Staff, Staff.id == Device.staff_id, isouter=True)
                .where(Device.device_code.in_(scoped_device_codes))
            )
        ).all()
        for row in device_rows:
            device_code = _clean_text(row.device_code)
            if not device_code:
                continue
            device_staff_by_code[device_code] = {
                "staff_id": _clean_text(row.staff_id),
                "staff_name": _clean_text(row.staff_name),
                "staff_role": _clean_text(row.staff_role),
                "staff_permission_role": _clean_text(row.staff_permission_role),
                "staff_hospital_code": _clean_text(row.staff_hospital_code),
                "staff_hospital_short_name": _clean_text(row.staff_hospital_short_name),
                "device_hospital_code": _clean_text(row.device_hospital_code),
                "device_hospital_short_name": _clean_text(row.device_hospital_short_name),
            }
    else:
        history_by_code = {}

    for item in items:
        recording = None
        for candidate_name in (
            _clean_text(item.get("archive_file_name")),
            _clean_text(item.get("display_file_name")),
            _clean_text(item.get("staged_file_name")),
            _clean_text(item.get("remote_file_name")),
        ):
            if candidate_name and candidate_name in recording_by_file_name:
                recording = recording_by_file_name[candidate_name]
                break
        resolved_device_code = _clean_text(item.get("device_code")) or _clean_text(item.get("sn"))
        historical_staff = resolve_device_staff_binding(
            history_by_code,
            device_code=resolved_device_code,
            occurred_at=(
                _clean_text(item.get("create_time"))
                or _clean_text(item.get("downloaded_at"))
                or _clean_text(item.get("updated_at"))
            ),
        )
        if recording is None:
            item["recording_id"] = None
            item["visit_id"] = None
            item["linked_visit_ids"] = []
            item["linked_visit_order_refs"] = []
            item["linked_customer_names"] = []
            item["is_split_hidden"] = False
            item["has_visit_link"] = False
        else:
            binding = _serialize_archive_recording_binding(recording)
            item["recording_id"] = binding["recording_id"]
            item["visit_id"] = binding["visit_id"]
            item["linked_visit_ids"] = binding["linked_visit_ids"]
            item["linked_visit_order_refs"] = binding["linked_visit_order_refs"]
            item["linked_customer_names"] = binding["linked_customer_names"]
            item["has_visit_link"] = bool(binding["linked_visit_ids"])

            # The archive manifest may keep the staff name from the original
            # device binding. Once a DB recording exists, treat it as the
            # source of truth so rebinding changes are reflected everywhere.
            if recording.staff_id:
                item["staff_id"] = recording.staff_id
            if recording.staff is not None:
                if _clean_text(recording.staff.name):
                    item["staff_name"] = recording.staff.name
                if _clean_text(recording.staff.role):
                    item["staff_role"] = recording.staff.role
                if _clean_text(recording.staff.permission_role):
                    item["staff_permission_role"] = recording.staff.permission_role
                if _clean_text(recording.staff.hospital_code):
                    item["staff_hospital_code"] = recording.staff.hospital_code
                if _clean_text(recording.staff.hospital_short_name):
                    item["staff_hospital_short_name"] = recording.staff.hospital_short_name

            transcript = None if lightweight else recording.transcript
            transcript_utterances: list[Any] = []
            if not lightweight:
                if transcript is not None and isinstance(transcript.utterances, list):
                    transcript_utterances = transcript.utterances
                elif isinstance(recording.transcript_segments, list):
                    transcript_utterances = recording.transcript_segments
                elif isinstance(recording.transcript_segments, dict):
                    raw_utterances = recording.transcript_segments.get("utterances")
                    if isinstance(raw_utterances, list):
                        transcript_utterances = raw_utterances

            transcript_text = (
                _clean_text(transcript.full_text if transcript is not None else None)
                or (None if lightweight else _clean_text(recording.transcript_text))
            )
            has_db_transcript = bool(
                transcript_text
                or transcript_utterances
                or recording.id in transcribed_recording_ids
                or (_clean_text(recording.status) or "").lower() in {"transcribed", "analyzed"}
            )
            recording_status = (_clean_text(recording.status) or "").lower()
            has_db_analysis = recording.id in analyzed_recording_ids or recording_status == "analyzed"
            current_pipeline_status = str(item.get("pipeline_status") or "").strip().lower()
            if recording_status == "filtered":
                item["pipeline_status"] = recording_status
                current_pipeline_status = recording_status
            item["is_split_hidden"] = recording_status == "filtered" and recording.id in split_hidden_parent_ids

            if has_db_transcript:
                item["has_transcript"] = True
                if not item.get("utterance_count") and transcript_utterances:
                    item["utterance_count"] = len(transcript_utterances)
                if not item.get("full_text_length") and transcript_text:
                    item["full_text_length"] = len(transcript_text)

            if has_db_analysis:
                item["has_analysis"] = True
                if current_pipeline_status != "filtered":
                    item["pipeline_status"] = "analyzed"
                    item["error_message"] = None
            elif recording_status == "failed":
                item["pipeline_status"] = recording_status
                item["error_message"] = None
            elif has_db_transcript and current_pipeline_status in {"", "archived", "downloaded", "failed", "transcribing", "analyzing"}:
                item["pipeline_status"] = "transcribed"
                item["error_message"] = None

            latest_analysis = latest_analysis_by_recording_id.get(recording.id)
            if isinstance(latest_analysis, dict):
                item["_latest_analysis_result"] = latest_analysis
            if (
                not lightweight
                and _standardized_indication_count(_refresh_archive_analysis_result(None, latest_analysis)) > 0
            ):
                _clear_resolved_quality_fields(item)
                manifest_path_text = _clean_text(item.get("_manifest_path"))
                manifest_path = Path(manifest_path_text) if manifest_path_text else None
                manifest = _read_json_file(manifest_path) if manifest_path else None
                if manifest is not None:
                    _clear_manifest_quality_fields(manifest, manifest_path)

        resolved_staff_id = _clean_text(item.get("staff_id"))
        fallback_staff = None
        if not recording or recording.staff is None:
            fallback_staff = historical_staff
        if fallback_staff is None:
            fallback_staff = staff_by_id.get(resolved_staff_id or "")
        if fallback_staff is None and resolved_device_code:
            fallback_staff = device_staff_by_code.get(resolved_device_code) or staff_by_badge.get(resolved_device_code)
        if fallback_staff:
            if not resolved_staff_id and fallback_staff.get("staff_id"):
                item["staff_id"] = fallback_staff["staff_id"]
            if not _clean_text(item.get("staff_name")) and fallback_staff.get("staff_name"):
                item["staff_name"] = fallback_staff["staff_name"]
            if not _clean_text(item.get("staff_role")) and fallback_staff.get("staff_role"):
                item["staff_role"] = fallback_staff["staff_role"]
            if fallback_staff.get("staff_permission_role"):
                item["staff_permission_role"] = fallback_staff["staff_permission_role"]
            if fallback_staff.get("staff_hospital_code"):
                item["staff_hospital_code"] = fallback_staff["staff_hospital_code"]
            if fallback_staff.get("staff_hospital_short_name"):
                item["staff_hospital_short_name"] = fallback_staff["staff_hospital_short_name"]
            if fallback_staff.get("device_hospital_code"):
                item["device_hospital_code"] = fallback_staff["device_hospital_code"]
            if fallback_staff.get("device_hospital_short_name"):
                item["device_hospital_short_name"] = fallback_staff["device_hospital_short_name"]

        item["needs_visit_link"] = (
            bool(item.get("has_transcript"))
            and str(item.get("pipeline_status") or "") not in {"filtered", "failed"}
            and not bool(item.get("linked_visit_ids"))
        )
    return items


async def _resolve_archive_transcript(
    db: AsyncSession,
    *,
    summary: dict[str, Any],
    manifest: dict[str, Any] | None,
) -> dict[str, Any] | None:
    transcript_path_text = _clean_text((manifest or {}).get("transcriptPath"))
    if transcript_path_text:
        transcript_path = _resolve_archive_manifest_file_path(transcript_path_text)
        if transcript_path is not None:
            transcript = _read_json_file(transcript_path)
            if transcript is not None:
                return transcript

    recording_id = _clean_text(summary.get("recording_id"))
    if not recording_id:
        return None

    transcript_row = (
        await db.execute(select(Transcript).where(Transcript.recording_id == recording_id))
    ).scalars().first()
    recording = await db.get(Recording, recording_id)
    if transcript_row is None and recording is None:
        return None

    utterances: list[Any] = []
    if transcript_row is not None and isinstance(transcript_row.utterances, list):
        utterances = transcript_row.utterances
    elif recording is not None and isinstance(recording.transcript_segments, list):
        utterances = recording.transcript_segments
    elif recording is not None and isinstance(recording.transcript_segments, dict):
        raw_utterances = recording.transcript_segments.get("utterances")
        if isinstance(raw_utterances, list):
            utterances = raw_utterances

    full_text = (
        _clean_text(transcript_row.full_text if transcript_row is not None else None)
        or _clean_text(recording.transcript_text if recording is not None else None)
        or ""
    )
    if not utterances and not full_text:
        return None

    return {
        "stageKey": summary.get("stage_key"),
        "deviceCode": summary.get("device_code") or summary.get("sn"),
        "fileId": summary.get("file_id"),
        "remoteFileName": summary.get("remote_file_name"),
        "audioPath": summary.get("audio_path"),
        "asrProvider": transcript_row.asr_provider if transcript_row is not None else "archive_import",
        "durationMs": transcript_row.duration_ms if transcript_row is not None else summary.get("duration_ms"),
        "fullText": full_text,
        "utterances": utterances,
    }


async def _ensure_archive_recording_entry(
    db: AsyncSession,
    *,
    item_id: str,
) -> tuple[Recording, bool]:
    archive_index = _load_archive_recording_index()
    payload = archive_index.get(item_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="归档录音未找到")

    summary = payload["summary"]
    manifest = payload.get("manifest") or {}
    archive_metadata = payload.get("archive_metadata") or {}

    staged_file_name = _clean_text(manifest.get("stagedFileName")) or _clean_text(summary.get("staged_file_name")) or _clean_text(summary.get("display_file_name"))
    if not staged_file_name:
        raise HTTPException(status_code=422, detail="归档录音缺少文件名，暂不能关联到诊单")

    audio_path = _resolve_archive_recording_audio_path(archive_metadata, manifest)
    if audio_path is None or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="归档音频文件不存在，暂不能关联到诊单")

    transcript_path_text = _clean_text(manifest.get("transcriptPath"))
    transcript_path = _resolve_archive_manifest_file_path(transcript_path_text) if transcript_path_text else None
    transcript_payload = _read_json_file(transcript_path) if transcript_path else None
    if not transcript_payload:
        raise HTTPException(status_code=422, detail="当前录音还没有可用的 ASR 转写结果，暂不能推荐关联到诊单")

    utterances = transcript_payload.get("utterances") if isinstance(transcript_payload.get("utterances"), list) else []
    full_text = _clean_text(transcript_payload.get("fullText")) or ""
    if not utterances and not full_text:
        raise HTTPException(status_code=422, detail="当前录音转写内容为空，暂不能推荐关联到诊单")

    remote_created_at = (
        _coerce_datetime(manifest.get("remoteCreatedAt"))
        or _coerce_datetime(summary.get("create_time"))
        or _coerce_datetime(manifest.get("createdAt"))
        or _coerce_datetime(archive_metadata.get("createTimeMs"))
        or datetime.now(timezone.utc)
    )
    resolved_device_code = _clean_text(manifest.get("deviceCode")) or _clean_text(summary.get("device_code")) or _clean_text(summary.get("sn"))
    history_by_code = await load_device_staff_history(db, [resolved_device_code] if resolved_device_code else [])
    historical_staff = resolve_device_staff_binding(
        history_by_code,
        device_code=resolved_device_code,
        occurred_at=remote_created_at,
    )
    historical_staff_id = _clean_text((historical_staff or {}).get("staff_id"))
    known_history_staff_ids = {
        _clean_text(entry.get("staff_id"))
        for entry in history_by_code.get(resolved_device_code or "", [])
        if _clean_text(entry.get("staff_id"))
    }
    updated_at = _coerce_datetime(manifest.get("updatedAt")) or remote_created_at
    duration_seconds = _coerce_int(manifest.get("durationSeconds"))
    duration_ms = _coerce_int(transcript_payload.get("durationMs")) or _coerce_int(summary.get("duration_ms"))
    if duration_seconds is None and duration_ms is not None and duration_ms > 0:
        duration_seconds = max(1, duration_ms // 1000)

    analysis_path = _resolve_archive_analysis_result_path(None, manifest)
    analysis_input_path = _resolve_archive_manifest_file_path(manifest.get("analysisInputPath"))
    analysis_raw = _read_json_file(analysis_input_path) if analysis_input_path else None
    if analysis_raw is None:
        analysis_raw = transcript_payload
    analysis_result = _refresh_archive_analysis_result(
        analysis_path,
        _read_json_file(analysis_path) if analysis_path else None,
        raw=analysis_raw,
    )
    overall_score = None
    if isinstance(analysis_result, dict):
        overall_score = extract_preferred_overall_score(analysis_result)

    settings = get_settings()
    existing = (
        await db.execute(
            select(Recording)
            .where(or_(Recording.file_name == staged_file_name, Recording.file_path == settings.make_relative_path(audio_path)))
            .options(
                selectinload(Recording.visit),
                selectinload(Recording.visit_links).selectinload(RecordingVisitLink.visit),
            )
            .order_by(Recording.created_at.desc())
        )
    ).scalars().first()

    created = False
    if existing is None:
        existing = Recording(
            id=_new_id(),
            file_name=staged_file_name,
            file_path=settings.make_relative_path(audio_path),
            file_size=_coerce_int(manifest.get("fileSize")) or _coerce_int(summary.get("file_size")),
            duration_seconds=duration_seconds,
            status="uploaded",
            staff_id=historical_staff_id or _clean_text(manifest.get("staffId")) or _clean_text(summary.get("staff_id")),
            device_id=_clean_text(manifest.get("deviceId")) or resolved_device_code,
            created_at=remote_created_at,
            updated_at=updated_at,
        )
        db.add(existing)
        await db.flush()
        created = True
    else:
        existing.file_path = settings.make_relative_path(audio_path)
        existing.file_size = _coerce_int(manifest.get("fileSize")) or _coerce_int(summary.get("file_size"))
        existing.duration_seconds = duration_seconds
        if historical_staff_id and (
            existing.staff_id is None
            or existing.staff_id == _clean_text(manifest.get("staffId"))
            or existing.staff_id == _clean_text(summary.get("staff_id"))
            or existing.staff_id in known_history_staff_ids
        ):
            existing.staff_id = historical_staff_id
        elif existing.staff_id is None:
            existing.staff_id = _clean_text(manifest.get("staffId")) or _clean_text(summary.get("staff_id"))
        existing.device_id = _clean_text(manifest.get("deviceId")) or existing.device_id or resolved_device_code
        existing.updated_at = updated_at

    recording_id = existing.id
    transcript = (
        await db.execute(select(Transcript).where(Transcript.recording_id == recording_id))
    ).scalar_one_or_none()
    if transcript is None:
        transcript = Transcript(recording_id=recording_id)
        db.add(transcript)
    transcript.asr_provider = _clean_text(transcript_payload.get("asrProvider")) or "archive_import"
    transcript.asr_task_id = _clean_text(summary.get("stage_key"))
    transcript.status = "completed"
    transcript.full_text = full_text
    transcript.utterances = utterances
    transcript.duration_ms = duration_ms
    transcript.error_message = None
    transcript.completed_at = updated_at

    existing.transcript_text = full_text
    existing.transcript_segments = utterances
    existing.status = "analyzed" if analysis_result else "transcribed"

    analysis_file_name = f"recording_{recording_id}.json"
    task = (
        await db.execute(
            select(AnalysisTask)
            .where(AnalysisTask.file_name == analysis_file_name)
            .order_by(AnalysisTask.created_at.desc())
        )
    ).scalars().first()
    should_dispatch_analysis_after_commit = False
    if analysis_result:
        try:
            analysis_result = await attach_unlinked_sap_preview_to_result(db, recording_id, analysis_result) or analysis_result
        except Exception as exc:
            logger.warning("failed to attach SAP preview to archive analysis result recording_id=%s: %s", recording_id, exc)
        if task is None:
            task = AnalysisTask(
                file_name=analysis_file_name,
                file_path=settings.make_relative_path(settings.upload_path / "analysis_input" / analysis_file_name),
            )
            db.add(task)

        task.status = "done"
        task.progress = 100
        task.error_message = None
        task.result = analysis_result
        task.duration_ms = duration_ms
        task.segment_count = len(utterances)
        task.overall_score = overall_score
        task.completed_at = updated_at
    else:
        if task is not None and task.status in {"pending", "failed"}:
            await db.delete(task)
            task = None
        should_dispatch_analysis_after_commit = task is None

    analysis_input_dest = settings.upload_path / "analysis_input" / analysis_file_name
    if analysis_result:
        copied_input = _copy_if_exists(analysis_input_path, analysis_input_dest)
        if not copied_input and isinstance(analysis_raw, dict):
            analysis_input_dest.parent.mkdir(parents=True, exist_ok=True)
            analysis_input_dest.write_text(json.dumps(analysis_raw, ensure_ascii=False), encoding="utf-8")
    if analysis_result:
        result_dest = settings.results_path / f"recording_{recording_id}.result.json"
        result_dest.parent.mkdir(parents=True, exist_ok=True)
        result_dest.write_text(json.dumps(analysis_result, ensure_ascii=False), encoding="utf-8")

    await db.commit()
    await _sync_visit_orders_for_recording_context(db, existing)

    if should_dispatch_analysis_after_commit:
        try:
            await create_or_dispatch_recording_analysis(db, recording_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "failed to auto-dispatch archive recording analysis item=%s recording_id=%s error=%s",
                item_id,
                recording_id,
                exc,
            )

    ensured = (
        await db.execute(
            select(Recording)
            .where(Recording.id == recording_id)
            .options(
                selectinload(Recording.visit),
                selectinload(Recording.visit_links).selectinload(RecordingVisitLink.visit),
            )
        )
    ).scalar_one()
    return ensured, created


async def _upsert_local_device_cache(
    db: AsyncSession,
    *,
    sn: str,
    name: str | None,
    status: str | None = None,
    battery_level: int | None = None,
    dingtalk_team_code: str | None = None,
    dingtalk_user_id: str | None = None,
) -> None:
    device = (await db.execute(select(Device).where(Device.device_code == sn).limit(1))).scalar_one_or_none()
    display_name = name or sn
    if device is None:
        device = Device(
            name=display_name,
            device_code=sn,
            status=status or "offline",
            battery_level=battery_level,
            dingtalk_team_code=dingtalk_team_code,
            dingtalk_user_id=dingtalk_user_id,
            dingtalk_binding_synced_at=datetime.now(timezone.utc)
            if dingtalk_team_code or dingtalk_user_id
            else None,
            is_active=True,
        )
        db.add(device)
        await db.commit()
        await db.refresh(device)
        if battery_level is not None:
            await handle_device_battery_update(db, device, battery_level=battery_level)
        return

    changed = False
    if display_name and device.name != display_name:
        device.name = display_name
        changed = True
    if status and device.status != status:
        device.status = status
        changed = True
    if battery_level is not None and device.battery_level != battery_level:
        device.battery_level = battery_level
        changed = True
    if dingtalk_team_code is not None and device.dingtalk_team_code != dingtalk_team_code:
        device.dingtalk_team_code = dingtalk_team_code
        changed = True
    if dingtalk_user_id is not None and device.dingtalk_user_id != dingtalk_user_id:
        device.dingtalk_user_id = dingtalk_user_id
        changed = True
    if (dingtalk_team_code or dingtalk_user_id) and device.dingtalk_binding_synced_at is None:
        device.dingtalk_binding_synced_at = datetime.now(timezone.utc)
        changed = True
    if not device.is_active:
        device.is_active = True
        changed = True

    if changed:
        await db.commit()
        await db.refresh(device)
    if battery_level is not None:
        await handle_device_battery_update(db, device, battery_level=battery_level)


async def _sync_local_device_cache_from_remote(
    db: AsyncSession,
    devices: list[dict],
    *,
    hospital_code: str | None = None,
    hospital_short_name: str | None = None,
    update_dingtalk_binding: bool = True,
) -> None:
    normalized_devices: list[tuple[str, str, str | None, int | None, str | None, str | None]] = []
    for item in devices:
        sn = _clean_text(item.get("sn"))
        if not sn:
            continue
        name = _clean_text(item.get("name")) or sn
        status = _normalize_device_status_value(item.get("status"))
        battery_level = _normalize_device_battery_value(item.get("battery"))
        dingtalk_team_code = _clean_text(item.get("teamCode"))
        dingtalk_user_id = _clean_text(item.get("userId"))
        normalized_devices.append((sn, name, status, battery_level, dingtalk_team_code, dingtalk_user_id))

    if not normalized_devices:
        return

    existing_devices = {
        row.device_code: row
        for row in (
            await db.execute(
                select(Device).where(Device.device_code.in_([item[0] for item in normalized_devices]))
            )
        ).scalars().all()
    }

    changed = False
    synced_at = datetime.now(timezone.utc)
    for sn, name, status, battery_level, dingtalk_team_code, dingtalk_user_id in normalized_devices:
        device = existing_devices.get(sn)
        if device is None:
            device = Device(
                name=name,
                device_code=sn,
                status=status or "offline",
                battery_level=battery_level,
                dingtalk_team_code=dingtalk_team_code,
                dingtalk_user_id=dingtalk_user_id,
                dingtalk_binding_synced_at=synced_at if dingtalk_team_code or dingtalk_user_id else None,
                hospital_code=hospital_code,
                hospital_short_name=hospital_short_name,
                is_active=True,
            )
            db.add(device)
            existing_devices[sn] = device
            changed = True
            continue

        if name and device.name != name:
            device.name = name
            changed = True
        if status and device.status != status:
            device.status = status
            changed = True
        if battery_level is not None and device.battery_level != battery_level:
            device.battery_level = battery_level
            changed = True
        if update_dingtalk_binding:
            if device.dingtalk_team_code != dingtalk_team_code:
                device.dingtalk_team_code = dingtalk_team_code
                changed = True
            if device.dingtalk_user_id != dingtalk_user_id:
                device.dingtalk_user_id = dingtalk_user_id
                changed = True
            if dingtalk_team_code or dingtalk_user_id:
                device.dingtalk_binding_synced_at = synced_at
                changed = True
        if hospital_code and device.hospital_code != hospital_code:
            device.hospital_code = hospital_code
            changed = True
        if hospital_short_name and device.hospital_short_name != hospital_short_name:
            device.hospital_short_name = hospital_short_name
            changed = True
        if not device.is_active:
            device.is_active = True
            changed = True

    if changed:
        await db.commit()
        for device in existing_devices.values():
            await db.refresh(device)

    for sn, _name, _status, battery_level, _dingtalk_team_code, _dingtalk_user_id in normalized_devices:
        if battery_level is None:
            continue
        device = existing_devices.get(sn)
        if device is not None:
            await handle_device_battery_update(db, device, battery_level=battery_level)


def _normalize_remote_status_rows(status_rows: list[dict]) -> list[tuple[str, str | None, int | None]]:
    normalized: list[tuple[str, str | None, int | None]] = []
    for item in status_rows:
        sn = _clean_text(item.get("sn"))
        if not sn:
            continue
        status = _normalize_device_status_value(item.get("status"))
        if status is None and isinstance(item.get("online"), bool):
            status = "online" if item.get("online") else "offline"
        battery_level = _normalize_device_battery_value(item.get("battery"))
        normalized.append((sn, status, battery_level))
    return normalized


def _iot_hospital_short_name(hospital_code: str | None, current_user: User | None = None) -> str | None:
    normalized = _clean_text(hospital_code)
    if not normalized:
        return None
    user_hospital_code = _clean_text(getattr(current_user, "hospital_code", None))
    user_hospital_name = _clean_text(getattr(current_user, "hospital_name", None))
    if user_hospital_code == normalized and user_hospital_name:
        return user_hospital_name
    if normalized == "6501":
        return "长沙雅美"
    return normalized


def _effective_iot_hospital_code(scope: Any, requested_hospital_code: str | None) -> str | None:
    if is_global_role(getattr(scope, "role", None)):
        return requested_hospital_code if is_iot_hospital_code(requested_hospital_code) else None
    scope_hospital_code = _clean_text(getattr(scope, "hospital_code", None))
    return scope_hospital_code if is_iot_hospital_code(scope_hospital_code) else None


def _device_should_use_iot(device: Device | None) -> bool:
    return bool(device is not None and is_iot_hospital_code(device.hospital_code))


async def _query_remote_device_statuses(sn_list: list[str]) -> list[dict]:
    status_rows: list[dict] = []
    unique_sn_list = list(dict.fromkeys(sn for sn in sn_list if _clean_text(sn)))
    for index in range(0, len(unique_sn_list), 20):
        chunk = unique_sn_list[index:index + 20]
        if not chunk:
            continue
        payload = await dvi_query_device_status(chunk)
        status_rows.extend(item for item in payload.get("result") or [] if isinstance(item, dict))
    return status_rows


async def _query_remote_device_statuses_for_devices(
    db: AsyncSession,
    sn_list: list[str],
) -> list[dict]:
    unique_sn_list = list(dict.fromkeys(sn for sn in sn_list if _clean_text(sn)))
    if not unique_sn_list:
        return []

    local_devices = {
        row.device_code: row
        for row in (
            await db.execute(select(Device).where(Device.device_code.in_(unique_sn_list)))
        ).scalars().all()
    }
    iot_codes = [sn for sn in unique_sn_list if _device_should_use_iot(local_devices.get(sn))]
    dvi_codes = [sn for sn in unique_sn_list if sn not in set(iot_codes)]

    status_rows: list[dict] = []
    if iot_codes:
        if len(iot_codes) == 1:
            status_rows.extend(await iot_query_device_statuses(iot_codes))
        else:
            requested_iot_codes = set(iot_codes)
            iot_rows = await iot_list_devices()
            matched_rows = [
                row
                for row in iot_rows
                if (row_sn := _clean_text(row.get("sn"))) and row_sn in requested_iot_codes
            ]
            status_rows.extend(matched_rows)
            found_codes = {_clean_text(row.get("sn")) for row in matched_rows}
            missing_codes = sorted(code for code in requested_iot_codes if code not in found_codes)
            if 0 < len(missing_codes) <= 3:
                status_rows.extend(await iot_query_device_statuses(missing_codes))
    if dvi_codes:
        status_rows.extend(await _query_remote_device_statuses(dvi_codes))
    return status_rows


async def _sync_local_device_status_from_remote(
    db: AsyncSession,
    status_rows: list[dict],
) -> dict[str, dict[str, object]]:
    normalized_statuses = _normalize_remote_status_rows(status_rows)
    if not normalized_statuses:
        return {}

    existing_devices = {
        row.device_code: row
        for row in (
            await db.execute(
                select(Device).where(Device.device_code.in_([item[0] for item in normalized_statuses]))
            )
        ).scalars().all()
    }

    changed = False
    for sn, status, battery_level in normalized_statuses:
        device = existing_devices.get(sn)
        if device is None:
            device = Device(
                name=sn,
                device_code=sn,
                status=status or "offline",
                battery_level=battery_level,
                is_active=True,
            )
            db.add(device)
            existing_devices[sn] = device
            changed = True
            continue
        if status and device.status != status:
            device.status = status
            changed = True
        if battery_level is not None and device.battery_level != battery_level:
            device.battery_level = battery_level
            changed = True

    if changed:
        await db.commit()
        for device in existing_devices.values():
            await db.refresh(device)

    for sn, _status, battery_level in normalized_statuses:
        if battery_level is None:
            continue
        device = existing_devices.get(sn)
        if device is not None:
            await handle_device_battery_update(db, device, battery_level=battery_level)

    return {
        sn: {"status": status, "battery": battery_level}
        for sn, status, battery_level in normalized_statuses
    }


async def _load_system_binding_map(
    db: AsyncSession,
    device_codes: list[str],
) -> dict[str, dict[str, object]]:
    if not device_codes:
        return {}

    now = datetime.now(timezone.utc)

    def normalize_dt(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def iso_dt(value: datetime | None) -> str | None:
        normalized = normalize_dt(value)
        return normalized.isoformat() if normalized is not None else None

    def serialize_row(row: Any, *, binding_status: str) -> dict[str, object]:
        return {
            "deviceId": row.device_id,
            "staffId": row.staff_id,
            "staffName": row.staff_name,
            "externalAccount": row.external_account,
            "hospitalCode": row.staff_hospital_code,
            "hospitalShortName": row.hospital_short_name,
            "deviceHospitalCode": row.device_hospital_code,
            "deviceHospitalShortName": row.device_hospital_short_name,
            "positionName": row.position_name,
            "isActive": bool(row.is_active),
            "accountOpened": row.account_username is not None,
            "accountUsername": row.account_username,
            "accountIsActive": row.account_is_active,
            "bindingStatus": binding_status,
            "effectiveStart": iso_dt(getattr(row, "effective_start", None)),
            "effectiveEnd": iso_dt(getattr(row, "effective_end", None)),
        }

    linked_user_sq = (
        select(
            User.staff_id.label("staff_id"),
            User.username.label("account_username"),
            User.is_active.label("account_is_active"),
            func.row_number()
            .over(
                partition_by=User.staff_id,
                order_by=(User.updated_at.desc(), User.created_at.desc(), User.id.desc()),
            )
            .label("row_num"),
        )
        .where(User.staff_id.is_not(None))
        .subquery()
    )

    binding_rows = (
        await db.execute(
            select(
                Device.device_code.label("device_code"),
                Device.id.label("device_id"),
                Device.hospital_code.label("device_hospital_code"),
                Device.hospital_short_name.label("device_hospital_short_name"),
                Staff.id.label("staff_id"),
                Staff.name.label("staff_name"),
                Staff.external_account.label("external_account"),
                Staff.hospital_code.label("staff_hospital_code"),
                Staff.hospital_short_name.label("hospital_short_name"),
                Staff.is_active.label("is_active"),
                PositionProfile.name.label("position_name"),
                DeviceStaffBinding.effective_from.label("effective_start"),
                DeviceStaffBinding.effective_to.label("effective_end"),
                DeviceStaffBinding.created_at.label("binding_created_at"),
                linked_user_sq.c.account_username,
                linked_user_sq.c.account_is_active,
            )
            .select_from(Device)
            .join(DeviceStaffBinding, DeviceStaffBinding.device_id == Device.id)
            .join(Staff, Staff.id == DeviceStaffBinding.staff_id)
            .outerjoin(PositionProfile, PositionProfile.id == Staff.position_id)
            .outerjoin(
                linked_user_sq,
                and_(linked_user_sq.c.staff_id == Staff.id, linked_user_sq.c.row_num == 1),
            )
            .where(
                Device.device_code.in_(device_codes),
                or_(
                    DeviceStaffBinding.effective_to.is_(None),
                    DeviceStaffBinding.effective_to > now,
                ),
            )
            .order_by(
                Device.device_code.asc(),
                DeviceStaffBinding.effective_from.asc(),
                DeviceStaffBinding.created_at.asc(),
                DeviceStaffBinding.id.asc(),
            )
        )
    ).all()

    result: dict[str, dict[str, object]] = {}
    binding_rows_by_code: dict[str, list[tuple[Any, datetime | None, datetime | None, bool]]] = {}
    for row in binding_rows:
        if not row.staff_id:
            continue
        start_at = normalize_dt(row.effective_start)
        end_at = normalize_dt(row.effective_end)
        is_active_now = (start_at is None or start_at <= now) and (end_at is None or end_at > now)
        is_future = start_at is not None and start_at > now
        if not is_active_now and not is_future:
            continue
        binding_rows_by_code.setdefault(row.device_code, []).append((row, start_at, end_at, is_active_now))

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    far_future = datetime(9999, 12, 31, tzinfo=timezone.utc)
    for device_code, candidates in binding_rows_by_code.items():
        active_candidates = [item for item in candidates if item[3]]
        if active_candidates:
            selected = max(
                active_candidates,
                key=lambda item: (
                    item[1] or epoch,
                    normalize_dt(getattr(item[0], "binding_created_at", None)) or epoch,
                ),
            )
            result[device_code] = serialize_row(selected[0], binding_status="active")
            continue

        selected = min(
            candidates,
            key=lambda item: (
                item[1] or far_future,
                normalize_dt(getattr(item[0], "binding_created_at", None)) or far_future,
            ),
        )
        result[device_code] = serialize_row(selected[0], binding_status="scheduled")

    rows = (
        await db.execute(
            select(
                Device.device_code.label("device_code"),
                Device.id.label("device_id"),
                Device.hospital_code.label("device_hospital_code"),
                Device.hospital_short_name.label("device_hospital_short_name"),
                Staff.id.label("staff_id"),
                Staff.name.label("staff_name"),
                Staff.external_account.label("external_account"),
                Staff.hospital_code.label("staff_hospital_code"),
                Staff.hospital_short_name.label("hospital_short_name"),
                Staff.is_active.label("is_active"),
                PositionProfile.name.label("position_name"),
                linked_user_sq.c.account_username,
                linked_user_sq.c.account_is_active,
            )
            .outerjoin(Staff, Staff.id == Device.staff_id)
            .outerjoin(PositionProfile, PositionProfile.id == Staff.position_id)
            .outerjoin(
                linked_user_sq,
                and_(linked_user_sq.c.staff_id == Staff.id, linked_user_sq.c.row_num == 1),
            )
            .where(Device.device_code.in_(device_codes))
        )
    ).all()

    for row in rows:
        if not row.staff_id or row.device_code in result:
            continue
        result[row.device_code] = serialize_row(row, binding_status="active")
    return result


# ── 企业工牌配置 ─────────────────────────────────────

class ConfigureBadgeRequest(BaseModel):
    code_identity: str = "DT_IDENTITY"
    status: str = "OPEN"


@router.post("/configure-badge")
async def api_configure_badge(body: ConfigureBadgeRequest):
    """为企业开通钉工牌电子码。"""
    try:
        return await configure_corp_badge(
            code_identity=body.code_identity,
            status=body.status,
        )
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)


# ── 电子码 CRUD ──────────────────────────────────────

class AvailableTime(BaseModel):
    gmt_start: str = Field(..., alias="gmtStart")
    gmt_end: str = Field(..., alias="gmtEnd")

    model_config = {"populate_by_name": True}


class CreateBadgeCodeRequest(BaseModel):
    request_id: str
    code_identity: str = "DT_VISITOR"
    user_identity: str
    user_corp_relation_type: str = "INTERNAL_STAFF"
    status: str = "OPEN"
    code_value: str | None = None
    code_value_type: str | None = None
    gmt_expired: str | None = None
    available_times: list[AvailableTime] | None = None
    ext_info: dict[str, str] | None = None


@router.post("/badge-codes")
async def api_create_badge_code(body: CreateBadgeCodeRequest):
    """创建钉工牌电子码（访客码/会展码）。"""
    times = None
    if body.available_times:
        times = [{"gmtStart": t.gmt_start, "gmtEnd": t.gmt_end} for t in body.available_times]
    try:
        return await create_badge_code(
            request_id=body.request_id,
            code_identity=body.code_identity,
            user_identity=body.user_identity,
            user_corp_relation_type=body.user_corp_relation_type,
            status=body.status,
            code_value=body.code_value,
            code_value_type=body.code_value_type,
            gmt_expired=body.gmt_expired,
            available_times=times,
            ext_info=body.ext_info,
        )
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)


class UpdateBadgeCodeRequest(BaseModel):
    code_id: str
    code_identity: str = "DT_VISITOR"
    user_identity: str
    user_corp_relation_type: str = "INTERNAL_STAFF"
    status: str | None = None
    code_value: str | None = None
    gmt_expired: str | None = None
    available_times: list[AvailableTime] | None = None
    ext_info: dict[str, str] | None = None


@router.put("/badge-codes")
async def api_update_badge_code(body: UpdateBadgeCodeRequest):
    """更新钉工牌电子码。"""
    times = None
    if body.available_times:
        times = [{"gmtStart": t.gmt_start, "gmtEnd": t.gmt_end} for t in body.available_times]
    try:
        return await update_badge_code(
            code_id=body.code_id,
            code_identity=body.code_identity,
            user_identity=body.user_identity,
            user_corp_relation_type=body.user_corp_relation_type,
            status=body.status,
            code_value=body.code_value,
            gmt_expired=body.gmt_expired,
            available_times=times,
            ext_info=body.ext_info,
        )
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)


class DecodeBadgeCodeRequest(BaseModel):
    pay_code: str
    code_identity: str = "DT_IDENTITY"


@router.post("/badge-codes/decode")
async def api_decode_badge_code(body: DecodeBadgeCodeRequest):
    """解码钉工牌电子码。"""
    try:
        return await decode_badge_code(
            pay_code=body.pay_code,
            code_identity=body.code_identity,
        )
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)


class VerifyResultRequest(BaseModel):
    code_identity: str
    user_identity: str
    user_corp_relation_type: str = "INTERNAL_STAFF"
    verify_event: str
    verify_time: str
    verify_location: str | None = None


@router.post("/badge-codes/verify-result")
async def api_notify_verify_result(body: VerifyResultRequest):
    """同步钉工牌码验证结果。"""
    try:
        return await notify_badge_code_verify_result(
            code_identity=body.code_identity,
            user_identity=body.user_identity,
            user_corp_relation_type=body.user_corp_relation_type,
            verify_event=body.verify_event,
            verify_time=body.verify_time,
            verify_location=body.verify_location,
        )
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)


# ── DVI 设备管理 ───────────────────────────────────────

@router.get("/devices")
async def list_devices(
    sn: str | None = Query(None),
    team_code: str | None = Query(None, alias="teamCode"),
    user_id: str | None = Query(None, alias="userId"),
    hospital_code: str | None = Query(None, alias="hospitalCode"),
    sync_status: bool = Query(False, alias="syncStatus"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """查询设备列表（长沙雅美工牌走 IOT，其余机构走 DVI），并按当前账号机构过滤。"""
    sn = sn if isinstance(sn, str) else None
    team_code = team_code if isinstance(team_code, str) else None
    user_id = user_id if isinstance(user_id, str) else None
    hospital_code = hospital_code.strip() if isinstance(hospital_code, str) and hospital_code.strip() else None
    sync_status = sync_status if isinstance(sync_status, bool) else False
    scope = await build_permission_scope(current_user)
    iot_hospital_code = _effective_iot_hospital_code(scope, hospital_code)
    iot_hospital_short_name = _iot_hospital_short_name(iot_hospital_code, current_user)
    all_devices: list[dict] = []
    next_token = ""
    remote_devices_available = False
    should_query_remote_devices = sync_status or bool(sn)
    try:
        if should_query_remote_devices:
            if iot_hospital_code:
                all_devices = await asyncio.wait_for(
                    iot_list_devices(device_no=sn),
                    timeout=_DINGTALK_DEVICE_REMOTE_TIMEOUT_SECONDS,
                )
                await _sync_local_device_cache_from_remote(
                    db,
                    all_devices,
                    hospital_code=iot_hospital_code,
                    hospital_short_name=iot_hospital_short_name,
                    update_dingtalk_binding=False,
                )
            else:
                for _ in range(20):  # safety cap
                    page = await asyncio.wait_for(
                        dvi_list_devices(
                            max_results=20,
                            next_token=next_token,
                            sn=sn,
                            team_code=team_code,
                            user_id=user_id,
                        ),
                        timeout=_DINGTALK_DEVICE_REMOTE_TIMEOUT_SECONDS,
                    )
                    all_devices.extend(page.get("result") or [])
                    next_token = page.get("nextToken") or ""
                    if not next_token:
                        break
                await _sync_local_device_cache_from_remote(db, all_devices)
            remote_devices_available = True
    except (DingTalkConfigError, DingTalkApiError, TimeoutError) as exc:
        logger.warning("failed to list DingTalk remote devices; using local device cache: %r", exc)
        all_devices = []
        remote_devices_available = False
    try:
        remote_device_by_code = {
            device_sn: item
            for item in all_devices
            if (device_sn := _clean_text(item.get("sn")))
        }
        device_codes = list(remote_device_by_code)
        local_stmt = select(Device).where(Device.is_active.is_(True))
        if sn:
            local_stmt = local_stmt.where(Device.device_code == sn)
        if not is_global_role(scope.role):
            if not scope.hospital_code:
                local_stmt = None
            else:
                local_stmt = local_stmt.where(Device.hospital_code == scope.hospital_code)
        elif hospital_code:
            local_stmt = local_stmt.where(Device.hospital_code == hospital_code)
        if team_code or user_id:
            if local_stmt is not None:
                if remote_devices_available:
                    local_stmt = local_stmt.where(Device.device_code.in_(device_codes)) if device_codes else None
                elif team_code:
                    local_stmt = local_stmt.where(Device.dingtalk_team_code == team_code)
                if local_stmt is not None and user_id:
                    local_stmt = local_stmt.where(Device.dingtalk_user_id == user_id)
        local_devices = (
            {
                row.device_code: row
                for row in (await db.execute(local_stmt)).scalars().all()
                if _clean_text(row.device_code)
            }
            if local_stmt is not None
            else {}
        )
        visible_device_codes = set(local_devices)
        status_by_code: dict[str, dict[str, object]] = {}
        raw_status_by_code: dict[str, dict[str, object]] = {}
        if sync_status and visible_device_codes and remote_devices_available:
            try:
                if iot_hospital_code:
                    status_rows = [
                        remote_device_by_code[code]
                        for code in sorted(visible_device_codes)
                        if code in remote_device_by_code
                    ]
                else:
                    status_rows = await asyncio.wait_for(
                        _query_remote_device_statuses_for_devices(db, sorted(visible_device_codes)),
                        timeout=_DINGTALK_DEVICE_REMOTE_TIMEOUT_SECONDS,
                    )
                raw_status_by_code = {
                    row_sn: item
                    for item in status_rows
                    if (row_sn := _clean_text(item.get("sn")))
                }
                status_by_code = await _sync_local_device_status_from_remote(db, status_rows)
            except (DingTalkConfigError, DingTalkApiError, TimeoutError) as exc:
                logger.warning("failed to sync visible device status on list refresh: %r", exc)

        system_binding_map = await _load_system_binding_map(db, list(visible_device_codes))
        enriched_devices = []
        ordered_device_codes = [
            code for code in device_codes if code in visible_device_codes
        ] + sorted(visible_device_codes - set(device_codes))
        for current_sn in ordered_device_codes:
            item = remote_device_by_code.get(current_sn)
            local_device = local_devices.get(current_sn)
            if item is None:
                if local_device is None:
                    continue
                enriched = {
                    "sn": current_sn,
                    "name": local_device.name or current_sn,
                    "status": local_device.status,
                    "battery": local_device.battery_level,
                    "teamCode": local_device.dingtalk_team_code,
                    "userId": local_device.dingtalk_user_id,
                    "dingtalkBindingSyncedAt": local_device.dingtalk_binding_synced_at.isoformat()
                    if local_device.dingtalk_binding_synced_at
                    else None,
                    "dviAvailable": False,
                    "source": "local",
                }
            else:
                enriched = dict(item)
                if enriched.get("remoteProvider") == "iot":
                    enriched["iotAvailable"] = True
                    enriched["dviAvailable"] = False
                else:
                    enriched["dviAvailable"] = True
            if not current_sn:
                continue
            latest_raw_status = raw_status_by_code.get(current_sn)
            latest_status = status_by_code.get(current_sn)
            if latest_raw_status is not None:
                for field_name in ("status", "recordingStartTime", "firmware"):
                    if field_name in latest_raw_status:
                        enriched[field_name] = latest_raw_status[field_name]
                if "battery" in latest_raw_status:
                    raw_battery = latest_raw_status.get("battery")
                    normalized_battery = _normalize_device_battery_value(raw_battery)
                    if normalized_battery is not None:
                        # Keep DingTalk's timestamped payload for the frontend tooltip.
                        enriched["battery"] = raw_battery if isinstance(raw_battery, dict) else normalized_battery
                    elif latest_status and latest_status.get("battery") is not None:
                        enriched["battery"] = latest_status["battery"]
            if latest_status is not None:
                if latest_status.get("status") and not (latest_raw_status and "status" in latest_raw_status):
                    enriched["status"] = latest_status["status"]
                if latest_status.get("battery") is not None and not (latest_raw_status and "battery" in latest_raw_status):
                    enriched["battery"] = latest_status["battery"]
            enriched["hospitalCode"] = local_device.hospital_code if local_device else None
            enriched["hospitalShortName"] = local_device.hospital_short_name if local_device else None
            enriched["systemBinding"] = system_binding_map.get(current_sn or "", None)
            enriched_devices.append(enriched)
        return {"result": enriched_devices, "totalCount": len(enriched_devices)}
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)


class SnListRequest(BaseModel):
    sn_list: list[str] = Field(..., alias="snList")

    model_config = {"populate_by_name": True}


@router.post("/devices/status")
async def get_device_status(body: SnListRequest, db: AsyncSession = Depends(get_db)):
    """批量查询设备状态。"""
    try:
        status_rows = await _query_remote_device_statuses_for_devices(db, body.sn_list)
        await _sync_local_device_status_from_remote(
            db,
            [item for item in status_rows if isinstance(item, dict)],
        )
        return {"result": status_rows}
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)


@router.post("/devices/details")
async def get_device_details(body: SnListRequest, db: AsyncSession = Depends(get_db)):
    """批量查询设备详情。"""
    try:
        unique_sn_list = list(dict.fromkeys(sn for sn in body.sn_list if _clean_text(sn)))
        local_devices = {
            row.device_code: row
            for row in (
                await db.execute(select(Device).where(Device.device_code.in_(unique_sn_list)))
            ).scalars().all()
        }
        iot_codes = [sn for sn in unique_sn_list if _device_should_use_iot(local_devices.get(sn))]
        dvi_codes = [sn for sn in unique_sn_list if sn not in set(iot_codes)]
        rows: list[dict] = []
        if iot_codes:
            rows.extend(await iot_query_device_statuses(iot_codes))
        if dvi_codes:
            payload = await dvi_query_device_detail(dvi_codes)
            rows.extend(item for item in payload.get("result") or [] if isinstance(item, dict))
        return {"result": rows}
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)


# ── DVI 设备绑定/解绑 ──────────────────────────────────

class BindDeviceRequest(BaseModel):
    sn: str
    team_code: str = Field(..., alias="teamCode")
    user_id: str = Field(..., alias="userId")

    model_config = {"populate_by_name": True}


@router.post("/devices/bind")
async def bind_device(body: BindDeviceRequest, db: AsyncSession = Depends(get_db)):
    """绑定设备到用户。"""
    try:
        payload = await dvi_update_device_binding(
            action="bind",
            sn=body.sn,
            team_code=body.team_code,
            user_id=body.user_id,
        )
        await _upsert_local_device_cache(
            db,
            sn=body.sn,
            name=body.sn,
            dingtalk_team_code=body.team_code,
            dingtalk_user_id=body.user_id,
        )
        return payload
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)


@router.post("/devices/unbind")
async def unbind_device(body: BindDeviceRequest, db: AsyncSession = Depends(get_db)):
    """解绑设备。"""
    try:
        payload = await dvi_update_device_binding(
            action="unbind",
            sn=body.sn,
            team_code=body.team_code,
            user_id=body.user_id,
        )
        device = (await db.execute(select(Device).where(Device.device_code == body.sn).limit(1))).scalar_one_or_none()
        if device is not None:
            device.dingtalk_team_code = None
            device.dingtalk_user_id = None
            device.dingtalk_binding_synced_at = datetime.now(timezone.utc)
            await db.commit()
        return payload
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)


class SystemBindDeviceRequest(BaseModel):
    sn: str
    staff_id: str = Field(..., alias="staffId")
    device_name: str | None = Field(None, alias="deviceName")
    effective_start: datetime | None = Field(None, alias="effectiveStart")
    effective_end: datetime | None = Field(None, alias="effectiveEnd")
    override_overlap: bool = Field(False, alias="overrideOverlap")
    effective_at: datetime | None = Field(None, alias="effectiveAt")

    model_config = {"populate_by_name": True}


class SystemUnbindDeviceRequest(BaseModel):
    sn: str
    clear_history: bool = Field(False, alias="clearHistory")
    clear_recording_owners: bool = Field(False, alias="clearRecordingOwners")

    model_config = {"populate_by_name": True}


@router.post("/devices/system-bind")
async def bind_system_device(
    body: SystemBindDeviceRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    staff = await db.get(Staff, body.staff_id)
    if staff is None:
        raise HTTPException(404, "Staff not found")
    if not staff.is_active:
        raise HTTPException(400, "人员已停用，不能绑定工牌")
    scope = await build_permission_scope(current_user)
    if not is_global_role(scope.role):
        if not scope.hospital_code or staff.hospital_code != scope.hospital_code:
            raise HTTPException(403, "不能绑定其他机构人员")
        existing_device = await get_device_by_code(db, body.sn)
        if existing_device and not _device_visible_for_scope(existing_device, scope):
            raise HTTPException(403, "不能绑定其他机构工牌")

    try:
        device = await bind_staff_to_device(
            db,
            staff=staff,
            device_code=body.sn,
            device_name=body.device_name,
            effective_start=body.effective_start or body.effective_at,
            effective_end=body.effective_end,
            override_overlap=body.override_overlap,
        )
    except DeviceBindingOverlapError as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return {
        "success": True,
        "deviceCode": device.device_code,
        "staffId": staff.id,
    }


@router.post("/devices/system-unbind")
async def unbind_system_device(
    body: SystemUnbindDeviceRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not body.clear_history:
        raise HTTPException(400, "请先确认是否清空工牌归属者历史")

    scope = await build_permission_scope(current_user)
    existing_device = await get_device_by_code(db, body.sn)
    if existing_device and not _device_visible_for_scope(existing_device, scope):
        raise HTTPException(403, "不能解绑其他机构工牌")

    device = await clear_device_staff_history(
        db,
        device_code=body.sn,
        clear_recording_owners=body.clear_recording_owners,
    )
    clear_staged_device_staff_assignments(body.sn)
    return {
        "success": True,
        "deviceCode": body.sn,
        "staffId": device.staff_id if device else None,
        "clearedHistory": True,
        "clearedRecordingOwners": body.clear_recording_owners,
    }


# ── DVI 录音控制 ──────────────────────────────────────

class RecordingControlRequest(BaseModel):
    team_code: str = Field(..., alias="teamCode")
    user_id: str = Field(..., alias="userId")

    model_config = {"populate_by_name": True}


@router.post("/devices/recording/start")
async def start_recording(body: RecordingControlRequest):
    """发起录音。"""
    try:
        return await dvi_control_recording(
            action="start",
            team_code=body.team_code,
            user_id=body.user_id,
        )
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)


@router.post("/devices/recording/stop")
async def stop_recording(body: RecordingControlRequest):
    """停止录音。"""
    try:
        return await dvi_control_recording(
            action="stop",
            team_code=body.team_code,
            user_id=body.user_id,
        )
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)


@router.get("/devices/recording-durations")
async def list_recording_durations(
    max_results: int = Query(50, alias="maxResults", ge=1, le=100),
    next_token: str = Query("", alias="nextToken"),
    sn: str | None = Query(None),
    start_time: str | None = Query(None, alias="startTime"),
    end_time: str | None = Query(None, alias="endTime"),
    team_code: str | None = Query(None, alias="teamCode"),
    user_id: str | None = Query(None, alias="userId"),
):
    """查询录音时长。"""
    try:
        return await dvi_list_recording_durations(
            max_results=max_results,
            next_token=next_token,
            sn=sn,
            start_time=start_time,
            end_time=end_time,
            team_code=team_code,
            user_id=user_id,
        )
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)


# ── DVI 音频文件 ──────────────────────────────────────

class ListAudioRequest(BaseModel):
    sn: str
    start_timestamp: int | None = Field(None, alias="startTimestamp")
    end_timestamp: int | None = Field(None, alias="endTimestamp")

    model_config = {"populate_by_name": True}


class AudioImportRequest(BaseModel):
    sn_list: list[str] | None = Field(None, alias="snList")
    lookback_minutes: int = Field(240, alias="lookbackMinutes", ge=1, le=7 * 24 * 60)
    run_pipeline_inline: bool = Field(False, alias="runPipelineInline")

    model_config = {"populate_by_name": True}


class AudioImportItemOut(BaseModel):
    device_code: str = Field(..., alias="deviceCode")
    file_name: str = Field(..., alias="fileName")
    status: str
    message: str
    file_id: str | None = Field(None, alias="fileId")
    stage_key: str | None = Field(None, alias="stageKey")

    model_config = {"populate_by_name": True}


class AudioImportResultOut(BaseModel):
    imported: int
    skipped: int
    filtered: int
    failed: int
    queued: int
    items: list[AudioImportItemOut]


class AudioArchiveFileRequest(BaseModel):
    sn: str
    file_id: str = Field(..., alias="fileId")
    file_name: str | None = Field(None, alias="fileName")
    duration_ms: int | None = Field(None, alias="duration")
    file_size: int | None = Field(None, alias="fileSize")
    create_time_ms: int | None = Field(None, alias="createTime")
    download_url: str | None = Field(None, alias="downloadUrl")
    source: str | None = None
    overwrite: bool = False

    model_config = {"populate_by_name": True}


class AudioArchiveRequest(BaseModel):
    sn_list: list[str] | None = Field(None, alias="snList")
    overwrite: bool = False

    model_config = {"populate_by_name": True}


class AudioArchiveItemOut(BaseModel):
    sn: str
    file_id: str = Field(..., alias="fileId")
    status: str
    saved_path: str | None = Field(None, alias="savedPath")
    message: str | None = None

    model_config = {"populate_by_name": True}


class AudioArchiveResultOut(BaseModel):
    archive_root: str = Field(..., alias="archiveRoot")
    downloaded: int
    filtered: int = 0
    skipped: int
    failed: int
    items: list[AudioArchiveItemOut]


@router.post("/audio-files/list")
async def list_device_audio_files(
    body: ListAudioRequest,
    db: AsyncSession = Depends(get_db),
):
    """查询设备音频文件（自动分页获取全部）。"""
    all_files: list[dict] = []
    next_token = ""
    try:
        device = await get_device_by_code(db, body.sn)
        if _device_should_use_iot(device):
            all_files = await iot_list_audio_files(
                device_no=body.sn,
                start_timestamp=body.start_timestamp,
                end_timestamp=body.end_timestamp,
            )
        else:
            for _ in range(50):  # safety cap
                page = await dvi_list_audio_files(
                    body.sn,
                    max_results=20,
                    next_token=next_token,
                    start_timestamp=body.start_timestamp,
                    end_timestamp=body.end_timestamp,
                )
                all_files.extend(page.get("result") or [])
                next_token = page.get("nextToken") or ""
                if not next_token:
                    break
        return {"result": all_files, "totalCount": len(all_files)}
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)


@router.get("/audio-files/sync-status")
async def get_audio_sync_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    return await get_dingtalk_audio_sync_status_snapshot(
        db,
        task=getattr(request.app.state, "dingtalk_audio_sync_task", None),
        enabled=bool(getattr(request.app.state, "dingtalk_audio_sync_enabled", False)),
        started_at=getattr(request.app.state, "dingtalk_audio_sync_started_at", None),
        note=getattr(request.app.state, "dingtalk_audio_sync_note", None),
    )


@router.get("/audio-files/archive-sync-status")
async def get_audio_archive_sync_status(request: Request):
    return await get_dingtalk_audio_archive_sync_status_snapshot(
        task=getattr(request.app.state, "dingtalk_audio_archive_sync_task", None),
        enabled=bool(getattr(request.app.state, "dingtalk_audio_archive_sync_enabled", False)),
        started_at=getattr(request.app.state, "dingtalk_audio_archive_sync_started_at", None),
        note=getattr(request.app.state, "dingtalk_audio_archive_sync_note", None),
    )


async def _archive_managed_staff_ids_for_user(db: AsyncSession | None, user: User) -> set[str] | None:
    return await resolve_visible_staff_ids_for_user(db, user)


def _archive_item_staff_id(item: dict[str, Any]) -> str | None:
    return _clean_text(item.get("staff_id"))


def _archive_item_visible_to_staff_ids(item: dict[str, Any], visible_staff_ids: set[str] | None) -> bool:
    if visible_staff_ids is None:
        return True
    item_staff_id = _archive_item_staff_id(item)
    return bool(item_staff_id and item_staff_id in visible_staff_ids)


@router.get("/audio-archive/recordings", response_model=PaginatedResponse[DingtalkArchiveRecordingOut])
async def list_archive_recordings(
    keyword: str | None = Query(None),
    status: str | None = Query(None),
    staff_id: str | None = Query(None, alias="staffId"),
    link_state: str | None = Query(None, alias="linkState"),
    exclude_filtered: bool = Query(False, alias="excludeFiltered"),
    problem_only: bool = Query(False, alias="problemOnly"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    archive_index = _load_archive_recording_index()
    items = [payload["summary"] for payload in archive_index.values()]
    items = await _attach_archive_recording_bindings(db, items)

    normalized_keyword = _clean_text(keyword)
    normalized_status = _clean_text(status)
    requested_staff_id = _clean_text(staff_id)
    visible_staff_ids = await _archive_managed_staff_ids_for_user(db, current_user)
    normalized_link_state = _clean_text(link_state)

    def matches(item: dict[str, Any]) -> bool:
        if not _archive_item_visible_to_staff_ids(item, visible_staff_ids):
            return False
        if normalized_status and str(item.get("pipeline_status") or "").strip().lower() != normalized_status.lower():
            return False
        if requested_staff_id and _archive_item_staff_id(item) != requested_staff_id:
            return False
        current_status = str(item.get("pipeline_status") or "").strip().lower()
        if exclude_filtered and current_status in {"filtered", "failed"}:
            return False
        if problem_only and current_status not in {"filtered", "failed"}:
            return False
        if normalized_link_state == "linked" and not item.get("has_visit_link"):
            return False
        if normalized_link_state == "unlinked" and item.get("has_visit_link"):
            return False
        if normalized_link_state == "needs_link" and not item.get("needs_visit_link"):
            return False
        if normalized_keyword:
            haystack = " ".join(
                str(value or "")
                for value in (
                    item.get("display_file_name"),
                    item.get("archive_file_name"),
                    item.get("staged_file_name"),
                    item.get("remote_file_name"),
                    item.get("file_id"),
                    item.get("sn"),
                    item.get("device_code"),
                    item.get("staff_name"),
                    item.get("stage_key"),
                )
            ).lower()
            if normalized_keyword.lower() not in haystack:
                return False
        return True

    filtered = [item for item in items if matches(item)]
    filtered.sort(
        key=lambda item: (
            1 if item.get("needs_visit_link") else 0,
            1 if str(item.get("pipeline_status") or "") not in {"filtered", "failed"} else 0,
            item.get("create_time")
            or item.get("downloaded_at")
            or item.get("updated_at")
            or ""
        ),
        reverse=True,
    )

    total = len(filtered)
    start = (page - 1) * page_size
    end = start + page_size
    paged_items = [DingtalkArchiveRecordingOut.model_validate(item) for item in filtered[start:end]]
    return make_page_response(paged_items, total, page, page_size)


@router.get("/audio-archive/recordings/{item_id}", response_model=DingtalkArchiveRecordingDetailOut)
async def get_archive_recording_detail(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    archive_index = _load_archive_recording_index()
    payload = archive_index.get(item_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="归档录音未找到")

    summary = dict(payload["summary"])
    [summary] = await _attach_archive_recording_bindings(db, [summary])
    visible_staff_ids = await _archive_managed_staff_ids_for_user(db, current_user)
    if not _archive_item_visible_to_staff_ids(summary, visible_staff_ids):
        raise HTTPException(status_code=404, detail="归档录音未找到")
    manifest = payload.get("manifest")
    archive_metadata = payload.get("archive_metadata")

    transcript = await _resolve_archive_transcript(db, summary=summary, manifest=manifest)

    analysis_result = await _resolve_archive_analysis_result(
        db,
        summary=summary,
        manifest=manifest,
    )

    detail = {
        **summary,
        "manifest": manifest,
        "archive_metadata": archive_metadata,
        "transcript": transcript,
        "analysis_result": analysis_result,
        "analysis_summary": _build_archive_analysis_summary(summary, transcript, analysis_result),
    }
    return DingtalkArchiveRecordingDetailOut.model_validate(detail)


@router.post("/audio-archive/recordings/{item_id}/ensure-recording", response_model=DingtalkArchiveEnsureRecordingOut)
async def ensure_archive_recording(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    archive_index = _load_archive_recording_index()
    payload = archive_index.get(item_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="归档录音未找到")

    summary = dict(payload["summary"])
    [summary] = await _attach_archive_recording_bindings(db, [summary])
    visible_staff_ids = await _archive_managed_staff_ids_for_user(db, current_user)
    if not _archive_item_visible_to_staff_ids(summary, visible_staff_ids):
        raise HTTPException(status_code=404, detail="归档录音未找到")

    recording, created = await _ensure_archive_recording_entry(db, item_id=item_id)
    payload = {
        "item_id": item_id,
        "created_new_recording": created,
        **_serialize_archive_recording_binding(recording),
    }
    return DingtalkArchiveEnsureRecordingOut.model_validate(payload)


@router.get("/audio-archive/recordings/{item_id}/media")
async def get_archive_recording_media(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    archive_index = _load_archive_recording_index()
    payload = archive_index.get(item_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="归档录音未找到")

    summary = dict(payload["summary"])
    [summary] = await _attach_archive_recording_bindings(db, [summary])
    visible_staff_ids = await _archive_managed_staff_ids_for_user(db, current_user)
    if not _archive_item_visible_to_staff_ids(summary, visible_staff_ids):
        raise HTTPException(status_code=404, detail="归档录音未找到")

    audio_path = _resolve_archive_recording_audio_path(
        payload.get("archive_metadata"),
        payload.get("manifest"),
    )
    if audio_path is None or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="归档音频文件不存在")

    media_type = mimetypes.guess_type(audio_path.name)[0] or "audio/mpeg"
    return FileResponse(audio_path, media_type=media_type, filename=audio_path.name)


@router.get("/audio-files/{file_id}")
async def get_audio_info(file_id: str):
    """获取音频文件信息。"""
    try:
        return await dvi_get_audio_file_info(file_id)
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)


@router.get("/audio-files/{file_id}/download-url")
async def get_audio_download(file_id: str):
    """获取音频文件下载地址。"""
    try:
        return await dvi_get_audio_download_url(file_id)
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)


@router.post("/audio-files/archive-item", response_model=AudioArchiveItemOut)
async def archive_dingtalk_audio_item(body: AudioArchiveFileRequest):
    try:
        result = await archive_audio_item(
            RemoteAudioItem(
                sn=body.sn,
                file_id=body.file_id,
                file_name=body.file_name,
                duration_ms=body.duration_ms,
                file_size=body.file_size,
                create_time_ms=body.create_time_ms,
                download_url=body.download_url,
                source=body.source,
            ),
            overwrite=body.overwrite,
        )
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)

    return AudioArchiveItemOut(
        sn=result.sn,
        fileId=result.file_id,
        status=result.status,
        savedPath=str(result.saved_path) if result.saved_path else None,
        message=result.message,
    )


@router.post("/audio-files/archive", response_model=AudioArchiveResultOut)
async def archive_dingtalk_audio(body: AudioArchiveRequest):
    try:
        result = await archive_audio_files(
            sns=body.sn_list,
            overwrite=body.overwrite,
        )
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)

    return AudioArchiveResultOut(
        archiveRoot=str(get_archive_root()),
        downloaded=result.downloaded,
        filtered=result.filtered,
        skipped=result.skipped,
        failed=result.failed,
        items=[
            AudioArchiveItemOut(
                sn=item.sn,
                fileId=item.file_id,
                status=item.status,
                savedPath=str(item.saved_path) if item.saved_path else None,
                message=item.message,
            )
            for item in result.items
        ],
    )


@router.post("/audio-files/import", response_model=AudioImportResultOut)
async def import_dingtalk_audio(
    body: AudioImportRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await sync_dingtalk_audio_files(
            db,
            device_codes=body.sn_list,
            lookback_minutes=body.lookback_minutes,
            run_pipeline_inline=body.run_pipeline_inline,
        )
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)

    return AudioImportResultOut(
        imported=result.imported,
        skipped=result.skipped,
        filtered=result.filtered,
        failed=result.failed,
        queued=result.queued,
        items=[
            AudioImportItemOut(
                deviceCode=item.device_code,
                fileName=item.file_name,
                status=item.status,
                message=item.message,
                fileId=item.file_id,
                stageKey=item.stage_key,
            )
            for item in result.items
        ],
    )


# ── DVI 团队 ──────────────────────────────────────────

@router.get("/teams")
async def list_teams(
    max_results: int = Query(50, alias="maxResults", ge=1, le=100),
    next_token: str = Query("", alias="nextToken"),
):
    """查询团队列表。"""
    try:
        return await dvi_list_teams(max_results=max_results, next_token=next_token)
    except (DingTalkConfigError, DingTalkApiError) as exc:
        raise _handle(exc)
