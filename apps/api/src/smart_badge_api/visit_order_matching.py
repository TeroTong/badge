from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date as _date_type, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from pypinyin import Style, lazy_pinyin
except Exception:  # pragma: no cover - optional dependency
    Style = None
    lazy_pinyin = None

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.api.data_scope import visit_order_scope_condition, visit_scope_condition
from smart_badge_api.analysis.llm_client import chat_completion, parse_json_response
from smart_badge_api.core.permissions import PermissionScope
from smart_badge_api.customer_type import customer_type_from_visit_order
from smart_badge_api.db.models import PositionProfile, Recording, RecordingVisitLink, Staff, Visit, VisitOrder, WecomTenant
from smart_badge_api.schemas.matching import (
    MatchEvidenceOut,
    RecordingMatchCandidateOut,
    RecordingVisitOrderMatchOut,
    VisitOrderMatchCandidateOut,
    VisitOrderMatchLineItemOut,
    VisitOrderRecordingMatchOut,
)
from smart_badge_api.visit_order_sync import (
    VALIDATED_DIR,
    _build_visit_notes,
    _discover_payload_metadata,
    _infer_recording_file_name,
    _parse_clock_to_seconds,
    fetch_latest_remote_visit_order_date,
    sync_visit_orders_for_context,
)
from smart_badge_api.visit_linking import ordered_recording_visit_links, ordered_visit_recording_links, sync_recording_visit_links

_MAX_TRANSCRIPT_CHARS = 1800
_MAX_LLM_CANDIDATES = 8
_SHORTLIST_LIMIT = 6
_SHORTLIST_TIME_WINDOW_MINUTES = 240
_SHORTLIST_NEAR_TIME_RESERVE = 3
_LLM_AUTO_THRESHOLD = 0.985
_LLM_AUTO_MARGIN = 0.18
_LLM_SUGGEST_THRESHOLD = 0.58
_RECORDING_ORDER_TIME_WEIGHT = 1.20
_ADVISOR_TRIAGE_TIME_WEIGHT = 1.40
_DOCTOR_CONSULT_TIME_WEIGHT = 1.40
_LOCAL_RECORDING_TZ = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger("smart_badge.visit_order_matching")

_DEPARTMENT_ASSISTANT_HOSPITAL_CODES = {"6501"}
_DEPARTMENT_ASSISTANT_KEYWORDS = ("科室助理", "科助", "department_assistant", "dept_assistant")
_DEPARTMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "JGKS01": ("口腔科", "口腔"),
    "JGKS02": ("皮肤科", "皮肤"),
    "JGKS03": ("外科",),
    "JGKS04": ("微整科", "微整", "微整形", "注射"),
    "JGKS05": ("中医",),
    "JGKS06": ("纹绣",),
    "JGKS07": ("会籍",),
    "JGKS08": ("毛发移植科", "毛发", "植发"),
    "JGKS09": ("非手术",),
    "JGKS10": ("私密中心", "私密"),
    "JGKS11": ("纤体中心", "纤体"),
    "JGKS12": ("植发中心", "植发"),
    "JGKS13": ("形体私密中心", "形体私密"),
    "JGKS14": ("SPA中心", "SPA", "spa"),
}


def _department_assistant_staff_keys(staff: Staff | None) -> set[str]:
    if staff is None:
        return set()
    return {
        value
        for value in (
            _clean_text(getattr(staff, "id", None)),
            _clean_text(getattr(staff, "external_account", None)),
            _clean_text(getattr(staff, "wecom_user_id", None)),
        )
        if value
    }


def _department_assistant_config_text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    return _clean_text(str(value))


def _department_assistant_codes_from_config(config: dict[str, Any] | None, staff: Staff | None) -> list[str]:
    if not isinstance(config, dict) or config.get("enabled") is False:
        return []
    staff_keys = _department_assistant_staff_keys(staff)
    if not staff_keys:
        return []

    result: list[str] = []
    for item in config.get("departments") or []:
        if not isinstance(item, dict):
            continue
        raw_staff_ids = item.get("assistant_staff_ids") or item.get("staff_ids") or item.get("assistant_ids") or []
        if not isinstance(raw_staff_ids, list):
            continue
        configured_staff_ids = {
            text
            for value in raw_staff_ids
            if (text := _department_assistant_config_text(value))
        }
        if not configured_staff_ids.intersection(staff_keys):
            continue
        code = _clean_text(item.get("department_code"))
        if code and code in _DEPARTMENT_ALIASES and code not in result:
            result.append(code)
    return result


def _compute_age(birthday: str | None, ref_date: str | None = None) -> int | None:
    """从出生日期计算年龄。支持 YYYY-MM-DD 和 YYYYMMDD 格式。"""
    if not birthday:
        return None
    try:
        bd = _date_type.fromisoformat(birthday[:10])
    except (ValueError, IndexError):
        return None
    if ref_date:
        rd = ref_date.strip()
        try:
            if len(rd) == 8 and rd.isdigit():
                today = _date_type(int(rd[:4]), int(rd[4:6]), int(rd[6:8]))
            else:
                today = _date_type.fromisoformat(rd)
        except (ValueError, IndexError):
            today = _date_type.today()
    else:
        today = _date_type.today()
    age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
    return age if 0 <= age <= 150 else None


@dataclass
class _OrderCandidate:
    visit_order: VisitOrder
    local_visit: Visit | None
    companion_local_visit_ids: list[str] = field(default_factory=list)
    companion_visit_order_refs: list[str] = field(default_factory=list)
    companion_customer_codes: list[str] = field(default_factory=list)
    confidence: float = 0.0
    decision: str = "ignore"
    method: str = "heuristic"
    reasons: list[str] = field(default_factory=list)
    excluded_reasons: list[str] = field(default_factory=list)
    identity_conflicts: list[str] = field(default_factory=list)
    manual_review_required: bool = False
    manual_review_reason: str | None = None
    evidence: list[MatchEvidenceOut] = field(default_factory=list)
    heuristic_score: float = 0.0
    hard_excluded: bool = False


@dataclass
class _RecordingCandidate:
    recording: Recording
    payload_meta: dict[str, str | int | None] | None
    confidence: float = 0.0
    decision: str = "ignore"
    method: str = "heuristic"
    reasons: list[str] = field(default_factory=list)
    excluded_reasons: list[str] = field(default_factory=list)
    identity_conflicts: list[str] = field(default_factory=list)
    manual_review_required: bool = False
    manual_review_reason: str | None = None
    evidence: list[MatchEvidenceOut] = field(default_factory=list)
    heuristic_score: float = 0.0
    hard_excluded: bool = False


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _norm(value: str | None) -> str:
    return "".join(str(value or "").strip().lower().split())


