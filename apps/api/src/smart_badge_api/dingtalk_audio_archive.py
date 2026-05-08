from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiofiles
import httpx
from sqlalchemy import select

from smart_badge_api.api.audit import append_audit_log
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import AuditLog, Device
from smart_badge_api.db.session import _session_factory
from smart_badge_api.dingtalk import (
    DingTalkApiError,
    DingTalkConfigError,
    dvi_get_audio_download_url,
    dvi_list_audio_files,
    dvi_list_devices,
)
from smart_badge_api.dingtalk_iot import iot_list_audio_files, iot_list_devices, is_iot_hospital_code
from smart_badge_api.dingtalk_audio_quality import duration_ms_to_seconds, pre_asr_quality_decision
from smart_badge_api.periodic_locks import DINGTALK_AUDIO_ARCHIVE_LOCK_ID, periodic_advisory_lock

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger("smart_badge.dingtalk_audio_archive")

ARCHIVE_SYNC_AUDIT_MODULE = "录音管理"
ARCHIVE_SYNC_AUDIT_ACTION = "钉钉音频归档同步"
ARCHIVE_SYNC_OPERATOR = "系统钉钉归档"
ARCHIVE_SYNC_IP = "dingtalk-archive-sync"
SYNC_STATE_FILE_NAME = ".sync_state.json"


@dataclass(slots=True)
class RemoteAudioItem:
    sn: str
    file_id: str
    file_name: str | None
    duration_ms: int | None
    file_size: int | None
    create_time_ms: int | None
    download_url: str | None = None
    source: str | None = None


@dataclass(slots=True)
class ArchiveAudioItemResult:
    sn: str
    file_id: str
    status: str
    saved_path: Path | None = None
    message: str | None = None


@dataclass(slots=True)
class ArchiveAudioBatchResult:
    downloaded: int = 0
    filtered: int = 0
    skipped: int = 0
    failed: int = 0
    items: list[ArchiveAudioItemResult] = field(default_factory=list)


def get_archive_root() -> Path:
    root = get_settings().dingtalk_audio_stage_path / "archive"
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_archive_sync_state_path() -> Path:
    return get_archive_root() / SYNC_STATE_FILE_NAME


