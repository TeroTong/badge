"""认证相关 DTO。"""

from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str | None = None
    advisor_code: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class WecomCodeExchangeRequest(BaseModel):
    code: str


class WecomAuthorizeUrlOut(BaseModel):
    authorize_url: str


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: str
    username: str
    display_name: str
    role: str
    is_active: bool
    staff_id: str | None = None
    staff_name: str | None = None
    staff_external_account: str | None = None
    staff_wecom_user_id: str | None = None
    staff_wecom_corp_id: str | None = None
    hospital_code: str | None = None
    hospital_name: str | None = None

    model_config = {"from_attributes": True}
