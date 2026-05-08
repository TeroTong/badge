"""分析任务分发层。"""

from __future__ import annotations

import asyncio
import logging

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.brokers.stub import StubBroker

from smart_badge_api.analysis.runner import execute_analysis
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.session import _session_factory
from smart_badge_api.sap_push_service import execute_sap_push_log

WORKER_ENTRYPOINT = "uv run dramatiq smart_badge_api.task_queue"
logger = logging.getLogger("smart_badge.task_queue")


def _configure_broker() -> None:
    settings = get_settings()
    broker = dramatiq.get_broker()

    if "dramatiq" in {settings.task_dispatch_mode, settings.sap_rfc_dispatch_mode}:
        dramatiq.set_broker(RedisBroker(url=settings.redis_url))
        return

    if not isinstance(broker, StubBroker):
        dramatiq.set_broker(StubBroker())


_configure_broker()


@dramatiq.actor(queue_name="analysis", max_retries=0)
def run_analysis_actor(task_id: str) -> None:
    asyncio.run(execute_analysis(task_id))


@dramatiq.actor(queue_name="sap_push", max_retries=0)
def run_sap_push_actor(push_log_id: str) -> None:
    asyncio.run(execute_sap_push_log(push_log_id))


async def execute_visit_order_push_materialization(keys: list[list[str]] | list[tuple[str, str]]) -> None:
    from smart_badge_api.visit_order_sync import (
        retry_visit_order_sync,
        sync_pushed_sap_hana_visit_orders_for_recording_contexts,
        sync_sap_hana_customer_birthdays_for_keys,
    )

    normalized_keys = {
        (str(item[0] or "").strip(), str(item[1] or "").strip())
        for item in keys
        if len(item) >= 2 and str(item[0] or "").strip() and str(item[1] or "").strip()
    }
    if not normalized_keys:
        return

    async with _session_factory() as db:
        try:
            birthday_result = await retry_visit_order_sync(
                lambda: sync_sap_hana_customer_birthdays_for_keys(db, keys=normalized_keys),
                label="sap-hana-push-customer-birthday",
                attempts=3,
                initial_delay_seconds=1.0,
            )
            if any(birthday_result.values()):
                logger.info(
                    "SAP HANA push customer birthday sync result=%s keys=%s",
                    birthday_result,
                    sorted(normalized_keys),
                )
        except Exception:
            logger.exception(
                "SAP HANA push customer birthday lookup failed; delayed retry will continue keys=%s",
                sorted(normalized_keys),
            )

        try:
            result = await retry_visit_order_sync(
                lambda: sync_pushed_sap_hana_visit_orders_for_recording_contexts(
                    db,
                    keys=normalized_keys,
                    include_customer_birthdays=False,
                ),
                label="sap-hana-push-materialize",
                attempts=3,
                initial_delay_seconds=1.0,
            )
            logger.info(
                "SAP HANA push materialization finished synced=%d new=%d updated=%d range=%s keys=%s",
                result.synced_count,
                result.new_count,
                result.updated_count,
                result.date_range,
                sorted(normalized_keys),
            )
        except Exception:
            logger.exception(
                "SAP HANA visit order push saved but Visitorders materialization failed; periodic sync will retry keys=%s",
                sorted(normalized_keys),
            )


@dramatiq.actor(queue_name="visit_order", max_retries=0)
def run_visit_order_push_materialization_actor(keys: list[list[str]]) -> None:
    asyncio.run(execute_visit_order_push_materialization(keys))


def get_dispatch_runtime() -> dict[str, str | bool | None]:
    mode = get_settings().task_dispatch_mode
    return {
        "task_dispatch_mode": mode,
        "requires_worker": mode == "dramatiq",
        "worker_entrypoint": WORKER_ENTRYPOINT if mode == "dramatiq" else None,
    }


async def dispatch_analysis_task(task_id: str) -> bool:
    """分发分析任务。

    Returns:
        True: 已在当前请求内同步执行完成。
        False: 已异步分发，稍后由后台执行。
    """

    mode = get_settings().task_dispatch_mode
    if mode == "dramatiq":
        run_analysis_actor.send(task_id)
        return False

    if mode == "background":
        asyncio.create_task(execute_analysis(task_id))
        return False

    if mode == "eager":
        await execute_analysis(task_id)
        return True

    raise RuntimeError(f"Unsupported task dispatch mode: {mode}")


async def dispatch_sap_push_log(push_log_id: str) -> bool:
    """分发 SAP 咨询单回传任务。

    Returns:
        True: 已在当前请求内同步执行完成。
        False: 已异步分发，稍后由后台执行。
    """

    mode = get_settings().sap_rfc_dispatch_mode
    if mode == "dramatiq":
        run_sap_push_actor.send(push_log_id)
        return False

    if mode == "background":
        asyncio.create_task(execute_sap_push_log(push_log_id))
        return False

    if mode == "eager":
        await execute_sap_push_log(push_log_id)
        return True

    raise RuntimeError(f"Unsupported SAP RFC dispatch mode: {mode}")


async def dispatch_visit_order_push_materialization(keys: set[tuple[str, str]]) -> bool:
    payload = [[jgbm, dzdh] for jgbm, dzdh in sorted(keys) if jgbm and dzdh]
    if not payload:
        return False

    mode = get_settings().task_dispatch_mode
    if mode == "dramatiq":
        try:
            run_visit_order_push_materialization_actor.send(payload)
        except Exception:
            logger.exception("failed to dispatch visit-order materialization to Dramatiq; falling back to background task")
            asyncio.create_task(execute_visit_order_push_materialization(payload))
        return False

    if mode == "background":
        asyncio.create_task(execute_visit_order_push_materialization(payload))
        return False

    if mode == "eager":
        await execute_visit_order_push_materialization(payload)
        return True

    raise RuntimeError(f"Unsupported visit-order push dispatch mode: {mode}")
