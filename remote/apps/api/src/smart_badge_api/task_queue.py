"""分析任务分发层。"""

from __future__ import annotations

import asyncio

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.brokers.stub import StubBroker

from smart_badge_api.analysis.runner import execute_analysis
from smart_badge_api.core.config import get_settings
from smart_badge_api.sap_push_service import execute_sap_push_log

WORKER_ENTRYPOINT = "uv run dramatiq smart_badge_api.task_queue"


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
