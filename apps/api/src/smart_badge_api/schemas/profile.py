from __future__ import annotations

from pydantic import BaseModel, Field

from smart_badge_api.schemas.audit_logs import AuditLogOut


class AccountProfileOut(BaseModel):
    id: str
    username: str
    display_name: str
    role: str
    is_active: bool
    created_at: str
    updated_at: str
    activity_count: int
    last_activity_at: str | None = None
    recent_activities: list[AuditLogOut]


class MyBadgeOut(BaseModel):
    bound: bool
    reason: str | None = None
    device_id: str | None = None
    device_code: str | None = None
    device_name: str | None = None
    staff_id: str | None = None
    staff_name: str | None = None
    external_account: str | None = None
    hospital_short_name: str | None = None
    position_name: str | None = None
    status: str | None = None
    online: bool | None = None
    battery_level: int | None = None
    team_code: str | None = None
    user_id: str | None = None
    can_control_recording: bool = False
    is_recording: bool = False
    recording_started_at: str | None = None
    remote_warning: str | None = None


class AccountProfileUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=6, max_length=128)


class MessageOut(BaseModel):
    message: str
