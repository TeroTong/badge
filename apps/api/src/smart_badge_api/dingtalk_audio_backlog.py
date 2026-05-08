from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from smart_badge_api.api.audit import append_audit_log
from smart_badge_api.asr.tencent_task_registry import (
    build_tencent_task_registry_key,
    delete_tencent_task_registry_entries,
    list_tencent_task_registry_entries_for_source,
)
from smart_badge_api.asr.tencent_request_audit import load_local_tencent_request_events
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import AuditLog, Device, Staff
from smart_badge_api.db.session import _session_factory
from smart_badge_api.device_binding import load_device_staff_history, resolve_device_staff_binding
from smart_badge_api.dingtalk_audio_archive import get_archive_root
from smart_badge_api.dingtalk_audio_sync import (
    _clean_text,
    _coerce_datetime,
    _coerce_int,
    _coerce_remote_timestamp,
    _ensure_recording_stub_from_manifest,
    _ensure_stage_paths,
    _read_manifest,
    _stage_key,
    _write_manifest,
    execute_dingtalk_recording_pipeline,
)
from smart_badge_api.periodic_locks import DINGTALK_AUDIO_BACKLOG_LOCK_ID, periodic_advisory_lock

logger = logging.getLogger("smart_badge.dingtalk_audio_backlog")

BACKLOG_SYNC_AUDIT_MODULE = "录音管理"
BACKLOG_SYNC_AUDIT_ACTION = "钉钉归档补处理"
BACKLOG_SYNC_OPERATOR = "系统归档补处理"
BACKLOG_SYNC_IP = "dingtalk-backlog-sync"
@dataclass(slots=True)
class DeviceProfile:
    device_id: str | None
    staff_id: str | None
    staff_name: str | None
    staff_role: str | None


@dataclass(slots=True)
class DingtalkArchiveBacklogSyncResult:
    archive_items: int = 0
    staged_new: int = 0
    already_staged: int = 0
    processed_now: int = 0
    process_summary: dict[str, int] = field(default_factory=dict)
    final_archive_status: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class FailedManifestRecoveryDecision:
    mode: str = "none"
    clear_registry_keys: list[str] = field(default_factory=list)


