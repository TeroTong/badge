from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.audit import append_audit_log
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.core.permissions import is_global_role
from smart_badge_api.db.models import User, WecomTenant
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.pagination import PaginatedResponse, make_page_response
from smart_badge_api.schemas.wecom_tenants import WecomTenantCreate, WecomTenantOut, WecomTenantUpdate
from smart_badge_api.wecom import WecomConfigError
from smart_badge_api.wecom_tenants import normalize_wecom_host, validate_wecom_frontend_url

router = APIRouter(prefix="/wecom/tenants", tags=["机构管理"])

_DEPARTMENT_ASSISTANT_CODES = {
    "JGKS01",
    "JGKS02",
    "JGKS03",
    "JGKS04",
    "JGKS05",
    "JGKS06",
    "JGKS07",
    "JGKS08",
    "JGKS09",
    "JGKS10",
    "JGKS11",
    "JGKS12",
    "JGKS13",
    "JGKS14",
}

_HOSPITAL_ADMIN_UPDATE_FIELDS = {"department_assistant_match_config"}


def _clean(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _require_text(value: str | None, label: str) -> str:
    text = _clean(value)
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"请填写{label}")
    return text


def _format_time(value) -> str:
    return value.isoformat() if value else ""


def _user_hospital_code(user: User) -> str | None:
    return _clean(getattr(user, "hospital_code", None))


def _assert_global_user(user: User, action: str) -> None:
    if not is_global_role(getattr(user, "role", None)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"只有系统管理员可以{action}")


def _assert_tenant_visible_to_user(tenant: WecomTenant, user: User) -> None:
    if is_global_role(getattr(user, "role", None)):
        return
    hospital_code = _user_hospital_code(user)
    if not hospital_code or _clean(tenant.default_hospital_code) != hospital_code:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "机构配置不存在")


def _assert_tenant_update_allowed(user: User, data: dict[str, Any]) -> None:
    if is_global_role(getattr(user, "role", None)):
        return
    disallowed = sorted(set(data) - _HOSPITAL_ADMIN_UPDATE_FIELDS)
    if disallowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "机构管理员只能维护本机构的科室助理配置")


def _clean_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        text = _clean(str(value))
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
    return result


def _normalize_department_assistant_match_config(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "机构科室助理配置格式不正确")

    departments: list[dict[str, Any]] = []
    for item in raw.get("departments") or []:
        if not isinstance(item, dict):
            continue
        code = _clean(item.get("department_code"))
        if not code or code not in _DEPARTMENT_ASSISTANT_CODES:
            continue
        departments.append(
            {
                "department_code": code,
                "department_name": _clean(item.get("department_name")),
                "assistant_staff_ids": _clean_list(item.get("assistant_staff_ids")),
            }
        )

    return {
        "enabled": bool(raw.get("enabled", True)),
        "departments": departments,
    }


def _to_out(row: WecomTenant) -> WecomTenantOut:
    return WecomTenantOut(
        id=row.id,
        name=row.name,
        host=row.host,
        corp_id=row.corp_id,
        agent_id=row.agent_id,
        frontend_url=row.frontend_url,
        callback_configured=bool(row.callback_token and row.callback_aes_key),
        default_hospital_code=row.default_hospital_code,
        default_hospital_name=row.default_hospital_name,
        sap_summary_template_name=row.sap_summary_template_name,
        sap_summary_template_version=row.sap_summary_template_version,
        sap_summary_template=row.sap_summary_template,
        sap_summary_prompt=row.sap_summary_prompt,
        sap_summary_enabled=bool(getattr(row, "sap_summary_enabled", True)),
        sap_auto_update_existing_consultation=bool(
            getattr(row, "sap_auto_update_existing_consultation", False)
        ),
        department_assistant_match_config=row.department_assistant_match_config,
        is_default=bool(row.is_default),
        is_active=bool(row.is_active),
        agent_secret_configured=bool(row.agent_secret),
        created_at=_format_time(row.created_at),
        updated_at=_format_time(row.updated_at),
    )


def _normalize_host(raw: str | None) -> str | None:
    if not _clean(raw):
        return None
    host = normalize_wecom_host(raw)
    if not host:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请填写合法的公网域名")
    return host


