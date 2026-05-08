from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class StaffCreate(BaseModel):
    name: str | None = None
    phone: str | None = None
    external_account: str | None = None
    wecom_user_id: str | None = None
    gender: str | None = None
    hospital_code: str | None = None
    hospital_short_name: str | None = None
    position_id: str | None = None
    role: str | None = None
    permission_role: str | None = None
    is_active: bool = True


class StaffUpdate(BaseModel):
    name: str | None = None
    phone: str | None = None
    external_account: str | None = None
    wecom_user_id: str | None = None
    gender: str | None = None
    hospital_code: str | None = None
    hospital_short_name: str | None = None
    position_id: str | None = None
    role: str | None = None
    permission_role: str | None = None
    is_active: bool | None = None


class StaffOut(BaseModel):
    id: str
    name: str
    phone: str | None
    external_account: str | None
    wecom_user_id: str | None
    wecom_corp_id: str | None = None
    gender: str | None
    hospital_code: str | None
    hospital_short_name: str | None
    position_id: str | None
    position_name: str | None = None
    role: str
    permission_role: str = "staff"
    badge_id: str | None
    is_doctor: bool = False
    is_nurse: bool = False
    is_anesthetist: bool = False
    is_cashier: bool = False
    is_guide: bool = False
    is_pre_advisor: bool = False
    is_onsite_advisor: bool = False
    is_advisor_assistant: bool = False
    is_doctor_assistant: bool = False
    is_vip_service: bool = False
    is_active: bool
    account_opened: bool = False
    account_username: str | None = None
    account_is_active: bool | None = None
    account_last_login_at: str | None = None

    model_config = {"from_attributes": True}


class StaffAccountActionOut(BaseModel):
    staff_id: str
    staff_name: str
    username: str
    is_active: bool
    created: bool = False
    source_field: str | None = None
    source_label: str | None = None
    temporary_password: str | None = None
    message: str


class StaffImportRow(BaseModel):
    name: str
    phone: str | None = None
    external_account: str | None = None
    wecom_user_id: str | None = None
    wecom_corp_id: str | None = None
    gender: str | None = None
    hospital_code: str | None = None
    hospital_short_name: str | None = None
    position_name: str | None = None
    permission_role: str | None = None
    is_active: bool = True


class StaffImportRequest(BaseModel):
    rows: list[StaffImportRow]


class StaffImportResult(BaseModel):
    created_count: int


class StaffDirectorySyncStatus(BaseModel):
    scheduler_enabled: bool
    scheduler_running: bool
    scheduler_started_at: datetime | None = None
    scheduler_note: str | None = None
    interval_seconds: int
    last_synced_at: datetime | None = None
    next_scheduled_at: datetime | None = None
    last_sync_status: Literal["not_started", "success", "failed"] = "not_started"
    checked_count: int | None = None
    updated_count: int | None = None
    missing_count: int | None = None
    deactivated_count: int | None = None
    error_message: str | None = None


class StaffHospitalOptionOut(BaseModel):
    hospital_code: str
    hospital_name: str


class StaffIdentityLookupOut(BaseModel):
    external_account: str
    name: str | None = None
    hospital_code: str | None = None
    hospital_short_name: str | None = None
    phone: str | None = None
    dingtalk_user_id: str | None = None
    source: str


class StaffBadgeBindingCandidateOut(BaseModel):
    id: str
    name: str
    external_account: str | None = None
    badge_id: str | None = None
    hospital_code: str | None = None
    hospital_short_name: str | None = None
    position_name: str | None = None
    is_active: bool
    account_opened: bool = False
    account_username: str | None = None
    account_is_active: bool | None = None


class StaffBadgeBindingUpdate(BaseModel):
    device_code: str | None = None
    device_name: str | None = None
    effective_start: datetime | None = None
    effective_end: datetime | None = None
    override_overlap: bool = False
    effective_at: datetime | None = None