def _iter_archive_metadata(archive_root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for meta_path in sorted(archive_root.rglob("*.json")):
        if meta_path.name.startswith("."):
            continue
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        file_id = _clean_text(payload.get("fileId"))
        sn = _clean_text(payload.get("sn"))
        audio_path = _clean_text(payload.get("audioPath"))
        if not file_id or not sn or not audio_path:
            continue
        status = (_clean_text(payload.get("status")) or "").lower()
        if not Path(audio_path).is_file() and status != "filtered":
            continue
        items.append(payload)
    return items


def _iter_stage_manifests(stage_paths: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for manifest_path in sorted(stage_paths.manifest_dir.glob("*.json")):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        stage_key = _clean_text(payload.get("stageKey"))
        if not stage_key:
            continue
        items.append(payload)
    return items


async def _load_device_profiles() -> dict[str, DeviceProfile]:
    async with _session_factory() as db:
        rows = (
            await db.execute(
                select(
                    Device.device_code,
                    Device.id,
                    Device.staff_id,
                    Staff.name,
                    Staff.role,
                ).join(Staff, Staff.id == Device.staff_id, isouter=True)
            )
        ).all()
        history_by_code = await load_device_staff_history(
            db,
            [str(device_code) for device_code, *_rest in rows if _clean_text(device_code)],
        )

    profiles: dict[str, DeviceProfile] = {}
    for device_code, device_id, staff_id, staff_name, staff_role in rows:
        normalized_code = _clean_text(device_code)
        if not normalized_code:
            continue
        resolved_staff = resolve_device_staff_binding(
            history_by_code,
            device_code=normalized_code,
            occurred_at=None,
        )
        profiles[normalized_code] = DeviceProfile(
            device_id=str(device_id) if device_id else None,
            staff_id=_clean_text((resolved_staff or {}).get("staff_id")) or (str(staff_id) if staff_id else None),
            staff_name=_clean_text((resolved_staff or {}).get("staff_name")) or _clean_text(staff_name),
            staff_role=_clean_text((resolved_staff or {}).get("staff_role")) or _clean_text(staff_role) or "consultant",
        )
    return profiles


def _build_manifest(
    archive_metadata: dict[str, Any],
    *,
    device_profile: DeviceProfile | None,
) -> dict[str, Any]:
    device_code = _clean_text(archive_metadata.get("sn")) or ""
    file_id = _clean_text(archive_metadata.get("fileId")) or ""
    audio_path = str(archive_metadata.get("audioPath") or "").strip()
    duration_ms = _coerce_int(archive_metadata.get("durationMs"))
    duration_seconds = max(duration_ms // 1000, 1) if duration_ms else None
    remote_created_at = _coerce_remote_timestamp(archive_metadata.get("createTimeMs"))
    now_iso = datetime.now(timezone.utc).isoformat()
    status = _clean_text(archive_metadata.get("status")) or "downloaded"
    return {
        "stageKey": _stage_key(device_code, file_id),
        "deviceCode": device_code,
        "deviceId": device_profile.device_id if device_profile else None,
        "staffId": device_profile.staff_id if device_profile else None,
        "staffName": device_profile.staff_name if device_profile else "",
        "staffRole": device_profile.staff_role if device_profile else "consultant",
        "fileId": file_id,
        "remoteFileName": _clean_text(archive_metadata.get("remoteFileName")),
        "stagedFileName": Path(audio_path).name,
        "audioPath": audio_path,
        "fileSize": _coerce_int(archive_metadata.get("fileSize")),
        "durationMs": duration_ms,
        "durationSeconds": duration_seconds,
        "remoteCreatedAt": remote_created_at.isoformat() if remote_created_at else None,
        "status": status,
        "qualityStage": _clean_text(archive_metadata.get("qualityStage")),
        "qualityReason": _clean_text(archive_metadata.get("qualityReason")),
        "createdAt": now_iso,
        "archiveDownloadedAt": _clean_text(archive_metadata.get("downloadedAt")),
    }


def _entry_has_submit_trace(
    entry: dict[str, Any],
    events: list[dict[str, Any]],
) -> bool:
    entry_chunk_index = _coerce_int(entry.get("chunk_index"))
    entry_chunk_count = _coerce_int(entry.get("chunk_count"))
    for event in events:
        event_chunk_index = _coerce_int(event.get("chunk_index"))
        event_chunk_count = _coerce_int(event.get("chunk_count"))
        if (
            entry_chunk_index is not None
            and entry_chunk_count is not None
            and event_chunk_index is not None
            and event_chunk_count is not None
            and (event_chunk_index != entry_chunk_index or event_chunk_count != entry_chunk_count)
        ):
            continue
        if _coerce_int(event.get("task_id")) is not None:
            return True
        if str(event.get("status") or "").strip() in {"submitted", "completed", "task_failed"}:
            return True
    return False


def _is_tencent_download_failure(manifest: dict[str, Any], local_events: list[dict[str, Any]]) -> bool:
    retryable_markers = ("Failed to download audio file", "FailedOperation.NoSuchTask", "NoSuchTask")
    error_message = _clean_text(manifest.get("errorMessage")) or ""
    if any(marker in error_message for marker in retryable_markers):
        return True
    for event in local_events:
        if str(event.get("status") or "").strip() != "task_failed":
            continue
        event_error = _clean_text(event.get("error_message")) or ""
        if any(marker in event_error for marker in retryable_markers):
            return True
    return False


def _failed_manifest_recovery_decision(manifest: dict[str, Any]) -> FailedManifestRecoveryDecision:
    analysis_result_path = _existing_artifact_path(manifest.get("analysisResultPath"))
    if analysis_result_path is not None:
        return FailedManifestRecoveryDecision()
    transcript_path = _existing_artifact_path(manifest.get("transcriptPath"))
    if transcript_path is not None:
        return FailedManifestRecoveryDecision(mode="retry_analysis_only")
    stage_key = _clean_text(manifest.get("stageKey"))
    if not stage_key:
        return FailedManifestRecoveryDecision()

    registry_entries = list_tencent_task_registry_entries_for_source(stage_key)
    local_events = [
        event
        for event in load_local_tencent_request_events()
        if _clean_text(event.get("source_id")) == stage_key
    ]

    if _is_tencent_download_failure(manifest, local_events):
        clear_registry_keys = [
            build_tencent_task_registry_key(
                source_id=stage_key,
                chunk_index=_coerce_int(entry.get("chunk_index")) or 0,
                chunk_count=_coerce_int(entry.get("chunk_count")) or 0,
            )
            for entry in registry_entries
        ]
        return FailedManifestRecoveryDecision(
            mode="retry_pre_submit",
            clear_registry_keys=clear_registry_keys,
        )

    clear_registry_keys: list[str] = []
    has_resumable_task = False
    has_blocking_submit_trace = False

    for entry in registry_entries:
        task_id = _coerce_int(entry.get("task_id"))
        if task_id is not None:
            has_resumable_task = True
            continue
        key = build_tencent_task_registry_key(
            source_id=stage_key,
            chunk_index=_coerce_int(entry.get("chunk_index")) or 0,
            chunk_count=_coerce_int(entry.get("chunk_count")) or 0,
        )
        if _entry_has_submit_trace(entry, local_events):
            has_blocking_submit_trace = True
        else:
            clear_registry_keys.append(key)

    if has_blocking_submit_trace:
        return FailedManifestRecoveryDecision(mode="none", clear_registry_keys=clear_registry_keys)
    if has_resumable_task:
        return FailedManifestRecoveryDecision(mode="resume_submitted", clear_registry_keys=clear_registry_keys)
    if registry_entries:
        return FailedManifestRecoveryDecision(mode="retry_pre_submit", clear_registry_keys=clear_registry_keys)

    for event in local_events:
        if _coerce_int(event.get("task_id")) is not None:
            return FailedManifestRecoveryDecision()
        if str(event.get("status") or "").strip() in {"submitted", "completed", "task_failed"}:
            return FailedManifestRecoveryDecision()
    return FailedManifestRecoveryDecision(mode="retry_pre_submit")


def _can_safely_retry_failed_manifest(manifest: dict[str, Any]) -> bool:
    return _failed_manifest_recovery_decision(manifest).mode != "none"


def _should_process_manifest_status(
    status: str,
    *,
    retry_failed: bool,
    manifest: dict[str, Any] | None = None,
) -> bool:
    normalized = status.strip().lower()
    if normalized in {"analyzed", "filtered", "transcribing", "analyzing"}:
        return False
    if normalized == "failed":
        return retry_failed or (manifest is not None and _can_safely_retry_failed_manifest(manifest))
    return True


def _is_stale_processing_manifest(
    manifest: dict[str, Any],
    *,
    timeout_seconds: int,
    now: datetime | None = None,
) -> bool:
    if timeout_seconds <= 0:
        return False
    normalized_status = _clean_text(manifest.get("status")) or ""
    if normalized_status not in {"transcribing", "analyzing"}:
        return False
    updated_at = _coerce_datetime(manifest.get("updatedAt")) or _coerce_datetime(manifest.get("createdAt"))
    if updated_at is None:
        return False
    resolved_now = now or datetime.now(timezone.utc)
    return updated_at <= resolved_now - timedelta(
        seconds=_effective_stale_processing_timeout_seconds(manifest, timeout_seconds)
    )


def _effective_stale_processing_timeout_seconds(
    manifest: dict[str, Any],
    base_timeout_seconds: int,
) -> int:
    if base_timeout_seconds <= 0:
        return base_timeout_seconds

    duration_seconds = _coerce_int(manifest.get("durationSeconds"))
    if duration_seconds is None:
        duration_ms = _coerce_int(manifest.get("durationMs"))
        if duration_ms and duration_ms > 0:
            duration_seconds = max(duration_ms // 1000, 1)

    if duration_seconds is None or duration_seconds <= 0:
        return base_timeout_seconds

    # Long consultations can legitimately spend much longer than 15 minutes in
    # ASR/analysis, especially when split into multiple Tencent ASR chunks.
    scaled_timeout = int(duration_seconds * 0.6) + 1800
    return max(base_timeout_seconds, 2700, min(scaled_timeout, 6 * 3600))


def _existing_artifact_path(path_text: Any) -> Path | None:
    text = _clean_text(path_text)
    if not text:
        return None
    candidate = Path(text)
    if candidate.is_file():
        return candidate
    resolved = get_settings().resolve_path(text)
    return resolved if resolved.is_file() else None


async def _recover_stale_processing_manifest(
    stage_paths: Any,
    manifest: dict[str, Any],
    *,
    timeout_seconds: int,
) -> bool:
    if not _is_stale_processing_manifest(manifest, timeout_seconds=timeout_seconds):
        return False

    stale_status = _clean_text(manifest.get("status")) or "transcribing"
    analysis_result_path = _existing_artifact_path(manifest.get("analysisResultPath"))
    transcript_path = _existing_artifact_path(manifest.get("transcriptPath"))
    if analysis_result_path is not None:
        manifest["status"] = "analyzed"
        manifest["errorMessage"] = None
    elif transcript_path is not None:
        manifest["status"] = "transcribed"
        manifest["errorMessage"] = None
    else:
        effective_timeout_seconds = _effective_stale_processing_timeout_seconds(
            manifest,
            timeout_seconds,
        )
        manifest["status"] = "failed"
        manifest["errorMessage"] = (
            f"录音处理在 {stale_status} 阶段超过 {effective_timeout_seconds} 秒未完成，已自动标记为失败，"
            "避免长期停留在处理中状态。"
        )
    _write_manifest(stage_paths, manifest)

    async with _session_factory() as db:
        recording = await _ensure_recording_stub_from_manifest(db, manifest, status=_clean_text(manifest.get("status")) or "failed")
        if recording is not None:
            await db.commit()
    return True


async def _process_stage_keys(stage_keys: list[str], *, workers: int) -> Counter[str]:
    summary: Counter[str] = Counter()
    semaphore = asyncio.Semaphore(max(workers, 1))

    async def worker(stage_key: str) -> None:
        async with semaphore:
            await execute_dingtalk_recording_pipeline(stage_key)
            manifest = _read_manifest(_ensure_stage_paths(), stage_key)
            status = _clean_text((manifest or {}).get("status")) or "missing"
            summary[status] += 1

    await asyncio.gather(*(worker(stage_key) for stage_key in stage_keys))
    return summary


def _compute_archive_status_counter(archive_items: list[dict[str, Any]]) -> Counter[str]:
    paths = _ensure_stage_paths()
    counter: Counter[str] = Counter()
    for archive_metadata in archive_items:
        device_code = _clean_text(archive_metadata.get("sn")) or ""
        file_id = _clean_text(archive_metadata.get("fileId")) or ""
        stage_key = _stage_key(device_code, file_id)
        manifest = _read_manifest(paths, stage_key)
        status = _clean_text((manifest or {}).get("status")) or "archived"
        counter[status] += 1
    return counter


async def sync_dingtalk_audio_archive_backlog(
    *,
    workers: int | None = None,
    limit: int | None = None,
    sns: list[str] | None = None,
    retry_failed: bool | None = None,
) -> DingtalkArchiveBacklogSyncResult:
    settings = get_settings()
    resolved_workers = workers if workers is not None else settings.dingtalk_audio_backlog_sync_workers
    resolved_limit = limit if limit is not None else settings.dingtalk_audio_backlog_sync_limit_per_run
    resolved_retry_failed = (
        retry_failed
        if retry_failed is not None
        else settings.dingtalk_audio_backlog_retry_failed_enabled
    )

    archive_root = get_archive_root()
    stage_paths = _ensure_stage_paths()
    device_profiles = await _load_device_profiles()
    archive_items = _iter_archive_metadata(archive_root)
    stage_manifests = _iter_stage_manifests(stage_paths)
    stage_manifest_by_key = {
        (_clean_text(item.get("stageKey")) or ""): item
        for item in stage_manifests
        if _clean_text(item.get("stageKey"))
    }

    if sns:
        allowed_sns = {str(item).strip() for item in sns if str(item).strip()}
        archive_items = [
            item
            for item in archive_items
            if (_clean_text(item.get("sn")) or "") in allowed_sns
        ]

    result = DingtalkArchiveBacklogSyncResult(archive_items=len(archive_items))
    pending_stage_keys: list[str] = []
    seen_stage_keys: set[str] = set()

    for archive_metadata in archive_items:
        device_code = _clean_text(archive_metadata.get("sn")) or ""
        file_id = _clean_text(archive_metadata.get("fileId")) or ""
        stage_key = _stage_key(device_code, file_id)
        seen_stage_keys.add(stage_key)
        manifest = stage_manifest_by_key.get(stage_key) or _read_manifest(stage_paths, stage_key)
        if manifest is None:
            manifest = _build_manifest(
                archive_metadata,
                device_profile=device_profiles.get(device_code),
            )
            _write_manifest(stage_paths, manifest)
            result.staged_new += 1
        else:
            result.already_staged += 1
            recovered = await _recover_stale_processing_manifest(
                stage_paths,
                manifest,
                timeout_seconds=settings.dingtalk_audio_stale_processing_timeout_seconds,
            )
            if recovered:
                manifest = _read_manifest(stage_paths, stage_key) or manifest

        status = _clean_text((manifest or {}).get("status")) or "downloaded"
        if _should_process_manifest_status(status, retry_failed=resolved_retry_failed, manifest=manifest):
            if status.strip().lower() == "failed":
                recovery_decision = _failed_manifest_recovery_decision(manifest)
                if recovery_decision.clear_registry_keys:
                    await delete_tencent_task_registry_entries(recovery_decision.clear_registry_keys)
            pending_stage_keys.append(stage_key)

    for stage_key, manifest in stage_manifest_by_key.items():
        if stage_key in seen_stage_keys:
            continue
        recovered = await _recover_stale_processing_manifest(
            stage_paths,
            manifest,
            timeout_seconds=settings.dingtalk_audio_stale_processing_timeout_seconds,
        )
        if recovered:
            manifest = _read_manifest(stage_paths, stage_key) or manifest

        status = _clean_text((manifest or {}).get("status")) or "downloaded"
        if _should_process_manifest_status(status, retry_failed=resolved_retry_failed, manifest=manifest):
            if status.strip().lower() == "failed":
                recovery_decision = _failed_manifest_recovery_decision(manifest)
                if recovery_decision.clear_registry_keys:
                    await delete_tencent_task_registry_entries(recovery_decision.clear_registry_keys)
            pending_stage_keys.append(stage_key)

    pending_stage_keys = list(dict.fromkeys(pending_stage_keys))
    if resolved_limit and resolved_limit > 0:
        pending_stage_keys = pending_stage_keys[:resolved_limit]

    process_summary: Counter[str] = Counter()
    if pending_stage_keys:
        logger.info(
            "processing DingTalk archive backlog pending=%d workers=%d retry_failed=%s",
            len(pending_stage_keys),
            max(resolved_workers, 1),
            resolved_retry_failed,
        )
        process_summary = await _process_stage_keys(
            pending_stage_keys,
            workers=max(resolved_workers, 1),
        )

    result.processed_now = sum(process_summary.values())
    result.process_summary = dict(process_summary)
    result.final_archive_status = dict(_compute_archive_status_counter(archive_items))
    return result


def _build_audit_content(
    *,
    status: str,
    result: DingtalkArchiveBacklogSyncResult | None = None,
    error_message: str | None = None,
) -> str:
    payload: dict[str, Any] = {"status": status}
    if result is not None:
        payload.update(
            {
                "summary": (
                    f"归档共 {result.archive_items} 条，本轮补入 {result.staged_new} 条，"
                    f"处理 {result.processed_now} 条，当前状态 {result.final_archive_status}"
                ),
                "archive_items": result.archive_items,
                "staged_new": result.staged_new,
                "already_staged": result.already_staged,
                "processed_now": result.processed_now,
                "process_summary": result.process_summary,
                "final_archive_status": result.final_archive_status,
            }
        )
    if error_message:
        payload["error_message"] = error_message[:500]
    return json.dumps(payload, ensure_ascii=False)


async def _write_audit_log(
    status: str,
    result: DingtalkArchiveBacklogSyncResult | None = None,
    error: str | None = None,
) -> None:
    try:
        async with _session_factory() as db:
            await append_audit_log(
                db,
                operator_name=BACKLOG_SYNC_OPERATOR,
                ip_address=BACKLOG_SYNC_IP,
                module_name=BACKLOG_SYNC_AUDIT_MODULE,
                action_name=BACKLOG_SYNC_AUDIT_ACTION,
                content=_build_audit_content(status=status, result=result, error_message=error),
            )
    except Exception:
        logger.exception("failed to write DingTalk audio backlog audit log")


async def periodic_dingtalk_audio_backlog_sync(
    stop_event: asyncio.Event,
    *,
    interval_seconds: int | None = None,
    workers: int | None = None,
    retry_failed: bool | None = None,
    limit: int | None = None,
) -> None:
    settings = get_settings()
    resolved_interval = (
        interval_seconds
        if interval_seconds is not None
        else settings.dingtalk_audio_backlog_sync_interval_seconds
    )

    logger.info(
        "starting DingTalk audio backlog sync loop interval_seconds=%d archive_root=%s",
        resolved_interval,
        get_archive_root(),
    )

    while not stop_event.is_set():
        try:
            async with periodic_advisory_lock("dingtalk_audio_backlog", DINGTALK_AUDIO_BACKLOG_LOCK_ID) as acquired:
                if acquired:
                    result = await sync_dingtalk_audio_archive_backlog(
                        workers=workers,
                        limit=limit,
                        retry_failed=retry_failed,
                    )
                    await _write_audit_log("success" if not result.process_summary.get("failed") else "partial", result)
        except Exception as exc:
            logger.exception("DingTalk audio backlog sync loop failed: %s", exc)
            await _write_audit_log("failed", error=str(exc))

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(resolved_interval, 1))
        except asyncio.TimeoutError:
            continue

    logger.info("DingTalk audio backlog sync loop stopped")


async def get_dingtalk_audio_backlog_sync_status_snapshot(
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
            resolved_note = f"钉钉归档补处理服务异常退出：{type(exc).__name__}: {exc}"

    latest_log = None
    try:
        async with _session_factory() as db:
            latest_log = (
                await db.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.module_name == BACKLOG_SYNC_AUDIT_MODULE,
                        AuditLog.action_name == BACKLOG_SYNC_AUDIT_ACTION,
                    )
                    .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    except Exception:
        logger.exception("failed to query DingTalk audio backlog audit log")

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
        "startedAt": started_at,
        "note": resolved_note,
        "lastSyncAt": last_sync_at,
        "lastSyncSummary": last_sync_summary,
        "lastSyncStatus": last_sync_status,
        "archiveRoot": str(get_archive_root()),
    }
