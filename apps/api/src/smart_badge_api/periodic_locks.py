from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text

from smart_badge_api.db.session import _session_factory

logger = logging.getLogger("smart_badge.periodic_locks")

DINGTALK_AUDIO_SYNC_LOCK_ID = 0x5342414447451001
DINGTALK_AUDIO_ARCHIVE_LOCK_ID = 0x5342414447451002
DINGTALK_AUDIO_BACKLOG_LOCK_ID = 0x5342414447451003
VISIT_ORDER_CONTEXT_SYNC_LOCK_ID = 0x5342414447451004
STAFF_DIRECTORY_REFRESH_LOCK_ID = 0x5342414447451005
ASR_HOTWORD_SYNC_LOCK_ID = 0x5342414447451006


def _session_dialect_name(db) -> str:
    try:
        bind = db.get_bind()
    except Exception:
        return ""
    return str(getattr(getattr(bind, "dialect", None), "name", "") or "").lower()


async def _try_acquire_advisory_lock(db, lock_id: int) -> bool:
    if _session_dialect_name(db) not in {"postgresql", "postgres"}:
        return True
    acquired = (
        await db.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": lock_id},
        )
    ).scalar_one()
    return bool(acquired)


async def _release_advisory_lock(db, lock_id: int) -> None:
    if _session_dialect_name(db) not in {"postgresql", "postgres"}:
        return
    try:
        await db.execute(
            text("SELECT pg_advisory_unlock(:lock_id)"),
            {"lock_id": lock_id},
        )
    except Exception:
        logger.warning("failed to release periodic advisory lock lock_id=%s", lock_id, exc_info=True)


@asynccontextmanager
async def periodic_advisory_lock(name: str, lock_id: int) -> AsyncIterator[bool]:
    async with _session_factory() as db:
        acquired = await _try_acquire_advisory_lock(db, lock_id)
        if not acquired:
            logger.info("%s skipped because another process holds the advisory lock", name)
            yield False
            return
        try:
            yield True
        finally:
            await _release_advisory_lock(db, lock_id)
