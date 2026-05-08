from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.account_provisioning import (
    AccountProvisioningError,
    provision_staff_account,
    resolve_staff_for_user,
    sync_user_scope_from_staff,
)
from smart_badge_api.api.audit import append_audit_log
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.core.config import get_settings
from smart_badge_api.core.permissions import normalize_permission_role
from smart_badge_api.core.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    verify_password,
)
from smart_badge_api.db.models import Staff, User
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
    WecomAuthorizeUrlOut,
    WecomCodeExchangeRequest,
)
from smart_badge_api.wecom import (
    WecomApiError,
    WecomConfigError,
    WecomMemberIdentity,
    WecomTenantConfig,
    build_wecom_authorize_url,
    fetch_wecom_member_identity,
)
from smart_badge_api.wecom_tenants import resolve_wecom_tenant_config, validate_wecom_frontend_url

router = APIRouter(prefix="/auth", tags=["认证"])
WECOM_LOGIN_STATE = "smart_badge_wecom_login"


async def _build_user_out(db: AsyncSession, user: User) -> UserOut:
    staff = await resolve_staff_for_user(db, user=user, persist_link=True)
    return UserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=normalize_permission_role(user.role),
        is_active=user.is_active,
        staff_id=user.staff_id,
        staff_name=staff.name if staff else None,
        staff_external_account=staff.external_account if staff else None,
        staff_wecom_user_id=staff.wecom_user_id if staff else None,
        staff_wecom_corp_id=staff.wecom_corp_id if staff else None,
        hospital_code=user.hospital_code,
        hospital_name=user.hospital_name,
    )


def _sanitize_redirect_path(raw_redirect: str | None) -> str:
    default_path = get_settings().wecom_default_redirect_path
    redirect = (raw_redirect or "").strip()
    if not redirect:
        return default_path
    if not redirect.startswith("/") or redirect.startswith("//"):
        return default_path
    return redirect