def _normalize_frontend_url(raw: str | None) -> str | None:
    if not _clean(raw):
        return None
    try:
        return validate_wecom_frontend_url(str(raw))
    except WecomConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


async def _ensure_host_available(db: AsyncSession, host: str | None, *, exclude_id: str | None = None) -> None:
    if not host:
        return
    stmt = select(WecomTenant.id).where(WecomTenant.host == host)
    if exclude_id:
        stmt = stmt.where(WecomTenant.id != exclude_id)
    exists = (await db.execute(stmt.limit(1))).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该公网域名已绑定其他机构")


async def _ensure_hospital_code_available(
    db: AsyncSession,
    hospital_code: str,
    *,
    exclude_id: str | None = None,
) -> None:
    stmt = select(WecomTenant.id).where(WecomTenant.default_hospital_code == hospital_code)
    if exclude_id:
        stmt = stmt.where(WecomTenant.id != exclude_id)
    exists = (await db.execute(stmt.limit(1))).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该机构编码已绑定其他机构")


async def _clear_other_defaults(db: AsyncSession, tenant_id: str) -> None:
    await db.execute(
        update(WecomTenant)
        .where(WecomTenant.id != tenant_id, WecomTenant.is_default.is_(True))
        .values(is_default=False)
    )


async def _ensure_can_disable_or_delete(db: AsyncSession, tenant: WecomTenant, *, deleting: bool = False) -> None:
    if tenant.is_default:
        action = "删除" if deleting else "停用"
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"默认机构配置不能直接{action}，请先设置其他配置为默认")
    if not tenant.is_active:
        return
    active_count = (
        await db.execute(select(func.count()).select_from(WecomTenant).where(WecomTenant.is_active.is_(True)))
    ).scalar_one()
    if active_count <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "至少需要保留一个启用的机构配置")


@router.get("", response_model=PaginatedResponse[WecomTenantOut])
async def list_wecom_tenants(
    keyword: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PaginatedResponse[WecomTenantOut]:
    keyword_text = keyword.strip() if isinstance(keyword, str) else ""
    active_filter = is_active if isinstance(is_active, bool) else None
    page_number = page if isinstance(page, int) and not isinstance(page, bool) and page >= 1 else 1
    page_limit = (
        page_size
        if isinstance(page_size, int) and not isinstance(page_size, bool) and 1 <= page_size <= 100
        else 20
    )
    stmt = select(WecomTenant).order_by(
        WecomTenant.is_default.desc(),
        WecomTenant.is_active.desc(),
        WecomTenant.updated_at.desc(),
    )
    if not is_global_role(getattr(current_user, "role", None)):
        hospital_code = _user_hospital_code(current_user)
        if not hospital_code:
            return make_page_response([], 0, page_number, page_limit)
        stmt = stmt.where(WecomTenant.default_hospital_code == hospital_code)
    if keyword_text:
        like = f"%{keyword_text}%"
        stmt = stmt.where(
            or_(
                WecomTenant.name.ilike(like),
                WecomTenant.host.ilike(like),
                WecomTenant.corp_id.ilike(like),
                WecomTenant.default_hospital_code.ilike(like),
            )
        )
    if active_filter is not None:
        stmt = stmt.where(WecomTenant.is_active.is_(active_filter))

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(stmt.offset((page_number - 1) * page_limit).limit(page_limit))
    ).scalars().all()
    return make_page_response([_to_out(row) for row in rows], total, page_number, page_limit)