def _clean_text(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _department_label(code: str | None) -> str | None:
    normalized = _clean_text(code)
    if not normalized:
        return None
    aliases = _DEPARTMENT_ALIASES.get(normalized)
    if aliases:
        return aliases[0]
    return normalized


async def _load_staff_position_text(db: AsyncSession, staff: Staff | None) -> str | None:
    if staff is None:
        return None

    fragments = [
        _clean_text(staff.role),
    ]
    if staff.position_id:
        position = await db.get(PositionProfile, staff.position_id)
        if position is not None:
            fragments.extend([_clean_text(position.name), _clean_text(position.note)])

    hospital_code = _clean_text(staff.hospital_code)
    if hospital_code:
        config = (
            await db.execute(
                select(WecomTenant.department_assistant_match_config)
                .where(
                    WecomTenant.default_hospital_code == hospital_code,
                    WecomTenant.is_active.is_(True),
                )
                .order_by(WecomTenant.is_default.desc(), WecomTenant.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        department_codes = _department_assistant_codes_from_config(config, staff)
        if department_codes:
            department_labels = [_department_label(code) or code for code in department_codes]
            fragments.append(f"科室助理 {' '.join(department_codes)} {' '.join(department_labels)}")

    text = " ".join(fragment for fragment in fragments if fragment)
    return text or None


def _is_department_assistant_staff(staff: Staff | None, staff_position_text: str | None = None) -> bool:
    if staff is None:
        return False
    hospital_code = _clean_text(staff.hospital_code)
    if hospital_code not in _DEPARTMENT_ASSISTANT_HOSPITAL_CODES:
        return False

    raw_text = " ".join(
        fragment
        for fragment in (
            _clean_text(staff.role),
            _clean_text(staff_position_text),
        )
        if fragment
    )
    normalized_text = _norm(raw_text)
    return any(_norm(keyword) in normalized_text for keyword in _DEPARTMENT_ASSISTANT_KEYWORDS)


def _department_codes_from_text(text: str | None) -> list[str]:
    normalized_text = _norm(text)
    if not normalized_text:
        return []

    result: list[str] = []
    for code, aliases in _DEPARTMENT_ALIASES.items():
        if _norm(code) in normalized_text or any(_norm(alias) in normalized_text for alias in aliases):
            result.append(code)
    return result


def _department_codes_from_order(order: VisitOrder) -> list[str]:
    result: list[str] = []
    direct_code = _clean_text(order.jgks)
    if direct_code and direct_code in _DEPARTMENT_ALIASES:
        result.append(direct_code)
    result.extend(_department_codes_from_text(" ".join(filter(None, [order.jgks, order.jgks_txt]))))
    return list(dict.fromkeys(result))


def _department_assistant_order_match(
    staff: Staff | None,
    staff_position_text: str | None,
    order: VisitOrder,
) -> bool:
    if not _is_department_assistant_staff(staff, staff_position_text):
        return False

    staff_hospital_code = _clean_text(staff.hospital_code if staff else None)
    order_hospital_code = _clean_text(order.jgbm)
    if staff_hospital_code and order_hospital_code and staff_hospital_code != order_hospital_code:
        return False

    department_hints = _department_codes_from_text(staff_position_text)
    if not department_hints:
        return True

    order_departments = set(_department_codes_from_order(order))
    return bool(order_departments.intersection(department_hints))


def _department_assistant_order_signal(
    staff: Staff | None,
    staff_position_text: str | None,
    order: VisitOrder,
) -> tuple[float, MatchEvidenceOut | None, str | None]:
    if not _department_assistant_order_match(staff, staff_position_text, order):
        return 0.0, None, None

    department_hints = _department_codes_from_text(staff_position_text)
    order_department_text = _clean_text(order.jgks_txt) or _department_label(order.jgks) or "未标注"
    if department_hints:
        hint_labels = [_department_label(code) or code for code in department_hints]
        return (
            0.18,
            _make_evidence(
                "department_assistant",
                "科室助理科室匹配",
                f"员工岗位配置科室={','.join(hint_labels[:3])}，到诊单科室={order_department_text}",
                "high",
            ),
            "录音者为科室助理，且到诊单科室与岗位配置科室一致",
        )

    return (
        0.08,
        _make_evidence(
            "department_assistant",
            "科室助理同机构候选",
            f"录音者为长沙雅美科室助理，到诊单机构={_clean_text(order.jgbm) or '未标注'}，科室={order_department_text}",
            "low",
        ),
        "录音者为科室助理，按同机构同日到诊单纳入人工确认候选",
    )


def _department_assistant_visit_order_scope_condition(staff: Staff | None, staff_position_text: str | None):
    if not _is_department_assistant_staff(staff, staff_position_text):
        return None

    hospital_code = _clean_text(staff.hospital_code if staff else None)
    if not hospital_code:
        return None

    department_hints = _department_codes_from_text(staff_position_text)
    conditions = [VisitOrder.jgbm == hospital_code]
    if department_hints:
        department_conditions = [VisitOrder.jgks.in_(department_hints)]
        for code in department_hints:
            for alias in (code, *_DEPARTMENT_ALIASES.get(code, ())):
                cleaned_alias = _clean_text(alias)
                if cleaned_alias:
                    department_conditions.append(VisitOrder.jgks_txt.ilike(f"%{cleaned_alias}%"))
        conditions.append(or_(*department_conditions))

    return and_(*conditions)


def _recording_order_date_condition(record_date: str | None):
    normalized = _clean_text(record_date)
    if not normalized:
        return None
    return or_(VisitOrder.sjrq == normalized, VisitOrder.crtdt == normalized)


_MONTH_DAY_ARCHIVE_FILE_RE = re.compile(
    r"^(?P<month>\d{2})(?P<day>\d{2})_(?P<hms>\d{6})(?P<index>_\d+)?(?P<ext>\.[A-Za-z0-9]+)?$"
)


def _legacy_day_only_variant(file_name: str | None) -> str | None:
    text = str(file_name or "").strip()
    if not text:
        return None
    matched = _MONTH_DAY_ARCHIVE_FILE_RE.fullmatch(text)
    if not matched:
        return None
    return (
        f"{matched.group('day')}_{matched.group('hms')}"
        f"{matched.group('index') or ''}"
        f"{matched.group('ext') or ''}"
    )


def _recording_file_lookup_keys(file_name: str | None) -> list[str]:
    raw = str(file_name or "").strip()
    if not raw:
        return []

    path = Path(raw)
    stem = path.stem.strip()
    suffix = path.suffix.strip().lower()
    variants = [raw]
    if stem:
        variants.append(stem)
        if not suffix:
            for ext in (".mp3", ".wav", ".m4a"):
                variants.append(f"{stem}{ext}")
    legacy_raw = _legacy_day_only_variant(raw)
    if legacy_raw:
        variants.append(legacy_raw)
    legacy_stem = _legacy_day_only_variant(stem)
    if legacy_stem:
        variants.append(legacy_stem)
        if not suffix:
            for ext in (".mp3", ".wav", ".m4a"):
                variants.append(f"{legacy_stem}{ext}")

    seen: set[str] = set()
    result: list[str] = []
    for value in variants:
        candidate = value.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        result.append(candidate)
    return result


def _lookup_by_file_name(mapping: dict[str, Any], file_name: str | None) -> Any:
    for key in _recording_file_lookup_keys(file_name):
        if key in mapping:
            return mapping[key]
    return None


def _transcript_text(recording: Recording) -> str | None:
    transcript = recording.transcript.full_text if recording.transcript else None
    for candidate in (transcript, recording.transcript_text):
        text = _clean_text(candidate)
        if text:
            return text
    return None


def _transcript_excerpt(text: str | None, max_chars: int = _MAX_TRANSCRIPT_CHARS) -> str | None:
    cleaned = _clean_text(text)
    if not cleaned:
        return None
    if len(cleaned) <= max_chars:
        return cleaned
    return f"{cleaned[:max_chars].rstrip()}..."


def _advisor_matches(advisor_code: str | None, order: VisitOrder) -> bool:
    if not advisor_code:
        return False
    return advisor_code in {order.fzuer, order.fzr_id_dq}


def _role_specific_staff_score(
    staff: Staff | None,
    advisor_code: str | None,
    order: VisitOrder,
) -> tuple[float, MatchEvidenceOut | None]:
    if staff is None:
        return 0.0, None

    staff_code = _clean_text(staff.external_account) or advisor_code
    staff_name = _clean_text(staff.name)
    if not staff_code and not staff_name:
        return 0.0, None

    candidates: list[tuple[float, str, str, set[str]]] = []
    if staff.is_doctor:
        candidates.append((0.14, "医生", "预约医生", {order.yyuer or ""}))
    if staff.is_doctor_assistant:
        candidates.append((0.10, "医助", "预约医生链路", {order.yyuer or ""}))
    if staff.is_onsite_advisor:
        candidates.append((0.12, "现场顾问", "现场顾问/接诊顾问", {order.advxc or "", order.fzuer or "", order.fzr_id_dq or ""}))
    if staff.is_pre_advisor:
        candidates.append((0.10, "院前顾问", "院前顾问", {order.advyq or ""}))
    if staff.is_advisor_assistant:
        candidates.append((0.10, "顾问助理", "顾问助理", {order.assxc or ""}))
    if staff.is_guide:
        candidates.append((0.08, "导诊/分诊", "分诊人员", {order.fzrid or ""}))

    for score, role_label, target_label, raw_codes in candidates:
        codes = {_clean_text(value) or "" for value in raw_codes if _clean_text(value)}
        if staff_code and staff_code in codes:
            return score, _make_evidence(
                "role_code",
                "角色编码匹配",
                f"{role_label}编码={staff_code} 与到诊单{target_label}一致",
                "high" if score >= 0.12 else "medium",
            )

    if staff.is_pre_advisor and staff_name and staff_name == order.advyq_name:
        return 0.05, _make_evidence("role_name", "角色姓名匹配", f"院前顾问姓名={staff_name} 与到诊单一致", "low")
    if staff.is_onsite_advisor and staff_name and staff_name == order.advxc_long:
        return 0.05, _make_evidence("role_name", "角色姓名匹配", f"现场顾问姓名={staff_name} 与到诊单一致", "low")

    return 0.0, None


def _role_specific_staff_mismatch_score(
    staff: Staff | None,
    advisor_code: str | None,
    order: VisitOrder,
) -> tuple[float, MatchEvidenceOut | None, str | None]:
    if staff is None:
        return 0.0, None, None

    staff_code = _clean_text(staff.external_account) or advisor_code
    if not staff_code:
        return 0.0, None, None

    candidates: list[tuple[float, str, str, set[str]]] = []
    if staff.is_doctor:
        candidates.append((-0.10, "医生", "预约医生", {order.yyuer or ""}))
    if staff.is_doctor_assistant:
        candidates.append((-0.07, "医助", "预约医生链路", {order.yyuer or ""}))
    if staff.is_onsite_advisor:
        candidates.append((-0.12, "现场顾问", "现场顾问/接诊顾问", {order.advxc or "", order.fzuer or "", order.fzr_id_dq or ""}))
    if staff.is_pre_advisor:
        candidates.append((-0.10, "院前顾问", "院前顾问", {order.advyq or ""}))
    if staff.is_advisor_assistant:
        candidates.append((-0.08, "顾问助理", "顾问助理", {order.assxc or ""}))
    if staff.is_guide:
        candidates.append((-0.06, "导诊/分诊", "分诊人员", {order.fzrid or ""}))

    for penalty, role_label, target_label, raw_codes in candidates:
        codes = sorted({_clean_text(value) or "" for value in raw_codes if _clean_text(value)})
        if not codes or staff_code in codes:
            continue
        codes_text = " / ".join(codes[:3])
        return (
            penalty,
            _make_evidence(
                "role_code_mismatch",
                "角色编码不一致",
                f"{role_label}编码={staff_code}，但到诊单{target_label}为 {codes_text}",
                "medium" if abs(penalty) >= 0.08 else "low",
            ),
            f"录音员工编码与到诊单{target_label}不一致（录音员工={staff_code}，到诊单={codes_text}）",
        )

    return 0.0, None, None


def _name_in_text(name: str | None, text: str | None) -> bool:
    """Check if a person's name (≥2 chars) appears in text."""
    n = _norm(name)
    if not n or len(n) < 2:
        return False
    t = _norm(text)
    return bool(t and n in t)


_COMPOUND_SURNAMES = {
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫", "万俟", "闻人",
    "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台", "皇甫", "宗政", "濮阳", "公冶",
    "太叔", "申屠", "公孙", "慕容", "仲孙", "钟离", "长孙", "宇文", "司徒", "鲜于",
    "司空", "闾丘", "子车", "亓官", "司寇", "巫马", "公西", "颛孙", "壤驷", "公良",
    "漆雕", "乐正", "宰父", "谷梁", "拓跋", "夹谷", "轩辕", "令狐", "段干", "百里",
    "呼延", "东郭", "南门", "羊舌", "微生", "公户", "公玉", "公仪", "梁丘", "公仲",
    "公上", "公门", "公山", "公坚", "左丘", "公伯", "西门", "公祖", "第五", "公乘",
    "贯丘", "公皙", "南荣", "东里", "东宫", "仲长", "子书", "子桑", "即墨", "达奚",
    "褚师",
}

_FEMALE_HONORIFICS = ("女士", "小姐", "美女", "姐姐", "姐")
_MALE_HONORIFICS = ("先生", "帅哥", "哥哥", "哥", "总")
_GENERIC_HONORIFICS = _FEMALE_HONORIFICS + _MALE_HONORIFICS
_FEMALE_GENDER_MARKERS = ("女生", "女性", "女孩子", "小姐姐")
_MALE_GENDER_MARKERS = ("男生", "男性", "男孩子", "小哥哥")
_REFERENCE_CONTEXT_MARKERS = (
    "已经面诊",
    "已经面过",
    "已经看过",
    "已经做过",
    "已经来过",
    "已经咨询",
    "已经沟通过",
    "那个妹妹",
    "另一个",
    "另外一个",
    "上一位",
    "前一个",
    "不是这个",
    "不是她",
    "不是他",
)
_ADDRESS_NAME_BLACKLIST = {
    "客户", "顾客", "医生", "老师", "顾问", "主任", "院长", "工作人员", "美女", "帅哥",
}
_ADDRESS_NAME_PRONOUN_TOKENS = {
    "你", "您", "他", "她", "它", "我", "咱", "俺", "其", "该", "这", "那", "哪", "谁",
    "你的", "您的", "他的", "她的", "我的", "咱的", "这个", "那个",
}
_ADDRESS_NAME_NON_NAME_MARKERS = (
    "问",
    "找",
    "联系",
    "都是",
    "都问",
    "今天",
    "这边",
    "那边",
)
_ADDRESS_LEADING_POLITE_PREFIXES = ("请问", "您好", "你好", "这位", "是")
_ADDRESS_LEADING_REFERENCE_PREFIXES = ("联系", "转给", "发给", "问下", "找下", "问", "找", "加", "跟", "和")
_TITLE_EXTENSION_SUFFIX_CHARS = {"院", "店", "部", "办", "处", "室", "所", "校", "司", "经", "监", "裁", "助", "秘"}
_REFERENCE_PREFIX_MARKERS = (
    "都是问",
    "都问",
    "先问",
    "去问",
    "想问",
    "问下",
    "找下",
    "联系",
    "转给",
    "发给",
    "问",
    "找",
    "加",
    "跟",
    "和",
)
_DIRECT_ADDRESS_PREFIX_EXCEPTIONS = ("请问", "敢问")
_COMMON_SINGLE_CHAR_SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚"
    "程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓"
    "牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘斜厉戎祖武符刘景詹束龙"
    "叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索籍赖卓蔺屠蒙池乔阴郁胥能苍双闻"
    "莘党翟谭贡劳逄姬申扶堵冉宰郦雍璩桑桂濮牛寿通边扈燕冀僪浦尚农温别"
    "庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国"
    "文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾沙"
    "乜养鞠须丰巢关蒯相查后荆红游竺权逑盖益桓公"
)


def _customer_surname(name: str | None) -> str | None:
    clean_name = _clean_text(name)
    if not clean_name or len(clean_name) < 2:
        return None
    first_two = clean_name[:2]
    if first_two in _COMPOUND_SURNAMES and len(clean_name) >= 3:
        return first_two
    return clean_name[:1]


def _looks_like_addressed_name(name: str | None) -> bool:
    clean_name = _clean_text(name)
    if not clean_name:
        return False
    if clean_name in _ADDRESS_NAME_BLACKLIST or clean_name in _ADDRESS_NAME_PRONOUN_TOKENS:
        return False
    if any(token in clean_name for token in _ADDRESS_NAME_PRONOUN_TOKENS):
        return False
    if any(marker in clean_name for marker in _ADDRESS_NAME_NON_NAME_MARKERS):
        return False
    if any(marker in clean_name for marker in ("觉得", "一下", "可以", "让", "请", "就是", "然后", "那个", "这个")):
        return False
    if clean_name.endswith("的") or clean_name.startswith(("的", "跟", "把", "给")):
        return False

    if len(clean_name) == 1:
        return clean_name in _COMMON_SINGLE_CHAR_SURNAMES

    first_two = clean_name[:2]
    if first_two in _COMPOUND_SURNAMES:
        return len(clean_name) in {2, 3, 4}

    return clean_name[:1] in _COMMON_SINGLE_CHAR_SURNAMES


def _surname_sound_key(surname: str | None) -> str | None:
    clean_surname = _clean_text(surname)
    if not clean_surname:
        return None
    if lazy_pinyin is None or Style is None:
        return None
    try:
        pronunciation = "".join(lazy_pinyin(clean_surname, style=Style.NORMAL)).lower().strip()
    except Exception:
        return None
    if not pronunciation:
        return None
    return re.sub(r"(ng|n|r)$", "", pronunciation)


def _surnames_sound_similar(left: str | None, right: str | None) -> bool:
    clean_left = _clean_text(left)
    clean_right = _clean_text(right)
    if not clean_left or not clean_right:
        return False
    if clean_left == clean_right:
        return True
    left_key = _surname_sound_key(clean_left)
    right_key = _surname_sound_key(clean_right)
    return bool(left_key and right_key and left_key == right_key)


def _extract_consultant_self_identification_signals(transcript_text: str | None) -> list[dict[str, str]]:
    text = _clean_text(transcript_text)
    if not text:
        return []

    signals: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in re.finditer(r"(?:我姓|您叫我|你叫我)([\u4e00-\u9fff])", text):
        surname = match.group(1)
        key = ("surname", surname)
        if key in seen:
            continue
        seen.add(key)
        signals.append({"type": "surname", "value": surname})

    for match in re.finditer(r"(?:我是|我叫)([\u4e00-\u9fff]{2,4})(?:老师|顾问|医生)?", text):
        full_name = match.group(1)
        if full_name in _ADDRESS_NAME_BLACKLIST:
            continue
        key = ("full_name", full_name)
        if key in seen:
            continue
        seen.add(key)
        signals.append({"type": "full_name", "value": full_name})

    return signals[:6]


def _staff_self_identification_signal(
    staff: Staff | None,
    transcript_text: str | None,
) -> tuple[float, MatchEvidenceOut | None, str | None]:
    if staff is None:
        return 0.0, None, None

    staff_name = _clean_text(staff.name)
    staff_surname = _customer_surname(staff_name)
    if not staff_name or not staff_surname:
        return 0.0, None, None

    for signal in _extract_consultant_self_identification_signals(transcript_text):
        if signal["type"] == "full_name" and signal["value"] == staff_name:
            return (
                0.06,
                _make_evidence("staff_identity", "咨询师自报姓名匹配", f"录音中出现“我是/我叫{staff_name}”类自我介绍", "medium"),
                f"录音中咨询师自报姓名，与录音员工「{staff_name}」一致",
            )
        if signal["type"] == "surname" and signal["value"] == staff_surname:
            return (
                0.04,
                _make_evidence("staff_identity", "咨询师自报姓氏匹配", f"录音中出现“我姓{staff_surname}”类自我介绍", "medium"),
                f"录音中咨询师自报姓氏，与录音员工「{staff_name}」一致",
            )

    return 0.0, None, None


def _extract_addressed_identity_signals(transcript_text: str | None) -> list[dict[str, Any]]:
    text = _clean_text(transcript_text)
    if not text:
        return []

    by_token: dict[str, dict[str, Any]] = {}
    for match in re.finditer(r"([\u4e00-\u9fff]{1,4}?)(女士|先生|小姐|老师|总)", text):
        name = match.group(1)
        honorific = match.group(2)
        removed_prefix = ""
        for prefix in (*_ADDRESS_LEADING_REFERENCE_PREFIXES, *_ADDRESS_LEADING_POLITE_PREFIXES):
            if name.startswith(prefix) and len(name) > len(prefix):
                name = name[len(prefix):]
                removed_prefix = prefix
                break
        if honorific in {"总", "老师"} and removed_prefix in _ADDRESS_LEADING_REFERENCE_PREFIXES:
            continue
        if not _looks_like_addressed_name(name):
            continue
        if name.endswith(("顾问", "医生", "老师", "主任", "院长")):
            continue
        suffix = text[match.end(): match.end() + 1]
        if honorific == "总" and suffix and suffix in _TITLE_EXTENSION_SUFFIX_CHARS:
            continue
        prefix = _norm(text[max(0, match.start() - 4): match.start()])
        if honorific in {"总", "老师"} and any(prefix.endswith(marker) for marker in _REFERENCE_PREFIX_MARKERS):
            if not any(prefix.endswith(marker) for marker in _DIRECT_ADDRESS_PREFIX_EXCEPTIONS):
                continue
        token = f"{name}{honorific}"
        entry = by_token.setdefault(
            token,
            {
                "token": token,
                "name": name,
                "honorific": honorific,
                "surname": _customer_surname(name),
                "count": 0,
                "first_index": match.start(),
                "last_index": match.start(),
            },
        )
        entry["count"] += 1
        entry["last_index"] = match.start()

    return sorted(
        by_token.values(),
        key=lambda item: (int(item["count"]), int(item["last_index"])),
        reverse=True,
    )


def _address_signal_context(
    transcript_text: str | None,
    signal: dict[str, Any],
    *,
    before: int = 8,
    after: int = 40,
) -> tuple[str, str]:
    text = _clean_text(transcript_text) or ""
    start = max(int(signal.get("first_index") or 0) - before, 0)
    signal_start = max(int(signal.get("first_index") or 0), 0)
    signal_end = min(int(signal.get("last_index") or signal_start) + len(str(signal.get("token") or "")), len(text))
    end = min(signal_end + after, len(text))
    return text[start:signal_start], text[signal_end:end]


def _is_reference_only_identity_signal(signal: dict[str, Any], transcript_text: str | None) -> bool:
    if int(signal.get("count") or 0) >= 2:
        return False
    before_context, after_context = _address_signal_context(transcript_text, signal)
    merged_context = _norm(f"{before_context}{after_context}")
    if not merged_context:
        return False
    return any(_norm(marker) in merged_context for marker in _REFERENCE_CONTEXT_MARKERS)


def _meaningful_addressed_identity_signals(transcript_text: str | None) -> list[dict[str, Any]]:
    return [
        signal
        for signal in _extract_addressed_identity_signals(transcript_text)
        if not _is_reference_only_identity_signal(signal, transcript_text)
    ]


def _addressed_signal_matches_customer_name(signal: dict[str, Any], customer_name: str | None) -> bool:
    return _addressed_signal_match_level(signal, customer_name) in {"exact", "surname"}


def _addressed_signal_match_level(signal: dict[str, Any], customer_name: str | None) -> str:
    full_name = _clean_text(customer_name)
    if not full_name:
        return "none"
    signal_name = _clean_text(str(signal.get("name") or ""))
    if not signal_name:
        return "none"
    if signal_name == full_name:
        return "exact"
    signal_surname = _customer_surname(signal_name)
    customer_surname = _customer_surname(full_name)
    if signal_surname and customer_surname and signal_surname == customer_surname:
        return "surname"
    if _surnames_sound_similar(signal_surname, customer_surname):
        return "phonetic"
    return "none"


def _build_identity_conflicts_for_candidate(customer_name: str | None, transcript_text: str | None) -> list[str]:
    signals = _meaningful_addressed_identity_signals(transcript_text)
    if not signals:
        return []

    match_levels = {signal["token"]: _addressed_signal_match_level(signal, customer_name) for signal in signals}
    matching_signals = [signal for signal in signals if match_levels.get(signal["token"]) in {"exact", "surname"}]
    phonetic_signals = [signal for signal in signals if match_levels.get(signal["token"]) == "phonetic"]
    conflicting_signals = [signal for signal in signals if match_levels.get(signal["token"]) == "none"]
    strong_conflicting_signals = [signal for signal in conflicting_signals if int(signal.get("count") or 0) >= 2 or len(str(signal.get("name") or "")) >= 2]
    conflicts: list[str] = []

    if matching_signals and strong_conflicting_signals:
        conflicts.append(
            f"录音中同时出现匹配与冲突的客户称呼：{'、'.join(signal['token'] for signal in signals[:3])}"
        )
    elif matching_signals and phonetic_signals:
        conflicts.append(
            f"录音中还出现与候选客户姓氏同音或近音的称呼：{'、'.join(signal['token'] for signal in phonetic_signals[:3])}，可能包含 ASR 误识别"
        )
    elif strong_conflicting_signals and customer_name:
        conflicts.append(
            f"录音中出现的客户称呼与候选客户「{customer_name}」不一致：{'、'.join(signal['token'] for signal in strong_conflicting_signals[:3])}"
        )
    elif phonetic_signals and customer_name:
        conflicts.append(
            f"录音中出现与候选客户「{customer_name}」姓氏同音或近音的称呼：{'、'.join(signal['token'] for signal in phonetic_signals[:3])}，需防止 ASR 姓氏误识别"
        )

    surnames = {str(signal.get("surname") or "") for signal in signals if signal.get("surname")}
    sound_keys = {_surname_sound_key(surname) or surname for surname in surnames if surname}
    if len(surnames) >= 2 and len(sound_keys) >= 2:
        conflicts.append("录音中存在多个不同姓氏的客户称呼，真实客户身份需要人工确认")
    elif len(surnames) >= 2:
        conflicts.append("录音中存在多个同音或近音姓氏的客户称呼，可能混有 ASR 误识别，需人工确认")

    return _merge_unique_reasons(conflicts)


def _build_overall_identity_conflicts(transcript_text: str | None) -> list[str]:
    signals = _meaningful_addressed_identity_signals(transcript_text)
    if len(signals) < 2:
        return []

    surnames = {str(signal.get("surname") or "") for signal in signals if signal.get("surname")}
    conflicts: list[str] = []
    sound_keys = {_surname_sound_key(surname) or surname for surname in surnames if surname}
    if len(surnames) >= 2 and len(sound_keys) >= 2:
        conflicts.append(f"录音中出现多个客户称呼：{'、'.join(signal['token'] for signal in signals[:4])}")
        conflicts.append("不同称呼的姓氏不一致，需人工确认真实客户身份")
    elif len(surnames) >= 2:
        conflicts.append(f"录音中出现多个同音或近音客户称呼：{'、'.join(signal['token'] for signal in signals[:4])}")
        conflicts.append("这些称呼可能包含 ASR 同音姓氏误识别，需人工确认真实客户身份")
    return conflicts


def _manual_review_reason_from_conflicts(conflicts: list[str]) -> str | None:
    if not conflicts:
        return None
    return conflicts[0]


def _customer_name_match_signal(
    customer_name: str | None,
    transcript_text: str | None,
    customer_gender: str | None = None,
) -> tuple[float, MatchEvidenceOut | None, str | None]:
    full_name = _clean_text(customer_name)
    text = _clean_text(transcript_text)
    if not full_name or not text:
        return 0.0, None, None

    if _name_in_text(full_name, text):
        return (
            0.36,
            _make_evidence("customer_name", "客户姓名出现在录音", f"到诊单客户 {full_name} 在转写文本中被提及", "high"),
            f"录音转写中提及了客户姓名「{full_name}」",
        )

    surname = _customer_surname(full_name)
    if not surname:
        return 0.0, None, None

    honorifics = _GENERIC_HONORIFICS
    gender_norm = _norm(customer_gender)
    if "女" in gender_norm:
        honorifics = _FEMALE_HONORIFICS
    elif "男" in gender_norm:
        honorifics = _MALE_HONORIFICS

    normalized_text = _norm(text)
    for honorific in honorifics:
        token = _norm(f"{surname}{honorific}")
        if token and token in normalized_text:
            return (
                0.28,
                _make_evidence("customer_name", "客户称呼出现在录音", f"录音中出现称呼「{surname}{honorific}」", "high"),
                f"录音转写中提及了与客户姓名匹配的称呼「{surname}{honorific}」",
            )

    for signal in _extract_addressed_identity_signals(text):
        if _addressed_signal_match_level(signal, customer_name) != "phonetic":
            continue
        return (
            0.16,
            _make_evidence(
                "customer_name",
                "客户姓氏近音称呼出现在录音",
                f"录音中出现称呼「{signal['token']}」，与客户姓氏「{surname}」同音或近音，可能存在 ASR 误识别",
                "medium",
            ),
            f"录音转写中提及了与客户姓氏同音或近音的称呼「{signal['token']}」，需结合其他证据判断",
        )

    return 0.0, None, None


def _normalize_gender_label(value: Any) -> str | None:
    text = _clean_text(str(value or ""))
    if not text:
        return None
    normalized = _norm(text)
    if normalized in {"男", "m", "male"} or any(token in normalized for token in ("男生", "男性", "男士", "先生", "帅哥")):
        return "男"
    if normalized in {"女", "f", "female"} or any(token in normalized for token in ("女生", "女性", "女士", "小姐", "美女", "姐姐")):
        return "女"
    return None


def _extract_content_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, dict):
        for key in ("content", "summary", "value", "gender", "性别", "age", "年龄", "birthdate", "birthday", "出生日期"):
            if key in value:
                text = _extract_content_text(value.get(key))
                if text:
                    return text
        return None
    if isinstance(value, list):
        for item in value:
            text = _extract_content_text(item)
            if text:
                return text
        return None
    return _clean_text(str(value))


def _find_demographic_text(data: Any, target_keys: tuple[str, ...]) -> str | None:
    normalized_targets = {_norm(key) for key in target_keys}

    def _search(value: Any) -> str | None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if _norm(str(key)) in normalized_targets:
                    text = _extract_content_text(nested)
                    if text:
                        return text
                found = _search(nested)
                if found:
                    return found
            return None
        if isinstance(value, list):
            for item in value:
                found = _search(item)
                if found:
                    return found
        return None

    return _search(data)


def _payload_analysis_sections(payload_data: dict | None) -> list[dict[str, Any]]:
    if not isinstance(payload_data, dict):
        return []

    sections: list[dict[str, Any]] = [payload_data]
    for key in ("consultAnalyzeResult", "strategyAnalyzeResult", "requirementAnalyzeResult", "tagsAnalyzeResult"):
        raw_value = payload_data.get(key)
        if isinstance(raw_value, dict):
            sections.append(raw_value)
            continue
        parsed = _safe_json_parse(raw_value) if isinstance(raw_value, str) else None
        if parsed:
            sections.append(parsed)
    return sections


def _infer_customer_gender_from_transcript(transcript_text: str | None) -> dict[str, Any]:
    text = _clean_text(transcript_text)
    if not text:
        return {"gender": None, "strength": None, "signals": []}

    normalized_text = _norm(text)
    male_score = 0
    female_score = 0
    male_signals: list[str] = []
    female_signals: list[str] = []

    for token in _MALE_GENDER_MARKERS:
        count = normalized_text.count(_norm(token))
        if count <= 0:
            continue
        male_score += min(count, 3) * (2 if token in {"男生", "男性"} else 1)
        male_signals.append(token)

    for token in _FEMALE_GENDER_MARKERS:
        count = normalized_text.count(_norm(token))
        if count <= 0:
            continue
        female_score += min(count, 3) * (2 if token in {"女生", "女性"} else 1)
        female_signals.append(token)

    # Explicit gender markers are stronger than generic honorifics. Once we see
    # at least one explicit marker, allow same-direction honorifics to reinforce
    # the signal into a "strong" judgment. This keeps opening misaddress cases
    # from being over-classified while still recognizing phrases like "男生" + "帅哥".
    if male_score > 0:
        for token in ("帅哥", "先生", "男士"):
            count = normalized_text.count(_norm(token))
            if count <= 0:
                continue
            male_score += min(count, 2)
            male_signals.append(token)
    if female_score > 0:
        for token in ("美女", "女士", "小姐"):
            count = normalized_text.count(_norm(token))
            if count <= 0:
                continue
            female_score += min(count, 2)
            female_signals.append(token)

    for signal in _extract_addressed_identity_signals(text):
        if _is_reference_only_identity_signal(signal, text):
            continue
        if int(signal.get("count") or 0) < 2:
            continue
        token = str(signal.get("token") or "").strip()
        if not token:
            continue
        if any(token.endswith(honorific) for honorific in _MALE_HONORIFICS):
            male_score += 2
            male_signals.append(token)
        elif any(token.endswith(honorific) for honorific in _FEMALE_HONORIFICS):
            female_score += 2
            female_signals.append(token)

    if male_score >= female_score + 2 and male_score >= 2:
        return {
            "gender": "男",
            "strength": "strong" if male_score >= 3 else "weak",
            "signals": _dedupe_terms(male_signals)[:4],
        }
    if female_score >= male_score + 2 and female_score >= 2:
        return {
            "gender": "女",
            "strength": "strong" if female_score >= 3 else "weak",
            "signals": _dedupe_terms(female_signals)[:4],
        }
    return {"gender": None, "strength": None, "signals": []}


def _extract_payload_demographics(payload_data: dict | None, transcript_text: str | None = None) -> dict[str, Any]:
    gender: str | None = None
    age: str | None = None
    gender_source: str | None = None
    gender_strength: str | None = None
    gender_signals: list[str] = []

    for section in _payload_analysis_sections(payload_data):
        if not gender:
            gender_text = _find_demographic_text(section, ("gender", "性别"))
            normalized_gender = _normalize_gender_label(gender_text)
            if normalized_gender:
                gender = normalized_gender
                gender_source = "analysis"
                gender_strength = "structured"
                if gender_text:
                    gender_signals = [_clean_text(gender_text) or normalized_gender]
        if not age:
            age_text = _find_demographic_text(section, ("age", "年龄", "birthdate", "birthday", "出生日期"))
            if age_text:
                age = age_text
        if gender and age:
            break

    if not gender:
        inferred = _infer_customer_gender_from_transcript(transcript_text)
        inferred_gender = _normalize_gender_label(inferred.get("gender"))
        if inferred_gender:
            gender = inferred_gender
            gender_source = "transcript"
            gender_strength = str(inferred.get("strength") or "") or "weak"
            gender_signals = [str(item).strip() for item in inferred.get("signals") or [] if str(item).strip()]

    return {
        "gender": gender,
        "age": age,
        "gender_source": gender_source,
        "gender_strength": gender_strength,
        "gender_signals": gender_signals,
    }


def _parse_audio_timestamp(ts: str | None) -> int | None:
    """Parse audioStartTime/audioEndTime (YYMMDDHHmmss) to seconds-since-midnight."""
    if not ts or not isinstance(ts, str) or len(ts) < 12:
        return None
    try:
        hh, mm, ss = int(ts[6:8]), int(ts[8:10]), int(ts[10:12])
        return hh * 3600 + mm * 60 + ss
    except (ValueError, IndexError):
        return None


def _payload_duration_seconds(payload_data: dict | None) -> int | None:
    """Calculate recording duration in seconds from payload timestamps."""
    if not payload_data:
        return None
    start = _parse_audio_timestamp(str(payload_data.get("audioStartTime") or ""))
    end = _parse_audio_timestamp(str(payload_data.get("audioEndTime") or ""))
    if start is not None and end is not None and end > start:
        return end - start
    return None


def _recording_created_start_seconds(recording: Recording) -> int | None:
    created_at = recording.created_at
    if created_at is None:
        return None
    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(_LOCAL_RECORDING_TZ)
    return created_at.hour * 3600 + created_at.minute * 60 + created_at.second


def _recording_start_reference(
    recording: Recording,
    payload_meta: dict[str, str | int | None] | None,
    full_payload: dict | None = None,
) -> tuple[int | None, str]:
    if payload_meta:
        start_seconds = payload_meta.get("start_seconds")
        if isinstance(start_seconds, int):
            return start_seconds, "录音开始"
        if isinstance(start_seconds, str) and start_seconds.isdigit():
            return int(start_seconds), "录音开始"

    payload_seconds = _parse_audio_timestamp(str(full_payload.get("audioStartTime") or "")) if full_payload else None
    if payload_seconds is not None:
        return payload_seconds, "录音开始"

    created_seconds = _recording_created_start_seconds(recording)
    if created_seconds is not None:
        return created_seconds, "录音创建时间"
    return None, "录音开始"


# ---------------------------------------------------------------------------
# Full payload loading  — load raw payloads w/ analysis results
# ---------------------------------------------------------------------------

_full_payload_cache: dict[str, dict] | None = None


def _load_full_payloads() -> dict[str, dict]:
    """Return {file_name: raw_payload_dict} for every validated payload."""
    global _full_payload_cache
    if _full_payload_cache is not None:
        return _full_payload_cache

    root = VALIDATED_DIR.resolve()
    result: dict[str, dict] = {}
    if not root.exists():
        _full_payload_cache = result
        return result

    for payload_path in root.glob("*/payload.jsonl"):
        try:
            first_line = ""
            with payload_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped:
                        first_line = stripped
                        break
            if not first_line:
                continue
            data: dict = json.loads(first_line)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        file_name = _infer_recording_file_name(data, payload_path)
        if file_name:
            for key in _recording_file_lookup_keys(file_name):
                result.setdefault(key, data)
    _full_payload_cache = result
    return result


def _build_payload_metadata_map() -> dict[str, dict[str, str | int | None]]:
    payload_map: dict[str, dict[str, str | int | None]] = {}
    for item in _discover_payload_metadata():
        file_name = str(item.get("file_name") or "").strip()
        if not file_name:
            continue
        for key in _recording_file_lookup_keys(file_name):
            payload_map.setdefault(key, item)
    return payload_map


def _safe_json_parse(value: str | None) -> dict | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _extract_match_terms(value: str | None, *, max_length: int = 24) -> list[str]:
    text = _clean_text(value)
    if not text:
        return []

    # Prefer concise lexical units over long narrative sentences.
    chunks = re.split(r"[\n\r\t,，。；;：:、/|＋+（）()]+", text)
    terms: list[str] = []
    for chunk in chunks:
        normalized = _clean_text(chunk)
        if not normalized:
            continue
        if len(normalized) > max_length:
            sub_chunks = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,12}", normalized)
            terms.extend(sub_chunks)
            continue
        terms.append(normalized)
    return terms


def _parse_customer_age_hint(value: str | None) -> float | None:
    text = _clean_text(value)
    if not text:
        return None

    range_match = re.search(r"(\d{2})\s*[-~至]\s*(\d{2})", text)
    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        if 0 < start <= end <= 150:
            return (start + end) / 2

    plus_match = re.search(r"(\d{2})(?:多岁|岁多)", text)
    if plus_match:
        age = int(plus_match.group(1))
        if 0 < age <= 150:
            return float(age)

    numbers = [int(item) for item in re.findall(r"\d{2,3}", text)]
    for age in numbers:
        if 0 < age <= 150:
            return float(age)
    return None


def _extract_analysis_keywords(payload_data: dict | None) -> list[str]:
    values: list[str] = []
    for section in _payload_analysis_sections(payload_data):
        _append_term_values(values, section)
    return _dedupe_terms(values)[:40]


def _dedupe_terms(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if len(text) < 2:
            continue
        key = _norm(text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _append_term_values(target: list[str], value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str):
        target.extend(_extract_match_terms(value))
        return
    if isinstance(value, list):
        for item in value:
            _append_term_values(target, item)
        return
    if isinstance(value, dict):
        for nested_key in (
            "content",
            "summary",
            "项目",
            "value",
            "sub_tag",
            "category",
            "label",
            "text",
            "name",
            "type",
            "area",
            "surface_need",
            "deep_need",
            "discovery_process",
        ):
            if nested_key in value:
                _append_term_values(target, value.get(nested_key))


def _collect_terms_by_labels(payload_data: dict | None, target_labels: tuple[str, ...]) -> list[str]:
    normalized_targets = {_norm(label) for label in target_labels}
    values: list[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if _norm(str(key)) in normalized_targets:
                    _append_term_values(values, nested)
                _walk(nested)
            return
        if isinstance(value, list):
            for item in value:
                _walk(item)

    for section in _payload_analysis_sections(payload_data):
        _walk(section)
    return _dedupe_terms(values)


def _collect_parent_labels_for_child_labels(payload_data: dict | None, target_labels: tuple[str, ...]) -> list[str]:
    normalized_targets = {_norm(label) for label in target_labels}
    ignored_parents = {_norm(label) for label in ("summary", "content", "result")}
    values: list[str] = []

    def _walk(value: Any, parent_label: str | None = None) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                current_label = _clean_text(str(key))
                current_norm = _norm(current_label)
                if current_norm in normalized_targets and parent_label and _norm(parent_label) not in ignored_parents:
                    values.append(parent_label)
                _walk(nested, current_label or parent_label)
            return
        if isinstance(value, list):
            for item in value:
                _walk(item, parent_label)

    for section in _payload_analysis_sections(payload_data):
        _walk(section)
    return _dedupe_terms(values)


def _extract_structured_demands(payload_data: dict | None) -> dict[str, list[str]]:
    area_terms = _collect_terms_by_labels(
        payload_data,
        ("focus_areas", "area", "适应症", "部位", "标签", "category"),
    )
    area_terms.extend(
        _collect_parent_labels_for_child_labels(
            payload_data,
            ("推荐方案清单", "推荐方案", "核心诉求", "核心需求", "需求关键词", "项目需求"),
        )
    )
    return {
        "project_terms": _collect_terms_by_labels(
            payload_data,
            ("项目需求", "推荐方案清单", "推荐方案", "咨询项目", "治疗项目", "项目", "方案"),
        ),
        "need_terms": _collect_terms_by_labels(
            payload_data,
            ("核心诉求", "核心需求", "需求关键词", "客户求美需求", "诉求", "需求", "深层诉求", "表层诉求"),
        ),
        "areas": _dedupe_terms(area_terms),
    }


def _structured_demand_score(
    payload_data: dict | None,
    order: VisitOrder,
) -> tuple[float, MatchEvidenceOut | None]:
    demands = _extract_structured_demands(payload_data)
    if not demands["project_terms"] and not demands["need_terms"] and not demands["areas"]:
        return 0.0, None

    order_project_text = " ".join(filter(None, [
        order.remark_dz,
        order.jgks_txt or order.jgks,
        order.dymd_txt,
    ]))
    order_need_text = " ".join(filter(None, [
        order.remark_dz,
        order.dymd_txt,
        order.dztyp_txt,
        order.dzly_txt,
    ]))

    order_keywords = _extract_order_keywords(order)
    project_score, project_hits = _keyword_match_score(demands["project_terms"] + demands["areas"], order_project_text, order_keywords)
    need_score, need_hits = _keyword_match_score(demands["need_terms"] + demands["areas"], order_need_text, order_keywords)

    scaled_project = min(project_score, 0.08)
    scaled_need = min(need_score, 0.06)
    total_score = min(scaled_project + scaled_need, 0.12)
    if total_score <= 0:
        return 0.0, None

    detail_parts: list[str] = []
    if project_hits:
        detail_parts.append("项目=" + "、".join(project_hits[:3]))
    if need_hits:
        detail_parts.append("诉求=" + "、".join(need_hits[:3]))
    detail = "；".join(detail_parts) or "录音分析提取的结构化诉求与到诊单匹配"
    strength = "high" if project_hits and need_hits else "medium"
    return total_score, _make_evidence("structured_demand", "结构化诉求匹配", detail, strength)


_PROJECT_BUCKET_KEYWORDS: dict[str, tuple[str, ...]] = {
    "slimming_injection": ("瘦脸针", "肉毒", "除皱针", "咬肌", "下颌缘", "botox"),
    "eye_surgery": ("眼袋", "祛眼袋", "内取", "外切", "泪沟回填", "双眼皮", "重睑", "开眼角", "开内眦", "内眦", "内眼角", "提肌", "调肌力", "去皮去脂"),
    "filler_rejuvenation": ("玻尿酸", "胶原", "苹果肌", "泪沟", "填充", "复位", "眶周", "卧蚕", "鼻基底", "面中", "唇", "丰唇", "唇部"),
    "skin_rejuvenation": ("热玛吉", "超声炮", "水光", "光子", "皮秒", "射频", "激光"),
}

_PROCEDURE_TERM_VARIANTS: dict[str, tuple[str, ...]] = {
    "双眼皮": ("双眼皮", "重睑"),
    "开内眦": ("开内眦", "内眦", "内眼角", "开眼角"),
    "提肌": ("提肌", "上睑提肌", "调肌力", "肌力调整", "轻度提肌", "轻度提肌调整", "轻度体积", "经历调整"),
    "去皮去脂": ("去皮去脂", "去皮", "去脂"),
    "眼袋": ("眼袋", "祛眼袋", "内取", "外切"),
    "提眉": ("提眉",),
}


def _extract_project_buckets(values: list[str]) -> set[str]:
    buckets: set[str] = set()
    normalized_values = [_norm(value) for value in values if _norm(value)]
    for bucket, keywords in _PROJECT_BUCKET_KEYWORDS.items():
        normalized_keywords = tuple(_norm(keyword) for keyword in keywords)
        if any(any(keyword in value for keyword in normalized_keywords) for value in normalized_values):
            buckets.add(bucket)
    return buckets


def _extract_canonical_procedure_terms(values: list[str]) -> set[str]:
    terms: set[str] = set()
    normalized_values = [_norm(value) for value in values if _norm(value)]
    for canonical, variants in _PROCEDURE_TERM_VARIANTS.items():
        normalized_variants = tuple(_norm(variant) for variant in variants)
        if any(any(variant in value for variant in normalized_variants) for value in normalized_values):
            terms.add(canonical)
    return terms


def _procedure_plan_alignment_score(
    transcript_text: str | None,
    order: VisitOrder,
) -> tuple[float, MatchEvidenceOut | None]:
    transcript_terms = _extract_canonical_procedure_terms([transcript_text or ""])
    order_terms = _extract_canonical_procedure_terms([
        order.remark_dz or "",
    ])
    hits = sorted(transcript_terms & order_terms)
    if not hits:
        return 0.0, None

    score = 0.08
    strength = "medium"
    if len(hits) >= 2:
        score = 0.16
        strength = "high"
    if len(hits) >= 3:
        score = 0.20
        strength = "high"
    detail = "匹配术式：" + "、".join(hits[:4])
    return score, _make_evidence("procedure_plan", "术式方案匹配", detail, strength)


def _project_conflict_score(
    transcript_text: str | None,
    payload_data: dict | None,
    order: VisitOrder,
) -> tuple[float, MatchEvidenceOut | None]:
    recording_terms = _dedupe_terms([
        *(_extract_analysis_keywords(payload_data) or []),
        *(_extract_structured_demands(payload_data).get("project_terms") or []),
        *(_extract_structured_demands(payload_data).get("need_terms") or []),
        transcript_text or "",
    ])
    order_terms = _dedupe_terms([
        order.remark_dz or "",
        *(_extract_order_keywords(order) or []),
    ])

    recording_buckets = _extract_project_buckets(recording_terms)
    order_buckets = _extract_project_buckets(order_terms)
    if not recording_buckets or not order_buckets:
        return 0.0, None
    if recording_buckets & order_buckets:
        return 0.0, None

    detail = (
        f"录音主题={ '、'.join(sorted(recording_buckets)) }；"
        f"到诊单主题={ '、'.join(sorted(order_buckets)) }"
    )
    return -0.18, _make_evidence("project_conflict", "咨询项目明显不匹配", detail, "high")


def _stage_conflict_score(
    transcript_text: str | None,
    order: VisitOrder,
) -> tuple[float, MatchEvidenceOut | None]:
    transcript_norm = _norm(transcript_text)
    if not transcript_norm:
        return 0.0, None

    order_text = _norm(" ".join(
        part for part in (
            order.dztyp_txt,
            order.dzsta_txt,
            order.jcsta_txt,
            order.dymd_txt,
            order.remark_dz,
        )
        if part
    ))
    if not order_text:
        return 0.0, None

    followup_markers = ("拆线", "复查", "复诊", "换药", "术后", "恢复")
    consult_markers = ("价格", "费用", "预算", "方案", "院长", "双眼皮", "开眼角", "内眼角", "提肌", "什么时候做", "这个月做", "下个月")

    if any(marker in order_text for marker in followup_markers):
        consult_hits = sum(1 for marker in consult_markers if marker in transcript_norm)
        if consult_hits >= 2:
            detail = "到诊单备注更像术后/拆线，但录音在讨论初次手术方案、报价或手术时间"
            return -0.28, _make_evidence("stage_conflict", "接待阶段明显不匹配", detail, "high")
    return 0.0, None


def _extract_recording_stage_signals(transcript_text: str | None) -> dict[str, str]:
    signals: dict[str, str] = {}
    text_parts: list[str] = []

    if transcript_text:
        text_parts.append(transcript_text)

    text_blob = _norm(" ".join(text_parts))
    keyword_groups = {
        "triage": ("分诊", "登记", "建档", "接待", "排队", "初诊"),
        "doctor_consult": ("面诊", "医生看", "医生给你看", "方案设计", "设计方案", "适应症", "材料"),
        "pricing": ("报价", "价格", "费用", "套餐", "定金", "充值", "刷卡", "付款", "交钱", "成交"),
        "followup": ("复诊", "复查", "术后", "恢复", "回访", "下次", "跟进"),
    }
    stage_labels = {
        "triage": "分诊/初诊阶段",
        "doctor_consult": "面诊/方案阶段",
        "pricing": "报价/成交阶段",
        "followup": "复诊/术后阶段",
    }
    for stage, keywords in keyword_groups.items():
        if stage in signals:
            continue
        if any(_norm(keyword) in text_blob for keyword in keywords):
            signals[stage] = stage_labels[stage]
    return signals


def _extract_order_stage_signals(order: VisitOrder) -> dict[str, str]:
    signals: dict[str, str] = {}
    status_text = " ".join(
        part for part in (
            order.dztyp_txt,
            order.dzsta_txt,
            order.jcsta_txt,
            order.dymd_txt,
            order.remark_dz,
        ) if part
    )
    norm_status = _norm(status_text)

    if order.fzsj or order.fzdh or order.fzrid:
        signals["triage"] = "到诊单已进入分诊阶段"
    if order.jzsj or order.yyuer:
        signals["doctor_consult"] = "到诊单已进入接诊/面诊阶段"
    if any(token in norm_status for token in ("成交", "交钱", "定金", "充值", "付款", "刷卡")):
        signals["pricing"] = "到诊单存在报价/成交特征"
    if any(token in norm_status for token in ("复诊", "复查", "术后", "恢复", "回访", "跟进")):
        signals["followup"] = "到诊单存在复诊/术后特征"
    return signals


def _stage_alignment_score(
    transcript_text: str | None,
    order: VisitOrder,
) -> tuple[float, MatchEvidenceOut | None]:
    recording_stages = _extract_recording_stage_signals(transcript_text)
    order_stages = _extract_order_stage_signals(order)
    if not recording_stages or not order_stages:
        return 0.0, None

    weights = {
        "pricing": 0.08,
        "doctor_consult": 0.06,
        "followup": 0.06,
        "triage": 0.05,
    }
    labels = {
        "pricing": "报价/成交",
        "doctor_consult": "面诊/方案",
        "followup": "复诊/术后",
        "triage": "分诊/初诊",
    }
    hits = [stage for stage in ("pricing", "doctor_consult", "followup", "triage") if stage in recording_stages and stage in order_stages]
    if not hits:
        return 0.0, None

    score = sum(weights.get(stage, 0.0) for stage in hits[:2])
    score = min(score, 0.12)
    hit_labels = "、".join(labels[stage] for stage in hits[:2])
    detail = f"录音与到诊单同时命中 {hit_labels} 阶段"
    strength = "high" if "pricing" in hits or len(hits) >= 2 else "medium"
    return score, _make_evidence("stage", "接待阶段匹配", detail, strength)


def _extract_order_keywords(order: VisitOrder) -> list[str]:
    """Extract searchable keywords from visit order fields."""
    # Common non-treatment words to skip
    _STOP = {
        "今日", "接待", "人员", "顾客", "咨询", "前往", "科室", "所做", "事项",
        "其他", "描述", "剩余", "赠送", "转退", "退费", "项目", "潜在", "需求",
        "背景", "预算", "未成交", "原因", "跟进", "方案", "预约", "已知", "报备",
        "线上", "转款", "调单", "主诉", "推荐", "铺垫", "过程", "中对", "比医",
        "院个", "名称", "买一", "送一", "社会",
    }
    keywords: list[str] = []
    for val in (
        order.remark_dz,
        order.dymd_txt, order.jgks_txt or order.jgks,
    ):
        text = str(val or "").strip()
        if text and len(text) >= 2:
            keywords.append(text)
    # Extract key terms from notes
    for note_field in (order.remark_dz,):
        text = str(note_field or "").strip()
        if not text:
            continue
        terms = re.findall(r'[\u4e00-\u9fff]{2,8}', text)
        for t in terms:
            if len(t) >= 2 and t not in _STOP:
                keywords.append(t)
    seen: set[str] = set()
    unique: list[str] = []
    for kw in keywords:
        lower = kw.lower()
        if lower not in seen:
            seen.add(lower)
            unique.append(kw)
    return unique


def _keyword_match_score(
    source_keywords: list[str],
    target_text: str | None,
    target_keywords: list[str] | None = None,
) -> tuple[float, list[str]]:
    """Score keyword overlap between source keywords and target text/keywords.

    Returns (score, matched_keywords) where score is 0.0 to 0.25.
    """
    hits: list[str] = []
    norm_target = _norm(target_text)
    norm_target_kws = {_norm(k) for k in (target_keywords or []) if _norm(k)}

    for kw in source_keywords:
        nk = _norm(kw)
        if not nk or len(nk) < 2:
            continue
        matched = False
        # Check in text
        if norm_target and nk in norm_target:
            matched = True
        # Check in keyword list
        if not matched and norm_target_kws:
            for tk in norm_target_kws:
                if nk == tk:
                    matched = True
                    break
                shorter = min(len(nk), len(tk))
                longer = max(len(nk), len(tk))
                if shorter >= 4 and longer <= shorter * 2 and (nk in tk or tk in nk):
                    matched = True
                    break
        if matched and kw not in hits:
            hits.append(kw)

    score = min(len(hits) * 0.06, 0.25)
    return score, hits[:5]


def _preferred_order_time_anchor(order: VisitOrder, staff: Staff | None) -> tuple[str, int] | None:
    triage_seconds = _parse_clock_to_seconds(order.fzsj)
    consult_seconds = _parse_clock_to_seconds(order.jzsj)

    if staff is not None:
        is_advisor = staff.is_onsite_advisor or staff.is_pre_advisor or staff.is_advisor_assistant or staff.is_guide
        is_doctor = staff.is_doctor or staff.is_doctor_assistant
        if is_advisor and triage_seconds is not None:
            return "分诊", triage_seconds
        if is_doctor and consult_seconds is not None:
            return "接诊", consult_seconds

    if triage_seconds is not None:
        return "分诊", triage_seconds
    if consult_seconds is not None:
        return "接诊", consult_seconds
    return None


def _time_proximity_score(
    record_seconds: int | None,
    order: VisitOrder,
    staff: Staff | None = None,
    record_time_label: str = "录音开始",
) -> tuple[float, MatchEvidenceOut | None]:
    """Score based on how close the recording time is to the preferred business anchor.

    The anchor is role-aware: advisors use triage time (fzsj), doctors use
    consult time (jzsj).  A recording that starts shortly *after* the anchor
    is the most natural scenario, so we give a small directional bonus when
    the recording is 0-15 min after (not before) the anchor.

    Returns (score, evidence) where score is 0.0 to 0.22.
    """
    if record_seconds is None:
        return 0.0, None

    time_anchor = _preferred_order_time_anchor(order, staff)
    if time_anchor is None:
        return 0.0, None
    anchor_label, order_seconds = time_anchor

    diff = abs(record_seconds - order_seconds)
    after_anchor = record_seconds >= order_seconds
    after_delta = (record_seconds - order_seconds) if after_anchor else 0
    diff_minutes = diff // 60

    def _fmt(secs: int) -> str:
        return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}"

    def _detail(*, suffix: str | None = None) -> str:
        relation = _describe_time_offset(
            anchor_label,
            diff_minutes,
            after_anchor=after_anchor,
            record_time_label=record_time_label,
        )
        if suffix:
            relation = f"{relation}，{suffix}"
        return f"{record_time_label} {_fmt(record_seconds)} vs {anchor_label} {_fmt(order_seconds)}；{relation}"

    # Ideal: 10-20 min AFTER anchor (centered on ~15 min)
    # Best tier: 0-20 min AFTER anchor (natural scenario: recording starts after triage/consult)
    if after_anchor and after_delta <= 1200:
        return 0.35, _make_evidence(
            "time", f"{anchor_label}后理想时段",
            _detail(),
            "high",
        )
    if diff <= 600:  # ≤10 min (before anchor)
        return 0.30, _make_evidence(
            "time", f"{anchor_label}时间非常接近",
            _detail(),
            "high",
        )
    if diff <= 1800:  # ≤30 min
        return 0.20, _make_evidence(
            "time", f"{anchor_label}时间接近",
            _detail(),
            "high",
        )
    if diff <= 3600:  # ≤1 hr
        return -0.03, _make_evidence(
            "time", f"{anchor_label}时间差已超半小时",
            _detail(suffix="时间差偏大"),
            "medium",
        )
    if diff <= 7200:  # ≤2 hr
        return -0.07, _make_evidence(
            "time", f"{anchor_label}时间差较大",
            _detail(suffix="时间差偏大"),
            "low",
        )
    if diff <= 14400:  # ≤4 hr
        return -0.12, _make_evidence(
            "time", f"{anchor_label}时间偏差较大",
            _detail(suffix="时间差很大"),
            "medium",
        )
    return -0.20, _make_evidence(
        "time", f"{anchor_label}时间偏差很大",
        _detail(suffix="时间差很大"),
        "high",
    )


def _staff_role_label(staff: Staff | None, staff_position_text: str | None = None) -> str | None:
    """Return a human-readable role label for matching context."""
    if staff is None:
        return None
    labels: list[str] = []
    if _is_department_assistant_staff(staff, staff_position_text):
        labels.append("科室助理")
    if staff.is_doctor:
        labels.append("医生")
    if staff.is_onsite_advisor:
        labels.append("现场顾问")
    if staff.is_pre_advisor:
        labels.append("院前顾问")
    if staff.is_advisor_assistant:
        labels.append("顾问助理")
    if staff.is_doctor_assistant:
        labels.append("医助")
    if staff.is_nurse:
        labels.append("护士")
    return "、".join(labels) if labels else None


def _role_time_window_score(
    record_seconds: int | None,
    rec_end_seconds: int | None,
    order: VisitOrder,
    staff: Staff | None,
    record_time_label: str = "录音",
) -> tuple[float, MatchEvidenceOut | None]:
    """Score based on whether the recording falls into the expected time window
    for the staff member's role.

    - Advisor/consultant: expected between triage time (fzsj) and doctor consultation time (jzsj)
    - Doctor: expected after doctor consultation time (jzsj)

    Returns (score, evidence) where score may be positive or negative.
    """
    if record_seconds is None or staff is None:
        return 0.0, None

    triage_seconds = _parse_clock_to_seconds(order.fzsj)
    consult_seconds = _parse_clock_to_seconds(order.jzsj)
    is_advisor = staff.is_onsite_advisor or staff.is_pre_advisor or staff.is_advisor_assistant
    is_doctor = staff.is_doctor or staff.is_doctor_assistant

    if not is_advisor and not is_doctor:
        return 0.0, None

    def _fmt(secs: int) -> str:
        return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}"

    def _distance_to_window(start: int, end: int, value_start: int, value_end: int | None) -> int:
        actual_end = value_end if value_end is not None else value_start
        if value_start <= end and actual_end >= start:
            return 0
        if actual_end < start:
            return start - actual_end
        return value_start - end

    # Allow ±15 min tolerance for early start / late stop
    _TOLERANCE = 900  # 15 minutes

    if is_advisor and triage_seconds is not None:
        window_start = triage_seconds - _TOLERANCE
        # If consult time exists, advisor window ends at consult time + tolerance;
        # otherwise use triage + 4 hours as a generous upper bound.
        window_end = (consult_seconds + _TOLERANCE) if consult_seconds is not None else (triage_seconds + 14400)
        rec_start_in = window_start <= record_seconds <= window_end
        rec_end_in = rec_end_seconds is not None and window_start <= rec_end_seconds <= window_end + _TOLERANCE
        if rec_start_in or rec_end_in:
            detail_parts = [f"{record_time_label} {_fmt(record_seconds)}"]
            if rec_end_seconds is not None:
                detail_parts.append(f"-{_fmt(rec_end_seconds)}")
            detail_parts.append(f"，顾问窗口 {_fmt(triage_seconds)}")
            if consult_seconds is not None:
                detail_parts.append(f"-{_fmt(consult_seconds)}")
            detail = "".join(detail_parts)
            # Stronger score when both triage and consult times bracket the recording
            if consult_seconds is not None and rec_start_in:
                return 0.22, _make_evidence("role_time", "录音时段落入顾问接待窗口", detail, "high")
            return 0.14, _make_evidence("role_time", "录音时段大致落入顾问接待窗口", detail, "medium")

        if consult_seconds is not None:
            distance = _distance_to_window(window_start, window_end, record_seconds, rec_end_seconds)
            if distance > 3600:
                detail = (
                    f"{record_time_label} {_fmt(record_seconds)}"
                    f"{f'-{_fmt(rec_end_seconds)}' if rec_end_seconds is not None else ''}，"
                    f"明显偏离顾问窗口 {_fmt(triage_seconds)}-{_fmt(consult_seconds)}"
                )
                return -0.24, _make_evidence("role_time", "录音时段偏离顾问接待窗口", detail, "high")
            if distance > 1800:
                detail = (
                    f"{record_time_label} {_fmt(record_seconds)}"
                    f"{f'-{_fmt(rec_end_seconds)}' if rec_end_seconds is not None else ''}，"
                    f"偏离顾问窗口 {_fmt(triage_seconds)}-{_fmt(consult_seconds)}"
                )
                return -0.20, _make_evidence("role_time", "录音时段偏离顾问接待窗口", detail, "medium")

    if is_doctor and consult_seconds is not None:
        window_start = consult_seconds - _TOLERANCE
        rec_start_in = record_seconds >= window_start
        if rec_start_in:
            detail = f"{record_time_label} {_fmt(record_seconds)}，医生接诊时间 {_fmt(consult_seconds)} 之后"
            return 0.20, _make_evidence("role_time", "录音时段落入医生接诊窗口", detail, "high")

        distance = window_start - record_seconds
        if distance > 1800:
            detail = f"{record_time_label} {_fmt(record_seconds)} 明显早于医生接诊时间 {_fmt(consult_seconds)}"
            return -0.20, _make_evidence("role_time", "录音时段偏离医生接诊窗口", detail, "high")
        if distance > 600:
            detail = f"{record_time_label} {_fmt(record_seconds)} 早于医生接诊时间 {_fmt(consult_seconds)}"
            return -0.14, _make_evidence("role_time", "录音时段偏离医生接诊窗口", detail, "medium")
        if distance > 0:
            detail = f"{record_time_label} {_fmt(record_seconds)} 略早于医生接诊时间 {_fmt(consult_seconds)}"
            return -0.08, _make_evidence("role_time", "录音时段轻微偏离医生接诊窗口", detail, "low")

    return 0.0, None


def _order_note_text(order: VisitOrder) -> str | None:
    return _build_visit_notes(order)


def _order_consult_project(order: VisitOrder) -> str | None:
    for value in (order.remark_dz, order.dymd_txt):
        text = str(value or "").strip()
        if text:
            return text
    return None


def _build_match_line_item(order: VisitOrder) -> VisitOrderMatchLineItemOut:
    return VisitOrderMatchLineItemOut(
        fzdh=str(order.fzdh or "").strip() or None,
        dzseg=str(order.dzseg or "").strip() or None,
        triage_staff_code=str(order.advxc or order.fzuer or order.fzr_id_dq or "").strip() or None,
        triage_staff_name=str(order.advxc_long or order.fzr_name_dq or "").strip() or None,
        triage_time=str(order.fzsj or "").strip() or None,
        consult_time=str(order.jzsj or "").strip() or None,
        triage_status_text=str(order.fzsta_txt or "").strip() or None,
        deal_status_text=str(order.jcsta_txt or "").strip() or None,
        consult_project=_order_consult_project(order),
        note_summary=_order_note_text(order),
    )


_COMPANION_CODE_PATTERN = re.compile(r"(?:同行|陪同|姐姐|妹妹|家属|闺蜜|朋友)[^A-Za-z0-9]{0,4}([A-Za-z0-9]{5,})")
_COMPANION_TRANSCRIPT_HINTS = ("姐姐", "妹妹", "你们俩", "你俩", "你们两个", "两位", "同行", "一起", "陪同")


def _extract_companion_customer_codes(order: VisitOrder) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for field_value in (order.remark_dz,):
        text = str(field_value or "")
        if not text:
            continue
        for match in _COMPANION_CODE_PATTERN.finditer(text):
            code = _clean_text(match.group(1))
            if not code or code in seen:
                continue
            seen.add(code)
            codes.append(code)
    return codes


def _visit_order_ref(order: object) -> str:
    order_no = _clean_text(
        str(
            getattr(order, "dzdh", None)
            or getattr(order, "external_visit_order_no", None)
            or ""
        )
    )
    order_seg = _clean_text(
        str(
            getattr(order, "dzseg", None)
            or getattr(order, "external_visit_order_seg", None)
            or ""
        )
    )
    if not order_no:
        return ""
    return f"{order_no}{f'-{order_seg}' if order_seg else ''}"


def _find_companion_orders(base_order: VisitOrder, pool: list[VisitOrder]) -> list[VisitOrder]:
    base_customer_code = _clean_text(base_order.kunr)
    if not base_customer_code:
        return []

    base_companion_codes = set(_extract_companion_customer_codes(base_order))
    companions: list[VisitOrder] = []
    seen_refs: set[tuple[str | None, str | None]] = set()
    for order in pool:
        if order is base_order or (order.id and base_order.id and order.id == base_order.id):
            continue
        order_customer_code = _clean_text(order.kunr)
        if not order_customer_code:
            continue
        order_companion_codes = set(_extract_companion_customer_codes(order))
        if order_customer_code not in base_companion_codes and base_customer_code not in order_companion_codes:
            continue
        ref = (order.dzdh, order.dzseg)
        if ref in seen_refs:
            continue
        seen_refs.add(ref)
        companions.append(order)
    return companions


def _companion_visit_signal(
    transcript_text: str | None,
    order: VisitOrder,
    companion_orders: list[VisitOrder],
) -> tuple[float, MatchEvidenceOut | None, str | None]:
    if not companion_orders:
        return 0.0, None, None
    companion_codes = [code for code in _extract_companion_customer_codes(order) if code]
    refs = [_visit_order_ref(item) for item in companion_orders]
    transcript_has_hint = any(term in str(transcript_text or "") for term in _COMPANION_TRANSCRIPT_HINTS)
    detail = f"到诊需求/备注互指同行客户编码 {','.join(companion_codes[:3])}，关联到 {', '.join(refs[:3])}"
    strength = "high" if transcript_has_hint else "medium"
    score = 0.1 if transcript_has_hint else 0.06
    reason = "到诊单备注存在同行客户编码互指，可作为主单之外的辅绑定线索"
    return score, _make_evidence("companion", "同行到诊单互指", detail, strength), reason


def _make_evidence(kind: str, label: str, detail: str, strength: str = "medium") -> MatchEvidenceOut:
    return MatchEvidenceOut(type=kind, label=label, detail=detail, strength=strength)


_HARD_ROLE_TIME_EXCLUSION_SCORE = -0.20
_HEURISTIC_CONFIDENCE_BASE_CAP = 0.89
_HEURISTIC_CONFIDENCE_HEADROOM = 0.08
_HEURISTIC_CONFIDENCE_COMPRESS = 4.5


def _heuristic_confidence(score: float) -> float:
    normalized_score = max(float(score or 0.0), 0.0)
    if normalized_score <= _HEURISTIC_CONFIDENCE_BASE_CAP:
        return normalized_score
    overflow = normalized_score - _HEURISTIC_CONFIDENCE_BASE_CAP
    lifted = _HEURISTIC_CONFIDENCE_HEADROOM * (1.0 - math.exp(-overflow * _HEURISTIC_CONFIDENCE_COMPRESS))
    return min(_HEURISTIC_CONFIDENCE_BASE_CAP + lifted, 0.97)


def _finalize_candidate(candidate: _OrderCandidate | _RecordingCandidate) -> None:
    if candidate.hard_excluded:
        candidate.confidence = min(max(candidate.heuristic_score, 0.0), 0.19)
        candidate.decision = "ignore"
        return

    candidate.confidence = _heuristic_confidence(candidate.heuristic_score)
    if candidate.confidence >= 0.35:
        candidate.decision = "recommend"


def _candidate_order_to_out(
    candidate: _OrderCandidate,
    dzdh_segments: dict[str, list[str]] | None = None,
    current_recording_id: str | None = None,
    dzdh_orders: dict[str, list[VisitOrder]] | None = None,
) -> VisitOrderMatchCandidateOut:
    linked_recording_count = len(candidate.local_visit.recording_links) if candidate.local_visit else 0
    linked_recording_names: list[str] = []
    if candidate.local_visit and candidate.local_visit.recordings:
        linked_recording_names = [
            r.file_name for r in candidate.local_visit.recordings
            if r.id != current_recording_id
        ]
    vo = candidate.visit_order
    merged_segments: list[str] = []
    if dzdh_segments:
        segs = dzdh_segments.get(vo.dzdh, [])
        if len(segs) > 1:
            merged_segments = segs
    merged_orders = (
        sorted(
            dzdh_orders.get(vo.dzdh, [vo]),
            key=lambda order: (order.dzseg or "", order.fzdh or "", order.id or ""),
        )
        if dzdh_orders
        else [vo]
    )
    customer_type_code, customer_type_label = customer_type_from_visit_order(vo)
    return VisitOrderMatchCandidateOut(
        visit_order_id=vo.id,
        local_visit_id=candidate.local_visit.id if candidate.local_visit else None,
        associated_local_visit_ids=candidate.companion_local_visit_ids,
        companion_visit_order_refs=candidate.companion_visit_order_refs,
        companion_customer_codes=candidate.companion_customer_codes,
        dzdh=vo.dzdh,
        dzseg=vo.dzseg,
        customer_name=vo.ninam,
        customer_code=vo.kunr,
        customer_type_code=customer_type_code,
        customer_type_label=customer_type_label,
        visit_date=vo.sjrq,
        advisor_code=vo.fzuer or vo.fzr_id_dq,
        triage_time=vo.fzsj,
        confidence=round(candidate.confidence, 4),
        decision=candidate.decision,
        method=candidate.method,
        reasons=candidate.reasons,
        excluded_reasons=candidate.excluded_reasons,
        identity_conflicts=candidate.identity_conflicts,
        manual_review_required=candidate.manual_review_required,
        manual_review_reason=candidate.manual_review_reason,
        evidence=candidate.evidence,
        merged_segments=merged_segments,
        merged_line_items=[_build_match_line_item(order) for order in merged_orders],
        linked_recording_count=linked_recording_count,
        linked_recording_names=linked_recording_names,
    )


def _candidate_recording_to_out(candidate: _RecordingCandidate) -> RecordingMatchCandidateOut:
    current_visit = candidate.recording.visit
    return RecordingMatchCandidateOut(
        recording_id=candidate.recording.id,
        local_visit_id=current_visit.id if current_visit else None,
        file_name=candidate.recording.file_name,
        created_at=candidate.recording.created_at.isoformat() if candidate.recording.created_at else "",
        staff_name=candidate.recording.staff.name if candidate.recording.staff else None,
        advisor_code=(str(candidate.payload_meta.get("advisor_code") or "").strip() or None) if candidate.payload_meta else None,
        customer_name=current_visit.customer.name if current_visit and current_visit.customer else None,
        current_visit_id=current_visit.id if current_visit else None,
        current_visit_order_no=current_visit.external_visit_order_no if current_visit else None,
        current_visit_order_seg=current_visit.external_visit_order_seg if current_visit else None,
        confidence=round(candidate.confidence, 4),
        decision=candidate.decision,
        method=candidate.method,
        reasons=candidate.reasons,
        excluded_reasons=candidate.excluded_reasons,
        identity_conflicts=candidate.identity_conflicts,
        manual_review_required=candidate.manual_review_required,
        manual_review_reason=candidate.manual_review_reason,
        evidence=candidate.evidence,
    )


def _candidate_display_name(candidate: _OrderCandidate | _RecordingCandidate) -> str:
    if isinstance(candidate, _OrderCandidate):
        return f"到诊单 {candidate.visit_order.dzdh}"
    return f"录音 {candidate.recording.file_name}"


def _derive_candidate_exclusion_hints(candidate: _OrderCandidate | _RecordingCandidate) -> list[str]:
    hints: list[str] = []
    for evidence in candidate.evidence:
        if evidence.label == "咨询项目明显不匹配":
            hints.append("咨询主题与当前录音/到诊单的项目方向明显冲突")
        elif "性别冲突" in evidence.label:
            hints.append("客户性别特征与当前候选档案不一致")
        elif evidence.label == "客户年龄差距大":
            hints.append("客户年龄特征与当前候选档案差距过大")
        elif "偏离顾问接待窗口" in evidence.label:
            hints.append("录音时段不符合顾问接待窗口")
        elif "偏离医生接诊窗口" in evidence.label:
            hints.append("录音时段不符合医生接诊窗口")

    for reason in candidate.reasons:
        clean_reason = _clean_text(reason)
        if not clean_reason:
            continue
        if "明显冲突" in clean_reason or "偏离" in clean_reason:
            hints.append(clean_reason)
    return _merge_unique_reasons(hints)


def _populate_excluded_reasons(candidates: list[_OrderCandidate | _RecordingCandidate]) -> None:
    if not candidates:
        return

    best_candidate = max(candidates, key=lambda item: (item.confidence, item.heuristic_score))
    best_label = _candidate_display_name(best_candidate)

    for candidate in candidates:
        if candidate.decision in {"auto", "recommend"}:
            candidate.excluded_reasons = []
            continue

        explicit_reasons = _merge_unique_reasons(candidate.excluded_reasons)
        fallback_reasons = _derive_candidate_exclusion_hints(candidate)
        if candidate.hard_excluded:
            fallback_reasons = _merge_unique_reasons(
                explicit_reasons,
                fallback_reasons,
                ["该候选命中了硬性排除条件，不能作为最终匹配结果"],
            )
        elif explicit_reasons:
            fallback_reasons = _merge_unique_reasons(explicit_reasons, fallback_reasons)
        elif candidate is not best_candidate:
            gap_reason = (
                f"综合证据弱于更优候选（{best_label} 置信度 {best_candidate.confidence:.2f}，当前候选 {candidate.confidence:.2f}）"
            )
            fallback_reasons = _merge_unique_reasons(fallback_reasons, [gap_reason])
        else:
            fallback_reasons = _merge_unique_reasons(
                explicit_reasons,
                fallback_reasons,
                ["当前身份、时间、内容证据不足以支持自动或推荐匹配"],
            )

        if not fallback_reasons:
            fallback_reasons = ["当前身份、时间、内容证据不足以支持该候选"]
        candidate.excluded_reasons = fallback_reasons[:3]
        candidate.manual_review_required = candidate.manual_review_required or bool(candidate.identity_conflicts)
        if candidate.manual_review_required and not candidate.manual_review_reason:
            candidate.manual_review_reason = _manual_review_reason_from_conflicts(candidate.identity_conflicts)


def _ensure_recommended_order_candidate(candidates: list[_OrderCandidate]) -> tuple[_OrderCandidate | None, bool]:
    if not candidates:
        return None, False

    for candidate in candidates:
        if candidate.decision in {"auto", "recommend"}:
            return candidate, False

    fallback = next((candidate for candidate in candidates if not candidate.hard_excluded), candidates[0])
    fallback.decision = "recommend"
    fallback.reasons = _merge_unique_reasons(
        ["当前为保底推荐：虽未达到常规阈值，但在现有候选中综合证据最强，请人工确认"],
        fallback.reasons,
    )[:8]
    return fallback, True


def _promote_candidate_to_front(candidates: list[_OrderCandidate], target: _OrderCandidate | None) -> None:
    if not candidates or target is None:
        return
    try:
        index = candidates.index(target)
    except ValueError:
        return
    if index > 0:
        candidates.insert(0, candidates.pop(index))


def _merge_unique_reasons(*reason_groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in reason_groups:
        for reason in group:
            clean_reason = _clean_text(reason)
            if not clean_reason or clean_reason in seen:
                continue
            seen.add(clean_reason)
            merged.append(clean_reason)
    return merged


def _merge_unique_evidence(*evidence_groups: list[MatchEvidenceOut]) -> list[MatchEvidenceOut]:
    merged: list[MatchEvidenceOut] = []
    seen: set[tuple[str, str, str]] = set()
    for group in evidence_groups:
        for item in group:
            key = (item.type, item.label, item.detail)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _replace_summary_candidate_refs(summary: str | None, replacements: dict[str, str]) -> str | None:
    text = _clean_text(summary)
    if not text:
        return None
    for old_value, new_value in replacements.items():
        if old_value and new_value:
            text = text.replace(old_value, new_value)
    return text


def _staff_role_bucket(staff: Staff | None) -> str:
    if staff is None:
        return "other"
    if staff.is_doctor or staff.is_doctor_assistant:
        return "doctor"
    if staff.is_onsite_advisor or staff.is_pre_advisor or staff.is_advisor_assistant or staff.is_guide:
        return "advisor"
    return "other"


def _staff_match_code(staff: Staff | None, payload_meta: dict[str, str | int | None] | None = None) -> str | None:
    if staff is not None:
        code = _clean_text(staff.external_account)
        if code:
            return code
    if payload_meta:
        return _clean_text(str(payload_meta.get("advisor_code") or ""))
    return None


def _staff_match_name(staff: Staff | None) -> str | None:
    if staff is None:
        return None
    return _clean_text(staff.name)


def _order_identity_key(order: VisitOrder) -> str:
    return str(order.id or f"{order.dzdh}:{order.dzseg or ''}")


def _recording_identity_key(recording: Recording) -> str:
    return str(recording.id or recording.file_name or id(recording))


def _advisor_code_matches_order(advisor_code: str | None, order: VisitOrder) -> bool:
    code = _clean_text(advisor_code)
    if not code:
        return False
    return code in {
        _clean_text(order.fzuer),
        _clean_text(order.fzr_id_dq),
        _clean_text(order.advxc),
        _clean_text(order.advyq),
        _clean_text(order.assxc),
        _clean_text(order.fzrid),
    }


def _doctor_matches_order(staff: Staff | None, order: VisitOrder) -> bool:
    code = _staff_match_code(staff)
    doctor_codes = {
        _clean_text(order.yyuer),
    }
    return bool(code and code in doctor_codes)


def _recording_order_time_delta_minutes(
    recording: Recording,
    payload_meta: dict[str, str | int | None] | None,
    order: VisitOrder,
    staff: Staff | None,
    full_payload: dict | None = None,
) -> tuple[str | None, int | None]:
    record_seconds, _ = _recording_start_reference(recording, payload_meta, full_payload)
    if record_seconds is None:
        return None, None
    anchor = _preferred_order_time_anchor(order, staff)
    if anchor is None:
        return None, None
    anchor_label, anchor_seconds = anchor
    return anchor_label, abs(record_seconds - anchor_seconds) // 60


def _shortlist_time_rank(time_diff_minutes: int | None) -> int:
    if time_diff_minutes is None:
        return 0
    if time_diff_minutes <= 20:
        return 5
    if time_diff_minutes <= 45:
        return 4
    if time_diff_minutes <= 90:
        return 3
    if time_diff_minutes <= _SHORTLIST_TIME_WINDOW_MINUTES:
        return 2
    return 1


def _role_aware_time_weight(staff: Staff | None, time_evidence: MatchEvidenceOut | None) -> float:
    role_bucket = _staff_role_bucket(staff)
    label = _clean_text(time_evidence.label if time_evidence else "")
    if role_bucket == "advisor":
        if "分诊" in label:
            return _ADVISOR_TRIAGE_TIME_WEIGHT
        return _RECORDING_ORDER_TIME_WEIGHT
    if role_bucket == "doctor":
        if "接诊" in label:
            return _DOCTOR_CONSULT_TIME_WEIGHT
        return _RECORDING_ORDER_TIME_WEIGHT
    return _RECORDING_ORDER_TIME_WEIGHT


def _time_reason_detail(time_evidence: MatchEvidenceOut) -> str:
    detail = _clean_text(time_evidence.detail) or ""
    if "；" in detail:
        return detail.rsplit("；", 1)[-1].strip()
    return detail or (_clean_text(time_evidence.label) or "")


def _describe_time_offset(
    anchor_label: str,
    diff_minutes: int,
    *,
    after_anchor: bool,
    record_time_label: str = "录音开始",
) -> str:
    record_label = record_time_label if record_time_label.endswith("时间") else f"{record_time_label}时间"
    anchor_time_label = f"{anchor_label}时间"
    if diff_minutes <= 0:
        return f"{record_label}与{anchor_time_label}一致"
    direction = "之后" if after_anchor else "之前"
    return f"{record_label}在{anchor_time_label}{diff_minutes}分钟{direction}"


def _apply_customer_gender_score(
    candidate: _OrderCandidate | _RecordingCandidate,
    order: VisitOrder,
    order_all_text: str,
    demographics: dict[str, Any],
) -> None:
    recording_gender = _normalize_gender_label(demographics.get("gender"))
    if not recording_gender:
        return

    order_gender = _normalize_gender_label(order.customer_gender)
    gender_source = _clean_text(str(demographics.get("gender_source") or ""))
    gender_strength = _clean_text(str(demographics.get("gender_strength") or ""))
    gender_signals = [str(value).strip() for value in demographics.get("gender_signals") or [] if str(value).strip()]
    signals_suffix = f"，依据：{'、'.join(gender_signals[:3])}" if gender_signals else ""

    if order_gender:
        if recording_gender == order_gender:
            bonus = 0.10
            evidence_strength = "medium"
            if gender_source == "transcript":
                bonus = 0.12 if gender_strength == "strong" else 0.09
                evidence_strength = "high" if gender_strength == "strong" else "medium"

            candidate.heuristic_score += bonus
            candidate.evidence.append(
                _make_evidence(
                    "demographics",
                    "客户性别吻合(档案)",
                    f"档案性别={order.customer_gender}, 录音推断={recording_gender}{signals_suffix}",
                    evidence_strength,
                )
            )
            candidate.reasons.append("录音中的客户性别特征与到诊单档案一致")

            if recording_gender == "男" and gender_source == "transcript" and gender_strength == "strong":
                candidate.heuristic_score += 0.06
                candidate.evidence.append(
                    _make_evidence(
                        "demographics",
                        "男性客户特征吻合",
                        f"录音中多次出现男性指向描述，到诊单客户为男性{signals_suffix}",
                        "high",
                    )
                )
                candidate.reasons.append("录音多次呈现男性客户特征，男性客户在医美场景中的区分度更高")
            return

        if gender_source == "transcript" and gender_strength == "strong":
            penalty = 0.30
            # In this business setting, explicit male markers in the transcript
            # are relatively distinctive. If the order档案 is female but the
            # recording strongly points to a male customer, treat it as a
            # stronger contradiction than a generic gender mismatch.
            if recording_gender == "男" and order_gender == "女":
                penalty = 0.42
        elif gender_source == "transcript":
            penalty = 0.18
        else:
            penalty = 0.12
        candidate.heuristic_score -= penalty
        candidate.evidence.append(
            _make_evidence(
                "demographics",
                "客户性别冲突(档案)",
                f"档案性别={order.customer_gender}, 录音推断={recording_gender}{signals_suffix}",
                "high",
            )
        )
        candidate.reasons.append("录音中的客户性别特征与到诊单档案冲突")
        return

    order_text_all = _norm(order_all_text)
    gender_match = False
    if recording_gender == "女" and order_text_all and any(token in order_text_all for token in ("女士", "女性", "小姐", "姐")):
        gender_match = True
    elif recording_gender == "男" and order_text_all and any(token in order_text_all for token in ("先生", "男性", "男士")):
        gender_match = True
    if gender_match:
        candidate.heuristic_score += 0.05 if gender_source == "transcript" else 0.04
        candidate.evidence.append(
            _make_evidence(
                "demographics",
                "客户性别吻合(备注)",
                f"录音推断={recording_gender}{signals_suffix}",
                "low",
            )
        )


def _build_order_shortlist_facts(
    order: VisitOrder,
    payload_meta: dict[str, str | int | None] | None,
    staff: Staff | None,
    recording: Recording | None = None,
    full_payload: dict | None = None,
    staff_position_text: str | None = None,
) -> dict[str, Any]:
    advisor_code = _clean_text(str(payload_meta.get("advisor_code") or "")) if payload_meta else None
    customer_code = _clean_text(str(payload_meta.get("customer_code") or "")) if payload_meta else None
    visit_order_no = _clean_text(str(payload_meta.get("visit_order_no") or "")) if payload_meta else None
    role_bucket = _staff_role_bucket(staff)
    direct_match = bool(visit_order_no and visit_order_no == _clean_text(order.dzdh))
    customer_match = bool(customer_code and customer_code == _clean_text(order.kunr))
    department_assistant_match = _department_assistant_order_match(staff, staff_position_text, order)
    if role_bucket == "advisor":
        role_match = _advisor_code_matches_order(advisor_code, order)
    elif role_bucket == "doctor":
        role_match = _doctor_matches_order(staff, order)
    else:
        role_match = _advisor_code_matches_order(advisor_code, order) or _doctor_matches_order(staff, order)
    anchor_label, time_diff_minutes = _recording_order_time_delta_minutes(recording, payload_meta, order, staff, full_payload) if recording is not None else (None, None)

    reasons: list[str] = []
    evidence: list[MatchEvidenceOut] = []
    if direct_match:
        reasons.append("payload 中的到诊单号与该候选一致，优先纳入候选")
        evidence.append(_make_evidence("shortlist", "到诊单号命中", f"payload.DZDH={visit_order_no}", "high"))
    if customer_match:
        reasons.append("payload 中的客户编码与到诊单客户编码一致，优先纳入候选")
        evidence.append(_make_evidence("shortlist", "客户编码命中", f"KUNR={customer_code}", "high"))
    if role_match:
        reasons.append("录音者编码与到诊单中的对应岗位编码一致，纳入候选")
        evidence.append(_make_evidence("shortlist", "录音者编码命中", f"录音者角色={_staff_role_label(staff, staff_position_text) or '未知'}", "high"))
    if department_assistant_match:
        department_hints = _department_codes_from_text(staff_position_text)
        if department_hints:
            labels = [_department_label(code) or code for code in department_hints]
            reasons.append(f"录音者为科室助理，且岗位科室配置命中到诊单科室（{','.join(labels[:3])}）")
            evidence.append(
                _make_evidence(
                    "shortlist",
                    "科室助理科室命中",
                    f"到诊单科室={_clean_text(order.jgks_txt) or _department_label(order.jgks) or '未标注'}",
                    "high",
                )
            )
        else:
            reasons.append("录音者为科室助理，按同机构同日到诊单纳入快速推荐候选")
            evidence.append(
                _make_evidence(
                    "shortlist",
                    "科室助理同机构候选",
                    f"到诊单机构={_clean_text(order.jgbm) or '未标注'}，科室={_clean_text(order.jgks_txt) or _department_label(order.jgks) or '未标注'}",
                    "low",
                )
            )
    if anchor_label and time_diff_minutes is not None:
        strength = "high" if time_diff_minutes <= 30 else "medium" if time_diff_minutes <= 90 else "low"
        evidence.append(
            _make_evidence(
                "shortlist",
                f"{anchor_label}时间候选过滤",
                f"录音与{anchor_label}时间差 {time_diff_minutes} 分钟",
                strength,
            )
        )

    sort_key = (
        int(direct_match),
        int(customer_match),
        int(department_assistant_match),
        _shortlist_time_rank(time_diff_minutes),
        int(role_match),
        -(time_diff_minutes if time_diff_minutes is not None else 10**6),
        _clean_text(order.dzdh) or "",
    )
    return {
        "direct_match": direct_match,
        "customer_match": customer_match,
        "role_match": role_match,
        "department_assistant_match": department_assistant_match,
        "time_anchor": anchor_label,
        "time_diff_minutes": time_diff_minutes,
        "reasons": reasons,
        "evidence": evidence,
        "sort_key": sort_key,
    }


def _shortlist_orders_for_recording(
    recording: Recording,
    orders: list[VisitOrder],
    payload_meta: dict[str, str | int | None] | None,
    staff: Staff | None,
    full_payload: dict | None = None,
    staff_position_text: str | None = None,
) -> list[tuple[VisitOrder, dict[str, Any]]]:
    if not orders:
        return []

    facts_by_id = {
        _order_identity_key(order): _build_order_shortlist_facts(
            order,
            payload_meta,
            staff,
            recording,
            full_payload,
            staff_position_text=staff_position_text,
        )
        for order in orders
    }
    scoped_orders = orders
    preserve_order = False

    direct_matches = [order for order in orders if facts_by_id[_order_identity_key(order)]["direct_match"]]
    if direct_matches:
        scoped_orders = direct_matches
    else:
        customer_matches = [order for order in orders if facts_by_id[_order_identity_key(order)]["customer_match"]]
        if customer_matches:
            scoped_orders = customer_matches
        else:
            role_matches = [order for order in orders if facts_by_id[_order_identity_key(order)]["role_match"]]
            if role_matches:
                role_matches = sorted(role_matches, key=lambda order: facts_by_id[_order_identity_key(order)]["sort_key"], reverse=True)
                nearby_orders = [
                    order
                    for order in orders
                    if not facts_by_id[_order_identity_key(order)]["role_match"]
                    if isinstance(facts_by_id[_order_identity_key(order)]["time_diff_minutes"], int)
                    and facts_by_id[_order_identity_key(order)]["time_diff_minutes"] <= _SHORTLIST_TIME_WINDOW_MINUTES
                ]
                nearby_orders = sorted(
                    nearby_orders,
                    key=lambda order: (
                        facts_by_id[_order_identity_key(order)]["time_diff_minutes"],
                        -int(facts_by_id[_order_identity_key(order)]["role_match"]),
                        _clean_text(order.dzdh) or "",
                    ),
                )
                if len(role_matches) <= 1:
                    scoped_orders = nearby_orders[:_SHORTLIST_NEAR_TIME_RESERVE] + role_matches[:_SHORTLIST_LIMIT]
                else:
                    scoped_orders = role_matches[:_SHORTLIST_LIMIT] + nearby_orders[:_SHORTLIST_NEAR_TIME_RESERVE]
                preserve_order = True
            else:
                department_matches = [
                    order
                    for order in orders
                    if facts_by_id[_order_identity_key(order)]["department_assistant_match"]
                ]
                if department_matches:
                    scoped_orders = department_matches

    if not preserve_order:
        scoped_orders = sorted(scoped_orders, key=lambda order: facts_by_id[_order_identity_key(order)]["sort_key"], reverse=True)
        near_orders = [
            order
            for order in scoped_orders
            if isinstance(facts_by_id[_order_identity_key(order)]["time_diff_minutes"], int)
            and facts_by_id[_order_identity_key(order)]["time_diff_minutes"] <= _SHORTLIST_TIME_WINDOW_MINUTES
        ]
        if near_orders:
            seen_ids = {_order_identity_key(order) for order in near_orders}
            scoped_orders = near_orders + [order for order in scoped_orders if _order_identity_key(order) not in seen_ids]

    return [(order, facts_by_id[_order_identity_key(order)]) for order in scoped_orders[:_SHORTLIST_LIMIT]]


def _build_recording_shortlist_facts(
    recording: Recording,
    payload_meta: dict[str, str | int | None] | None,
    order: VisitOrder,
    full_payload: dict | None = None,
) -> dict[str, Any]:
    advisor_code = _clean_text(str(payload_meta.get("advisor_code") or "")) if payload_meta else None
    customer_code = _clean_text(str(payload_meta.get("customer_code") or "")) if payload_meta else None
    visit_order_no = _clean_text(str(payload_meta.get("visit_order_no") or "")) if payload_meta else None
    role_bucket = _staff_role_bucket(recording.staff)
    direct_match = bool(visit_order_no and visit_order_no == _clean_text(order.dzdh))
    customer_match = bool(customer_code and customer_code == _clean_text(order.kunr))
    if role_bucket == "advisor":
        role_match = _advisor_code_matches_order(advisor_code, order)
    elif role_bucket == "doctor":
        role_match = _doctor_matches_order(recording.staff, order)
    else:
        role_match = _advisor_code_matches_order(advisor_code, order) or _doctor_matches_order(recording.staff, order)
    anchor_label, time_diff_minutes = _recording_order_time_delta_minutes(recording, payload_meta, order, recording.staff, full_payload)

    reasons: list[str] = []
    evidence: list[MatchEvidenceOut] = []
    if direct_match:
        reasons.append("payload 中的到诊单号与当前到诊单一致，优先纳入候选")
        evidence.append(_make_evidence("shortlist", "到诊单号命中", f"payload.DZDH={visit_order_no}", "high"))
    if customer_match:
        reasons.append("payload 中的客户编码与当前到诊单客户编码一致，优先纳入候选")
        evidence.append(_make_evidence("shortlist", "客户编码命中", f"KUNR={customer_code}", "high"))
    if role_match:
        reasons.append("录音者编码与当前到诊单中的对应岗位编码一致，纳入候选")
        evidence.append(_make_evidence("shortlist", "录音者编码命中", f"录音者角色={_staff_role_label(recording.staff) or '未知'}", "high"))
    if anchor_label and time_diff_minutes is not None:
        strength = "high" if time_diff_minutes <= 30 else "medium" if time_diff_minutes <= 90 else "low"
        evidence.append(
            _make_evidence(
                "shortlist",
                f"{anchor_label}时间候选过滤",
                f"录音与{anchor_label}时间差 {time_diff_minutes} 分钟",
                strength,
            )
        )

    sort_key = (
        int(direct_match),
        int(customer_match),
        int(role_match),
        int(time_diff_minutes is not None and time_diff_minutes <= _SHORTLIST_TIME_WINDOW_MINUTES),
        -(time_diff_minutes if time_diff_minutes is not None else 10**6),
        recording.created_at.isoformat() if recording.created_at else "",
    )
    return {
        "direct_match": direct_match,
        "customer_match": customer_match,
        "role_match": role_match,
        "time_anchor": anchor_label,
        "time_diff_minutes": time_diff_minutes,
        "reasons": reasons,
        "evidence": evidence,
        "sort_key": sort_key,
    }


def _shortlist_recordings_for_order(
    recordings: list[Recording],
    payload_map: dict[str, dict[str, str | int | None]],
    order: VisitOrder,
    full_payloads: dict[str, dict] | None = None,
) -> list[tuple[Recording, dict[str, str | int | None] | None, dict[str, Any]]]:
    if not recordings:
        return []

    facts_by_id: dict[str, dict[str, Any]] = {}
    payloads_by_id: dict[str, dict[str, str | int | None] | None] = {}
    for recording in recordings:
        payload_meta = _lookup_by_file_name(payload_map, recording.file_name)
        record_key = _recording_identity_key(recording)
        payloads_by_id[record_key] = payload_meta
        facts_by_id[record_key] = _build_recording_shortlist_facts(
            recording,
            payload_meta,
            order,
            _lookup_by_file_name(full_payloads or {}, recording.file_name),
        )

    scoped_recordings = recordings
    direct_matches = [recording for recording in recordings if facts_by_id[_recording_identity_key(recording)]["direct_match"]]
    if direct_matches:
        scoped_recordings = direct_matches
    else:
        customer_matches = [recording for recording in recordings if facts_by_id[_recording_identity_key(recording)]["customer_match"]]
        if customer_matches:
            scoped_recordings = customer_matches
        else:
            role_matches = [recording for recording in recordings if facts_by_id[_recording_identity_key(recording)]["role_match"]]
            if role_matches:
                role_matches = sorted(role_matches, key=lambda recording: facts_by_id[_recording_identity_key(recording)]["sort_key"], reverse=True)
                nearby_recordings = [
                    recording
                    for recording in recordings
                    if isinstance(facts_by_id[_recording_identity_key(recording)]["time_diff_minutes"], int)
                    and facts_by_id[_recording_identity_key(recording)]["time_diff_minutes"] <= _SHORTLIST_TIME_WINDOW_MINUTES
                ]
                nearby_recordings = sorted(
                    nearby_recordings,
                    key=lambda recording: (
                        facts_by_id[_recording_identity_key(recording)]["time_diff_minutes"],
                        -int(facts_by_id[_recording_identity_key(recording)]["role_match"]),
                        recording.created_at.isoformat() if recording.created_at else "",
                    ),
                )
                merged_ids: set[str] = set()
                merged_recordings: list[Recording] = []
                for recording in role_matches[:_SHORTLIST_LIMIT]:
                    recording_id = _recording_identity_key(recording)
                    if recording_id in merged_ids:
                        continue
                    merged_ids.add(recording_id)
                    merged_recordings.append(recording)
                for recording in nearby_recordings[:_SHORTLIST_NEAR_TIME_RESERVE]:
                    recording_id = _recording_identity_key(recording)
                    if recording_id in merged_ids:
                        continue
                    merged_ids.add(recording_id)
                    merged_recordings.append(recording)
                scoped_recordings = merged_recordings or role_matches

    scoped_recordings = sorted(scoped_recordings, key=lambda recording: facts_by_id[_recording_identity_key(recording)]["sort_key"], reverse=True)
    near_recordings = [
        recording
        for recording in scoped_recordings
        if isinstance(facts_by_id[_recording_identity_key(recording)]["time_diff_minutes"], int)
        and facts_by_id[_recording_identity_key(recording)]["time_diff_minutes"] <= _SHORTLIST_TIME_WINDOW_MINUTES
    ]
    if near_recordings:
        seen_ids = {_recording_identity_key(recording) for recording in near_recordings}
        scoped_recordings = near_recordings + [recording for recording in scoped_recordings if _recording_identity_key(recording) not in seen_ids]

    return [
        (recording, payloads_by_id[_recording_identity_key(recording)], facts_by_id[_recording_identity_key(recording)])
        for recording in scoped_recordings[:_SHORTLIST_LIMIT]
    ]


def _score_order_for_recording(
    recording: Recording,
    payload_meta: dict[str, str | int | None] | None,
    order: VisitOrder,
    transcript_text: str | None,
    full_payload: dict | None = None,
    staff: Staff | None = None,
    staff_position_text: str | None = None,
) -> _OrderCandidate:
    """Score a visit-order candidate for a given recording.

    Uses multiple signals: customer code, advisor, customer name in
    transcript, time proximity, project/keyword matching, doctor name,
    and advisor name.
    """
    candidate = _OrderCandidate(visit_order=order, local_visit=None)
    advisor_code = _clean_text(str(payload_meta.get("advisor_code") or "")) if payload_meta else None
    customer_code = _clean_text(str(payload_meta.get("customer_code") or "")) if payload_meta else None
    record_seconds, record_time_label = _recording_start_reference(recording, payload_meta, full_payload)

    # --- 1. Customer code match (high value when available) ---
    if customer_code and customer_code == _clean_text(order.kunr):
        candidate.heuristic_score += 0.40
        candidate.evidence.append(_make_evidence("customer_code", "客户编码一致", f"KUNR={customer_code} 与到诊单一致", "high"))
        candidate.reasons.append("payload 的客户编码与到诊单客户编码一致")

    # --- 2. Advisor match ---
    if _advisor_matches(advisor_code, order):
        candidate.heuristic_score += 0.15
        candidate.evidence.append(_make_evidence("advisor", "顾问一致", f"录音顾问 {advisor_code} 与到诊单顾问一致", "high"))
        candidate.reasons.append("录音 payload 顾问与到诊单顾问一致")

    # --- 2b. Role-specific staff code match ---
    role_code_score, role_code_evidence = _role_specific_staff_score(staff, advisor_code, order)
    if role_code_score > 0 and role_code_evidence:
        candidate.heuristic_score += role_code_score
        candidate.evidence.append(role_code_evidence)
        candidate.reasons.append("录音员工角色与到诊单中的对应岗位编码一致")
    else:
        role_mismatch_score, role_mismatch_evidence, role_mismatch_reason = _role_specific_staff_mismatch_score(staff, advisor_code, order)
        if role_mismatch_score < 0 and role_mismatch_evidence and role_mismatch_reason:
            candidate.heuristic_score += role_mismatch_score
            candidate.evidence.append(role_mismatch_evidence)
            candidate.reasons.append(role_mismatch_reason)

    department_assistant_score, department_assistant_evidence, department_assistant_reason = (
        _department_assistant_order_signal(staff, staff_position_text, order)
    )
    if department_assistant_score > 0 and department_assistant_evidence and department_assistant_reason:
        candidate.heuristic_score += department_assistant_score
        candidate.evidence.append(department_assistant_evidence)
        candidate.reasons.append(department_assistant_reason)

    staff_identity_score, staff_identity_evidence, staff_identity_reason = _staff_self_identification_signal(staff, transcript_text)
    if staff_identity_score > 0 and staff_identity_evidence and staff_identity_reason:
        candidate.heuristic_score += staff_identity_score
        candidate.evidence.append(staff_identity_evidence)
        candidate.reasons.append(staff_identity_reason)

    # --- 3. Customer name from order → search in transcript ---
    name_score, name_evidence, name_reason = _customer_name_match_signal(order.ninam, transcript_text, order.customer_gender)
    if name_score > 0 and name_evidence and name_reason:
        candidate.heuristic_score += name_score
        candidate.evidence.append(name_evidence)
        candidate.reasons.append(name_reason)
    candidate.identity_conflicts = _build_identity_conflicts_for_candidate(order.ninam, transcript_text)
    candidate.manual_review_required = bool(candidate.identity_conflicts)
    candidate.manual_review_reason = _manual_review_reason_from_conflicts(candidate.identity_conflicts)

    # --- 4. Time proximity ---
    time_score, time_evidence = _time_proximity_score(record_seconds, order, staff, record_time_label=record_time_label)
    if time_score != 0 and time_evidence:
        time_score *= _role_aware_time_weight(staff, time_evidence)
        candidate.heuristic_score += time_score
        candidate.evidence.append(time_evidence)
        candidate.reasons.append(_time_reason_detail(time_evidence))

    # --- 5. Project / keyword matching ---
    analysis_keywords = _extract_analysis_keywords(full_payload)
    order_keywords = _extract_order_keywords(order)

    # Match order keywords against transcript
    kw_score_1, kw_hits_1 = _keyword_match_score(order_keywords, transcript_text)
    # Match analysis keywords against order notes
    order_notes = _order_note_text(order) or ""
    order_all_text = " ".join(filter(None, [
        order_notes,
        order.dymd_txt, order.jgks_txt or order.jgks,
        order.remark_dz,
    ]))
    kw_score_2, kw_hits_2 = _keyword_match_score(analysis_keywords, order_all_text, order_keywords)

    kw_score = min(max(kw_score_1, kw_score_2), 0.25)
    kw_hits = list(dict.fromkeys(kw_hits_1 + kw_hits_2))[:5]
    if kw_score > 0:
        candidate.heuristic_score += kw_score
        candidate.evidence.append(_make_evidence(
            "project", "咨询项目/关键词匹配",
            "匹配词：" + "、".join(kw_hits),
            "high" if kw_score >= 0.15 else "medium",
        ))
        candidate.reasons.append("录音内容与到诊单咨询项目/备注存在关键词重合")

    procedure_score, procedure_evidence = _procedure_plan_alignment_score(transcript_text, order)
    if procedure_score > 0 and procedure_evidence:
        candidate.heuristic_score += procedure_score
        candidate.evidence.append(procedure_evidence)
        candidate.reasons.append("录音中的具体术式组合与到诊单备注高度一致")

    # --- 5c. Structured demand alignment ---
    demand_score, demand_evidence = _structured_demand_score(full_payload, order)
    if demand_score > 0 and demand_evidence:
        candidate.heuristic_score += demand_score
        candidate.evidence.append(demand_evidence)
        candidate.reasons.append("录音分析中的结构化诉求与到诊单项目/备注字段匹配")

    conflict_score, conflict_evidence = _project_conflict_score(transcript_text, full_payload, order)
    if conflict_score < 0 and conflict_evidence:
        candidate.heuristic_score += conflict_score
        candidate.evidence.append(conflict_evidence)
        candidate.reasons.append("录音咨询主题与到诊单项目/备注存在明显冲突")

    # --- 5b. Consultation-stage alignment ---
    stage_score, stage_evidence = _stage_alignment_score(transcript_text, order)
    if stage_score > 0 and stage_evidence:
        candidate.heuristic_score += stage_score
        candidate.evidence.append(stage_evidence)
        candidate.reasons.append("录音所处接待阶段与到诊单业务阶段一致")

    stage_conflict_score, stage_conflict_evidence = _stage_conflict_score(transcript_text, order)
    if stage_conflict_score < 0 and stage_conflict_evidence:
        candidate.heuristic_score += stage_conflict_score
        candidate.evidence.append(stage_conflict_evidence)
        candidate.reasons.append("录音所处阶段与到诊单备注中的业务阶段存在明显冲突")

    # --- 6. On-site advisor name in transcript ---
    advxc_long = _clean_text(order.advxc_long)
    if advxc_long and advxc_long not in ("美学公海",):
        if _name_in_text(advxc_long, transcript_text):
            candidate.heuristic_score += 0.05
            candidate.evidence.append(_make_evidence("advisor_name", "现场顾问姓名出现在录音", f"顾问 {advxc_long} 在转写中被提及", "low"))
            candidate.reasons.append(f"录音中提及了现场顾问「{advxc_long}」")

    # --- 8. Pre-visit advisor (advyq) match ---
    if advisor_code and not _advisor_matches(advisor_code, order):
        advyq = _clean_text(order.advyq)
        if advyq and advisor_code == advyq:
            candidate.heuristic_score += 0.08
            candidate.evidence.append(_make_evidence("advyq", "院前顾问一致", f"录音顾问 {advisor_code} 与院前顾问一致", "medium"))
            candidate.reasons.append("录音顾问与到诊单院前美学顾问一致")

    # --- 9. Pre-visit advisor name in transcript ---
    advyq_name = _clean_text(order.advyq_name)
    if advyq_name and advyq_name not in ("美学公海",) and advyq_name != advxc_long:
        if _name_in_text(advyq_name, transcript_text):
            candidate.heuristic_score += 0.04
            candidate.evidence.append(_make_evidence("advyq_name", "院前顾问姓名出现在录音", f"院前顾问 {advyq_name} 在转写中被提及", "low"))
            candidate.reasons.append(f"录音中提及了院前顾问「{advyq_name}」")

    # --- 10. Department / specialty match ---
    dept_names = [_norm(d) for d in (order.jgks_txt, order.jgks) if _norm(d) and len(_norm(d)) >= 2]
    if dept_names and transcript_text:
        norm_transcript = _norm(transcript_text)
        for dept in dept_names:
            if dept in norm_transcript:
                candidate.heuristic_score += 0.06
                candidate.evidence.append(_make_evidence("department", "科室/专科匹配", f"科室「{dept}」在录音中被提及", "medium"))
                candidate.reasons.append(f"录音中提及了接诊科室「{dept}」")
                break

    # --- 11. Customer demographics cross-check ---
    demographics = _extract_payload_demographics(full_payload, transcript_text)
    _apply_customer_gender_score(candidate, order, order_all_text, demographics)
    # 年龄交叉验证
    if demographics.get("age"):
        order_age = _compute_age(order.customer_birthday, order.crtdt)
        if order_age is not None:
            try:
                rec_age_raw = demographics["age"]
                rec_age_num = _parse_customer_age_hint(str(rec_age_raw))
                if rec_age_num is None:
                    raise ValueError("invalid age hint")
                if abs(rec_age_num - order_age) <= 5:
                    candidate.heuristic_score += 0.06
                    candidate.evidence.append(_make_evidence("demographics", "客户年龄吻合", f"档案年龄={order_age}, 录音={rec_age_raw}", "medium"))
                elif abs(rec_age_num - order_age) > 15:
                    candidate.heuristic_score -= 0.06
                    candidate.evidence.append(_make_evidence("demographics", "客户年龄差距大", f"档案年龄={order_age}, 录音={rec_age_raw}", "medium"))
            except (ValueError, IndexError):
                pass

    # --- 12. Recording duration vs consultation time window ---
    rec_duration = _payload_duration_seconds(full_payload)
    rec_end_seconds: int | None = None
    if rec_duration is not None and rec_duration > 60:
        rec_end_seconds = (record_seconds or 0) + rec_duration if record_seconds else None
        order_start = _parse_clock_to_seconds(order.jzsj) or _parse_clock_to_seconds(order.fzsj)
        if rec_end_seconds and order_start:
            # Check if recording time window overlaps with order consultation time
            overlap_start = max(record_seconds or 0, order_start - 1800)
            overlap_end = min(rec_end_seconds, order_start + rec_duration + 3600)
            if overlap_start < overlap_end:
                candidate.heuristic_score += 0.04
                candidate.evidence.append(_make_evidence("duration", "录音时段覆盖接诊时间", f"录音时长 {rec_duration // 60} 分钟，与接诊时段重叠", "low"))

    # --- 13. Staff role-aware time window ---
    role_score, role_evidence = _role_time_window_score(
        record_seconds,
        rec_end_seconds,
        order,
        staff,
        record_time_label=record_time_label,
    )
    if role_score != 0 and role_evidence:
        candidate.heuristic_score += role_score
        candidate.evidence.append(role_evidence)
        if role_score <= _HARD_ROLE_TIME_EXCLUSION_SCORE:
            candidate.hard_excluded = True
        if role_score > 0:
            candidate.reasons.append(f"录音时段符合{_staff_role_label(staff) or '人员'}的预期接待时间窗口")
        else:
            candidate.reasons.append(f"录音时段偏离{_staff_role_label(staff) or '人员'}的预期接待时间窗口")

    _finalize_candidate(candidate)
    return candidate


def _score_recording_for_order(
    recording: Recording,
    payload_meta: dict[str, str | int | None] | None,
    order: VisitOrder,
    transcript_text: str | None,
    full_payload: dict | None = None,
    staff: Staff | None = None,
) -> _RecordingCandidate:
    """Score a recording candidate for a given visit-order.

    Uses the same multi-signal approach as _score_order_for_recording.
    """
    candidate = _RecordingCandidate(recording=recording, payload_meta=payload_meta)
    advisor_code = _clean_text(str(payload_meta.get("advisor_code") or "")) if payload_meta else None
    customer_code = _clean_text(str(payload_meta.get("customer_code") or "")) if payload_meta else None
    record_seconds, record_time_label = _recording_start_reference(recording, payload_meta, full_payload)

    # --- 1. Customer code match ---
    if customer_code and customer_code == _clean_text(order.kunr):
        candidate.heuristic_score += 0.40
        candidate.evidence.append(_make_evidence("customer_code", "客户编码一致", f"KUNR={customer_code} 与到诊单一致", "high"))
        candidate.reasons.append("payload 的客户编码与到诊单客户编码一致")

    # --- 2. Advisor match ---
    if _advisor_matches(advisor_code, order):
        candidate.heuristic_score += 0.15
        candidate.evidence.append(_make_evidence("advisor", "顾问一致", f"录音顾问 {advisor_code} 与到诊单顾问一致", "high"))
        candidate.reasons.append("录音 payload 顾问与到诊单顾问一致")

    # --- 2b. Role-specific staff code match ---
    role_code_score, role_code_evidence = _role_specific_staff_score(staff, advisor_code, order)
    if role_code_score > 0 and role_code_evidence:
        candidate.heuristic_score += role_code_score
        candidate.evidence.append(role_code_evidence)
        candidate.reasons.append("录音员工角色与到诊单中的对应岗位编码一致")
    else:
        role_mismatch_score, role_mismatch_evidence, role_mismatch_reason = _role_specific_staff_mismatch_score(staff, advisor_code, order)
        if role_mismatch_score < 0 and role_mismatch_evidence and role_mismatch_reason:
            candidate.heuristic_score += role_mismatch_score
            candidate.evidence.append(role_mismatch_evidence)
            candidate.reasons.append(role_mismatch_reason)

    staff_identity_score, staff_identity_evidence, staff_identity_reason = _staff_self_identification_signal(staff, transcript_text)
    if staff_identity_score > 0 and staff_identity_evidence and staff_identity_reason:
        candidate.heuristic_score += staff_identity_score
        candidate.evidence.append(staff_identity_evidence)
        candidate.reasons.append(staff_identity_reason)

    # --- 3. Customer name from order → search in transcript ---
    name_score, name_evidence, name_reason = _customer_name_match_signal(order.ninam, transcript_text, order.customer_gender)
    if name_score > 0 and name_evidence and name_reason:
        candidate.heuristic_score += name_score
        candidate.evidence.append(name_evidence)
        candidate.reasons.append(name_reason)
    candidate.identity_conflicts = _build_identity_conflicts_for_candidate(order.ninam, transcript_text)
    candidate.manual_review_required = bool(candidate.identity_conflicts)
    candidate.manual_review_reason = _manual_review_reason_from_conflicts(candidate.identity_conflicts)

    # --- 4. Time proximity ---
    time_score, time_evidence = _time_proximity_score(record_seconds, order, staff, record_time_label=record_time_label)
    if time_score != 0 and time_evidence:
        time_score *= _role_aware_time_weight(staff, time_evidence)
        candidate.heuristic_score += time_score
        candidate.evidence.append(time_evidence)
        candidate.reasons.append(_time_reason_detail(time_evidence))

    # --- 5. Project / keyword matching ---
    analysis_keywords = _extract_analysis_keywords(full_payload)
    order_keywords = _extract_order_keywords(order)

    kw_score_1, kw_hits_1 = _keyword_match_score(order_keywords, transcript_text)
    order_notes = _order_note_text(order) or ""
    order_all_text = " ".join(filter(None, [
        order_notes,
        order.dymd_txt, order.jgks_txt or order.jgks,
        order.remark_dz,
    ]))
    kw_score_2, kw_hits_2 = _keyword_match_score(analysis_keywords, order_all_text, order_keywords)

    kw_score = min(max(kw_score_1, kw_score_2), 0.25)
    kw_hits = list(dict.fromkeys(kw_hits_1 + kw_hits_2))[:5]
    if kw_score > 0:
        candidate.heuristic_score += kw_score
        candidate.evidence.append(_make_evidence(
            "project", "咨询项目/关键词匹配",
            "匹配词：" + "、".join(kw_hits),
            "high" if kw_score >= 0.15 else "medium",
        ))
        candidate.reasons.append("录音内容与到诊单咨询项目/备注存在关键词重合")

    procedure_score, procedure_evidence = _procedure_plan_alignment_score(transcript_text, order)
    if procedure_score > 0 and procedure_evidence:
        candidate.heuristic_score += procedure_score
        candidate.evidence.append(procedure_evidence)
        candidate.reasons.append("录音中的具体术式组合与到诊单备注高度一致")

    # --- 5c. Structured demand alignment ---
    demand_score, demand_evidence = _structured_demand_score(full_payload, order)
    if demand_score > 0 and demand_evidence:
        candidate.heuristic_score += demand_score
        candidate.evidence.append(demand_evidence)
        candidate.reasons.append("录音分析中的结构化诉求与到诊单项目/备注字段匹配")

    conflict_score, conflict_evidence = _project_conflict_score(transcript_text, full_payload, order)
    if conflict_score < 0 and conflict_evidence:
        candidate.heuristic_score += conflict_score
        candidate.evidence.append(conflict_evidence)
        candidate.reasons.append("录音咨询主题与到诊单项目/备注存在明显冲突")

    # --- 5b. Consultation-stage alignment ---
    stage_score, stage_evidence = _stage_alignment_score(transcript_text, order)
    if stage_score > 0 and stage_evidence:
        candidate.heuristic_score += stage_score
        candidate.evidence.append(stage_evidence)
        candidate.reasons.append("录音所处接待阶段与到诊单业务阶段一致")

    stage_conflict_score, stage_conflict_evidence = _stage_conflict_score(transcript_text, order)
    if stage_conflict_score < 0 and stage_conflict_evidence:
        candidate.heuristic_score += stage_conflict_score
        candidate.evidence.append(stage_conflict_evidence)
        candidate.reasons.append("录音所处阶段与到诊单备注中的业务阶段存在明显冲突")

    # --- 6. On-site advisor name in transcript ---
    advxc_long = _clean_text(order.advxc_long)
    if advxc_long and advxc_long not in ("美学公海",):
        if _name_in_text(advxc_long, transcript_text):
            candidate.heuristic_score += 0.05
            candidate.evidence.append(_make_evidence("advisor_name", "现场顾问姓名出现在录音", f"顾问 {advxc_long} 在转写中被提及", "low"))
            candidate.reasons.append(f"录音中提及了现场顾问「{advxc_long}」")

    # --- 8. Pre-visit advisor (advyq) match ---
    if advisor_code and not _advisor_matches(advisor_code, order):
        advyq = _clean_text(order.advyq)
        if advyq and advisor_code == advyq:
            candidate.heuristic_score += 0.08
            candidate.evidence.append(_make_evidence("advyq", "院前顾问一致", f"录音顾问 {advisor_code} 与院前顾问一致", "medium"))
            candidate.reasons.append("录音顾问与到诊单院前美学顾问一致")

    # --- 9. Pre-visit advisor name in transcript ---
    advyq_name = _clean_text(order.advyq_name)
    if advyq_name and advyq_name not in ("美学公海",) and advyq_name != advxc_long:
        if _name_in_text(advyq_name, transcript_text):
            candidate.heuristic_score += 0.04
            candidate.evidence.append(_make_evidence("advyq_name", "院前顾问姓名出现在录音", f"院前顾问 {advyq_name} 在转写中被提及", "low"))
            candidate.reasons.append(f"录音中提及了院前顾问「{advyq_name}」")

    # --- 10. Department / specialty match ---
    dept_names = [_norm(d) for d in (order.jgks_txt, order.jgks) if _norm(d) and len(_norm(d)) >= 2]
    if dept_names and transcript_text:
        norm_transcript = _norm(transcript_text)
        for dept in dept_names:
            if dept in norm_transcript:
                candidate.heuristic_score += 0.06
                candidate.evidence.append(_make_evidence("department", "科室/专科匹配", f"科室「{dept}」在录音中被提及", "medium"))
                candidate.reasons.append(f"录音中提及了接诊科室「{dept}」")
                break

    # --- 11. Customer demographics cross-check ---
    demographics = _extract_payload_demographics(full_payload, transcript_text)
    _apply_customer_gender_score(candidate, order, order_all_text, demographics)
    if demographics.get("age"):
        order_age = _compute_age(order.customer_birthday, order.crtdt)
        if order_age is not None:
            try:
                rec_age_raw = demographics["age"]
                rec_age_num = _parse_customer_age_hint(str(rec_age_raw))
                if rec_age_num is None:
                    raise ValueError("invalid age hint")
                if abs(rec_age_num - order_age) <= 5:
                    candidate.heuristic_score += 0.06
                    candidate.evidence.append(_make_evidence("demographics", "客户年龄吻合", f"档案年龄={order_age}, 录音={rec_age_raw}", "medium"))
                elif abs(rec_age_num - order_age) > 15:
                    candidate.heuristic_score -= 0.06
                    candidate.evidence.append(_make_evidence("demographics", "客户年龄差距大", f"档案年龄={order_age}, 录音={rec_age_raw}", "medium"))
            except (ValueError, IndexError):
                pass

    # --- 12. Recording duration vs consultation time window ---
    rec_duration = _payload_duration_seconds(full_payload)
    rec_end_seconds: int | None = None
    if rec_duration is not None and rec_duration > 60:
        rec_end_seconds = (record_seconds or 0) + rec_duration if record_seconds else None
        order_start = _parse_clock_to_seconds(order.jzsj) or _parse_clock_to_seconds(order.fzsj)
        if rec_end_seconds and order_start:
            overlap_start = max(record_seconds or 0, order_start - 1800)
            overlap_end = min(rec_end_seconds, order_start + rec_duration + 3600)
            if overlap_start < overlap_end:
                candidate.heuristic_score += 0.04
                candidate.evidence.append(_make_evidence("duration", "录音时段覆盖接诊时间", f"录音时长 {rec_duration // 60} 分钟，与接诊时段重叠", "low"))

    # --- 13. Staff role-aware time window ---
    role_score, role_evidence = _role_time_window_score(
        record_seconds,
        rec_end_seconds,
        order,
        staff,
        record_time_label=record_time_label,
    )
    if role_score != 0 and role_evidence:
        candidate.heuristic_score += role_score
        candidate.evidence.append(role_evidence)
        if role_score <= _HARD_ROLE_TIME_EXCLUSION_SCORE:
            candidate.hard_excluded = True
        if role_score > 0:
            candidate.reasons.append(f"录音时段符合{_staff_role_label(staff) or '人员'}的预期接待时间窗口")
        else:
            candidate.reasons.append(f"录音时段偏离{_staff_role_label(staff) or '人员'}的预期接待时间窗口")

    _finalize_candidate(candidate)
    return candidate


# ---------------------------------------------------------------------------
# Mutual exclusion — suppress competing candidates when one dominates
# ---------------------------------------------------------------------------

_EXCLUSION_THRESHOLD = 0.65  # min top score to trigger suppression
_EXCLUSION_MARGIN = 0.20  # min margin over second-place
_EXCLUSION_DECAY = 0.55  # multiply non-top candidates' scores


def _candidate_has_strong_identity_signal(candidate: _OrderCandidate | _RecordingCandidate) -> bool:
    labels = {item.label for item in candidate.evidence}
    return bool({"客户编码一致", "客户姓名出现在录音", "客户称呼出现在录音"} & labels)


def _candidate_has_staff_alignment_signal(candidate: _OrderCandidate | _RecordingCandidate) -> bool:
    labels = {item.label for item in candidate.evidence}
    return bool({"顾问一致", "角色编码匹配", "角色姓名匹配"} & labels) and "角色编码不一致" not in labels


def _candidate_has_staff_mismatch_signal(candidate: _OrderCandidate | _RecordingCandidate) -> bool:
    return any(item.label == "角色编码不一致" for item in candidate.evidence)


def _candidate_has_time_alignment_signal(candidate: _OrderCandidate | _RecordingCandidate) -> bool:
    return any(
        item.type in {"time", "role_time"}
        and any(token in item.label for token in ("时间接近", "理想时段", "落入"))
        and not any(token in item.label for token in ("偏差", "偏离"))
        for item in candidate.evidence
    )


def _candidate_has_content_alignment_signal(candidate: _OrderCandidate | _RecordingCandidate) -> bool:
    labels = {item.label for item in candidate.evidence}
    if "术式方案匹配" in labels:
        return True
    content_hits = sum(
        1
        for label in ("咨询项目/关键词匹配", "结构化诉求匹配", "科室/专科匹配", "接待阶段匹配")
        if label in labels
    )
    return content_hits >= 2


def _llm_confidence_floor(candidate: _OrderCandidate | _RecordingCandidate) -> float:
    if candidate.hard_excluded:
        return 0.0
    if _candidate_has_strong_identity_signal(candidate) and _candidate_has_time_alignment_signal(candidate):
        return min(candidate.heuristic_score * 0.70, 0.74)
    if _candidate_has_content_alignment_signal(candidate) and _candidate_has_time_alignment_signal(candidate):
        return min(candidate.heuristic_score * 0.55, 0.68)
    if _candidate_has_strong_identity_signal(candidate):
        return min(candidate.heuristic_score * 0.45, 0.58)
    return 0.0


def _customer_address_analysis_for_name(customer_name: str | None, transcript_text: str | None) -> dict[str, list[str]]:
    signals = _meaningful_addressed_identity_signals(transcript_text)
    matched: list[str] = []
    phonetic: list[str] = []
    conflicting: list[str] = []
    for signal in signals:
        level = _addressed_signal_match_level(signal, customer_name)
        token = str(signal.get("token") or "").strip()
        if not token:
            continue
        if level in {"exact", "surname"}:
            matched.append(token)
        elif level == "phonetic":
            phonetic.append(token)
        else:
            conflicting.append(token)
    return {
        "matched_signals": matched[:6],
        "phonetic_signals": phonetic[:6],
        "conflicting_signals": conflicting[:6],
    }


def _identity_reasons_from_evidence(candidate: _OrderCandidate | _RecordingCandidate) -> list[str]:
    forced: list[str] = []
    for evidence in candidate.evidence:
        if evidence.label == "客户编码一致":
            forced.append("录音中的客户编码与候选客户编码一致")
        elif evidence.label == "客户姓名出现在录音":
            forced.append(evidence.detail)
        elif evidence.label == "客户称呼出现在录音":
            forced.append(evidence.detail)
        elif evidence.label == "客户姓氏近音称呼出现在录音":
            forced.append(evidence.detail)
        elif evidence.label == "咨询师自报姓名匹配":
            forced.append(evidence.detail)
        elif evidence.label == "咨询师自报姓氏匹配":
            forced.append(evidence.detail)
        elif evidence.label == "LLM客户称呼分析":
            forced.append(evidence.detail)
    return _merge_unique_reasons(forced)


def _finalize_llm_candidate_reasons(
    candidate: _OrderCandidate | _RecordingCandidate,
    llm_reasons: list[str],
    *,
    max_count: int = 5,
) -> list[str]:
    forced = _identity_reasons_from_evidence(candidate)
    merged = _merge_unique_reasons(forced, candidate.reasons, llm_reasons)
    if len(merged) <= max_count:
        return merged
    result: list[str] = []
    for reason in forced:
        if reason in merged and reason not in result:
            result.append(reason)
        if len(result) >= max_count:
            return result
    for reason in merged:
        if reason not in result:
            result.append(reason)
        if len(result) >= max_count:
            break
    return result


def _llm_customer_address_payload(item: dict[str, Any]) -> tuple[list[str], list[MatchEvidenceOut]]:
    analysis = item.get("customer_address_analysis")
    if not isinstance(analysis, dict):
        return [], []

    matched = [str(value).strip() for value in analysis.get("matched_signals") or [] if str(value).strip()]
    conflicting = [str(value).strip() for value in analysis.get("conflicting_signals") or [] if str(value).strip()]
    conclusion = str(analysis.get("conclusion") or "").strip()

    reason_parts: list[str] = []
    if matched:
        reason_parts.append(f"LLM 判断更可信的客户称呼是「{'、'.join(matched[:3])}」")
    if conflicting:
        reason_parts.append(f"LLM 同时注意到冲突称呼「{'、'.join(conflicting[:3])}」")
    if conclusion:
        reason_parts.append(conclusion)

    reasons = ["；".join(reason_parts)] if reason_parts else []
    evidence_items = [
        _make_evidence(
            "llm",
            "LLM客户称呼分析",
            reasons[0],
            "high" if matched else "medium",
        )
    ] if reasons else []
    return reasons, evidence_items


def _apply_mutual_exclusion_orders(candidates: list[_OrderCandidate]) -> None:
    """If the top order candidate dominates, decay all others."""
    if len(candidates) < 2:
        return
    candidates.sort(key=lambda c: c.heuristic_score, reverse=True)
    top = candidates[0].heuristic_score
    second = candidates[1].heuristic_score
    strong_identity = _candidate_has_strong_identity_signal(candidates[0]) and _candidate_has_time_alignment_signal(candidates[0])
    threshold = 0.55 if strong_identity else _EXCLUSION_THRESHOLD
    margin = 0.10 if strong_identity else _EXCLUSION_MARGIN
    decay = 0.32 if strong_identity else _EXCLUSION_DECAY
    if top >= threshold and top - second >= margin:
        for c in candidates[1:]:
            c.heuristic_score *= decay
            c.confidence = _heuristic_confidence(c.heuristic_score)
            if c.confidence < 0.35:
                c.decision = "ignore"

    top_candidate = candidates[0]
    if (
        _candidate_has_strong_identity_signal(top_candidate)
        and _candidate_has_staff_alignment_signal(top_candidate)
        and _candidate_has_time_alignment_signal(top_candidate)
    ):
        for c in candidates[1:]:
            if _candidate_has_strong_identity_signal(c):
                continue
            identity_decay = 0.78
            if _candidate_has_staff_mismatch_signal(c):
                identity_decay = 0.72
            elif not _candidate_has_staff_alignment_signal(c):
                identity_decay = 0.78
            c.heuristic_score *= identity_decay
            c.confidence = _heuristic_confidence(c.heuristic_score)
            if c.confidence < 0.35:
                c.decision = "ignore"


def _apply_mutual_exclusion_recordings(candidates: list[_RecordingCandidate]) -> None:
    """If the top recording candidate dominates, decay all others."""
    if len(candidates) < 2:
        return
    candidates.sort(key=lambda c: c.heuristic_score, reverse=True)
    top = candidates[0].heuristic_score
    second = candidates[1].heuristic_score
    strong_identity = _candidate_has_strong_identity_signal(candidates[0]) and _candidate_has_time_alignment_signal(candidates[0])
    threshold = 0.55 if strong_identity else _EXCLUSION_THRESHOLD
    margin = 0.10 if strong_identity else _EXCLUSION_MARGIN
    decay = 0.32 if strong_identity else _EXCLUSION_DECAY
    if top >= threshold and top - second >= margin:
        for c in candidates[1:]:
            c.heuristic_score *= decay
            c.confidence = _heuristic_confidence(c.heuristic_score)
            if c.confidence < 0.35:
                c.decision = "ignore"


def _describe_llm_failure(exc: Exception | None) -> str:
    if exc is None:
        return "LLM 当前不可用"

    message = str(exc).strip()
    lower = message.lower()

    if "llm_api_key" in lower or "设置 llm_api_key" in lower or ("api key" in lower and "llm" in lower):
        return "LLM 未配置"
    if any(token in lower for token in ("timed out", "timeout", "readtimeout", "connecttimeout", "pooltimeout")):
        return "LLM 请求超时"
    if re.search(r"\b(401|403)\b", lower):
        return "LLM 鉴权失败"
    if "429" in lower:
        return "LLM 服务限流"
    if re.search(r"\b(500|502|503|504)\b", lower):
        return "LLM 服务异常"
    if "content 不是字符串" in lower or "json" in lower or "expecting value" in lower:
        return "LLM 返回格式异常"
    return "LLM 接口异常"


async def _llm_rank_orders(
    recording: Recording,
    payload_meta: dict[str, str | int | None] | None,
    transcript_excerpt: str | None,
    candidates: list[_OrderCandidate],
    full_payload: dict | None = None,
    staff_position_text: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    if not candidates:
        return None, None

    analysis_keywords = _extract_analysis_keywords(full_payload)
    demographics = _extract_payload_demographics(full_payload, transcript_excerpt or _transcript_text(recording))
    prompt = {
        "task": "shortlist 已经按到诊日期、录音者编码、录音者职位、分诊时间、接诊时间做过业务预筛选。你现在要在这些少量候选到诊单里，判断哪一个才真正对应这条录音，或者判断都不匹配。请基于相对证据做主判断，不要机械沿用 shortlist 顺序。",
        "reasoning_principles": [
            "综合身份、时间、内容三类证据做相对排序；单靠同日、同顾问或时间接近不能定案",
            "客户姓名、姓氏+称呼、客户编码一致是强身份信号；同音/近音称呼要结合后续称呼、时间和项目判断，不要直接否决",
            "多个称呼冲突时，优先重复次数多、靠后且与业务窗口/接待人互相印证的称呼；开场口误不能作为决定性负证据",
            "程序已命中的姓名/称呼/编码证据必须在 reasons 中保留，不得说原文不存在",
            "顾问/医生/医助身份只用于时间窗口加权：顾问更看分诊附近，医生或医助更看接诊后；门店临时换人不能单独否决",
            "咨询项目、备注、科室、结构化关键词与录音方案需互相印证；高度具体术式一致可显著加分，明显主题冲突需降权",
            "优势候选要拉开置信度；证据都弱时返回 best_candidate_id=null",
            "只有身份、时间、内容三类证据都足够强且明显优于其他候选时，才允许 auto_link；未选候选要写清 excluded_reasons",
        ],
        "recording": {
            "id": recording.id,
            "file_name": recording.file_name,
            "record_date": payload_meta.get("record_date") if payload_meta else None,
            "advisor_code": payload_meta.get("advisor_code") if payload_meta else None,
            "customer_code": payload_meta.get("customer_code") if payload_meta else None,
            "recording_staff_name": recording.staff.name if recording.staff else None,
            "staff_role": _staff_role_label(recording.staff, staff_position_text),
            "staff_position_context": staff_position_text,
            "consultant_self_identification": _extract_consultant_self_identification_signals(_transcript_text(recording))[:4],
            "analysis_keywords": analysis_keywords[:20],
            "demographics": demographics,
            "addressed_identity_signals": _extract_addressed_identity_signals(transcript_excerpt or _transcript_text(recording))[:6],
            "identity_conflicts": _build_overall_identity_conflicts(_transcript_text(recording)),
            "duration_seconds": _payload_duration_seconds(full_payload),
            "transcript_excerpt": transcript_excerpt,
        },
        "candidates": [
            {
                "candidate_id": candidate.visit_order.id,
                "dzdh": candidate.visit_order.dzdh,
                "dzseg": candidate.visit_order.dzseg,
                "customer_code": candidate.visit_order.kunr,
                "customer_name": candidate.visit_order.ninam,
                "customer_gender": candidate.visit_order.customer_gender,
                "customer_age": _compute_age(candidate.visit_order.customer_birthday, candidate.visit_order.crtdt),
                "visit_date": candidate.visit_order.sjrq,
                "advisor_code": candidate.visit_order.fzuer or candidate.visit_order.fzr_id_dq,
                "pre_visit_advisor": candidate.visit_order.advyq,
                "consult_time": candidate.visit_order.jzsj,
                "triage_time": candidate.visit_order.fzsj,
                "consult_project": _order_consult_project(candidate.visit_order),
                "doctor_name": None,
                "advisor_name": candidate.visit_order.advxc_long,
                "pre_visit_advisor_name": candidate.visit_order.advyq_name,
                "customer_type": customer_type_from_visit_order(candidate.visit_order)[1],
                "visit_type": candidate.visit_order.dztyp_txt,
                "visit_status": candidate.visit_order.dzsta_txt,
                "deal_status": candidate.visit_order.jcsta_txt,
                "department": candidate.visit_order.jgks_txt or candidate.visit_order.jgks,
                "visit_purpose": candidate.visit_order.dymd_txt,
                "channel": candidate.visit_order.qdly1_txt,
                "vip_level": candidate.visit_order.kulvl_dq,
                "remarks": [candidate.visit_order.remark_dz],
                "candidate_context": candidate.reasons[:8],
                "customer_address_analysis": _customer_address_analysis_for_name(
                    candidate.visit_order.ninam,
                    _transcript_text(recording),
                ),
                "identity_conflicts": candidate.identity_conflicts[:4],
                "manual_review_reason": candidate.manual_review_reason,
                "derived_evidence": [
                    {
                        "label": evidence.label,
                        "detail": evidence.detail,
                        "strength": evidence.strength,
                    }
                    for evidence in candidate.evidence[:10]
                ],
            }
            for candidate in candidates[:_MAX_LLM_CANDIDATES]
        ],
        "output_schema": {
            "best_candidate_id": "string or null",
            "best_confidence": "0-1 float",
            "auto_link": "boolean",
            "ranked_candidates": [
                {
                    "candidate_id": "string",
                    "confidence": "0-1 float",
                    "reasons": ["string"],
                    "excluded_reasons": ["string"],
                    "customer_address_analysis": {
                        "matched_signals": ["string"],
                        "conflicting_signals": ["string"],
                        "conclusion": "string",
                    },
                    "evidence": [{"label": "string", "detail": "string", "strength": "low|medium|high"}],
                }
            ],
            "summary": "string",
        },
    }
    try:
        raw = await asyncio.to_thread(
            chat_completion,
            "你是严谨的医疗业务匹配助手，只输出 JSON。",
            json.dumps(prompt, ensure_ascii=False),
            temperature=0.1,
            max_tokens=1800,
        )
        parsed = parse_json_response(raw)
    except Exception as exc:
        logger.warning("order-match llm ranking failed recording_id=%s error=%s", recording.id, exc)
        return None, _describe_llm_failure(exc)
    if not isinstance(parsed, dict):
        return None, "LLM 返回格式异常"
    return parsed, None


async def _llm_rank_recordings(
    order: VisitOrder,
    candidates: list[_RecordingCandidate],
    full_payloads: dict[str, dict] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    if not candidates:
        return None, None

    order_keywords = _extract_order_keywords(order)
    prompt = {
        "task": "候选录音已经按到诊日期、录音者编码、录音者职位、分诊时间、接诊时间做过业务预筛选。请在这些少量录音里判断哪条最可能对应当前到诊单，或者判断都不匹配。你需要做相对判断，而不是机械沿用 shortlist 顺序。",
        "reasoning_principles": [
            "综合身份、时间、内容三类证据做相对排序；单靠同日、同顾问或时间接近不能定案",
            "客户姓名、姓氏+称呼、客户编码一致是强身份信号；同音/近音称呼要结合后续称呼、时间和项目判断，不要直接否决",
            "多个称呼冲突时，优先重复次数多、靠后且与业务窗口/接待人互相印证的称呼；开场口误不能作为决定性负证据",
            "程序已命中的姓名/称呼/编码证据必须在 reasons 中保留，不得说原文不存在",
            "顾问/医生/医助身份只用于时间窗口加权：顾问更看分诊附近，医生或医助更看接诊后；门店临时换人不能单独否决",
            "咨询项目、备注、科室、结构化关键词与录音方案需互相印证；高度具体术式一致可显著加分，明显主题冲突需降权",
            "优势候选要拉开置信度；证据都弱时返回 best_candidate_id=null",
            "只有身份、时间、内容三类证据都足够强且明显优于其他候选时，才允许 auto_link；未选候选要写清 excluded_reasons",
        ],
        "visit_order": {
            "id": order.id,
            "dzdh": order.dzdh,
            "dzseg": order.dzseg,
            "visit_date": order.sjrq,
            "customer_code": order.kunr,
            "customer_name": order.ninam,
            "customer_gender": order.customer_gender,
            "customer_age": _compute_age(order.customer_birthday, order.crtdt),
            "advisor_code": order.fzuer or order.fzr_id_dq,
            "pre_visit_advisor": order.advyq,
            "consult_time": order.jzsj,
            "triage_time": order.fzsj,
            "consult_project": _order_consult_project(order),
            "doctor_name": None,
            "advisor_name": order.advxc_long,
            "pre_visit_advisor_name": order.advyq_name,
            "customer_type": customer_type_from_visit_order(order)[1],
            "visit_type": order.dztyp_txt,
            "visit_status": order.dzsta_txt,
            "deal_status": order.jcsta_txt,
            "department": order.jgks_txt or order.jgks,
            "visit_purpose": order.dymd_txt,
            "channel": order.qdly1_txt,
            "vip_level": order.kulvl_dq,
            "order_keywords": order_keywords[:15],
            "remarks": [order.remark_dz],
            "identity_conflicts": _build_overall_identity_conflicts(_transcript_text(candidates[0].recording) if candidates else None),
        },
        "candidates": [
            {
                "candidate_id": candidate.recording.id,
                "file_name": candidate.recording.file_name,
                "recording_staff_name": candidate.recording.staff.name if candidate.recording.staff else None,
                "staff_role": _staff_role_label(candidate.recording.staff),
                "advisor_code": candidate.payload_meta.get("advisor_code") if candidate.payload_meta else None,
                "customer_code": candidate.payload_meta.get("customer_code") if candidate.payload_meta else None,
                "record_date": candidate.payload_meta.get("record_date") if candidate.payload_meta else None,
                "consultant_self_identification": _extract_consultant_self_identification_signals(_transcript_text(candidate.recording))[:4],
                "analysis_keywords": _extract_analysis_keywords(
                    _lookup_by_file_name(full_payloads or {}, candidate.recording.file_name)
                )[:15],
                "demographics": _extract_payload_demographics(
                    _lookup_by_file_name(full_payloads or {}, candidate.recording.file_name),
                    _transcript_text(candidate.recording),
                ),
                "duration_seconds": _payload_duration_seconds(
                    _lookup_by_file_name(full_payloads or {}, candidate.recording.file_name)
                ),
                "transcript_excerpt": _transcript_excerpt(_transcript_text(candidate.recording)),
                "addressed_identity_signals": _extract_addressed_identity_signals(_transcript_text(candidate.recording))[:6],
                "customer_address_analysis": _customer_address_analysis_for_name(
                    order.ninam,
                    _transcript_text(candidate.recording),
                ),
                "candidate_context": candidate.reasons[:8],
                "identity_conflicts": candidate.identity_conflicts[:4],
                "manual_review_reason": candidate.manual_review_reason,
                "derived_evidence": [
                    {
                        "label": evidence.label,
                        "detail": evidence.detail,
                        "strength": evidence.strength,
                    }
                    for evidence in candidate.evidence[:10]
                ],
            }
            for candidate in candidates[:_MAX_LLM_CANDIDATES]
        ],
        "output_schema": {
            "best_candidate_id": "string or null",
            "best_confidence": "0-1 float",
            "auto_link": "boolean",
            "ranked_candidates": [
                {
                    "candidate_id": "string",
                    "confidence": "0-1 float",
                    "reasons": ["string"],
                    "excluded_reasons": ["string"],
                    "customer_address_analysis": {
                        "matched_signals": ["string"],
                        "conflicting_signals": ["string"],
                        "conclusion": "string",
                    },
                    "evidence": [{"label": "string", "detail": "string", "strength": "low|medium|high"}],
                }
            ],
            "summary": "string",
        },
    }
    try:
        raw = await asyncio.to_thread(
            chat_completion,
            "你是严谨的医疗业务匹配助手，只输出 JSON。",
            json.dumps(prompt, ensure_ascii=False),
            temperature=0.1,
            max_tokens=1800,
        )
        parsed = parse_json_response(raw)
    except Exception as exc:
        logger.warning("recording-match llm ranking failed visit_order_id=%s error=%s", order.id, exc)
        return None, _describe_llm_failure(exc)
    if not isinstance(parsed, dict):
        return None, "LLM 返回格式异常"
    return parsed, None


def _merge_llm_order_result(candidates: list[_OrderCandidate], llm_result: dict[str, Any] | None) -> str | None:
    if not llm_result:
        return None
    for candidate in candidates:
        if candidate.hard_excluded:
            continue
        candidate.method = "llm"
        candidate.confidence = max(min(candidate.confidence, 0.25), _llm_confidence_floor(candidate))
        candidate.decision = "ignore"
    candidate_map = {candidate.visit_order.id: candidate for candidate in candidates}
    for item in llm_result.get("ranked_candidates") or []:
        if not isinstance(item, dict):
            continue
        candidate = candidate_map.get(str(item.get("candidate_id") or ""))
        if not candidate:
            continue
        if candidate.hard_excluded:
            continue
        confidence = float(item.get("confidence") or 0.0)
        candidate.confidence = max(min(max(confidence, 0.0), 0.999), _llm_confidence_floor(candidate))
        candidate.method = "llm"
        candidate.decision = "recommend" if candidate.confidence >= _LLM_SUGGEST_THRESHOLD else "ignore"
        reasons = [str(reason).strip() for reason in item.get("reasons") or [] if str(reason).strip()]
        excluded_reasons = [
            str(reason).strip() for reason in item.get("excluded_reasons") or [] if str(reason).strip()
        ]
        if excluded_reasons:
            candidate.excluded_reasons = _merge_unique_reasons(candidate.excluded_reasons, excluded_reasons)[:5]
        evidence_items = []
        for evidence in item.get("evidence") or []:
            if not isinstance(evidence, dict):
                continue
            label = _clean_text(str(evidence.get("label") or ""))
            detail = _clean_text(str(evidence.get("detail") or ""))
            if not label or not detail:
                continue
            evidence_items.append(
                _make_evidence(
                    "llm",
                    label,
                    detail,
                    str(evidence.get("strength") or "medium"),
                )
            )
        address_reasons, address_evidence = _llm_customer_address_payload(item)
        reasons = _merge_unique_reasons(reasons, address_reasons)
        evidence_items.extend(address_evidence)
        if evidence_items:
            candidate.evidence = _merge_unique_evidence(candidate.evidence, evidence_items)[:10]
        if reasons or candidate.reasons:
            candidate.reasons = _finalize_llm_candidate_reasons(candidate, reasons, max_count=5)
    _populate_excluded_reasons(candidates)
    summary = _replace_summary_candidate_refs(
        str(llm_result.get("summary") or ""),
        {candidate.visit_order.id: f"到诊单 {candidate.visit_order.dzdh}" for candidate in candidates},
    )
    return summary


def _merge_llm_recording_result(candidates: list[_RecordingCandidate], llm_result: dict[str, Any] | None) -> str | None:
    if not llm_result:
        return None
    for candidate in candidates:
        if candidate.hard_excluded:
            continue
        candidate.method = "llm"
        candidate.confidence = max(min(candidate.confidence, 0.25), _llm_confidence_floor(candidate))
        candidate.decision = "ignore"
    candidate_map = {candidate.recording.id: candidate for candidate in candidates}
    for item in llm_result.get("ranked_candidates") or []:
        if not isinstance(item, dict):
            continue
        candidate = candidate_map.get(str(item.get("candidate_id") or ""))
        if not candidate:
            continue
        if candidate.hard_excluded:
            continue
        confidence = float(item.get("confidence") or 0.0)
        candidate.confidence = max(min(max(confidence, 0.0), 0.999), _llm_confidence_floor(candidate))
        candidate.method = "llm"
        candidate.decision = "recommend" if candidate.confidence >= _LLM_SUGGEST_THRESHOLD else "ignore"
        reasons = [str(reason).strip() for reason in item.get("reasons") or [] if str(reason).strip()]
        excluded_reasons = [
            str(reason).strip() for reason in item.get("excluded_reasons") or [] if str(reason).strip()
        ]
        if excluded_reasons:
            candidate.excluded_reasons = _merge_unique_reasons(candidate.excluded_reasons, excluded_reasons)[:5]
        evidence_items = []
        for evidence in item.get("evidence") or []:
            if not isinstance(evidence, dict):
                continue
            label = _clean_text(str(evidence.get("label") or ""))
            detail = _clean_text(str(evidence.get("detail") or ""))
            if not label or not detail:
                continue
            evidence_items.append(
                _make_evidence(
                    "llm",
                    label,
                    detail,
                    str(evidence.get("strength") or "medium"),
                )
            )
        address_reasons, address_evidence = _llm_customer_address_payload(item)
        reasons = _merge_unique_reasons(reasons, address_reasons)
        evidence_items.extend(address_evidence)
        if evidence_items:
            candidate.evidence = _merge_unique_evidence(candidate.evidence, evidence_items)[:10]
        if reasons or candidate.reasons:
            candidate.reasons = _finalize_llm_candidate_reasons(candidate, reasons, max_count=5)
    _populate_excluded_reasons(candidates)
    summary = _replace_summary_candidate_refs(
        str(llm_result.get("summary") or ""),
        {candidate.recording.id: f"录音 {candidate.recording.file_name}" for candidate in candidates},
    )
    return summary


async def analyze_recording_visit_order_match(
    db: AsyncSession,
    recording_id: str,
    *,
    apply_auto: bool = True,
    use_llm: bool = True,
    scope: PermissionScope | None = None,
) -> RecordingVisitOrderMatchOut | None:
    recording_load_options = [
        selectinload(Recording.transcript),
        selectinload(Recording.staff),
        selectinload(Recording.segments),
        selectinload(Recording.visit).selectinload(Visit.customer),
        selectinload(Recording.visit_links).selectinload(RecordingVisitLink.visit).selectinload(Visit.customer),
    ]

    async def load_recording_for_match(*, populate_existing: bool = False) -> Recording | None:
        stmt = select(Recording).where(Recording.id == recording_id).options(*recording_load_options)
        if populate_existing:
            stmt = stmt.execution_options(populate_existing=True)
        return (await db.execute(stmt)).scalars().first()

    def resolve_linked_context(current_recording: Recording) -> tuple[Visit | None, list[RecordingVisitLink], list[str], list[str], str | None, str | None]:
        current_linked_visit = current_recording.visit
        current_linked_visit_links = ordered_recording_visit_links(current_recording)
        current_linked_visit_ids = [link.visit_id for link in current_linked_visit_links if link.visit_id]
        current_linked_visit_order_refs = [
            ref
            for ref in (_visit_order_ref(link.visit) for link in current_linked_visit_links if link.visit is not None)
            if ref
        ]
        current_linked_order_no = current_linked_visit.external_visit_order_no if current_linked_visit else None
        current_linked_order_seg = current_linked_visit.external_visit_order_seg if current_linked_visit else None
        return (
            current_linked_visit,
            current_linked_visit_links,
            current_linked_visit_ids,
            current_linked_visit_order_refs,
            current_linked_order_no,
            current_linked_order_seg,
        )

    def resolve_sync_context(current_recording: Recording) -> tuple[str | None, set[str]]:
        current_advisor_code_for_sync = advisor_code or _clean_text(current_recording.staff.external_account if current_recording.staff else None)
        current_hospital_codes_for_sync = {
            _clean_text(current_recording.staff.hospital_code if current_recording.staff else None),
        } - {None, ""}
        return current_advisor_code_for_sync, current_hospital_codes_for_sync

    def build_visit_order_stmt(current_recording: Recording, current_staff_position_text: str | None):
        current_order_date_condition = _recording_order_date_condition(record_date)
        current_order_stmt = select(VisitOrder).where(current_order_date_condition) if current_order_date_condition is not None else select(VisitOrder)
        if scope is not None:
            scope_condition = visit_order_scope_condition(scope)
            department_assistant_scope_condition = _department_assistant_visit_order_scope_condition(
                current_recording.staff,
                current_staff_position_text,
            )
            if department_assistant_scope_condition is not None:
                current_order_stmt = current_order_stmt.where(or_(scope_condition, department_assistant_scope_condition))
            else:
                current_order_stmt = current_order_stmt.where(scope_condition)
        return current_order_stmt

    recording = await load_recording_for_match()
    if recording is None:
        return None

    payload_map = _build_payload_metadata_map()
    payload_meta = _lookup_by_file_name(payload_map, recording.file_name)
    full_payloads = _load_full_payloads()
    full_payload = _lookup_by_file_name(full_payloads, recording.file_name)
    transcript_text = _transcript_text(recording)
    transcript_excerpt = _transcript_excerpt(transcript_text)
    identity_conflicts = _build_overall_identity_conflicts(transcript_text)
    manual_review_required = bool(identity_conflicts)
    manual_review_reason = _manual_review_reason_from_conflicts(identity_conflicts)
    record_date = _clean_text(str(payload_meta.get("record_date") or "")) if payload_meta else None
    advisor_code = _clean_text(str(payload_meta.get("advisor_code") or "")) if payload_meta else None
    customer_code = _clean_text(str(payload_meta.get("customer_code") or "")) if payload_meta else None
    staff_position_text = await _load_staff_position_text(db, recording.staff)
    is_department_assistant = _is_department_assistant_staff(recording.staff, staff_position_text)

    (
        linked_visit,
        _linked_visit_links,
        linked_visit_ids,
        linked_visit_order_refs,
        linked_order_no,
        linked_order_seg,
    ) = resolve_linked_context(recording)

    if not record_date:
        record_date = recording.created_at.date().isoformat() if recording.created_at else None

    advisor_code_for_sync, hospital_codes_for_sync = resolve_sync_context(recording)

    order_stmt = build_visit_order_stmt(recording, staff_position_text)
    orders = (await db.execute(order_stmt.order_by(VisitOrder.dzdh.desc()))).scalars().all()
    if not orders and record_date and (advisor_code_for_sync or (is_department_assistant and hospital_codes_for_sync)):
        await sync_visit_orders_for_context(
            db,
            date_strings={record_date},
            advisor_codes={advisor_code_for_sync} if advisor_code_for_sync else set(),
            hospital_codes=hospital_codes_for_sync or None,
        )
        recording = await load_recording_for_match(populate_existing=True)
        if recording is None:
            return None
        staff_position_text = await _load_staff_position_text(db, recording.staff)
        is_department_assistant = _is_department_assistant_staff(recording.staff, staff_position_text)
        (
            linked_visit,
            _linked_visit_links,
            linked_visit_ids,
            linked_visit_order_refs,
            linked_order_no,
            linked_order_seg,
        ) = resolve_linked_context(recording)
        advisor_code_for_sync, hospital_codes_for_sync = resolve_sync_context(recording)
        order_stmt = build_visit_order_stmt(recording, staff_position_text)
        orders = (await db.execute(order_stmt.order_by(VisitOrder.dzdh.desc()))).scalars().all()

    # 按 DZDH 聚合所有行项目号，用于向前端标记已合并的多行项目到诊单
    dzdh_segments: dict[str, list[str]] = {}
    dzdh_orders: dict[str, list[VisitOrder]] = {}
    for _o in orders:
        dzdh_orders.setdefault(_o.dzdh, []).append(_o)
        dzdh_segments.setdefault(_o.dzdh, [])
        if _o.dzseg and _o.dzseg not in dzdh_segments[_o.dzdh]:
            dzdh_segments[_o.dzdh].append(_o.dzseg)
    for _orders in dzdh_orders.values():
        _orders.sort(key=lambda order: (order.dzseg or "", order.fzdh or "", order.id or ""))
    for _segs in dzdh_segments.values():
        _segs.sort()

    visit_stmt = select(Visit).options(selectinload(Visit.recordings)).where(Visit.external_visit_order_no.is_not(None))
    visit_stmt = visit_stmt.options(selectinload(Visit.recording_links))
    if scope is not None:
        visit_stmt = visit_stmt.where(visit_scope_condition(scope))
    visits = (await db.execute(visit_stmt)).scalars().all()
    visit_map = {(visit.external_visit_order_no, visit.external_visit_order_seg): visit for visit in visits}
    # 合并后同一 DZDH 只有一条 Visit，用 dzdh 索引做回退查找
    visit_by_dzdh: dict[str, Visit] = {}
    for v in visits:
        if v.external_visit_order_no and v.external_visit_order_no not in visit_by_dzdh:
            visit_by_dzdh[v.external_visit_order_no] = v

    shortlisted_orders = _shortlist_orders_for_recording(
        recording,
        orders,
        payload_meta,
        recording.staff,
        full_payload,
        staff_position_text=staff_position_text,
    )
    candidates: list[_OrderCandidate] = []
    for order, shortlist_facts in shortlisted_orders:
        candidate = _score_order_for_recording(
            recording,
            payload_meta,
            order,
            transcript_text,
            full_payload,
            staff=recording.staff,
            staff_position_text=staff_position_text,
        )
        candidate.local_visit = visit_map.get((order.dzdh, order.dzseg)) or visit_by_dzdh.get(order.dzdh)
        companion_orders = _find_companion_orders(order, orders)
        candidate.companion_customer_codes = [item for item in _extract_companion_customer_codes(order) if item]
        candidate.companion_visit_order_refs = [_visit_order_ref(item) for item in companion_orders]
        candidate.companion_local_visit_ids = [visit.id for visit in [(visit_map.get((item.dzdh, item.dzseg)) or visit_by_dzdh.get(item.dzdh)) for item in companion_orders] if visit is not None]
        companion_score, companion_evidence, companion_reason = _companion_visit_signal(transcript_text, order, companion_orders)
        if companion_score > 0 and companion_evidence and companion_reason:
            candidate.heuristic_score += companion_score
            candidate.evidence.append(companion_evidence)
            candidate.reasons.append(companion_reason)
        candidate.method = "shortlist_fallback"
        candidate.reasons = _merge_unique_reasons(shortlist_facts["reasons"], candidate.reasons)[:8]
        candidate.evidence = _merge_unique_evidence(shortlist_facts["evidence"], candidate.evidence)[:10]
        candidates.append(candidate)

    if not candidates:
        no_candidate_summary = "未找到可供推荐的到诊单候选。"
        if record_date:
            latest_remote_date = fetch_latest_remote_visit_order_date(hospital_codes_for_sync) if hospital_codes_for_sync else None
            if latest_remote_date and latest_remote_date < record_date:
                no_candidate_summary = (
                    f"{record_date} 的到诊单尚未同步到系统（当前源数据最新到 {latest_remote_date}），暂时无法推荐。"
                )
            else:
                no_candidate_summary = f"{record_date} 当天暂无可供推荐的到诊单候选。"
        return RecordingVisitOrderMatchOut(
            recording_id=recording.id,
            file_name=recording.file_name,
            record_date=record_date,
            advisor_code=advisor_code,
            customer_code=customer_code,
            customer_name=linked_visit.customer.name if linked_visit and linked_visit.customer else None,
            linked_visit_id=recording.visit_id,
            linked_visit_ids=linked_visit_ids,
            linked_visit_order_refs=linked_visit_order_refs,
            linked_visit_order_no=linked_order_no,
            linked_visit_order_seg=linked_order_seg,
            auto_applied=False,
            identity_conflicts=identity_conflicts,
            manual_review_required=manual_review_required,
            manual_review_reason=manual_review_reason,
            summary=no_candidate_summary,
            analyzed_at=_utcnow_iso(),
            candidates=[],
        )

    llm_result: dict[str, Any] | None = None
    llm_unavailable_reason: str | None = None
    llm_summary: str | None = None
    if use_llm:
        llm_candidates = [candidate for candidate in candidates if not candidate.hard_excluded]
        llm_result, llm_unavailable_reason = await _llm_rank_orders(
            recording,
            payload_meta,
            transcript_excerpt,
            llm_candidates,
            full_payload,
            staff_position_text=staff_position_text,
        )
        llm_summary = _merge_llm_order_result(candidates, llm_result)
    if not use_llm or llm_result is None:
        _apply_mutual_exclusion_orders(candidates)
        _populate_excluded_reasons(candidates)
    candidates.sort(key=lambda item: (0 if (item.local_visit and item.local_visit.recordings) else 1, item.confidence, item.heuristic_score, item.visit_order.dzdh), reverse=True)

    fallback_recommended, fallback_forced = _ensure_recommended_order_candidate(candidates)
    _promote_candidate_to_front(candidates, fallback_recommended)
    _populate_excluded_reasons(candidates)

    best = candidates[0]
    second_confidence = candidates[1].confidence if len(candidates) > 1 else 0.0
    auto_applied = False
    if (
        apply_auto
        and best.local_visit is not None
        and not manual_review_required
        and best.method == "llm"
        and bool(llm_result and llm_result.get("auto_link"))
        and best.confidence >= _LLM_AUTO_THRESHOLD
        and best.confidence - second_confidence >= _LLM_AUTO_MARGIN
    ):
        best.decision = "auto"
        target_visit_ids = [best.local_visit.id, *best.companion_local_visit_ids, *linked_visit_ids]
        primary_visit_id = recording.visit_id if recording.visit_id in target_visit_ids else best.local_visit.id
        target_visit_id_count = len({visit_id for visit_id in target_visit_ids if visit_id})
        target_has_other_recordings = any(
            link.recording_id != recording.id
            for link in ordered_visit_recording_links(best.local_visit)
        )
        if target_visit_id_count > 1 or target_has_other_recordings:
            best.decision = "recommended"
            best.reasons = _merge_unique_reasons(
                best.reasons,
                ["命中多到诊单或目标到诊单已有录音，需人工二次确认后再关联。"],
            )
        elif set(linked_visit_ids) != set(target_visit_ids) or recording.visit_id != primary_visit_id:
            await sync_recording_visit_links(
                db,
                recording,
                target_visit_ids,
                primary_visit_id=primary_visit_id,
                source="auto_match",
            )
            await db.commit()
            auto_applied = True
            refreshed_recording = await load_recording_for_match(populate_existing=True)
            if refreshed_recording is not None:
                recording = refreshed_recording

    (
        linked_visit,
        _linked_visit_links,
        linked_visit_ids,
        linked_visit_order_refs,
        linked_order_no,
        linked_order_seg,
    ) = resolve_linked_context(recording)

    visible_candidates = candidates[:_SHORTLIST_LIMIT]

    summary = llm_summary or "已先按日期、录音者编码、职位和分诊/接诊时间缩小到诊单候选，再结合录音内容给出推荐。"
    if not use_llm:
        summary = "当前为快速推荐结果：已先按日期、录音者编码、职位和分诊/接诊时间缩小候选，并用启发式信号给出即时推荐。"
    elif llm_result is None:
        llm_notice = llm_unavailable_reason or "LLM 当前不可用"
        summary = f"{llm_notice}，已先按日期、录音者编码、职位和分诊/接诊时间缩小候选，并用辅助信号给出兜底推荐。"
    if manual_review_required and manual_review_reason:
        summary = f"需人工确认：{manual_review_reason}。{summary}"
    if fallback_forced and not auto_applied:
        summary = f"{summary} 当前已保底推荐综合证据最强的到诊单，建议人工复核。"
    if auto_applied:
        summary = f"{summary} 系统已按高置信度结果自动关联。"

    return RecordingVisitOrderMatchOut(
        recording_id=recording.id,
        file_name=recording.file_name,
        record_date=record_date,
        advisor_code=advisor_code,
        customer_code=customer_code,
        customer_name=linked_visit.customer.name if linked_visit and linked_visit.customer else None,
        linked_visit_id=recording.visit_id,
        linked_visit_ids=linked_visit_ids,
        linked_visit_order_refs=linked_visit_order_refs,
        linked_visit_order_no=(best.visit_order.dzdh if auto_applied else linked_order_no),
        linked_visit_order_seg=(best.visit_order.dzseg if auto_applied else linked_order_seg),
        auto_applied=auto_applied,
        identity_conflicts=identity_conflicts,
        manual_review_required=manual_review_required,
        manual_review_reason=manual_review_reason,
        summary=summary,
        analyzed_at=_utcnow_iso(),
        candidates=[_candidate_order_to_out(candidate, dzdh_segments, recording.id, dzdh_orders) for candidate in visible_candidates],
    )


async def analyze_visit_order_recording_match(
    db: AsyncSession,
    visit_order_id: str,
) -> VisitOrderRecordingMatchOut | None:
    order = await db.get(VisitOrder, visit_order_id)
    if order is None:
        return None

    # 合并后同一 DZDH 只有一条 Visit，先按 (dzdh, dzseg) 查，查不到按 dzdh 回退
    local_visit = (
        await db.execute(
            select(Visit)
            .options(selectinload(Visit.recording_links).selectinload(RecordingVisitLink.recording))
            .where(
                and_(
                    Visit.external_visit_order_no == order.dzdh,
                    Visit.external_visit_order_seg == order.dzseg,
                )
            )
        )
    ).scalars().first()
    if local_visit is None:
        local_visit = (
            await db.execute(
                select(Visit)
                .options(selectinload(Visit.recording_links).selectinload(RecordingVisitLink.recording))
                .where(Visit.external_visit_order_no == order.dzdh)
            )
        ).scalars().first()

    payload_map = _build_payload_metadata_map()
    full_payloads = _load_full_payloads()

    recording_stmt = (
        select(Recording)
        .options(
            selectinload(Recording.staff),
            selectinload(Recording.transcript),
            selectinload(Recording.visit).selectinload(Visit.customer),
        )
        .where(
            or_(
                Recording.created_at.is_not(None),
                Recording.id.is_not(None),
            )
        )
        .order_by(Recording.created_at.desc())
    )
    recordings = (await db.execute(recording_stmt)).scalars().all()
    transcript_texts = [_transcript_text(recording) for recording in recordings]
    identity_conflicts = _merge_unique_reasons(*[_build_overall_identity_conflicts(text) for text in transcript_texts if text])
    manual_review_required = bool(identity_conflicts)
    manual_review_reason = _manual_review_reason_from_conflicts(identity_conflicts)

    candidates: list[_RecordingCandidate] = []
    same_day_recordings: list[Recording] = []
    for recording in recordings:
        payload_meta = _lookup_by_file_name(payload_map, recording.file_name)
        record_date = _clean_text(str(payload_meta.get("record_date") or "")) if payload_meta else None
        fallback_date = recording.created_at.date().isoformat() if recording.created_at else None
        if order.sjrq and (record_date or fallback_date) and (record_date or fallback_date) != order.sjrq:
            continue
        same_day_recordings.append(recording)

    shortlisted_recordings = _shortlist_recordings_for_order(same_day_recordings, payload_map, order, full_payloads)
    for recording, payload_meta, shortlist_facts in shortlisted_recordings:
        candidate = _score_recording_for_order(
            recording,
            payload_meta,
            order,
            _transcript_text(recording),
            _lookup_by_file_name(full_payloads, recording.file_name),
            staff=recording.staff,
        )
        candidate.method = "shortlist_fallback"
        candidate.reasons = _merge_unique_reasons(shortlist_facts["reasons"], candidate.reasons)[:8]
        candidate.evidence = _merge_unique_evidence(shortlist_facts["evidence"], candidate.evidence)[:10]
        candidates.append(candidate)

    llm_candidates = [candidate for candidate in candidates if not candidate.hard_excluded]
    llm_result, llm_unavailable_reason = await _llm_rank_recordings(order, llm_candidates, full_payloads)
    llm_summary = _merge_llm_recording_result(candidates, llm_result)
    if llm_result is None:
        _apply_mutual_exclusion_recordings(candidates)
        _populate_excluded_reasons(candidates)

    candidates.sort(key=lambda item: (item.confidence, item.heuristic_score, item.recording.created_at), reverse=True)
    candidates = candidates[:_SHORTLIST_LIMIT]
    summary = llm_summary or "已先按到诊日期、录音者编码、职位和分诊/接诊时间缩小录音候选，再结合录音内容给出推荐。"
    if llm_result is None:
        llm_notice = llm_unavailable_reason or "LLM 当前不可用"
        summary = f"{llm_notice}，已先按到诊日期、录音者编码、职位和分诊/接诊时间缩小候选，并用辅助信号给出兜底推荐。"
    if manual_review_required and manual_review_reason:
        summary = f"需人工确认：{manual_review_reason}。{summary}"

    linked_recording_ids = [link.recording_id for link in ordered_visit_recording_links(local_visit)] if local_visit else []
    customer_type_code, customer_type_label = customer_type_from_visit_order(order)
    return VisitOrderRecordingMatchOut(
        visit_order_id=order.id,
        local_visit_id=local_visit.id if local_visit else None,
        dzdh=order.dzdh,
        dzseg=order.dzseg,
        visit_date=order.sjrq,
        advisor_code=order.fzuer or order.fzr_id_dq,
        customer_code=order.kunr,
        customer_name=order.ninam,
        customer_type_code=customer_type_code,
        customer_type_label=customer_type_label,
        linked_recording_ids=linked_recording_ids,
        identity_conflicts=identity_conflicts,
        manual_review_required=manual_review_required,
        manual_review_reason=manual_review_reason,
        summary=summary,
        analyzed_at=_utcnow_iso(),
        candidates=[_candidate_recording_to_out(candidate) for candidate in candidates],
    )
