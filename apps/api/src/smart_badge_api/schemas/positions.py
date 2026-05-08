from pydantic import BaseModel


class PositionCreate(BaseModel):
    name: str
    position_type: str = "staff"
    mapped_role: str = "staff"
    is_super_admin: bool = False
    note: str = ""
    is_active: bool = True


class PositionUpdate(BaseModel):
    name: str | None = None
    position_type: str | None = None
    mapped_role: str | None = None
    is_super_admin: bool | None = None
    note: str | None = None
    is_active: bool | None = None


class PositionOut(BaseModel):
    id: str
    name: str
    position_type: str
    mapped_role: str
    is_super_admin: bool
    note: str
    is_active: bool
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}
