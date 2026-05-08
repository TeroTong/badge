from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class WecomTenantCreate(BaseModel):
    name: str
    host: str | None = None
    corp_id: str | None = None
    agent_id: str | None = None
    agent_secret: str | None = None
    frontend_url: str | None = None
    default_hospital_code: str
    default_hospital_name: str | None = None
    sap_summary_template_name: str | None = None
    sap_summary_template_version: str | None = None
    sap_summary_template: str | None = None
    sap_summary_prompt: str | None = None
    department_assistant_match_config: dict[str, Any] | None = None
    is_default: bool = False
    is_active: bool = True


class WecomTenantUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    corp_id: str | None = None
    agent_id: str | None = None
    agent_secret: str | None = None
    frontend_url: str | None = None
    default_hospital_code: str | None = None
    default_hospital_name: str | None = None
    sap_summary_template_name: str | None = None
    sap_summary_template_version: str | None = None
    sap_summary_template: str | None = None
    sap_summary_prompt: str | None = None
    department_assistant_match_config: dict[str, Any] | None = None
    is_default: bool | None = None
    is_active: bool | None = None


class WecomTenantOut(BaseModel):
    id: str
    name: str
    host: str | None
    corp_id: str | None
    agent_id: str | None
    frontend_url: str | None
    default_hospital_code: str | None
    default_hospital_name: str | None
    sap_summary_template_name: str | None
    sap_summary_template_version: str | None
    sap_summary_template: str | None
    sap_summary_prompt: str | None
    department_assistant_match_config: dict[str, Any] | None = None
    is_default: bool
    is_active: bool
    agent_secret_configured: bool
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}
