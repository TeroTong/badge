from __future__ import annotations

from pydantic import BaseModel, Field


class SapHanaVisitOrderTriageIn(BaseModel):
    fzdh: str | None = Field(None, alias="FZDH")
    advxc: str | None = Field(None, alias="ADVXC")
    advxc_long: str | None = Field(None, alias="ADVXC_LONG")
    assxc: str | None = Field(None, alias="ASSXC")
    fzsj: str | None = Field(None, alias="FZSJ")
    fzsta: str | None = Field(None, alias="FZSTA")
    ddsc: str | int | float | None = Field(None, alias="DDSC")
    jcsta: str | None = Field(None, alias="JCSTA")

    model_config = {
        "populate_by_name": True,
        "extra": "allow",
    }


class SapHanaVisitOrderPushIn(BaseModel):
    jgbm: str = Field(..., alias="JGBM")
    dzdh: str = Field(..., alias="DZDH")
    yydh: str | None = Field(None, alias="YYDH")
    crtdt: str | None = Field(None, alias="CRTDT")
    crttm: str | None = Field(None, alias="CRTTM")
    dzsta: str | None = Field(None, alias="DZSTA")
    kunr: str | None = Field(None, alias="KUNR")
    ninam: str | None = Field(None, alias="NINAM")
    kusex: str | None = Field(None, alias="KUSEX")
    kubsd: str | None = Field(None, alias="KUBSD")
    kulvl_dq: str | None = Field(None, alias="KULVL_DQ")
    kutyp_dq: str | None = Field(None, alias="KUTYP_DQ")
    kut30_dq: str | None = Field(None, alias="KUT30_DQ")
    kusta_dq: str | None = Field(None, alias="KUSTA_DQ")
    dzly: str | None = Field(None, alias="DZLY")
    dymd: str | None = Field(None, alias="DYMD")
    dymd_txt: str | None = Field(None, alias="DYMD_TXT")
    dztyp: str | None = Field(None, alias="DZTYP")
    remark_dz: str | None = Field(None, alias="REMARK_DZ")
    jgks: str | None = Field(None, alias="JGKS")
    fzuer: str | None = Field(None, alias="FZUER")
    fzuer_long: str | None = Field(None, alias="FZUER_LONG")
    vipkf: str | None = Field(None, alias="VIPKF")
    d_fzuer: str | None = Field(None, alias="D_FZUER")
    d_vipkf: str | None = Field(None, alias="D_VIPKF")
    advyq: str | None = Field(None, alias="ADVYQ")
    kusrc: str | None = Field(None, alias="KUSRC")
    kusrc2: str | None = Field(None, alias="KUSRC2")
    yyuer: str | None = Field(None, alias="YYUER")
    bjzx: str | None = Field(None, alias="BJZX")
    bhkx: str | None = Field(None, alias="BHKX")
    fzdata: list[SapHanaVisitOrderTriageIn] = Field(default_factory=list, alias="FZDATA")

    model_config = {
        "populate_by_name": True,
        "extra": "allow",
        "json_schema_extra": {
            "examples": [
                {
                    "JGBM": "6101",
                    "DZDH": "DZ2026041501",
                    "YYDH": "YY2026041501",
                    "CRTDT": "20260415",
                    "CRTTM": "093015",
                    "DZSTA": "C",
                    "KUNR": "70001234",
                    "NINAM": "李女士",
                    "KUSEX": "F",
                    "KULVL_DQ": "V1",
                    "KUTYP_DQ": "V",
                    "KUT30_DQ": "V",
                    "KUSTA_DQ": "V1",
                    "DZLY": "Y",
                    "DYMD": "A",
                    "DZTYP": "1",
                    "REMARK_DZ": "面部年轻化咨询",
                    "JGKS": "MRYX",
                    "FZUER": "81034062",
                    "FZUER_LONG": "杜娟",
                    "VIPKF": "82000001",
                    "D_FZUER": "81034062",
                    "D_VIPKF": "82000001",
                    "ADVYQ": "81030001",
                    "KUSRC": "D01",
                    "KUSRC2": "D0101",
                    "YYUER": "82010001",
                    "BJZX": "",
                    "BHKX": "",
                    "FZDATA": [
                        {
                            "FZDH": "FZ20260415001",
                            "ADVXC": "81034062",
                            "ADVXC_LONG": "杜娟",
                            "ASSXC": "81030088",
                            "FZSJ": "094500",
                            "FZSTA": "A",
                            "DDSC": "15",
                            "JCSTA": "N",
                        }
                    ],
                }
            ]
        },
    }


class SapHanaVisitOrderPushAck(BaseModel):
    state: str = Field(alias="STATE")
    msg: str = Field(alias="MSG")

    model_config = {
        "populate_by_name": True,
    }
