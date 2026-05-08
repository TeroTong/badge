from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class SapHanaVisitOrderListOut(BaseModel):
    id: str
    jgbm: str
    dzdh: str
    yydh: str | None = None
    crtdt: str | None = None
    crttm: str | None = None
    dzsta: str | None = None
    kunr: str | None = None
    ninam: str | None = None
    kusex: str | None = None
    kulvl_dq: str | None = None
    dzly: str | None = None
    dymd: str | None = None
    dztyp: str | None = None
    remark_dz: str | None = None
    jgks: str | None = None
    fzuer: str | None = None
    fzuer_long: str | None = None
    advyq: str | None = None
    yyuer: str | None = None
    bhkx: str | None = None
    fzdata_count: int = 0
    latest_fzdh: str | None = None
    latest_advxc: str | None = None
    latest_advxc_long: str | None = None
    latest_fzsj: str | None = None
    latest_fzsta: str | None = None
    latest_jcsta: str | None = None
    last_received_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class SapHanaVisitOrderDetailOut(SapHanaVisitOrderListOut):
    vipkf: str | None = None
    d_fzuer: str | None = None
    d_vipkf: str | None = None
    kusrc: str | None = None
    kusrc2: str | None = None
    bjzx: str | None = None
    fzdata: list[dict[str, Any]] = []
    source_payload: dict[str, Any] = {}
