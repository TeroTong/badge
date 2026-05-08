from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.wecom_sdk import WecomJsSdkConfigOut
from smart_badge_api.wecom import (
    WecomApiError,
    WecomConfigError,
    build_wecom_js_sdk_signature_for_url,
)
from smart_badge_api.wecom_tenants import resolve_wecom_tenant_config

router = APIRouter(prefix="/wecom/sdk", tags=["企业微信"])


def _translate_wecom_error(exc: WecomConfigError | WecomApiError) -> HTTPException:
    if isinstance(exc, WecomConfigError):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    return HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


@router.get("/config", response_model=WecomJsSdkConfigOut)
async def get_wecom_js_sdk_config(
    request: Request,
    url: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
):
    try:
        tenant = await resolve_wecom_tenant_config(db, request=request, url=url)
        signature = await build_wecom_js_sdk_signature_for_url(url, tenant=tenant)
        return WecomJsSdkConfigOut(
            corp_id=tenant.corp_id,
            agent_id=tenant.agent_id or None,
            timestamp=signature.timestamp,
            nonceStr=signature.nonceStr,
            signature=signature.signature,
        )
    except (WecomConfigError, WecomApiError) as exc:
        raise _translate_wecom_error(exc) from exc
