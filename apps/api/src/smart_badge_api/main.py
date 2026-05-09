import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from smart_badge_api.api.hot_read_cache import HotReadCacheMiddleware
from smart_badge_api.api.router import api_router
from smart_badge_api.api.routes.ws import router as ws_router
from smart_badge_api.asr.hotword_sync import periodic_asr_hotword_sync
from smart_badge_api.core.config import get_settings
from smart_badge_api.dingtalk_audio_archive import periodic_dingtalk_audio_archive_sync
from smart_badge_api.dingtalk_audio_backlog import periodic_dingtalk_audio_backlog_sync
from smart_badge_api.dingtalk_audio_sync import (
    periodic_dingtalk_audio_sync,
    start_dingtalk_pipeline_workers,
    stop_dingtalk_pipeline_workers,
)
from smart_badge_api.dingtalk_iot import close_shared_iot_client
from smart_badge_api.message_push import close_shared_message_push_client
from smart_badge_api.sap_push_scheduler import periodic_sap_auto_push_scan
from smart_badge_api.sap_push_service import close_shared_sap_push_client
from smart_badge_api.staff_sync import periodic_staff_directory_refresh
from smart_badge_api.visit_order_sync import dispose_sync_lookup_engine, periodic_visit_order_context_sync
from smart_badge_api.wecom import close_shared_wecom_client
from smart_badge_api.asr.tencent_cloud_provider import close_shared_tencent_client
from smart_badge_api.asr.xfyun_asr_provider import close_shared_xfyun_client

logger = logging.getLogger("smart_badge.startup")