@router.post("", response_model=WecomTenantOut, status_code=status.HTTP_201_CREATED)
async def create_wecom_tenant(
    body: WecomTenantCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WecomTenantOut:
    _assert_global_user(current_user, "新增机构配置")
    host = _normalize_host(body.host)
    await _ensure_host_available(db, host)
    hospital_code = _require_text(body.default_hospital_code, "机构编码")
    await _ensure_hospital_code_available(db, hospital_code)
    tenant = WecomTenant(
        name=_require_text(body.name, "机构名称"),
        host=host,
        corp_id=_clean(body.corp_id),
        agent_id=_clean(body.agent_id),
        agent_secret=_clean(body.agent_secret),
        callback_token=_clean(body.callback_token),
        callback_aes_key=_clean(body.callback_aes_key),
        frontend_url=_normalize_frontend_url(body.frontend_url),
        default_hospital_code=hospital_code,
        default_hospital_name=None,
        sap_summary_template_name=_clean(body.sap_summary_template_name),
        sap_summary_template_version=_clean(body.sap_summary_template_version),
        sap_summary_template=_clean(body.sap_summary_template),
        sap_summary_prompt=_clean(body.sap_summary_prompt),
        sap_summary_enabled=bool(body.sap_summary_enabled),
        sap_auto_update_existing_consultation=bool(body.sap_auto_update_existing_consultation),
        department_assistant_match_config=_normalize_department_assistant_match_config(
            body.department_assistant_match_config
        ),
        is_default=bool(body.is_default),
        is_active=bool(body.is_active),
    )
    db.add(tenant)
    try:
        await db.flush()
        if tenant.is_default:
            await _clear_other_defaults(db, tenant.id)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "机构配置重复，请检查公网域名") from exc
    await db.refresh(tenant)
    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="机构管理",
        action_name="新增机构配置",
        content=f"新增机构配置：{tenant.name}（{tenant.default_hospital_code}，{tenant.host or '-'}）",
    )
    return _to_out(tenant)


@router.put("/{tenant_id}", response_model=WecomTenantOut)
async def update_wecom_tenant(
    tenant_id: str,
    body: WecomTenantUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WecomTenantOut:
    tenant = await db.get(WecomTenant, tenant_id)
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "机构配置不存在")
    _assert_tenant_visible_to_user(tenant, current_user)

    data = body.model_dump(exclude_unset=True)
    _assert_tenant_update_allowed(current_user, data)
    if "host" in data:
        data["host"] = _normalize_host(data["host"])
        await _ensure_host_available(db, data["host"], exclude_id=tenant.id)
    if "frontend_url" in data:
        data["frontend_url"] = _normalize_frontend_url(data["frontend_url"])
    if "name" in data:
        data["name"] = _require_text(data["name"], "机构名称")
    for key in (
        "corp_id",
        "agent_id",
        "callback_token",
        "callback_aes_key",
        "sap_summary_template_name",
        "sap_summary_template_version",
        "sap_summary_template",
        "sap_summary_prompt",
    ):
        if key in data:
            data[key] = _clean(data[key])
    if "department_assistant_match_config" in data:
        data["department_assistant_match_config"] = _normalize_department_assistant_match_config(
            data["department_assistant_match_config"]
        )
    if "default_hospital_code" in data:
        data["default_hospital_code"] = _require_text(data["default_hospital_code"], "机构编码")
        await _ensure_hospital_code_available(db, data["default_hospital_code"], exclude_id=tenant.id)
    if "default_hospital_name" in data:
        data["default_hospital_name"] = None
    if "agent_secret" in data:
        secret = _clean(data["agent_secret"])
        if secret:
            data["agent_secret"] = secret
        else:
            data.pop("agent_secret", None)
    for secret_key in ("callback_token", "callback_aes_key"):
        if secret_key in data:
            secret = _clean(data[secret_key])
            if secret:
                data[secret_key] = secret
            else:
                data.pop(secret_key, None)
    if data.get("is_default") is False and tenant.is_default:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "默认机构配置不能直接取消，请将其他配置设为默认")
    if data.get("is_active") is False:
        await _ensure_can_disable_or_delete(db, tenant)

    for key, value in data.items():
        setattr(tenant, key, value)
    if tenant.is_default:
        tenant.is_active = True
        await db.flush()
        await _clear_other_defaults(db, tenant.id)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "机构配置重复，请检查公网域名") from exc
    await db.refresh(tenant)
    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="机构管理",
        action_name="更新机构配置",
        content=f"更新机构配置：{tenant.name}（{tenant.default_hospital_code or '-'}，{tenant.host or '-'}）",
    )
    return _to_out(tenant)


@router.delete("/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_wecom_tenant(
    tenant_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    _assert_global_user(current_user, "删除机构配置")
    tenant = await db.get(WecomTenant, tenant_id)
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "机构配置不存在")
    await _ensure_can_disable_or_delete(db, tenant, deleting=True)
    name = tenant.name
    host = tenant.host or "-"
    await db.delete(tenant)
    await db.commit()
    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="机构管理",
        action_name="删除机构配置",
        content=f"删除机构配置：{name}（{host}）",
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
