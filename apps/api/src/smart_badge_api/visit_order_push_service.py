from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.db.models import SapHanaVisitOrder
from smart_badge_api.schemas.visit_order_push import SapHanaVisitOrderPushIn


@dataclass(slots=True)
class SapHanaVisitOrderPushResult:
    received_count: int = 0
    created_count: int = 0
    updated_count: int = 0


def _clean_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_sap_date_token(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 8:
        try:
            return datetime.strptime(digits, "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    if len(text) >= 10:
        try:
            return datetime.fromisoformat(text[:10]).date().isoformat()
        except ValueError:
            return None
    return None


def _serialize_payload(payload: SapHanaVisitOrderPushIn) -> dict[str, Any]:
    return payload.model_dump(by_alias=True)


def _build_sap_hana_visit_order_values(payload: SapHanaVisitOrderPushIn) -> dict[str, Any]:
    serialized = _serialize_payload(payload)
    dzdh = _clean_text(payload.dzdh)
    jgbm = _clean_text(payload.jgbm)
    if not dzdh:
        raise ValueError("DZDH 不能为空")
    if not jgbm:
        raise ValueError("JGBM 不能为空")

    return {
        "jgbm": jgbm,
        "dzdh": dzdh,
        "yydh": _clean_text(payload.yydh),
        "crtdt": _clean_text(payload.crtdt),
        "crttm": _clean_text(payload.crttm),
        "dzsta": _clean_text(payload.dzsta),
        "kunr": _clean_text(payload.kunr),
        "ninam": _clean_text(payload.ninam),
        "kusex": _clean_text(payload.kusex),
        "kulvl_dq": _clean_text(payload.kulvl_dq),
        "kutyp_dq": _clean_text(payload.kutyp_dq),
        "kut30_dq": _clean_text(payload.kut30_dq),
        "kusta_dq": _clean_text(payload.kusta_dq),
        "dzly": _clean_text(payload.dzly),
        "dymd": _clean_text(payload.dymd),
        "dztyp": _clean_text(payload.dztyp),
        "remark_dz": _clean_text(payload.remark_dz),
        "jgks": _clean_text(payload.jgks),
        "fzuer": _clean_text(payload.fzuer),
        "fzuer_long": _clean_text(payload.fzuer_long),
        "vipkf": _clean_text(payload.vipkf),
        "d_fzuer": _clean_text(payload.d_fzuer),
        "d_vipkf": _clean_text(payload.d_vipkf),
        "advyq": _clean_text(payload.advyq),
        "kusrc": _clean_text(payload.kusrc),
        "kusrc2": _clean_text(payload.kusrc2),
        "yyuer": _clean_text(payload.yyuer),
        "bjzx": _clean_text(payload.bjzx),
        "bhkx": _clean_text(payload.bhkx),
        "fzdata": serialized.get("FZDATA") or [],
        "source_payload": serialized,
        "customer_birthday": _normalize_sap_date_token(payload.kubsd),
        "last_received_at": datetime.now(timezone.utc),
    }


async def upsert_sap_hana_visit_orders(
    db: AsyncSession,
    payloads: list[SapHanaVisitOrderPushIn],
) -> SapHanaVisitOrderPushResult:
    if not payloads:
        raise ValueError("至少需要一条到诊分诊单数据")

    keys = sorted(
        {
            (_clean_text(item.jgbm), _clean_text(item.dzdh))
            for item in payloads
            if _clean_text(item.jgbm) and _clean_text(item.dzdh)
        }
    )
    if not keys:
        raise ValueError("至少需要一条有效的 JGBM 和 DZDH")

    existing_rows = (
        await db.execute(
            select(SapHanaVisitOrder).where(
                tuple_(SapHanaVisitOrder.jgbm, SapHanaVisitOrder.dzdh).in_(keys)
            )
        )
    ).scalars().all()
    existing_by_key = {(row.jgbm, row.dzdh): row for row in existing_rows}

    result = SapHanaVisitOrderPushResult(received_count=len(payloads))
    changed = False

    for payload in payloads:
        values = _build_sap_hana_visit_order_values(payload)
        key = (values["jgbm"], values["dzdh"])
        target = existing_by_key.get(key)

        if target is None:
            target = SapHanaVisitOrder(**values)
            db.add(target)
            existing_by_key[key] = target
            result.created_count += 1
            changed = True
            continue

        row_changed = False
        for field, value in values.items():
            if getattr(target, field) != value:
                setattr(target, field, value)
                row_changed = True
        if row_changed:
            result.updated_count += 1
            changed = True

    if changed:
        await db.commit()

    return result