def _safe_part(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("_") or "unknown"


def _suffix(file_name: str | None) -> str:
    suffix = Path(file_name or "").suffix.lower()
    return suffix or ".mp3"


def _format_timestamp(create_time_ms: int | None) -> tuple[str, str]:
    if create_time_ms and create_time_ms > 0:
        dt = datetime.fromtimestamp(create_time_ms / 1000, tz=TZ_SHANGHAI)
        return dt.strftime("%Y%m"), dt.strftime("%m%d_%H%M%S")
    return "unknown", "unknown_time"


def _read_existing_file_id(meta_path: Path) -> str | None:
    if not meta_path.exists():
        return None
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    file_id = payload.get("fileId")
    if isinstance(file_id, str):
        normalized = file_id.strip()
        return normalized or None
    return None


def _read_existing_metadata(meta_path: Path) -> dict[str, Any]:
    if not meta_path.exists():
        return {}
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_sync_state() -> dict[str, Any]:
    path = get_archive_sync_state_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_sync_state(payload: dict[str, Any]) -> None:
    state = dict(payload)
    state["updatedAt"] = datetime.now(timezone.utc).isoformat()
    get_archive_sync_state_path().write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _coerce_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        coerced = int(value)
        return coerced if coerced > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            coerced = int(float(text))
        except ValueError:
            return None
        return coerced if coerced > 0 else None
    return None


def resolve_archive_paths(root: Path, item: RemoteAudioItem) -> tuple[Path, Path]:
    month_part, short_time_part = _format_timestamp(item.create_time_ms)
    archive_dir = root / _safe_part(item.sn) / month_part
    archive_dir.mkdir(parents=True, exist_ok=True)

    ext = _suffix(item.file_name)
    base_stem = short_time_part

    for index in range(1, 1000):
        suffix = "" if index == 1 else f"_{index}"
        stem = f"{base_stem}{suffix}"
        audio_path = archive_dir / f"{stem}{ext}"
        meta_path = archive_dir / f"{stem}.json"

        existing_file_id = _read_existing_file_id(meta_path)
        if existing_file_id == item.file_id:
            return audio_path, meta_path

        if not audio_path.exists() and not meta_path.exists():
            return audio_path, meta_path

    raise RuntimeError(f"failed to allocate archive path for {item.sn}/{item.file_id}")


async def _list_all_devices() -> list[str]:
    devices: list[str] = []
    errors: list[str] = []
    try:
        next_token = ""
        for _ in range(20):
            page = await dvi_list_devices(max_results=20, next_token=next_token)
            items = page.get("result") or []
            for item in items:
                sn = str(item.get("sn") or "").strip()
                if sn:
                    devices.append(sn)
            next_token = page.get("nextToken") or ""
            if not next_token:
                break
    except (DingTalkConfigError, DingTalkApiError) as exc:
        errors.append(str(exc))
        logger.warning("failed to list DVI devices for archive sync: %s", exc)

    try:
        for item in await iot_list_devices():
            sn = str(item.get("sn") or "").strip()
            if sn:
                devices.append(sn)
    except (DingTalkConfigError, DingTalkApiError) as exc:
        errors.append(str(exc))
        logger.warning("failed to list IOT devices for archive sync: %s", exc)

    devices = list(dict.fromkeys(devices))
    if not devices and errors:
        raise RuntimeError("; ".join(errors[:2]))
    return devices


async def _device_uses_iot(sn: str) -> bool:
    async with _session_factory() as db:
        hospital_code = (
            await db.execute(
                select(Device.hospital_code)
                .where(Device.device_code == sn)
                .limit(1)
            )
        ).scalar_one_or_none()
    return is_iot_hospital_code(hospital_code)


def _remote_audio_item_from_row(sn: str, row: dict[str, Any]) -> RemoteAudioItem | None:
    file_id = str(row.get("fileId") or "").strip()
    if not file_id:
        return None
    return RemoteAudioItem(
        sn=sn,
        file_id=file_id,
        file_name=str(row.get("fileName") or "").strip() or None,
        duration_ms=int(row["duration"]) if row.get("duration") is not None else None,
        file_size=int(row["fileSize"]) if row.get("fileSize") is not None else None,
        create_time_ms=int(row["createTime"]) if row.get("createTime") is not None else None,
        download_url=str(row.get("downloadUrl") or row.get("fileUrl") or "").strip() or None,
        source=str(row.get("remoteProvider") or row.get("source") or "").strip() or None,
    )


async def list_all_audio_for_device(
    sn: str,
    *,
    start_timestamp: int | None = None,
    end_timestamp: int | None = None,
    use_iot: bool | None = None,
) -> list[RemoteAudioItem]:
    resolved_use_iot = await _device_uses_iot(sn) if use_iot is None else use_iot
    if resolved_use_iot:
        return [
            item
            for row in await iot_list_audio_files(
                device_no=sn,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )
            if (item := _remote_audio_item_from_row(sn, row)) is not None
        ]

    items: list[RemoteAudioItem] = []
    next_token = ""
    for _ in range(500):
        page = await dvi_list_audio_files(
            sn,
            max_results=get_settings().dingtalk_audio_sync_page_size,
            next_token=next_token,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
        rows = page.get("result") or []
        for row in rows:
            if isinstance(row, dict) and (item := _remote_audio_item_from_row(sn, row)) is not None:
                items.append(item)
        next_token = page.get("nextToken") or ""
        if not next_token:
            break
    return items


async def archive_audio_item(
    item: RemoteAudioItem,
    *,
    overwrite: bool = False,
    client: httpx.AsyncClient | None = None,
    archive_root: Path | None = None,
) -> ArchiveAudioItemResult:
    root = archive_root or get_archive_root()
    audio_path, meta_path = resolve_archive_paths(root, item)
    existing_metadata = _read_existing_metadata(meta_path)
    existing_status = str(existing_metadata.get("status") or "").strip().lower()
    existing_file_matches = existing_metadata.get("fileId") == item.file_id
    existing_audio_available = audio_path.exists() or existing_status == "filtered"
    if existing_file_matches and not overwrite and existing_audio_available:
        return ArchiveAudioItemResult(
            sn=item.sn,
            file_id=item.file_id,
            status="skipped",
            saved_path=audio_path if audio_path.exists() else None,
            message="音频已质检过滤" if existing_status == "filtered" else "音频已归档",
        )

    duration_seconds = duration_ms_to_seconds(item.duration_ms)
    pre_decision = pre_asr_quality_decision(duration_seconds)
    if not pre_decision.passed:
        audio_path.unlink(missing_ok=True)
        metadata = {
            "sn": item.sn,
            "fileId": item.file_id,
            "remoteFileName": item.file_name,
            "durationMs": item.duration_ms,
            "durationSeconds": duration_seconds,
            "fileSize": item.file_size,
            "createTimeMs": item.create_time_ms,
            "filteredAt": datetime.now(TZ_SHANGHAI).isoformat(),
            "audioPath": str(audio_path),
            "status": "filtered",
            "qualityStage": pre_decision.stage,
            "qualityReason": pre_decision.reason,
        }
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return ArchiveAudioItemResult(
            sn=item.sn,
            file_id=item.file_id,
            status="filtered",
            saved_path=None,
            message=pre_decision.reason or "录音未通过 ASR 前质检，已直接过滤",
        )

    download_url = item.download_url
    if not download_url and (item.source == "iot" or item.file_id.startswith("iot:")):
        for candidate in await list_all_audio_for_device(item.sn, use_iot=True):
            if candidate.file_id == item.file_id:
                download_url = candidate.download_url
                break
    if not download_url:
        payload = await dvi_get_audio_download_url(item.file_id)
        download_url = (
            payload.get("url")
            or payload.get("downloadUrl")
            or (payload.get("result") or {}).get("url")
            or (payload.get("result") or {}).get("downloadUrl")
        )
    if not isinstance(download_url, str) or not download_url.strip():
        raise RuntimeError(f"missing download url for {item.sn}/{item.file_id}")

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=120.0, follow_redirects=True)
    try:
        async with http_client.stream("GET", download_url) as response:
            response.raise_for_status()
            async with aiofiles.open(audio_path, "wb") as handle:
                async for chunk in response.aiter_bytes():
                    if chunk:
                        await handle.write(chunk)
    finally:
        if owns_client:
            await http_client.aclose()

    metadata = {
        "sn": item.sn,
        "fileId": item.file_id,
        "remoteFileName": item.file_name,
        "durationMs": item.duration_ms,
        "fileSize": item.file_size,
        "createTimeMs": item.create_time_ms,
        "source": item.source or "dvi",
        "downloadedAt": datetime.now(TZ_SHANGHAI).isoformat(),
        "audioPath": str(audio_path),
    }
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return ArchiveAudioItemResult(
        sn=item.sn,
        file_id=item.file_id,
        status="downloaded",
        saved_path=audio_path,
        message="音频已归档",
    )


async def archive_audio_files(
    *,
    sns: list[str] | None = None,
    overwrite: bool = False,
    workers: int = 4,
    start_timestamp: int | None = None,
    end_timestamp: int | None = None,
) -> ArchiveAudioBatchResult:
    resolved_sns = await _list_all_devices() if sns is None else [sn for sn in sns if isinstance(sn, str) and sn.strip()]
    resolved_sns = list(dict.fromkeys(sn.strip() for sn in resolved_sns if sn.strip()))
    all_items: list[RemoteAudioItem] = []
    for sn in resolved_sns:
        all_items.extend(
            await list_all_audio_for_device(
                sn,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )
        )

    result = ArchiveAudioBatchResult()
    semaphore = asyncio.Semaphore(max(workers, 1))
    archive_root = get_archive_root()

    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        async def worker(item: RemoteAudioItem) -> None:
            async with semaphore:
                try:
                    item_result = await archive_audio_item(
                        item,
                        overwrite=overwrite,
                        client=client,
                        archive_root=archive_root,
                    )
                except Exception as exc:
                    result.failed += 1
                    result.items.append(
                        ArchiveAudioItemResult(
                            sn=item.sn,
                            file_id=item.file_id,
                            status="error",
                            message=str(exc),
                        )
                    )
                    return

                if item_result.status == "downloaded":
                    result.downloaded += 1
                elif item_result.status == "filtered":
                    result.filtered += 1
                elif item_result.status == "skipped":
                    result.skipped += 1
                else:
                    result.failed += 1
                result.items.append(item_result)

        await asyncio.gather(*(worker(item) for item in all_items))

    return result


def compute_incremental_archive_window(
    now: datetime,
    *,
    lookback_minutes: int,
    state: dict[str, Any] | None = None,
) -> tuple[int, int]:
    resolved_now = now.astimezone(timezone.utc)
    window_end = int(resolved_now.timestamp() * 1000)
    overlap_ms = max(lookback_minutes, 1) * 60 * 1000
    fallback_start = max(window_end - overlap_ms, 0)

    payload = state if isinstance(state, dict) else {}
    incremental = payload.get("incremental")
    previous_end = None
    if isinstance(incremental, dict):
        previous_end = _coerce_positive_int(incremental.get("lastWindowEndMs"))

    if previous_end is None:
        return fallback_start, window_end

    window_start = max(previous_end - overlap_ms, 0)
    if window_start >= window_end:
        window_start = fallback_start
    return window_start, window_end


def compute_archive_backfill_window(now: datetime, *, backfill_days: int) -> tuple[int, int]:
    resolved_now = now.astimezone(timezone.utc)
    window_end = int(resolved_now.timestamp() * 1000)
    duration_ms = max(backfill_days, 1) * 24 * 60 * 60 * 1000
    return max(window_end - duration_ms, 0), window_end


def _update_sync_state(
    *,
    mode: str,
    status: str,
    start_timestamp: int,
    end_timestamp: int,
    result: ArchiveAudioBatchResult | None = None,
    error_message: str | None = None,
) -> None:
    payload = _read_sync_state()
    payload[mode] = {
        "status": status,
        "lastWindowStartMs": start_timestamp,
        "lastWindowEndMs": end_timestamp,
        "downloaded": result.downloaded if result else 0,
        "filtered": result.filtered if result else 0,
        "skipped": result.skipped if result else 0,
        "failed": result.failed if result else 0,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    if error_message:
        payload[mode]["errorMessage"] = error_message[:500]
    _write_sync_state(payload)


def _build_sync_summary(result: ArchiveAudioBatchResult) -> str:
    return f"归档新增 {result.downloaded} 条，质检过滤 {result.filtered} 条，已存在 {result.skipped} 条，失败 {result.failed} 条"


def _build_audit_content(
    *,
    mode: str,
    status: str,
    start_timestamp: int,
    end_timestamp: int,
    result: ArchiveAudioBatchResult | None = None,
    error_message: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "mode": mode,
        "status": status,
        "startTimestamp": start_timestamp,
        "endTimestamp": end_timestamp,
    }
    if result is not None:
        payload["summary"] = _build_sync_summary(result)
        payload["downloaded"] = result.downloaded
        payload["filtered"] = result.filtered
        payload["skipped"] = result.skipped
        payload["failed"] = result.failed
        payload["items"] = [
            {
                "sn": item.sn,
                "file_id": item.file_id,
                "status": item.status,
                "saved_path": str(item.saved_path) if item.saved_path else None,
                "message": item.message,
            }
            for item in result.items[:20]
        ]
    if error_message:
        payload["errorMessage"] = error_message[:500]
    return json.dumps(payload, ensure_ascii=False)


async def _write_archive_audit_log(
    *,
    mode: str,
    status: str,
    start_timestamp: int,
    end_timestamp: int,
    result: ArchiveAudioBatchResult | None = None,
    error: str | None = None,
) -> None:
    try:
        async with _session_factory() as db:
            await append_audit_log(
                db,
                operator_name=ARCHIVE_SYNC_OPERATOR,
                ip_address=ARCHIVE_SYNC_IP,
                module_name=ARCHIVE_SYNC_AUDIT_MODULE,
                action_name=ARCHIVE_SYNC_AUDIT_ACTION,
                content=_build_audit_content(
                    mode=mode,
                    status=status,
                    start_timestamp=start_timestamp,
                    end_timestamp=end_timestamp,
                    result=result,
                    error_message=error,
                ),
            )
    except Exception:
        logger.exception("failed to write DingTalk archive sync audit log")


async def _run_archive_sync_once(
    *,
    mode: str,
    start_timestamp: int,
    end_timestamp: int,
    workers: int,
) -> None:
    try:
        result = await archive_audio_files(
            workers=workers,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
        status = "success" if result.failed == 0 else "partial"
        _update_sync_state(
            mode=mode,
            status=status,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            result=result,
        )
        await _write_archive_audit_log(
            mode=mode,
            status=status,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            result=result,
        )
    except Exception as exc:
        logger.exception(
            "DingTalk archive %s sync failed start=%s end=%s: %s",
            mode,
            start_timestamp,
            end_timestamp,
            exc,
        )
        _update_sync_state(
            mode=mode,
            status="failed",
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            error_message=str(exc),
        )
        await _write_archive_audit_log(
            mode=mode,
            status="failed",
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            error=str(exc),
        )


async def periodic_dingtalk_audio_archive_sync(
    stop_event: asyncio.Event,
    *,
    interval_seconds: int | None = None,
    lookback_minutes: int | None = None,
    workers: int | None = None,
    backfill_enabled: bool | None = None,
    backfill_interval_hours: int | None = None,
    backfill_days: int | None = None,
) -> None:
    settings = get_settings()
    resolved_interval = interval_seconds if interval_seconds is not None else settings.dingtalk_audio_archive_sync_interval_seconds
    resolved_lookback = (
        lookback_minutes if lookback_minutes is not None else settings.dingtalk_audio_archive_sync_lookback_minutes
    )
    resolved_workers = workers if workers is not None else settings.dingtalk_audio_archive_sync_workers
    resolved_backfill_enabled = (
        backfill_enabled if backfill_enabled is not None else settings.dingtalk_audio_archive_backfill_enabled
    )
    resolved_backfill_interval_hours = (
        backfill_interval_hours
        if backfill_interval_hours is not None
        else settings.dingtalk_audio_archive_backfill_interval_hours
    )
    resolved_backfill_days = (
        backfill_days if backfill_days is not None else settings.dingtalk_audio_archive_backfill_days
    )
    next_backfill_at = datetime.now(timezone.utc) + timedelta(hours=max(resolved_backfill_interval_hours, 1))

    logger.info(
        "starting DingTalk archive sync loop interval_seconds=%d lookback_minutes=%d workers=%d archive_root=%s",
        resolved_interval,
        resolved_lookback,
        resolved_workers,
        get_archive_root(),
    )

    while not stop_event.is_set():
        try:
            async with periodic_advisory_lock("dingtalk_audio_archive", DINGTALK_AUDIO_ARCHIVE_LOCK_ID) as acquired:
                if acquired:
                    now = datetime.now(timezone.utc)
                    state = _read_sync_state()
                    start_timestamp, end_timestamp = compute_incremental_archive_window(
                        now,
                        lookback_minutes=resolved_lookback,
                        state=state,
                    )
                    await _run_archive_sync_once(
                        mode="incremental",
                        start_timestamp=start_timestamp,
                        end_timestamp=end_timestamp,
                        workers=resolved_workers,
                    )

                    if resolved_backfill_enabled and datetime.now(timezone.utc) >= next_backfill_at:
                        backfill_start, backfill_end = compute_archive_backfill_window(
                            datetime.now(timezone.utc),
                            backfill_days=resolved_backfill_days,
                        )
                        await _run_archive_sync_once(
                            mode="backfill",
                            start_timestamp=backfill_start,
                            end_timestamp=backfill_end,
                            workers=resolved_workers,
                        )
                        next_backfill_at = datetime.now(timezone.utc) + timedelta(
                            hours=max(resolved_backfill_interval_hours, 1)
                        )
        except Exception as exc:
            logger.exception("DingTalk archive sync loop failed: %s", exc)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(resolved_interval, 1))
        except asyncio.TimeoutError:
            continue

    logger.info("DingTalk archive sync loop stopped")


async def get_dingtalk_audio_archive_sync_status_snapshot(
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
            resolved_note = f"钉钉音频归档同步服务异常退出：{type(exc).__name__}: {exc}"

    latest_log = None
    try:
        async with _session_factory() as db:
            latest_log = (
                await db.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.module_name == ARCHIVE_SYNC_AUDIT_MODULE,
                        AuditLog.action_name == ARCHIVE_SYNC_AUDIT_ACTION,
                    )
                    .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    except Exception:
        logger.exception("failed to query DingTalk archive sync audit log")

    last_sync_at = None
    last_sync_summary = None
    last_sync_status = None
    last_sync_mode = None
    if latest_log is not None:
        last_sync_at = latest_log.created_at
        try:
            payload = json.loads(latest_log.content)
            last_sync_summary = payload.get("summary")
            last_sync_status = payload.get("status")
            last_sync_mode = payload.get("mode")
        except (TypeError, ValueError):
            pass

    state = _read_sync_state()
    return {
        "enabled": enabled,
        "running": running,
        "started_at": started_at,
        "note": resolved_note,
        "archive_root": str(get_archive_root()),
        "state_path": str(get_archive_sync_state_path()),
        "last_sync_at": last_sync_at,
        "last_sync_status": last_sync_status,
        "last_sync_mode": last_sync_mode,
        "last_sync_summary": last_sync_summary,
        "incremental": state.get("incremental"),
        "backfill": state.get("backfill"),
    }
