from __future__ import annotations

import logging
from json import JSONDecodeError
from secrets import compare_digest

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.deps import get_db
from smart_badge_api.core.config import get_settings
from smart_badge_api.schemas.visit_order_push import SapHanaVisitOrderPushAck, SapHanaVisitOrderPushIn
from smart_badge_api.task_queue import dispatch_visit_order_push_materialization
from smart_badge_api.visit_order_push_service import upsert_sap_hana_visit_orders

router = APIRouter(prefix="/visit-orders", tags=["visit-orders-push"])
logger = logging.getLogger("smart_badge.visit_order_push")
_payload_adapter = TypeAdapter(SapHanaVisitOrderPushIn | list[SapHanaVisitOrderPushIn])


def _clean_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def require_sap_hana_push_api_key(x_api_key: str | None) -> None:
    expected = get_settings().sap_hana_push_api_key.strip()
    if not expected:
        raise ValueError("SAP HANA push API key is not configured")
    received = (x_api_key or "").strip()
    if not received or not compare_digest(received, expected):
        raise PermissionError("invalid X-API-Key")


def _ack_response(message: str, *, state: str, status_code: int) -> JSONResponse:
    payload = SapHanaVisitOrderPushAck(state=state, msg=message)
    return JSONResponse(status_code=status_code, content=payload.model_dump(by_alias=True))


def _format_validation_error(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()))
    detail = str(first.get("msg") or "invalid request payload")
    if location:
        return f"request validation failed: {location} {detail}"
    return f"request validation failed: {detail}"


@router.post(
    "/push",
    response_model=SapHanaVisitOrderPushAck,
    summary="SAP HANA visit-order push",
)
async def push_visit_orders_from_sap_hana(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    try:
        require_sap_hana_push_api_key(request.headers.get("X-API-Key"))
    except ValueError as exc:
        return _ack_response(str(exc), state="E", status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    except PermissionError as exc:
        return _ack_response(str(exc), state="E", status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        raw_payload = await request.json()
    except JSONDecodeError:
        return _ack_response("request body is not valid JSON", state="E", status_code=status.HTTP_400_BAD_REQUEST)

    try:
        payload = _payload_adapter.validate_python(raw_payload)
    except ValidationError as exc:
        return _ack_response(
            _format_validation_error(exc),
            state="E",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    items = payload if isinstance(payload, list) else [payload]

    try:
        result = await upsert_sap_hana_visit_orders(db, items)
    except ValueError as exc:
        return _ack_response(str(exc), state="E", status_code=status.HTTP_400_BAD_REQUEST)
    except Exception:
        logger.exception("unexpected error while handling SAP HANA visit order push")
        return _ack_response("internal server error", state="E", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    sync_keys = {
        (jgbm, dzdh)
        for item in items
        if (jgbm := _clean_text(item.jgbm)) and (dzdh := _clean_text(item.dzdh))
    }
    dispatched = False
    try:
        if sync_keys:
            await dispatch_visit_order_push_materialization(sync_keys)
            dispatched = True
    except Exception:
        logger.exception(
            "SAP HANA visit order push saved but async materialization dispatch failed; fallback sync will retry keys=%s",
            sorted(sync_keys),
        )

    message = (
        f"received={result.received_count}, "
        f"created={result.created_count}, updated={result.updated_count}; "
    )
    if dispatched:
        message += "Visitorders queued for async sync"
    else:
        message += "Visitorders sync not queued; fallback sync will retry"

    return _ack_response(message, state="S", status_code=status.HTTP_200_OK)
