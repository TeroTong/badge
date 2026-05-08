from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlparse

from fastapi import Request
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import WecomTenant
from smart_badge_api.wecom import WecomConfigError, WecomTenantConfig, legacy_wecom_tenant_config


def normalize_wecom_host(raw: str | None) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = f"//{value}"
    parsed = urlparse(value)
    host = (parsed.hostname or "").strip().lower()
    return host or None


def validate_wecom_frontend_url(raw_frontend_url: str) -> str:
    frontend_url = raw_frontend_url.strip().rstrip("/")
    parsed = urlparse(frontend_url)
    host = (parsed.hostname or "").strip().lower()
    if not parsed.scheme or not host:
        raise WecomConfigError("企业微信入口地址未配置为合法 URL，请改成可被企业微信客户端访问的完整域名")
    if host in {"localhost"}:
        raise WecomConfigError("企业微信入口地址不能使用 localhost，请改成企业微信可访问的真实域名")
    try:
        host_ip = ip_address(host)
    except ValueError:
        return frontend_url
    if host_ip.is_loopback or host_ip.is_unspecified:
        raise WecomConfigError("企业微信入口地址不能使用 0.0.0.0/127.0.0.1 这类本地监听地址")
    raise WecomConfigError("企业微信入口地址不能使用裸 IP，请改成企业微信可信域名")


def _tenant_to_config(row: WecomTenant) -> WecomTenantConfig:
    corp_id = str(row.corp_id or "").strip()
    agent_id = str(row.agent_id or "").strip()
    agent_secret = str(row.agent_secret or "").strip()
    frontend_url = str(row.frontend_url or "").strip().rstrip("/")
    if not (corp_id and agent_id and agent_secret and frontend_url):
        raise WecomConfigError("企业微信主体配置不完整，请先补充 CorpID、AgentID、Secret 和入口 URL")
    return WecomTenantConfig(
        id=row.id,
        name=row.name,
        corp_id=corp_id,
        agent_id=agent_id,
        agent_secret=agent_secret,
        frontend_url=frontend_url,
        host=row.host,
        is_default=bool(row.is_default),
        api_base_url=get_settings().wecom_api_base_url,
        oauth_base_url=get_settings().wecom_oauth_base_url,
    )


def _configured_tenant_conditions(*, require_host: bool = False) -> list:
    conditions = [
        WecomTenant.is_active.is_(True),
        WecomTenant.corp_id.is_not(None),
        WecomTenant.corp_id != "",
        WecomTenant.agent_id.is_not(None),
        WecomTenant.agent_id != "",
        WecomTenant.agent_secret.is_not(None),
        WecomTenant.agent_secret != "",
        WecomTenant.frontend_url.is_not(None),
        WecomTenant.frontend_url != "",
    ]
    if require_host:
        conditions.extend([WecomTenant.host.is_not(None), WecomTenant.host != ""])
    return conditions


def _request_host_candidates(request: Request | None) -> list[str]:
    if request is None:
        return []
    values: list[str] = []
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_host:
        values.extend(part.strip() for part in forwarded_host.split(","))
    values.extend(
        [
            request.headers.get("host", ""),
            request.headers.get("origin", ""),
            request.headers.get("referer", ""),
            str(request.url),
        ]
    )
    hosts: list[str] = []
    seen: set[str] = set()
    for value in values:
        host = normalize_wecom_host(value)
        if host and host not in seen:
            hosts.append(host)
            seen.add(host)
    return hosts


def _url_host_candidates(url: str | None) -> list[str]:
    host = normalize_wecom_host(url)
    return [host] if host else []


def _legacy_matches_hosts(hosts: list[str]) -> bool:
    legacy = legacy_wecom_tenant_config()
    if legacy is None:
        return False
    legacy_host = normalize_wecom_host(legacy.frontend_url)
    return not hosts or (legacy_host is not None and legacy_host in set(hosts))


async def resolve_wecom_tenant_config(
    db: AsyncSession,
    *,
    request: Request | None = None,
    url: str | None = None,
    tenant_id: str | None = None,
) -> WecomTenantConfig:
    normalized_tenant_id = str(tenant_id or "").strip()
    if normalized_tenant_id:
        row = (
            await db.execute(
                select(WecomTenant)
                .where(
                    *_configured_tenant_conditions(),
                    or_(
                        WecomTenant.id == normalized_tenant_id,
                        WecomTenant.corp_id == normalized_tenant_id,
                    ),
                )
                .order_by(WecomTenant.is_default.desc(), WecomTenant.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            return _tenant_to_config(row)
        raise WecomConfigError("未找到可用的企业微信主体配置")

    hosts: list[str] = []
    seen: set[str] = set()
    for host in [*_url_host_candidates(url), *_request_host_candidates(request)]:
        if host and host not in seen:
            hosts.append(host)
            seen.add(host)

    if hosts:
        row = (
            await db.execute(
                select(WecomTenant)
                .where(
                    *_configured_tenant_conditions(require_host=True),
                    WecomTenant.host.in_(hosts),
                )
                .order_by(WecomTenant.is_default.desc(), WecomTenant.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            return _tenant_to_config(row)

    if _legacy_matches_hosts(hosts):
        legacy = legacy_wecom_tenant_config()
        if legacy is not None:
            return legacy

    if not hosts:
        row = (
            await db.execute(
                select(WecomTenant)
                .where(and_(*_configured_tenant_conditions()))
                .order_by(WecomTenant.is_default.desc(), WecomTenant.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            return _tenant_to_config(row)

    raise WecomConfigError("当前访问域名未绑定企业微信主体，请先配置该域名对应的企微应用")
