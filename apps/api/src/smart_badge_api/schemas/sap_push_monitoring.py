from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from smart_badge_api.schemas.recordings import SapPushLogOut


class SapPushMonitoringOverviewOut(BaseModel):
    total_count: int
    succeeded_count: int
    failed_count: int
    pending_count: int
    auto_count: int
    manual_count: int
    latest_sent_at: str | None = None


class SapPushMonitoringLogOut(SapPushLogOut):
    log_id: str
    target_index: int = 1
    target_count: int = 1
    is_primary_target: bool = False
    result_status: Literal["succeeded", "failed", "queued", "sending", "prepared", "skipped"] | str
    result_reason: str | None = None
