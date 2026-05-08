from fastapi import APIRouter

from smart_badge_api.core.config import get_settings
from smart_badge_api.schemas.system import HealthResponse
from smart_badge_api.task_queue import get_dispatch_runtime

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    settings = get_settings()
    runtime = get_dispatch_runtime()
    return HealthResponse(
        status="ok",
        app_name=settings.app_name,
        environment=settings.app_env,
        api_prefix=settings.api_v1_prefix,
        task_dispatch_mode=runtime["task_dispatch_mode"],
        requires_worker=runtime["requires_worker"],
        worker_entrypoint=runtime["worker_entrypoint"],
        llm_configured=bool(settings.llm_api_key),
        websocket_auth_required=True,
    )
