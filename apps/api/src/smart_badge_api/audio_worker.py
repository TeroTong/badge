from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from smart_badge_api.core.config import get_settings
from smart_badge_api.dingtalk_audio_archive import periodic_dingtalk_audio_archive_sync
from smart_badge_api.dingtalk_audio_backlog import periodic_dingtalk_audio_backlog_sync
from smart_badge_api.dingtalk_audio_sync import (
    periodic_dingtalk_audio_sync,
    start_dingtalk_pipeline_workers,
    stop_dingtalk_pipeline_workers,
)

logger = logging.getLogger("smart_badge.audio_worker")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop_event.set)


async def run_audio_worker() -> None:
    settings = get_settings()
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    tasks: list[tuple[asyncio.Task, str]] = []
    pipeline_started = False

    if settings.dingtalk_audio_sync_enabled:
        tasks.append(
            (
                asyncio.create_task(
                    periodic_dingtalk_audio_sync(
                        stop_event,
                        interval_seconds=settings.dingtalk_audio_sync_interval_seconds,
                        lookback_minutes=settings.dingtalk_audio_sync_lookback_minutes,
                    )
                ),
                "dingtalk audio sync",
            )
        )

    if settings.dingtalk_audio_archive_sync_enabled:
        tasks.append(
            (
                asyncio.create_task(
                    periodic_dingtalk_audio_archive_sync(
                        stop_event,
                        interval_seconds=settings.dingtalk_audio_archive_sync_interval_seconds,
                        lookback_minutes=settings.dingtalk_audio_archive_sync_lookback_minutes,
                        workers=settings.dingtalk_audio_archive_sync_workers,
                        backfill_enabled=settings.dingtalk_audio_archive_backfill_enabled,
                        backfill_interval_hours=settings.dingtalk_audio_archive_backfill_interval_hours,
                        backfill_days=settings.dingtalk_audio_archive_backfill_days,
                    )
                ),
                "dingtalk archive sync",
            )
        )

    if settings.dingtalk_audio_backlog_sync_enabled:
        tasks.append(
            (
                asyncio.create_task(
                    periodic_dingtalk_audio_backlog_sync(
                        stop_event,
                        interval_seconds=settings.dingtalk_audio_backlog_sync_interval_seconds,
                        workers=settings.dingtalk_audio_backlog_sync_workers,
                        retry_failed=settings.dingtalk_audio_backlog_retry_failed_enabled,
                        limit=settings.dingtalk_audio_backlog_sync_limit_per_run,
                    )
                ),
                "dingtalk audio backlog sync",
            )
        )

    if not tasks:
        logger.warning("audio worker started with all audio tasks disabled")
        await stop_event.wait()
        return

    try:
        await start_dingtalk_pipeline_workers()
        pipeline_started = True
    except Exception:
        logger.exception("failed to start dingtalk pipeline workers")

    logger.info("audio worker started with tasks: %s", ", ".join(name for _task, name in tasks))
    try:
        await stop_event.wait()
    finally:
        stop_event.set()
        for task, _name in tasks:
            task.cancel()
        for task, name in tasks:
            with suppress(asyncio.CancelledError):
                await task
            logger.info("%s task stopped", name)
        if pipeline_started:
            with suppress(Exception):
                await stop_dingtalk_pipeline_workers()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_audio_worker())


if __name__ == "__main__":
    main()
