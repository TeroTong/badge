from __future__ import annotations

from urllib.parse import parse_qs, quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.audit import append_audit_log
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import User
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.wecom_menu import (
    WecomMenuActionOut,
    WecomMenuEntryOut,
    WecomMenuStateOut,
)
from smart_badge_api.wecom import (
    WecomApiError,
    WecomConfigError,
    WecomTenantConfig,
    delete_wecom_app_menu,
    fetch_wecom_app_menu,
    publish_wecom_app_menu,
)
from smart_badge_api.wecom_tenants import resolve_wecom_tenant_config, validate_wecom_frontend_url

router = APIRouter(prefix="/wecom/menu", tags=["企业微信菜单"])

_NO_MENU_ERRCODES = {46003}


def _validate_menu_frontend_url(tenant: WecomTenantConfig | None = None) -> str:
    frontend_url = tenant.frontend_url if tenant is not None else get_settings().frontend_url
    return validate_wecom_frontend_url(frontend_url)


def _build_wecom_entry_url(target_path: str, tenant: WecomTenantConfig | None = None) -> str:
    base_url = _validate_menu_frontend_url(tenant)
    if not target_path.startswith("/"):
        raise WecomConfigError("企业微信菜单跳转路径必须以 / 开头")
    return f"{base_url}/login?wecom=1&redirect={quote(target_path, safe='')}"


def build_default_wecom_menu_payload(tenant: WecomTenantConfig | None = None) -> dict:
    return {
        "button": [
            {
                "type": "view",
                "name": "我的工牌",
                "url": _build_wecom_entry_url("/wecom/badge", tenant),
            },
            {
                "type": "view",
                "name": "录音中心",
                "url": _build_wecom_entry_url("/wecom/recordings?tab=recordings", tenant),
            },
            {
                "type": "view",
                "name": "客户中心",
                "url": _build_wecom_entry_url("/wecom/customers", tenant),
            },
        ]
    }


def _extract_target_path(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    redirect = query.get("redirect", [None])[0]
    if isinstance(redirect, str) and redirect.startswith("/"):
        return redirect
    if parsed.path.startswith("/"):
        return parsed.path
    return None


def flatten_wecom_menu_entries(menu: dict) -> list[WecomMenuEntryOut]:
    items: list[WecomMenuEntryOut] = []

    def walk(buttons: list[dict], *, level: int) -> None:
        for button in buttons:
            if not isinstance(button, dict):
                continue
            sub_buttons = button.get("sub_button")
            if isinstance(sub_buttons, list) and sub_buttons:
                walk(sub_buttons, level=level + 1)
                continue
            label = str(button.get("name") or "").strip()
            if not label:
                continue
            target_url = str(button.get("url") or "").strip() or None
            items.append(
                WecomMenuEntryOut(
                    label=label,
                    type=str(button.get("type") or "view"),
                    level=level,
                    target_path=_extract_target_path(target_url),
                    target_url=target_url,
                )
            )

    walk(menu.get("button") or [], level=1)
    return items


def _resolve_agent_id(tenant: WecomTenantConfig | None = None) -> str:
    agent_id = (tenant.agent_id if tenant is not None else get_settings().wecom_agent_id).strip()
    if not agent_id:
        raise WecomConfigError("企业微信应用未配置 WECOM_AGENT_ID")
    return agent_id


def _translate_wecom_error(exc: WecomConfigError | WecomApiError) -> HTTPException:
    if isinstance(exc, WecomConfigError):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    return HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


@router.get("/default", response_model=WecomMenuStateOut)
async def get_default_wecom_menu(
    request: Request,
    tenant_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    try:
        tenant = await resolve_wecom_tenant_config(db, request=request, tenant_id=tenant_id)
        menu = build_default_wecom_menu_payload(tenant)
        return WecomMenuStateOut(
            agent_id=_resolve_agent_id(tenant),
            source="default",
            menu=menu,
            entries=flatten_wecom_menu_entries(menu),
        )
    except (WecomConfigError, WecomApiError) as exc:
        raise _translate_wecom_error(exc) from exc


@router.get("/current", response_model=WecomMenuStateOut)
async def get_current_wecom_menu(
    request: Request,
    tenant_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    try:
        tenant = await resolve_wecom_tenant_config(db, request=request, tenant_id=tenant_id)
        menu = await fetch_wecom_app_menu(tenant=tenant)
        return WecomMenuStateOut(
            agent_id=_resolve_agent_id(tenant),
            exists=bool(menu.get("button")),
            source="current",
            menu=menu,
            entries=flatten_wecom_menu_entries(menu),
        )
    except WecomApiError as exc:
        if exc.errcode in _NO_MENU_ERRCODES:
            return WecomMenuStateOut(
                agent_id=_resolve_agent_id(tenant),
                exists=False,
                source="current",
                menu={"button": []},
                entries=[],
            )
        raise _translate_wecom_error(exc) from exc
    except WecomConfigError as exc:
        raise _translate_wecom_error(exc) from exc


@router.post("/default/publish", response_model=WecomMenuActionOut)
async def publish_default_wecom_menu(
    request: Request,
    tenant_id: str | None = Query(None),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        tenant = await resolve_wecom_tenant_config(db, request=request, tenant_id=tenant_id)
        menu = build_default_wecom_menu_payload(tenant)
        await publish_wecom_app_menu(menu, tenant=tenant)
    except (WecomConfigError, WecomApiError) as exc:
        raise _translate_wecom_error(exc) from exc

    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="企业微信",
        action_name="发布应用菜单",
        content=f"发布企业微信应用菜单：corp_id={tenant.corp_id}，共 {len(flatten_wecom_menu_entries(menu))} 个入口",
    )
    return WecomMenuActionOut(
        agent_id=_resolve_agent_id(tenant),
        action="published",
        menu=menu,
        entries=flatten_wecom_menu_entries(menu),
    )


@router.delete("/current", response_model=WecomMenuActionOut)
async def remove_current_wecom_menu(
    request: Request,
    tenant_id: str | None = Query(None),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        tenant = await resolve_wecom_tenant_config(db, request=request, tenant_id=tenant_id)
        await delete_wecom_app_menu(tenant=tenant)
    except WecomApiError as exc:
        if exc.errcode not in _NO_MENU_ERRCODES:
            raise _translate_wecom_error(exc) from exc
    except WecomConfigError as exc:
        raise _translate_wecom_error(exc) from exc

    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="企业微信",
        action_name="删除应用菜单",
        content=f"删除企业微信应用菜单：corp_id={tenant.corp_id}",
    )
    return WecomMenuActionOut(
        agent_id=_resolve_agent_id(tenant),
        action="deleted",
        menu={"button": []},
        entries=[],
    )
