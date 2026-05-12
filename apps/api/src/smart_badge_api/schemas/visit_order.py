from __future__ import annotations

from pydantic import BaseModel


class VisitOrderOut(BaseModel):
    id: str
    dzdh: str
    dzseg: str | None
    sjrq: str | None
    jgbm: str | None
    fzuer: str | None
    fzuer_long: str | None
    advxc: str | None
    advxc_long: str | None
    ksgw: str | None = None
    ksgw_long: str | None = None
    advyq: str | None
    kunr: str | None
    ninam: str | None
    kusex: str | None
    kusex_txt: str | None
    yydh: str | None
    yyuer: str | None
    kutyp_dq: str | None
    kutyp_dq_txt: str | None
    kut30_dq: str | None
    kut30_dq_txt: str | None
    kusta_dq: str | None
    kusta_dq_txt: str | None
    kulvl_dq: str | None
    vipkf: str | None
    d_fzuer: str | None
    d_vipkf: str | None
    fzdh: str | None
    fzsj: str | None
    fzsta: str | None
    fzsta_txt: str | None
    ddsc: str | None
    bhkx: str | None
    assxc: str | None
    jgks: str | None
    jgks_txt: str | None
    dztyp: str | None
    dztyp_txt: str | None
    dzsta: str | None
    dzsta_txt: str | None
    dzly: str | None
    dymd: str | None
    jcsta: str | None
    jcsta_txt: str | None
    kusrc: str | None
    kusrc2: str | None
    remark_dz: str | None
    bjzx: str | None
    dymd_txt: str | None
    dzly_txt: str | None
    crtdt: str | None
    crttm: str | None

    model_config = {"from_attributes": True}


class VisitOrderSyncResult(BaseModel):
    synced_count: int
    new_count: int
    updated_count: int
    date_range: str