async def _resolve_staff_for_wecom_identity(
    db: AsyncSession,
    identity: WecomMemberIdentity,
    tenant: WecomTenantConfig,
) -> tuple[Staff | None, str | None]:
    staff = (
        await db.execute(
            select(Staff)
            .where(
                Staff.wecom_user_id == identity.userid,
                Staff.wecom_corp_id == tenant.corp_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if staff is not None:
        return staff, None
    if tenant.is_default:
        staff = (
            await db.execute(
                select(Staff)
                .where(
                    Staff.wecom_user_id == identity.userid,
                    Staff.wecom_corp_id.is_(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if staff is not None:
            staff.wecom_corp_id = tenant.corp_id
            await db.commit()
            await db.refresh(staff)
            return staff, "补全企业微信主体绑定"

    if identity.mobile:
        phone_matches = (
            await db.execute(
                select(Staff).where(
                    Staff.phone == identity.mobile,
                    or_(Staff.wecom_corp_id.is_(None), Staff.wecom_corp_id == tenant.corp_id),
                    or_(Staff.wecom_user_id.is_(None), Staff.wecom_user_id == identity.userid),
                )
            )
        ).scalars().all()
        if len(phone_matches) == 1:
            staff = phone_matches[0]
            if staff.wecom_user_id != identity.userid or staff.wecom_corp_id != tenant.corp_id:
                staff.wecom_user_id = identity.userid
                staff.wecom_corp_id = tenant.corp_id
                await db.commit()
                await db.refresh(staff)
                return staff, "首次通过手机号自动绑定企业微信成员"
            return staff, None

    external_account_matches = (
        await db.execute(
            select(Staff).where(
                Staff.external_account == identity.userid,
                or_(Staff.wecom_corp_id.is_(None), Staff.wecom_corp_id == tenant.corp_id),
                or_(Staff.wecom_user_id.is_(None), Staff.wecom_user_id == identity.userid),
            )
        )
    ).scalars().all()
    if len(external_account_matches) == 1:
        staff = external_account_matches[0]
        if staff.wecom_user_id != identity.userid or staff.wecom_corp_id != tenant.corp_id:
            staff.wecom_user_id = identity.userid
            staff.wecom_corp_id = tenant.corp_id
            await db.commit()
            await db.refresh(staff)
            return staff, "首次通过员工账号自动绑定企业微信成员"
        return staff, None

    return None, None


async def _ensure_user_for_wecom_staff(
    db: AsyncSession,
    staff: Staff,
    identity: WecomMemberIdentity,
) -> tuple[User, bool]:
    provisioned = await provision_staff_account(
        db,
        staff=staff,
        preserve_higher_role=True,
    )
    user = provisioned.user
    auto_created = provisioned.created

    changed = False
    display_name = staff.name or identity.name or ""
    if display_name and user.display_name != display_name:
        user.display_name = display_name
        changed = True
    before = (
        user.role,
        user.hospital_code,
        user.hospital_name,
    )
    sync_user_scope_from_staff(user, staff, preserve_higher_role=True)
    after = (
        user.role,
        user.hospital_code,
        user.hospital_name,
    )
    if before != after:
        changed = True
    if changed:
        await db.commit()
        await db.refresh(user)
    return user, auto_created


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已禁用")

    user.last_login_at = datetime.now(timezone.utc)
    token = create_access_token(user.id)
    refresh = create_refresh_token(user.id)
    await append_audit_log(
        db,
        operator_name=user.display_name or user.username,
        ip_address=request.client.host if request.client else "",
        module_name="登录系统",
        action_name="账号密码登录",
        content="账号密码登录",
    )
    return TokenResponse(access_token=token, refresh_token=refresh)


@router.get("/wecom/authorize-url", response_model=WecomAuthorizeUrlOut)
async def get_wecom_authorize_url(
    request: Request,
    redirect: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    callback_redirect = _sanitize_redirect_path(redirect)
    try:
        tenant = await resolve_wecom_tenant_config(db, request=request)
        frontend_url = validate_wecom_frontend_url(tenant.frontend_url)
        callback_url = f"{frontend_url.rstrip('/')}/login?{urlencode({'wecom': '1', 'redirect': callback_redirect})}"
        authorize_url = build_wecom_authorize_url(callback_url, state=WECOM_LOGIN_STATE, tenant=tenant)
    except WecomConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return WecomAuthorizeUrlOut(authorize_url=authorize_url)


@router.post("/wecom/exchange", response_model=TokenResponse)
async def exchange_wecom_code(
    body: WecomCodeExchangeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    try:
        tenant = await resolve_wecom_tenant_config(db, request=request)
        identity = await fetch_wecom_member_identity(body.code, tenant=tenant)
    except WecomConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except WecomApiError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    staff, binding_note = await _resolve_staff_for_wecom_identity(db, identity, tenant)
    if staff is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "当前企业微信成员尚未绑定系统人员，请先在人员管理中填写企业微信 UserId，或确保手机号可唯一匹配",
        )
    if not staff.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "当前企业微信成员绑定的人员已被禁用")

    try:
        user, auto_created = await _ensure_user_for_wecom_staff(db, staff, identity)
    except AccountProvisioningError as exc:
        detail = exc.detail
        if "工号或手机号" in detail:
            detail = "当前企业微信成员缺少登录账号来源，请联系管理员先补员工工号或手机号"
        raise HTTPException(exc.status_code, detail) from exc
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "当前企业微信成员绑定的系统账号已被禁用")

    user.last_login_at = datetime.now(timezone.utc)
    await append_audit_log(
        db,
        operator_name=user.display_name or user.username,
        ip_address=request.client.host if request.client else "",
        module_name="登录系统",
        action_name="企业微信免密登录",
        content=(
            f"企业微信免密登录：corp_id={tenant.corp_id} userid={identity.userid}"
            f"{'；' + binding_note if binding_note else ''}"
            f"{'；首次自动创建系统账号' if auto_created else ''}"
        ),
    )
    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )


@router.get("/me", response_model=UserOut)
async def get_me(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    return await _build_user_out(db, user)


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)):
    del body, request, db
    raise HTTPException(status.HTTP_403_FORBIDDEN, "系统不开放自主注册，请联系管理员开通账号")


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    user_id = decode_refresh_token(body.refresh_token)
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh_token invalid or expired")
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found or disabled")
    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )
