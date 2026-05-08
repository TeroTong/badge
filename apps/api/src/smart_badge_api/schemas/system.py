from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"]
    app_name: str
    environment: str
    api_prefix: str
    task_dispatch_mode: Literal["dramatiq", "background", "eager"]
    requires_worker: bool
    worker_entrypoint: str | None = None
    llm_configured: bool
    websocket_auth_required: bool
