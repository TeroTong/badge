from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class OrganizationStaffOut(BaseModel):
    id: str
    name: str
    external_account: str | None = None
    hospital_code: str | None = None
    hospital_short_name: str | None = None
    position_id: str | None = None
    position_name: str | None = None
    permission_role: str = "staff"
    is_active: bool = True


class OrganizationUnitCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    hospital_code: str | None = None
    parent_id: str | None = None
    sort_order: int = 0
    is_active: bool = True


class OrganizationUnitUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    parent_id: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class OrganizationUnitOut(BaseModel):
    id: str
    hospital_code: str
    hospital_name: str | None = None
    name: str
    parent_id: str | None = None
    path: str
    sort_order: int
    member_count: int = 0
    is_active: bool
    created_at: datetime
    updated_at: datetime


class OrganizationUnitMemberOut(BaseModel):
    unit_id: str
    staff_id: str
    staff_name: str
    external_account: str | None = None
    position_name: str | None = None
    hospital_code: str | None = None
    hospital_short_name: str | None = None
    is_primary: bool = False
    is_active: bool = True
    created_at: datetime


class OrganizationUnitMemberUpdate(BaseModel):
    staff_ids: list[str] = Field(default_factory=list)


class OrganizationUnitMemberMove(BaseModel):
    staff_ids: list[str] = Field(..., min_length=1)
    target_unit_id: str


class StaffManagementRelationCreate(BaseModel):
    manager_staff_id: str
    subordinate_staff_id: str


class StaffManagementRelationByUnitCreate(BaseModel):
    manager_staff_id: str
    unit_id: str
    include_descendants: bool = True


class StaffManagementRelationBulkCreate(BaseModel):
    manager_staff_id: str
    subordinate_staff_ids: list[str] = Field(..., min_length=1)


class StaffManagementRelationSync(BaseModel):
    subordinate_staff_ids: list[str] = Field(default_factory=list)


class StaffManagementRelationOut(BaseModel):
    id: str
    hospital_code: str
    manager_staff_id: str
    manager_name: str
    subordinate_staff_id: str
    subordinate_name: str
    created_at: datetime


class OrganizationOverviewOut(BaseModel):
    hospital_code: str
    hospital_name: str | None = None
    staff: list[OrganizationStaffOut]
    units: list[OrganizationUnitOut]
    memberships: list[OrganizationUnitMemberOut]
    management_relations: list[StaffManagementRelationOut]