async def _supervised_periodic(
    name: str,
    factory: Callable[[], Awaitable[None]],
    stop_event: asyncio.Event,
    *,
    initial_backoff: float = 5.0,
    max_backoff: float = 120.0,
) -> None:
    """运行周期任务并在协程内部异常时按抖动指数退避重启。

    `factory` 必须返回一个 fresh coroutine。`stop_event` 设置后立刻退出。
    """
    backoff = initial_backoff
    while not stop_event.is_set():
        try:
            await factory()
            # 协程正常返回（通常意味着 stop_event 触发），退出。
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("periodic task %s crashed; will restart", name)
            # 指数退避 + ±20% 抖动
            jitter = backoff * 0.2 * (2 * random.random() - 1)
            sleep_for = max(1.0, backoff + jitter)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
                return  # stop_event 期间触发
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, max_backoff)


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop_event = asyncio.Event()
        # 在启动所有周期任务之前，先启动钉钉录音流水线的有界队列与消费者池。
        try:
            await start_dingtalk_pipeline_workers()
        except Exception:
            logger.exception("failed to start dingtalk pipeline workers")

        # 后台预热 /recordings/archive 列表缓存：首次冷启动 ~6s 扫描 manifests
        # + archive 元数据，warm 后命中只需 ~10ms。
        # 注意：故意不预热 /analysis/results 缓存——它的冷启动需要 ~85s，会
        # 阻塞事件循环导致其它接口在此期间响应缓慢。改为依靠 in-route 的
        # 文件级 memo + single-flight lock，把首次访问成本摊到第一个用户。
        async def _warm_archive_recording_caches() -> None:
            try:
                from smart_badge_api.api.routes.dingtalk import warm_archive_recording_index_cache
                from smart_badge_api.api.routes.recordings import (
                    _load_archive_recording_list_items,
                )
                from smart_badge_api.db.session import _session_factory

                started_at = time.perf_counter()
                index_count = await asyncio.to_thread(warm_archive_recording_index_cache)
                async with _session_factory() as session:
                    list_items = await _load_archive_recording_list_items(session)
                logger.info(
                    "archive recording caches warmed up index_count=%d list_count=%d elapsed_ms=%.1f",
                    index_count,
                    len(list_items),
                    (time.perf_counter() - started_at) * 1000,
                )
            except Exception:
                logger.exception("failed to warm up archive recording caches")

        async def _periodic_archive_recording_index_refresh() -> None:
            from smart_badge_api.api.routes.dingtalk import warm_archive_recording_index_cache

            interval_seconds = max(10, settings.archive_recording_index_refresh_interval_seconds)
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                    return
                except asyncio.TimeoutError:
                    pass

                started_at = time.perf_counter()
                try:
                    index_count = await asyncio.to_thread(
                        warm_archive_recording_index_cache,
                        force_refresh=True,
                    )
                    logger.info(
                        "archive recording index cache refreshed index_count=%d elapsed_ms=%.1f",
                        index_count,
                        (time.perf_counter() - started_at) * 1000,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("failed to refresh archive recording index cache")

        archive_cache_warmup_task = asyncio.create_task(_warm_archive_recording_caches())
        archive_index_refresh_task: asyncio.Task | None = None
        if settings.archive_recording_index_refresh_interval_seconds > 0:
            archive_index_refresh_task = asyncio.create_task(_periodic_archive_recording_index_refresh())
        sync_task: asyncio.Task | None = None
        dingtalk_audio_task: asyncio.Task | None = None
        dingtalk_archive_task: asyncio.Task | None = None
        dingtalk_backlog_task: asyncio.Task | None = None
        asr_hotword_task: asyncio.Task | None = None
        sap_auto_push_task: asyncio.Task | None = None
        visit_order_sync_task: asyncio.Task | None = None
        resolved_dsn = settings.resolved_staff_directory_dsn

        # --- staff sync state ---
        app.state.staff_sync_task = None
        app.state.staff_sync_scheduler_configured = False
        app.state.staff_sync_scheduler_started_at = None
        app.state.staff_sync_scheduler_note = None

        if settings.staff_refresh_interval_seconds <= 0:
            app.state.staff_sync_scheduler_note = "员工状态定时同步已禁用：同步间隔必须大于 0 秒"
            logger.info("periodic staff refresh disabled because interval <= 0")
        elif not resolved_dsn:
            app.state.staff_sync_scheduler_note = "员工状态定时同步未启动：缺少 staff 目录数据源连接配置"
            logger.warning("periodic staff refresh disabled because no staff directory DSN is configured")
        else:
            sync_task = asyncio.create_task(
                _supervised_periodic(
                    "staff_sync",
                    lambda: periodic_staff_directory_refresh(stop_event, staff_directory_dsn=resolved_dsn),
                    stop_event,
                )
            )
            app.state.staff_sync_task = sync_task
            app.state.staff_sync_scheduler_configured = True
            app.state.staff_sync_scheduler_started_at = datetime.now(timezone.utc)
            app.state.staff_sync_scheduler_note = "员工状态定时同步服务运行中"
            logger.info(
                "periodic staff refresh task started interval_seconds=%d",
                settings.staff_refresh_interval_seconds,
            )

        # --- DingTalk audio sync state ---
        app.state.dingtalk_audio_sync_task = None
        app.state.dingtalk_audio_sync_enabled = False
        app.state.dingtalk_audio_sync_started_at = None
        app.state.dingtalk_audio_sync_note = None

        if not settings.dingtalk_audio_sync_enabled:
            app.state.dingtalk_audio_sync_note = "钉钉音频同步服务已禁用"
            logger.info("dingtalk audio sync disabled by config")
        elif not settings.dingtalk_enabled:
            app.state.dingtalk_audio_sync_note = "钉钉音频同步未启动：缺少钉钉集成配置"
            logger.warning("dingtalk audio sync disabled because DingTalk is not configured")
        elif settings.dingtalk_audio_sync_interval_seconds <= 0:
            app.state.dingtalk_audio_sync_note = "钉钉音频同步未启动：同步间隔必须大于 0 秒"
            logger.warning("dingtalk audio sync disabled because interval <= 0")
        else:
            dingtalk_audio_task = asyncio.create_task(
                _supervised_periodic(
                    "dingtalk_audio_sync",
                    lambda: periodic_dingtalk_audio_sync(
                        stop_event,
                        interval_seconds=settings.dingtalk_audio_sync_interval_seconds,
                        lookback_minutes=settings.dingtalk_audio_sync_lookback_minutes,
                    ),
                    stop_event,
                )
            )
            app.state.dingtalk_audio_sync_task = dingtalk_audio_task
            app.state.dingtalk_audio_sync_enabled = True
            app.state.dingtalk_audio_sync_started_at = datetime.now(timezone.utc)
            app.state.dingtalk_audio_sync_note = "钉钉音频同步服务运行中"
            logger.info(
                "dingtalk audio sync started interval_seconds=%d lookback_minutes=%d",
                settings.dingtalk_audio_sync_interval_seconds,
                settings.dingtalk_audio_sync_lookback_minutes,
            )

        # --- DingTalk archive sync state ---
        app.state.dingtalk_audio_archive_sync_task = None
        app.state.dingtalk_audio_archive_sync_enabled = False
        app.state.dingtalk_audio_archive_sync_started_at = None
        app.state.dingtalk_audio_archive_sync_note = None

        if not settings.dingtalk_audio_archive_sync_enabled:
            app.state.dingtalk_audio_archive_sync_note = "钉钉音频归档同步服务已禁用"
            logger.info("dingtalk archive sync disabled by config")
        elif not settings.dingtalk_enabled:
            app.state.dingtalk_audio_archive_sync_note = "钉钉音频归档同步未启动：缺少钉钉集成配置"
            logger.warning("dingtalk archive sync disabled because DingTalk is not configured")
        elif settings.dingtalk_audio_archive_sync_interval_seconds <= 0:
            app.state.dingtalk_audio_archive_sync_note = "钉钉音频归档同步未启动：同步间隔必须大于 0 秒"
            logger.warning("dingtalk archive sync disabled because interval <= 0")
        else:
            dingtalk_archive_task = asyncio.create_task(
                _supervised_periodic(
                    "dingtalk_audio_archive",
                    lambda: periodic_dingtalk_audio_archive_sync(
                        stop_event,
                        interval_seconds=settings.dingtalk_audio_archive_sync_interval_seconds,
                        lookback_minutes=settings.dingtalk_audio_archive_sync_lookback_minutes,
                        workers=settings.dingtalk_audio_archive_sync_workers,
                        backfill_enabled=settings.dingtalk_audio_archive_backfill_enabled,
                        backfill_interval_hours=settings.dingtalk_audio_archive_backfill_interval_hours,
                        backfill_days=settings.dingtalk_audio_archive_backfill_days,
                    ),
                    stop_event,
                )
            )
            app.state.dingtalk_audio_archive_sync_task = dingtalk_archive_task
            app.state.dingtalk_audio_archive_sync_enabled = True
            app.state.dingtalk_audio_archive_sync_started_at = datetime.now(timezone.utc)
            app.state.dingtalk_audio_archive_sync_note = "钉钉音频归档同步服务运行中"
            logger.info(
                "dingtalk archive sync started interval_seconds=%d lookback_minutes=%d backfill_enabled=%s",
                settings.dingtalk_audio_archive_sync_interval_seconds,
                settings.dingtalk_audio_archive_sync_lookback_minutes,
                settings.dingtalk_audio_archive_backfill_enabled,
            )

        # --- DingTalk audio backlog sync state ---
        app.state.dingtalk_audio_backlog_sync_task = None
        app.state.dingtalk_audio_backlog_sync_enabled = False
        app.state.dingtalk_audio_backlog_sync_started_at = None
        app.state.dingtalk_audio_backlog_sync_note = None

        if not settings.dingtalk_audio_backlog_sync_enabled:
            app.state.dingtalk_audio_backlog_sync_note = "钉钉归档补处理服务已禁用"
            logger.info("dingtalk audio backlog sync disabled by config")
        elif settings.dingtalk_audio_backlog_sync_interval_seconds <= 0:
            app.state.dingtalk_audio_backlog_sync_note = "钉钉归档补处理未启动：同步间隔必须大于 0 秒"
            logger.warning("dingtalk audio backlog sync disabled because interval <= 0")
        else:
            dingtalk_backlog_task = asyncio.create_task(
                _supervised_periodic(
                    "dingtalk_audio_backlog",
                    lambda: periodic_dingtalk_audio_backlog_sync(
                        stop_event,
                        interval_seconds=settings.dingtalk_audio_backlog_sync_interval_seconds,
                        workers=settings.dingtalk_audio_backlog_sync_workers,
                        retry_failed=settings.dingtalk_audio_backlog_retry_failed_enabled,
                        limit=settings.dingtalk_audio_backlog_sync_limit_per_run,
                    ),
                    stop_event,
                )
            )
            app.state.dingtalk_audio_backlog_sync_task = dingtalk_backlog_task
            app.state.dingtalk_audio_backlog_sync_enabled = True
            app.state.dingtalk_audio_backlog_sync_started_at = datetime.now(timezone.utc)
            app.state.dingtalk_audio_backlog_sync_note = "钉钉归档补处理服务运行中"
            logger.info(
                "dingtalk audio backlog sync started interval_seconds=%d workers=%d retry_failed=%s limit=%d",
                settings.dingtalk_audio_backlog_sync_interval_seconds,
                settings.dingtalk_audio_backlog_sync_workers,
                settings.dingtalk_audio_backlog_retry_failed_enabled,
                settings.dingtalk_audio_backlog_sync_limit_per_run,
            )

        # --- ASR hotword sync state ---
        app.state.asr_hotword_sync_task = None
        app.state.asr_hotword_sync_enabled = False
        app.state.asr_hotword_sync_started_at = None
        app.state.asr_hotword_sync_note = None
        app.state.sap_auto_push_task = None
        app.state.sap_auto_push_enabled = False
        app.state.sap_auto_push_started_at = None
        app.state.sap_auto_push_note = None
        app.state.visit_order_sync_task = None
        app.state.visit_order_sync_enabled = False
        app.state.visit_order_sync_started_at = None
        app.state.visit_order_sync_note = None

        if not settings.asr_hotword_auto_sync_enabled:
            app.state.asr_hotword_sync_note = "ASR 热词自动巡检已禁用"
            logger.info("ASR hotword sync disabled by config")
        elif settings.asr_hotword_auto_sync_interval_seconds <= 0:
            app.state.asr_hotword_sync_note = "ASR 热词自动巡检未启动：同步间隔必须大于 0 秒"
            logger.warning("ASR hotword sync disabled because interval <= 0")
        else:
            asr_hotword_task = asyncio.create_task(
                _supervised_periodic(
                    "asr_hotword_sync",
                    lambda: periodic_asr_hotword_sync(
                        stop_event,
                        interval_seconds=settings.asr_hotword_auto_sync_interval_seconds,
                    ),
                    stop_event,
                )
            )
            app.state.asr_hotword_sync_task = asr_hotword_task
            app.state.asr_hotword_sync_enabled = True
            app.state.asr_hotword_sync_started_at = datetime.now(timezone.utc)
            app.state.asr_hotword_sync_note = "ASR 热词自动巡检服务运行中"
            logger.info(
                "ASR hotword sync started interval_seconds=%d",
                settings.asr_hotword_auto_sync_interval_seconds,
            )

        # --- SAP auto push state ---
        if not settings.sap_rfc_auto_push_on_bind:
            app.state.sap_auto_push_note = "SAP 自动回传服务已禁用"
            logger.info("sap auto push disabled by config")
        elif settings.sap_rfc_auto_push_interval_seconds <= 0:
            app.state.sap_auto_push_note = "SAP 自动回传未启动：扫描间隔必须大于 0 秒"
            logger.warning("sap auto push disabled because interval <= 0")
        elif settings.sap_rfc_auto_push_stable_seconds <= 0:
            app.state.sap_auto_push_note = "SAP 自动回传未启动：稳定等待时间必须大于 0 秒"
            logger.warning("sap auto push disabled because stable seconds <= 0")
        else:
            sap_auto_push_task = asyncio.create_task(
                _supervised_periodic(
                    "sap_auto_push",
                    lambda: periodic_sap_auto_push_scan(
                        stop_event,
                        interval_seconds=settings.sap_rfc_auto_push_interval_seconds,
                        limit=settings.sap_rfc_auto_push_limit_per_run,
                    ),
                    stop_event,
                )
            )
            app.state.sap_auto_push_task = sap_auto_push_task
            app.state.sap_auto_push_enabled = True
            app.state.sap_auto_push_started_at = datetime.now(timezone.utc)
            app.state.sap_auto_push_note = "SAP 自动回传服务运行中"
            logger.info(
                "sap auto push started interval_seconds=%d stable_seconds=%d limit=%d",
                settings.sap_rfc_auto_push_interval_seconds,
                settings.sap_rfc_auto_push_stable_seconds,
                settings.sap_rfc_auto_push_limit_per_run,
            )

        # --- Visit order materialization reconcile state ---
        if not settings.visit_order_auto_sync_enabled:
            app.state.visit_order_sync_note = "到诊单兜底同步服务已禁用"
            logger.info("visit order context sync disabled by config")
        elif settings.visit_order_auto_sync_interval_seconds <= 0:
            app.state.visit_order_sync_note = "到诊单兜底同步未启动：同步间隔必须大于 0 秒"
            logger.warning("visit order context sync disabled because interval <= 0")
        else:
            visit_order_sync_task = asyncio.create_task(
                _supervised_periodic(
                    "visit_order_context_sync",
                    lambda: periodic_visit_order_context_sync(
                        stop_event,
                        interval_seconds=settings.visit_order_auto_sync_interval_seconds,
                    ),
                    stop_event,
                )
            )
            app.state.visit_order_sync_task = visit_order_sync_task
            app.state.visit_order_sync_enabled = True
            app.state.visit_order_sync_started_at = datetime.now(timezone.utc)
            app.state.visit_order_sync_note = "到诊单兜底同步服务运行中"
            logger.info(
                "visit order context sync started interval_seconds=%d",
                settings.visit_order_auto_sync_interval_seconds,
            )

        yield

        # --- shutdown ---
        stop_event.set()

        for task, name in [
            (archive_cache_warmup_task, "archive cache warmup"),
            (archive_index_refresh_task, "archive index refresh"),
            (sync_task, "staff sync"),
            (dingtalk_audio_task, "dingtalk audio sync"),
            (dingtalk_archive_task, "dingtalk archive sync"),
            (dingtalk_backlog_task, "dingtalk audio backlog sync"),
            (asr_hotword_task, "ASR hotword sync"),
            (sap_auto_push_task, "SAP auto push"),
            (visit_order_sync_task, "visit order context sync"),
        ]:
            if task is None:
                continue
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.TimeoutError:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            logger.info("%s task stopped", name)

        app.state.staff_sync_task = None
        app.state.staff_sync_scheduler_note = "员工状态定时同步服务已停止"
        app.state.dingtalk_audio_sync_task = None
        app.state.dingtalk_audio_sync_note = "钉钉音频同步服务已停止"
        app.state.dingtalk_audio_archive_sync_task = None
        app.state.dingtalk_audio_archive_sync_note = "钉钉音频归档同步服务已停止"
        app.state.dingtalk_audio_backlog_sync_task = None
        app.state.dingtalk_audio_backlog_sync_note = "钉钉归档补处理服务已停止"
        app.state.asr_hotword_sync_task = None
        app.state.asr_hotword_sync_note = "ASR 热词自动巡检服务已停止"
        app.state.sap_auto_push_task = None
        app.state.sap_auto_push_note = "SAP 自动回传服务已停止"
        app.state.visit_order_sync_task = None
        app.state.visit_order_sync_note = "到诊单兜底同步服务已停止"

        # 企业微信/钉钉 IOT 共享 httpx client 关闭。
        try:
            await stop_dingtalk_pipeline_workers()
        except Exception:
            logger.exception("failed to stop dingtalk pipeline workers cleanly")
        with suppress(Exception):
            await close_shared_wecom_client()
        with suppress(Exception):
            await close_shared_iot_client()
        with suppress(Exception):
            await close_shared_message_push_client()
        with suppress(Exception):
            await close_shared_sap_push_client()
        with suppress(Exception):
            await close_shared_tencent_client()
        with suppress(Exception):
            await close_shared_xfyun_client()
        with suppress(Exception):
            dispose_sync_lookup_engine()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url=f"{settings.api_v1_prefix}/docs",
        openapi_url=f"{settings.api_v1_prefix}/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        HotReadCacheMiddleware,
        api_prefix=settings.api_v1_prefix,
        enabled=settings.hot_read_cache_enabled,
        ttl_seconds=settings.hot_read_cache_ttl_seconds,
        badge_ttl_seconds=settings.hot_read_cache_badge_ttl_seconds,
        max_items=settings.hot_read_cache_max_items,
        max_body_bytes=settings.hot_read_cache_max_body_bytes,
    )

    # CORS: spec forbids "*" + credentials. The frontend uses Bearer tokens
    # (no cookies), so disabling credentials with wildcard origins is safe.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix=settings.api_v1_prefix)
    app.include_router(ws_router)
    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("smart_badge_api.main:app", host="0.0.0.0", port=8000, reload=True)
