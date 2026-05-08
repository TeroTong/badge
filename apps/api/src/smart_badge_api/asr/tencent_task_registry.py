from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from smart_badge_api.core.config import get_settings

_write_lock = asyncio.Lock()


def _registry_path() -> Path:
    return get_settings().resolved_tencent_asr_task_registry_path


def _submit_lock_path(*, source_id: str, chunk_index: int, chunk_count: int) -> Path:
    safe_source = source_id.replace("/", "_").replace("\\", "_").replace(":", "_")
    return _registry_path().parent / "tencent_submit_locks" / f"{safe_source}__{chunk_index}_{chunk_count}.lock"


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _read_registry() -> dict[str, dict[str, Any]]:
    path = _registry_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def _write_registry(payload: dict[str, dict[str, Any]]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp_path.replace(path)


def build_tencent_task_registry_key(*, source_id: str, chunk_index: int, chunk_count: int) -> str:
    return f"{source_id}::{chunk_index}/{chunk_count}"


def get_tencent_task_registry_entry(
    *,
    source_id: str,
    chunk_index: int,
    chunk_count: int,
) -> dict[str, Any] | None:
    payload = _read_registry()
    item = payload.get(
        build_tencent_task_registry_key(
            source_id=source_id,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
        )
    )
    return dict(item) if isinstance(item, dict) else None


def list_tencent_task_registry_entries_for_source(source_id: str) -> list[dict[str, Any]]:
    prefix = f"{source_id}::"
    payload = _read_registry()
    return [
        dict(value)
        for key, value in payload.items()
        if key.startswith(prefix) and isinstance(value, dict)
    ]


async def upsert_tencent_task_registry_entry(
    *,
    source_id: str,
    chunk_index: int,
    chunk_count: int,
    audio_name: str | None,
    audio_path: str | None,
    status: str,
    request_id: str | None,
    task_id: int | None,
    submitted_duration_ms: int | None,
    recognized_duration_ms: int | None,
    error_code: str | None,
    error_message: str | None,
) -> None:
    async with _write_lock:
        payload = _read_registry()
        key = build_tencent_task_registry_key(
            source_id=source_id,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
        )
        previous = payload.get(key) or {}
        now = datetime.now(UTC)
        payload[key] = {
            "source_id": source_id,
            "chunk_index": chunk_index,
            "chunk_count": chunk_count,
            "audio_name": audio_name,
            "audio_path": audio_path,
            "status": status,
            "request_id": request_id,
            "task_id": task_id,
            "submitted_duration_ms": submitted_duration_ms,
            "recognized_duration_ms": recognized_duration_ms,
            "error_code": error_code,
            "error_message": error_message,
            "created_at": previous.get("created_at") or _serialize_datetime(now),
            "updated_at": _serialize_datetime(now),
        }
        _write_registry(payload)


async def delete_tencent_task_registry_entries(keys: list[str]) -> int:
    if not keys:
        return 0
    key_set = {str(key).strip() for key in keys if str(key).strip()}
    if not key_set:
        return 0
    async with _write_lock:
        payload = _read_registry()
        removed = 0
        for key in key_set:
            if key in payload:
                payload.pop(key, None)
                removed += 1
        if removed:
            _write_registry(payload)
        return removed


async def acquire_tencent_submit_lock(
    *,
    source_id: str,
    chunk_index: int,
    chunk_count: int,
    timeout_seconds: float = 30.0,
    stale_lock_seconds: float = 120.0,
    poll_interval_seconds: float = 0.2,
) -> bool:
    lock_path = _submit_lock_path(source_id=source_id, chunk_index=chunk_index, chunk_count=chunk_count)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(timeout_seconds, poll_interval_seconds)

    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{os.getpid()} {datetime.now(UTC).isoformat()}".encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            try:
                age_seconds = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age_seconds >= stale_lock_seconds:
                try:
                    lock_path.unlink()
                    continue
                except FileNotFoundError:
                    continue

        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(max(poll_interval_seconds, 0.05))


async def release_tencent_submit_lock(
    *,
    source_id: str,
    chunk_index: int,
    chunk_count: int,
) -> None:
    lock_path = _submit_lock_path(source_id=source_id, chunk_index=chunk_index, chunk_count=chunk_count)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return
