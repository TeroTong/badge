"""按过渡期规则同步到诊单数据并与本地录音关联。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, or_, select, text, tuple_
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.analysis.customer_profile_score_sync import refresh_customer_profile_scores
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import Customer, Recording, RecordingVisitLink, SapHanaVisitOrder, Staff, Visit, VisitOrder
from smart_badge_api.schemas.visit_order import VisitOrderSyncResult
from smart_badge_api.visit_linking import sync_recording_visit_links

logger = logging.getLogger("smart_badge.visit_order_sync")
_CUSTOMER_CENTER_SYNC_LOCK_KEY = "smart_badge_customer_center_visit_order_sync"


def _iter_batches(items: list, size: int = 500):
    for index in range(0, len(items), size):
        yield items[index:index + size]


async def _try_acquire_customer_center_sync_lock(db: AsyncSession) -> bool:
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        result = await db.execute(
            text("select pg_try_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": _CUSTOMER_CENTER_SYNC_LOCK_KEY},
        )
        return bool(result.scalar_one())
    return True


try:
    VALIDATED_DIR = Path(__file__).resolve().parents[4] / "validated"
except IndexError:
    VALIDATED_DIR = Path.cwd() / "validated"

# 兼容 visit_orders 宽表的核心字段
_VISIT_ORDER_COLUMNS = [
    "dzdh", "dzseg", "sjrq", "jgbm",
    "fzuer", "fzuer_long", "advxc", "advxc_long", "advyq", "advyq_name",
    "kunr", "ninam", "kusex", "kusex_txt", "yydh", "yyuer",
    "kutyp_dq", "kutyp_dq_txt", "kut30_dq", "kut30_dq_txt", "kusta_dq", "kusta_dq_txt",
    "khlx", "khlx_yg", "khlx_t30", "khlx2", "kulvl_dq",
    "vipkf", "d_fzuer", "fzr_id_dq", "d_vipkf",
    "fzdh", "fzsj", "fzrq", "fzsta", "fzsta_txt",
    "fzrid", "ddsc", "bhkx", "assxc",
    "jzsj", "jzrq", "jgks", "jgks_txt",
    "dztyp", "dztyp_txt", "dzsta", "dzsta_txt", "dzly", "dymd",
    "jcsta", "jcsta_txt",
    "kusrc", "kusrc2", "qdly1_txt", "qdly2_txt", "qd1jfl", "qd2jfl",
    "remark_dz", "bjzx",
    "hylx_yg", "dymd_txt", "dzly_txt",
    "crtdt", "crttm",
    "fzr_name_dq",
    "jdrq",
]

_LEGACY_REMOTE_SELECT_COLUMNS = [
    "dzdh", "dzseg", "sjrq", "yybm", "yyjc", "jgbm",
    "fzuer", "fzr_name_dq as fzuer_long", "advxc", "advxc_name as advxc_long", "advyq", "advyq_name",
    "khbm as kunr", "khxm_jg as ninam", "null as kusex", "null as kusex_txt", "yydh", "yyuer",
    "khlx as kutyp_dq", "khlx as kutyp_dq_txt", "khlx_t30 as kut30_dq", "khlx_t30 as kut30_dq_txt", "khlx2 as kusta_dq", "khlx2 as kusta_dq_txt",
    "khlx", "khlx_yg", "khlx_t30", "khlx2", "kulvl_dq",
    "kf_id as vipkf", "fzr_id_dq as d_fzuer", "fzr_id_dq", "kf_id_dq as d_vipkf",
    "fzdh", "fzsj", "fzrq", "fzsta", "fzsta_txt",
    "fzrid", "ddsc", "bhkx", "assxc",
    "jzsj", "jzrq", "jgks_txt as jgks", "jgks_txt",
    "dztyp", "dztyp_txt", "dzsta", "dzsta_txt", "null as dzly", "null as dymd",
    "jcsta", "jcsta_txt",
    "qdly1_txt as kusrc", "qdly2_txt as kusrc2", "qdly1_txt", "qdly2_txt", "qd1jfl", "qd2jfl",
    "remark_dz", "null as bjzx",
    "hylx_yg", "dymd_txt", "dzly_txt",
    "crtdt", "crttm",
    "fzr_name_dq",
    "jdrq",
]

_DZSTA_LABELS = {
    "1": "未分诊",
    "A": "已确认",
    "C": "已分诊",
    "D": "已取消",
}
_KUSEX_LABELS = {
    "M": "男",
    "F": "女",
}
_CUSTOMER_BIRTHDAY_PAYLOAD_KEYS = (
    "kubsd",
    "csrq",
    "birthdate",
    "birth_date",
    "birthday",
    "customer_birthdate",
    "customer_birthday",
    "cust_birthdate",
    "cust_birthday",
    "gbdat",
    "kug_bdat",
    "kugbdat",
    "出生日期",
    "出生年月日",
    "生日",
)
_CUSTOMER_AGE_PAYLOAD_KEYS = (
    "age",
    "customer_age",
    "cust_age",
    "nl",
    "nianling",
    "年龄",
    "客户年龄",
    "顾客年龄",
)
_KUTYP_DQ_LABELS = {
    "Q": "潜客/新客",
    "V": "会员/老客",
}
_KUT30_DQ_LABELS = {
    "Q": "潜客/新客",
    "V": "会员/老客",
}
_KUSTA_DQ_LABELS = {
    "Q1": "建档未上门",
    "Q2": "上门未成交",
    "Q3": "体验会员",
    "V1": "付费会员",
}
_JGKS_LABELS = {
    "JGKS01": "口腔科",
    "JGKS02": "皮肤科",
    "JGKS03": "外科",
    "JGKS04": "微整科",
    "JGKS05": "中医",
    "JGKS06": "纹绣",
    "JGKS07": "会籍",
    "JGKS08": "毛发移植科",
    "JGKS09": "非手术",
    "JGKS10": "私密中心",
    "JGKS11": "纤体中心",
    "JGKS12": "植发中心",
    "JGKS13": "形体私密中心",
    "JGKS14": "SPA中心",
}
_DZLY_LABELS = {
    "Y": "已预约",
    "N": "未预约",
}
_DYMD_LABELS = {
    "A": "咨询",
    "B": "治疗",
    "C": "手术",
    "D": "复查",
    "X": "未到院购买",
    "Z": "其他",
}
_DZTYP_LABELS = {
    "1": "初诊",
    "2": "复诊",
    "3": "再咨",
    "4": "诊疗",
    "5": "未到院购买",
    "Z": "其他",
}
_FZSTA_LABELS = {
    "1": "待接诊",
    "A": "已接诊",
}
_JCSTA_LABELS = {
    "N": "未成交",
    "Y": "已成交",
    "Z": "已治疗",
}

# 过渡期规则：
# - 2026-04-14 及以前的历史录音继续使用旧 cur.visit_order 数据。
# - 2026-04-16 起的新到诊单只认 SAP HANA 推送。
_LEGACY_VISIT_ORDER_END_DATE = date(2026, 4, 14)
_SAP_HANA_VISIT_ORDER_START_DATE = date(2026, 4, 16)
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
_CUSTOMER_BIRTHDAY_RETRY_DELAY = timedelta(hours=48)


@lru_cache(maxsize=1)
def _sync_lookup_engine():
    settings = get_settings()
    url = make_url(settings.database_url)
    driver_name = url.drivername
    if driver_name == "postgresql+asyncpg":
        sync_url = url.set(drivername="postgresql+psycopg")
    elif driver_name == "sqlite+aiosqlite":
        sync_url = url.set(drivername="sqlite")
    elif "+" in driver_name:
        sync_url = url.set(drivername=driver_name.split("+", 1)[0])
    else:
        sync_url = url
    # 同步只读引擎：用于跨数据源回查到诊单。限制连接池规模，避免长尾连接堆积。
    return create_engine(
        sync_url.render_as_string(hide_password=False),
        future=True,
        pool_size=2,
        max_overflow=4,
        pool_recycle=1800,
        pool_pre_ping=True,
    )


def dispose_sync_lookup_engine() -> None:
    """FastAPI lifespan 关闭时调用：释放同步引擎连接池。"""
    cache_info = _sync_lookup_engine.cache_info()
    if cache_info.currsize == 0:
        return
    try:
        engine = _sync_lookup_engine()
        engine.dispose()
    except Exception:
        pass
    _sync_lookup_engine.cache_clear()


def _parse_clock_to_seconds(value: str | None) -> int | None:
    if not value:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) < 4:
        return None
    if len(digits) >= 6:
        digits = digits[-6:]
        hour = int(digits[0:2])
        minute = int(digits[2:4])
        second = int(digits[4:6])
    else:
        digits = digits[-4:]
        hour = int(digits[0:2])
        minute = int(digits[2:4])
        second = 0
    if hour > 23 or minute > 59 or second > 59:
        return None
    return hour * 3600 + minute * 60 + second


def _parse_audio_start(value: str | None) -> tuple[str | None, int | None]:
    raw = str(value or "").strip()
    if len(raw) < 12:
        return None, None
    try:
        parsed = datetime.strptime(raw[:12], "%y%m%d%H%M%S")
    except ValueError:
        return None, None
    return parsed.date().isoformat(), parsed.hour * 3600 + parsed.minute * 60 + parsed.second


def _infer_recording_file_name(data: dict, payload_path: Path) -> str | None:
    audio_id = str(data.get("audioId") or "").strip()
    if audio_id:
        return f"audio_{audio_id}.mp3"

    audio_url = str(data.get("audioUrl") or "").strip()
    if audio_url:
        candidate = Path(audio_url.split("?")[0]).name.strip()
        if candidate:
            return candidate

    folder_name = payload_path.parent.name.strip()
    if folder_name:
        return f"{folder_name}.mp3"
    return None


def _build_visit_notes(order: VisitOrder) -> str | None:
    notes: list[str] = []
    if order.remark_dz:
        notes.append(f"到诊需求：{order.remark_dz}")
    return "\n".join(notes) or None


def _format_time(raw: str | None) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if ":" in text:
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) >= 6:
            return f"{digits[0:2]}:{digits[2:4]}:{digits[4:6]}"
        return text[:8]
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) < 6:
        return None
    digits = digits[-6:]
    return f"{digits[:2]}:{digits[2:4]}:{digits[4:6]}"


def _first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _visit_status_from_order(order: VisitOrder) -> str:
    status_text = " ".join(
        part for part in (order.jcsta_txt, order.dzsta_txt, order.fzsta_txt, order.dztyp_txt) if part
    )
    if "已成交" in status_text or "成交" in status_text:
        return "closed_won"
    if "未成交" in status_text:
        return "closed_lost"
    if order.jzsj:
        return "diagnosed"
    if order.fzdh or order.fzsj:
        return "consulted"
    if order.fzuer or order.fzr_id_dq:
        return "assigned"
    return "created"


def _visit_created_at_from_order(order: VisitOrder) -> datetime:
    for date_text, time_text in (
        (order.crtdt, order.crttm),
        (order.sjrq, order.jzsj),
        (order.sjrq, order.fzsj),
        (order.sjrq, None),
    ):
        if not date_text:
            continue
        try:
            base = datetime.strptime(date_text, "%Y-%m-%d")
        except ValueError:
            continue
        seconds = _parse_clock_to_seconds(time_text)
        if seconds is not None:
            hour = seconds // 3600
            minute = (seconds % 3600) // 60
            second = seconds % 60
            base = base.replace(hour=hour, minute=minute, second=second)
        return base
    return datetime.utcnow()


def _comparable_datetime(value: datetime | None) -> datetime | None:
    """Normalize datetimes before comparison.

    Some historical rows come back from the DB as timezone-aware while newer
    values built from visit-order dates are naive. We only need chronological
    ordering here, so coerce everything to a naive UTC value first.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_jdrq(order: VisitOrder) -> datetime | None:
    """Parse jdrq (YYYYMMDD) into datetime for 建档日期."""
    raw = (order.jdrq or "").strip()
    if not raw or len(raw) != 8:
        return None
    try:
        dt = datetime.strptime(raw, "%Y%m%d")
    except ValueError:
        return None
    # Filter out placeholder values: 00000000, 19000101, 20000101, etc.
    if dt.year < 2005:
        return None
    return dt


def _visit_reference_seconds(order: VisitOrder) -> int | None:
    return (
        _parse_clock_to_seconds(order.jzsj)
        or _parse_clock_to_seconds(order.fzsj)
        or _parse_clock_to_seconds(order.crttm)
    )

def _discover_payload_metadata(validated_dir: Path | None = None) -> list[dict[str, str | int | None]]:
    root = (validated_dir or VALIDATED_DIR).resolve()
    if not root.exists():
        return []

    items: list[dict[str, str | int | None]] = []
    for payload_path in root.glob("*/payload.jsonl"):
        try:
            first_line = ""
            with payload_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if stripped:
                        first_line = stripped
                        break
            if not first_line:
                continue
            data = json.loads(first_line)
        except (OSError, json.JSONDecodeError):
            continue

        if not isinstance(data, dict):
            continue

        file_name = _infer_recording_file_name(data, payload_path)
        if not file_name:
            continue

        record_date, start_seconds = _parse_audio_start(str(data.get("audioStartTime") or ""))
        items.append(
            {
                "file_name": file_name,
                "advisor_code": str(data.get("FZUER") or "").strip() or None,
                "visit_order_no": str(data.get("DZDH") or "").strip() or None,
                "customer_code": str(data.get("KUNR") or "").strip() or None,
                "record_date": record_date,
                "start_seconds": start_seconds,
            }
        )
    return items


def _assign_recordings_to_orders(
    orders: list[VisitOrder],
    recordings_by_file_name: dict[str, Recording],
    payload_items: list[dict[str, str | int | None]],
) -> dict[tuple[str, str | None], list[Recording]]:
    order_key_by_dzdh: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    order_keys_by_customer_day_advisor: dict[tuple[str | None, str | None, str | None], list[tuple[str, str | None]]] = defaultdict(list)
    for order in orders:
        key = (order.dzdh, order.dzseg)
        order_key_by_dzdh[order.dzdh].append(key)
        order_keys_by_customer_day_advisor[(order.kunr, order.sjrq, order.fzuer)].append(key)

    assignments: dict[tuple[str, str | None], list[Recording]] = defaultdict(list)

    for item in payload_items:
        file_name = str(item.get("file_name") or "")
        recording = recordings_by_file_name.get(file_name)
        if not recording:
            continue

        visit_order_no = str(item.get("visit_order_no") or "").strip()
        direct_keys = order_key_by_dzdh.get(visit_order_no)
        if direct_keys and len(direct_keys) == 1:
            target_key = direct_keys[0]
            if recording.id not in {row.id for row in assignments[target_key]}:
                assignments[target_key].append(recording)
            continue

        customer_code = str(item.get("customer_code") or "").strip() or None
        record_date = str(item.get("record_date") or "").strip() or None
        advisor_code = str(item.get("advisor_code") or "").strip() or None
        customer_keys = order_keys_by_customer_day_advisor.get((customer_code, record_date, advisor_code))
        if customer_keys and len(customer_keys) == 1:
            target_key = customer_keys[0]
            if recording.id not in {row.id for row in assignments[target_key]}:
                assignments[target_key].append(recording)

    return assignments


async def sync_customer_center_from_visit_orders(
    db: AsyncSession,
    *,
    validated_dir: Path | None = None,
) -> tuple[int, int, int]:
    if not await _try_acquire_customer_center_sync_lock(db):
        logger.info("customer center visit-order sync is already running; skipped this round")
        await db.rollback()
        return 0, 0, 0

    orders = (
        await db.execute(select(VisitOrder).order_by(VisitOrder.sjrq.desc(), VisitOrder.dzdh.desc()))
    ).scalars().all()
    if not orders:
        await db.rollback()
        return 0, 0, 0

    existing_customers = (
        await db.execute(select(Customer).where(Customer.external_customer_code.is_not(None)))
    ).scalars().all()
    customer_by_code = {customer.external_customer_code: customer for customer in existing_customers if customer.external_customer_code}

    existing_visits = (
        await db.execute(select(Visit).where(Visit.external_visit_order_no.is_not(None)))
    ).scalars().all()
    visit_by_ref = {(visit.external_visit_order_no, visit.external_visit_order_seg): visit for visit in existing_visits}

    staff_rows = (await db.execute(select(Staff))).scalars().all()
    staff_by_external_code = {staff.external_account: staff for staff in staff_rows if staff.external_account}
    valid_staff_ids = {staff.id for staff in staff_rows}

    recording_rows = (
        await db.execute(
            select(Recording).options(
                selectinload(Recording.segments),
                selectinload(Recording.transcript),
                selectinload(Recording.visit_links).selectinload(RecordingVisitLink.visit),
            )
        )
    ).scalars().all()
    recordings_by_file_name = {recording.file_name: recording for recording in recording_rows}
    payload_items = _discover_payload_metadata(validated_dir)
    recording_assignments = _assign_recordings_to_orders(orders, recordings_by_file_name, payload_items)
    payload_by_file_name = {str(item.get("file_name") or ""): item for item in payload_items}
    payload_file_names = set(payload_by_file_name)

    new_customers = 0
    new_visits = 0
    linked_recordings = 0
    unlinked_recordings = 0
    profile_refresh_customer_ids: set[str] = set()
    customer_age_synced_ids: set[str] = set()

    # Clear previous auto-links for payload-backed recordings that currently point to external visit-order visits.
    for recording in recording_rows:
        if recording.file_name not in payload_file_names:
            continue
        external_link_ids = [
            link.visit_id
            for link in recording.visit_links
            if link.visit is not None
            and link.visit.external_visit_order_no is not None
            and (not link.source or link.source in {"sync", "sync_reset", "auto_match"})
        ]
        if not external_link_ids:
            continue
        await sync_recording_visit_links(db, recording, [], primary_visit_id=None, source="sync_reset")
        unlinked_recordings += 1

    # ── 按 DZDH 分组，同一到诊单号的多个行项目合并为一条接诊记录 ──
    orders_by_dzdh: dict[str, list[VisitOrder]] = defaultdict(list)
    for order in orders:
        orders_by_dzdh[order.dzdh].append(order)
    for dzdh in orders_by_dzdh:
        orders_by_dzdh[dzdh].sort(key=lambda o: o.dzseg or "")

    # 也建立 visit_by_dzdh 索引，用于查找已有的合并 Visit
    visit_by_dzdh: dict[str, Visit] = {}
    for (vo_no, vo_seg), v in visit_by_ref.items():
        if vo_no and vo_no not in visit_by_dzdh:
            visit_by_dzdh[vo_no] = v

    for dzdh, group_orders in orders_by_dzdh.items():
        primary_order = group_orders[0]  # 最小行项目号

        # ── 客户 ──
        customer_code = str(primary_order.kunr or "").strip() or None
        customer_name = str(primary_order.ninam or "").strip() or f"客户 {primary_order.dzdh}"
        customer = customer_by_code.get(customer_code) if customer_code else None
        if customer is None and customer_code:
            customer = (
                await db.execute(
                    select(Customer).where(Customer.external_customer_code == customer_code).limit(1)
                )
            ).scalar_one_or_none()
            if customer is not None:
                customer_by_code[customer_code] = customer
        if customer is None:
            computed_age = _compute_customer_current_age(primary_order)
            customer = Customer(
                name=customer_name,
                external_customer_code=customer_code,
                gender=primary_order.customer_gender,
                age=computed_age,
                source=primary_order.qdly1_txt or primary_order.dzly_txt,
                notes=None,
                created_at=_parse_jdrq(primary_order) or _visit_created_at_from_order(primary_order),
            )
            db.add(customer)
            await db.flush()
            if customer_code:
                customer_by_code[customer_code] = customer
            if computed_age is not None:
                customer_age_synced_ids.add(customer.id)
                profile_refresh_customer_ids.add(customer.id)
            new_customers += 1
        else:
            # Update created_at if this order's jdrq is earlier
            order_jdrq = _parse_jdrq(primary_order)
            existing_created_at = _comparable_datetime(customer.created_at)
            normalized_order_jdrq = _comparable_datetime(order_jdrq)
            if normalized_order_jdrq and (
                existing_created_at is None or normalized_order_jdrq < existing_created_at
            ):
                customer.created_at = order_jdrq
            customer.name = customer_name
            customer.source = primary_order.qdly1_txt or primary_order.dzly_txt or customer.source
            if primary_order.customer_gender and not customer.gender:
                customer.gender = primary_order.customer_gender
            computed_age = _compute_customer_current_age(primary_order)
            if computed_age is not None and customer.id not in customer_age_synced_ids:
                if computed_age != customer.age:
                    customer.age = computed_age
                    profile_refresh_customer_ids.add(customer.id)
                customer_age_synced_ids.add(customer.id)

        # ── 接诊记录：一个 DZDH 对应一条 Visit ──
        visit = visit_by_dzdh.get(dzdh)
        # 也检查旧的 per-segment Visit（向后兼容）
        if visit is None:
            for seg_order in group_orders:
                visit = visit_by_ref.get((dzdh, seg_order.dzseg))
                if visit is not None:
                    break
        if visit is None:
            visit = (
                await db.execute(
                    select(Visit)
                    .where(
                        Visit.external_visit_order_no == dzdh,
                        Visit.external_visit_order_seg == primary_order.dzseg,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()

        consultant = (
            staff_by_external_code.get(primary_order.fzr_id_dq or primary_order.fzuer or "")
            or staff_by_external_code.get(primary_order.fzuer or "")
        )

        if visit is None:
            visit = Visit(
                customer_id=customer.id,
                external_visit_order_no=dzdh,
                external_visit_order_seg=primary_order.dzseg,
                created_at=_visit_created_at_from_order(primary_order),
            )
            db.add(visit)
            await db.flush()
            new_visits += 1

        # 将所有行项目 key 都映射到这一条 Visit
        visit_by_dzdh[dzdh] = visit
        for seg_order in group_orders:
            visit_by_ref[(dzdh, seg_order.dzseg)] = visit

        visit.customer_id = customer.id
        visit.external_visit_order_seg = primary_order.dzseg
        visit.consultant_id = consultant.id if consultant else None
        visit.visit_date = date.fromisoformat(primary_order.sjrq) if primary_order.sjrq else None
        visit.visit_time = _format_time(primary_order.fzsj)
        visit.deal_status = primary_order.jcsta_txt
        visit.arrival_purpose = primary_order.dymd_txt
        visit.project_needs = _first_non_empty(primary_order.remark_dz)
        visit.updated_at = _visit_created_at_from_order(primary_order)

        # 状态：任一行项目 已成交 → 已成交
        statuses = {_visit_status_from_order(o) for o in group_orders}
        status_priority = ("closed_won", "closed_lost", "diagnosed", "consulted", "assigned", "created")
        visit.status = next((status for status in status_priority if status in statuses), None)

        visit.customer_value = None

        # 备注：合并所有行项目，多行项目时加标识
        if len(group_orders) == 1:
            visit.notes = _build_visit_notes(primary_order)
        else:
            notes_parts: list[str] = []
            for seg_order in group_orders:
                seg_note = _build_visit_notes(seg_order)
                advxc_label = seg_order.advxc_long or seg_order.advxc or ""
                seg_header = f"[行项目 {seg_order.dzseg}"
                if advxc_label:
                    seg_header += f" | {advxc_label}"
                seg_header += "]"
                notes_parts.append(f"{seg_header} {seg_note}" if seg_note else seg_header)
            visit.notes = "\n".join(notes_parts)

        # ── 关联录音 ──
        all_matched: list[Recording] = []
        for seg_order in group_orders:
            seg_key = (seg_order.dzdh, seg_order.dzseg)
            all_matched.extend(recording_assignments.get(seg_key, []))
        seen_ids: set[str] = set()
        for recording in all_matched:
            if recording.id in seen_ids:
                continue
            seen_ids.add(recording.id)
            payload_meta = payload_by_file_name.get(recording.file_name)
            advisor_code = str(payload_meta.get("advisor_code") or "").strip() if payload_meta else ""
            payload_staff = staff_by_external_code.get(advisor_code)
            if payload_staff and (recording.staff_id is None or recording.staff_id not in valid_staff_ids):
                recording.staff_id = payload_staff.id
            existing_linked_ids = {link.visit_id for link in recording.visit_links}
            target_visit_ids = [visit.id, *existing_linked_ids]
            primary_visit_id = recording.visit_id if recording.visit_id in target_visit_ids else visit.id
            if visit.id not in existing_linked_ids or recording.visit_id != primary_visit_id:
                await sync_recording_visit_links(
                    db,
                    recording,
                    target_visit_ids,
                    primary_visit_id=primary_visit_id,
                    source="sync",
                )
                linked_recordings += 1

    # 清理旧的 per-segment 孤立 Visit（同一 DZDH 有多条 Visit 时只保留合并后的）
    orphan_visit_ids: list[str] = []
    for v in existing_visits:
        if not v.external_visit_order_no:
            continue
        merged = visit_by_dzdh.get(v.external_visit_order_no)
        if merged and v.id != merged.id:
            orphan_visit_ids.append(v.id)
    if orphan_visit_ids:
        for orphan_id in orphan_visit_ids:
            orphan_visit = await db.get(Visit, orphan_id)
            if orphan_visit:
                # cascade 会自动删除 recording_links；清除 recordings.visit_id 引用
                await db.execute(
                    Recording.__table__.update()
                    .where(Recording.__table__.c.visit_id == orphan_id)
                    .values(visit_id=None)
                )
                await db.delete(orphan_visit)

    for customer_id in sorted(profile_refresh_customer_ids):
        try:
            await refresh_customer_profile_scores(db, customer_id)
        except Exception:
            logger.exception("Failed to refresh customer profile tags after visit-order archive sync: %s", customer_id)

    if new_customers or new_visits or linked_recordings or unlinked_recordings or orphan_visit_ids or profile_refresh_customer_ids:
        await db.commit()
    else:
        await db.rollback()

    return new_customers, new_visits, linked_recordings


def _discover_recording_dates_and_advisors(
    validated_dir: Path | None = None,
) -> tuple[set[str], set[str]]:
    """扫描录音目录，返回 (日期集合, 顾问编号集合)。

    日期格式: YYYY-MM-DD，从 payload.jsonl 的 audioStartTime (YYMMDDHHmmss) 推断。
    """
    root = (validated_dir or VALIDATED_DIR).resolve()
    if not root.exists():
        return set(), set()

    dates: set[str] = set()
    advisors: set[str] = set()

    for payload_path in root.glob("*/payload.jsonl"):
        try:
            with payload_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    data = json.loads(stripped)

                    advisor_code = str(data.get("FZUER") or "").strip()
                    if advisor_code:
                        advisors.add(advisor_code)

                    start_time = str(data.get("audioStartTime") or "").strip()
                    if len(start_time) >= 6:
                        yy = start_time[:2]
                        mm = start_time[2:4]
                        dd = start_time[4:6]
                        try:
                            d = date(2000 + int(yy), int(mm), int(dd))
                            dates.add(d.isoformat())
                        except ValueError:
                            pass
        except (OSError, json.JSONDecodeError):
            continue

    return dates, advisors


async def _discover_recording_context(
    db: AsyncSession,
    *,
    validated_dir: Path | None = None,
) -> tuple[set[str], set[str], set[str]]:
    dates, advisors = _discover_recording_dates_and_advisors(validated_dir)
    hospital_codes: set[str] = set()

    rows = (
        await db.execute(
            select(Recording.created_at, Staff.external_account, Staff.hospital_code)
            .join(Staff, Staff.id == Recording.staff_id, isouter=True)
        )
    ).all()
    for created_at, external_account, hospital_code in rows:
        if created_at:
            dates.add(created_at.date().isoformat())
        normalized_account = _clean_text(external_account)
        if normalized_account:
            advisors.add(normalized_account)
        normalized_hospital_code = _clean_text(hospital_code)
        if normalized_hospital_code:
            hospital_codes.add(normalized_hospital_code)

    return dates, advisors, hospital_codes


def _fix_encoding(value: str | None) -> str | None:
    """修复远端 PG 返回的乱码：latin1 -> utf-8。"""
    if value is None:
        return None
    try:
        return value.encode("latin1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return value


def _normalize_customer_gender(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None

    normalized = _fix_encoding(text) or text
    normalized = normalized.strip()
    if normalized in {"female", "f", "Ů"}:
        return "女"
    if normalized in {"male", "m", "��"}:
        return "男"
    if normalized in _KUSEX_LABELS:
        return _KUSEX_LABELS[normalized]
    if normalized in {"女"}:
        return "女"
    if normalized in {"男"}:
        return "男"
    return normalized


def _compute_customer_current_age(order) -> int | None:
    """按到诊日期和客户出生日期计算客户当次到诊年龄（用于客户档案）。"""
    bday_str = str(order.customer_birthday or "").strip()
    if not bday_str:
        return None
    explicit_age = _normalize_customer_age(bday_str)
    if explicit_age is not None:
        return explicit_age
    try:
        birthday = date.fromisoformat(bday_str)
        reference_date = _order_age_reference_date(order) or date.today()
        age = reference_date.year - birthday.year
        if (reference_date.month, reference_date.day) < (birthday.month, birthday.day):
            age -= 1
        return age if age >= 0 else None
    except (ValueError, TypeError):
        return None


def _order_age_reference_date(order) -> date | None:
    for attr_name in ("sjrq", "jzrq", "fzrq", "crtdt"):
        normalized = _normalize_sap_date_token(getattr(order, attr_name, None))
        if not normalized:
            continue
        try:
            return date.fromisoformat(normalized)
        except ValueError:
            continue
    return None


def _resolve_staff_directory_dsn(staff_directory_dsn: str | None = None) -> str:
    return (staff_directory_dsn or get_settings().resolved_staff_directory_dsn).strip()


def _fetch_remote_customer_birthdays(
    customer_codes: set[str],
    *,
    staff_directory_dsn: str | None = None,
) -> dict[str, str]:
    dsn = _resolve_staff_directory_dsn(staff_directory_dsn)
    normalized_codes = sorted({str(code or "").strip() for code in customer_codes if str(code or "").strip()})
    if not dsn or not normalized_codes:
        return {}

    try:
        import psycopg
    except ImportError:
        return {}

    birthday_by_code: dict[str, str] = {}
    query = """
        select distinct on (khbm) khbm, csrq
        from cur.customer
        where khbm = any(%(codes)s)
          and csrq is not null
          and btrim(csrq) <> ''
        order by khbm, inst_dt desc nulls last
    """
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                for index in range(0, len(normalized_codes), 1000):
                    chunk = normalized_codes[index:index + 1000]
                    cur.execute(query, {"codes": chunk})
                    for code, raw_birthday in cur.fetchall():
                        normalized_code = _clean_text(code)
                        birthday = _normalize_sap_date_token(raw_birthday)
                        if normalized_code and birthday:
                            birthday_by_code[normalized_code] = birthday
    except Exception:
        logger.exception("Failed to fetch customer birthdays from cur.customer")
        return {}
    return birthday_by_code


async def _apply_customer_birthdays_to_existing_visit_orders(
    db: AsyncSession,
    rows: list[SapHanaVisitOrder],
) -> int:
    birthday_by_key = {
        (_clean_text(row.jgbm) or "", _clean_text(row.dzdh) or ""): _normalize_sap_date_token(row.customer_birthday)
        for row in rows
        if _clean_text(row.jgbm) and _clean_text(row.dzdh) and _normalize_sap_date_token(row.customer_birthday)
    }
    if not birthday_by_key:
        return 0

    visit_orders = (
        await db.execute(
            select(VisitOrder).where(
                tuple_(VisitOrder.jgbm, VisitOrder.dzdh).in_(sorted(birthday_by_key))
            )
        )
    ).scalars().all()
    updated_count = 0
    for order in visit_orders:
        birthday = birthday_by_key.get((_clean_text(order.jgbm) or "", _clean_text(order.dzdh) or ""))
        if birthday and order.customer_birthday != birthday:
            order.customer_birthday = birthday
            updated_count += 1
    return updated_count


async def _sync_customer_birthdays_for_sap_rows(
    db: AsyncSession,
    rows: list[SapHanaVisitOrder],
    *,
    staff_directory_dsn: str | None = None,
    schedule_missing_retry: bool,
    close_missing_retry: bool = False,
) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    found_rows: list[SapHanaVisitOrder] = []
    checked_count = 0
    found_count = 0
    changed = False

    for row in rows:
        payload_birthday = _extract_sap_hana_kubsd_birthdate(row)
        if not payload_birthday:
            continue
        checked_count += 1
        if _normalize_sap_date_token(row.customer_birthday) != payload_birthday:
            row.customer_birthday = payload_birthday
            changed = True
        if row.customer_birthday_retry_at is not None:
            row.customer_birthday_retry_at = None
            changed = True
        found_rows.append(row)
        found_count += 1

    target_rows = [
        row for row in rows
        if _clean_text(row.kunr) and not _normalize_sap_date_token(row.customer_birthday)
    ]
    if not found_rows and not target_rows:
        return {
            "checked": 0,
            "found": 0,
            "scheduled": 0,
            "closed_missing": 0,
            "visit_orders_updated": 0,
        }

    birthdays: dict[str, str] = {}
    if target_rows:
        customer_codes = {_clean_text(row.kunr) for row in target_rows if _clean_text(row.kunr)}
        birthdays = await asyncio.to_thread(
            _fetch_remote_customer_birthdays,
            customer_codes,
            staff_directory_dsn=staff_directory_dsn,
        )
    scheduled_count = 0
    closed_missing_count = 0
    checked_count += len(target_rows)

    for row in target_rows:
        customer_code = _clean_text(row.kunr) or ""
        birthday = birthdays.get(customer_code)
        row.customer_birthday_lookup_at = now
        changed = True
        if birthday:
            row.customer_birthday = birthday
            row.customer_birthday_retry_at = None
            found_rows.append(row)
            found_count += 1
            continue

        if schedule_missing_retry:
            row.customer_birthday_retry_count = max(int(row.customer_birthday_retry_count or 0), 1)
            if row.customer_birthday_retry_at is None:
                row.customer_birthday_retry_at = now + _CUSTOMER_BIRTHDAY_RETRY_DELAY
            scheduled_count += 1
        elif close_missing_retry:
            row.customer_birthday_retry_count = int(row.customer_birthday_retry_count or 0) + 1
            row.customer_birthday_retry_at = None
            closed_missing_count += 1

    visit_orders_updated = await _apply_customer_birthdays_to_existing_visit_orders(db, found_rows)
    if changed or visit_orders_updated:
        await db.commit()
    if visit_orders_updated:
        await sync_customer_center_from_visit_orders(db)

    return {
        "checked": checked_count,
        "found": found_count,
        "scheduled": scheduled_count,
        "closed_missing": closed_missing_count,
        "visit_orders_updated": visit_orders_updated,
    }


async def sync_sap_hana_customer_birthdays_for_keys(
    db: AsyncSession,
    *,
    keys: set[tuple[str, str]],
    staff_directory_dsn: str | None = None,
) -> dict[str, int]:
    normalized_keys = sorted(
        {
            (str(jgbm or "").strip(), str(dzdh or "").strip())
            for jgbm, dzdh in keys
            if str(jgbm or "").strip() and str(dzdh or "").strip()
        }
    )
    if not normalized_keys:
        return {"checked": 0, "found": 0, "scheduled": 0, "closed_missing": 0, "visit_orders_updated": 0}

    rows = (
        await db.execute(
            select(SapHanaVisitOrder).where(
                tuple_(SapHanaVisitOrder.jgbm, SapHanaVisitOrder.dzdh).in_(normalized_keys)
            )
        )
    ).scalars().all()
    return await _sync_customer_birthdays_for_sap_rows(
        db,
        rows,
        staff_directory_dsn=staff_directory_dsn,
        schedule_missing_retry=True,
    )


async def sync_due_sap_hana_customer_birthday_retries(
    db: AsyncSession,
    *,
    limit: int = 500,
    staff_directory_dsn: str | None = None,
) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(SapHanaVisitOrder)
            .where(
                SapHanaVisitOrder.customer_birthday_retry_at.is_not(None),
                SapHanaVisitOrder.customer_birthday_retry_at <= now,
                SapHanaVisitOrder.kunr.is_not(None),
                SapHanaVisitOrder.kunr != "",
                or_(
                    SapHanaVisitOrder.customer_birthday.is_(None),
                    SapHanaVisitOrder.customer_birthday == "",
                ),
            )
            .order_by(SapHanaVisitOrder.customer_birthday_retry_at.asc())
            .limit(max(limit, 1))
        )
    ).scalars().all()
    return await _sync_customer_birthdays_for_sap_rows(
        db,
        rows,
        staff_directory_dsn=staff_directory_dsn,
        schedule_missing_retry=False,
        close_missing_retry=True,
    )


def _legacy_remote_row_to_visit_order_record(
    row: dict[str, object],
    *,
    customer_birthdays_by_code: dict[str, str] | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {}
    date_fields = {"sjrq", "fzrq", "jzrq", "crtdt"}
    time_fields = {"fzsj", "jzsj", "crttm"}
    institution_code = _clean_text(row.get("yybm")) or _clean_text(row.get("jgbm"))

    for col_name in _VISIT_ORDER_COLUMNS:
        value = row.get(col_name)
        if col_name in date_fields:
            record[col_name] = _normalize_sap_date_token(value)
        elif col_name in time_fields:
            record[col_name] = _normalize_sap_time_token(value)
        else:
            record[col_name] = value

    record["jgbm"] = institution_code
    record["customer_gender"] = None
    customer_code = _clean_text(record.get("kunr"))
    remote_birthday = (customer_birthdays_by_code or {}).get(customer_code or "")
    record["customer_birthday"] = remote_birthday or _normalize_sap_date_token(row.get("csrq"))
    return record


def _fetch_legacy_remote_visit_orders(
    *,
    date_strings: set[str],
    advisor_codes: set[str],
    hospital_codes: set[str],
    staff_directory_dsn: str | None = None,
) -> list[dict[str, object]]:
    dsn = _resolve_staff_directory_dsn(staff_directory_dsn)
    if not dsn:
        return []

    normalized_dates = sorted(_normalized_date_set(date_strings))
    normalized_advisors = sorted({str(code or "").strip() for code in advisor_codes if str(code or "").strip()})
    normalized_hospitals = sorted({str(code or "").strip() for code in hospital_codes if str(code or "").strip()})
    if not normalized_dates or (not normalized_advisors and not normalized_hospitals):
        return []

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        return []

    selected_columns = ", ".join([*_LEGACY_REMOTE_SELECT_COLUMNS, "csrq"])
    where_clauses = [
        "sjrq = any(%(dates)s)",
    ]
    params: dict[str, object] = {
        "dates": normalized_dates,
    }
    if normalized_advisors:
        where_clauses.append(
            """(
            fzuer = any(%(advisors)s)
            or fzr_id_dq = any(%(advisors)s)
            or advxc = any(%(advisors)s)
            or advyq = any(%(advisors)s)
        )"""
        )
        params["advisors"] = normalized_advisors
    if normalized_hospitals:
        where_clauses.append("(yybm = any(%(hospitals)s) or jgbm = any(%(hospitals)s))")
        params["hospitals"] = normalized_hospitals

    query = f"""
        select {selected_columns}
        from cur.visit_order
        where {" and ".join(where_clauses)}
    """

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    customer_codes = {_clean_text(row.get("kunr")) for row in rows if _clean_text(row.get("kunr"))}
    customer_birthdays = _fetch_remote_customer_birthdays(customer_codes, staff_directory_dsn=staff_directory_dsn)
    return [
        _legacy_remote_row_to_visit_order_record(row, customer_birthdays_by_code=customer_birthdays)
        for row in rows
    ]


def fetch_latest_remote_visit_order_date(
    hospital_codes: set[str],
    *,
    staff_directory_dsn: str | None = None,
) -> str | None:
    """Return the latest available SAP HANA visit-order date for the given institutions."""
    if not hospital_codes:
        return None

    del staff_directory_dsn

    normalized_codes = {str(code or "").strip() for code in hospital_codes if str(code or "").strip()}
    if not normalized_codes:
        return None

    try:
        with _sync_lookup_engine().connect() as conn:
            return conn.execute(
                select(func.max(SapHanaVisitOrder.crtdt)).where(
                    SapHanaVisitOrder.jgbm.in_(sorted(normalized_codes)),
                    SapHanaVisitOrder.crtdt.is_not(None),
                    SapHanaVisitOrder.crtdt != "",
                )
            ).scalar_one_or_none()
    except Exception:
        return None


async def _resolve_hospital_codes_for_advisors(
    db: AsyncSession,
    advisor_codes: set[str],
    *,
    staff_directory_dsn: str | None = None,
) -> set[str]:
    del staff_directory_dsn

    staff_rows = (
        await db.execute(
            select(Staff.external_account, Staff.hospital_code).where(
                Staff.external_account.in_(sorted(advisor_codes)),
                Staff.hospital_code.is_not(None),
            )
        )
    ).all()
    return {row.hospital_code for row in staff_rows if row.hospital_code}


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_sap_date_token(value: object) -> str | None:
    if isinstance(value, date):
        return value.isoformat()
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


def _normalize_customer_age(value: object) -> int | None:
    text = _clean_text(value)
    if not text:
        return None
    if _normalize_sap_date_token(text):
        return None

    candidates: list[str] = []
    if re.fullmatch(r"\d{1,3}(?:\.0+)?", text):
        candidates.append(text.split(".", 1)[0])
    for pattern in (
        r"^(?:年龄|年纪|客户年龄|顾客年龄|实际年龄)[:：]?\s*(\d{1,3})\s*岁?$",
        r"^(\d{1,3})\s*岁(?:半)?$",
    ):
        match = re.search(pattern, text)
        if match:
            candidates.append(match.group(1))

    for candidate in candidates:
        try:
            age = int(candidate)
        except (TypeError, ValueError):
            continue
        if 0 < age <= 120:
            return age
    return None


def _normalize_payload_key(value: object) -> str:
    text = str(value or "").strip().upper()
    return "".join(ch for ch in text if ch.isalnum())


def _find_payload_value(payload: object, keys: tuple[str, ...]) -> object | None:
    if not isinstance(payload, dict):
        return None
    values_by_key = {
        _normalize_payload_key(key): value
        for key, value in payload.items()
        if value not in (None, "")
    }
    for key in keys:
        value = values_by_key.get(_normalize_payload_key(key))
        if value not in (None, ""):
            return value
    return None


def _extract_sap_hana_kubsd_birthdate(row: SapHanaVisitOrder) -> str | None:
    payload = row.source_payload if isinstance(row.source_payload, dict) else {}
    return _normalize_sap_date_token(_find_payload_value(payload, ("kubsd",)))


def _extract_sap_hana_customer_birthdate_or_age(row: SapHanaVisitOrder) -> str | None:
    payload = row.source_payload if isinstance(row.source_payload, dict) else {}
    birthday_value = _find_payload_value(payload, _CUSTOMER_BIRTHDAY_PAYLOAD_KEYS)
    normalized_birthday = _normalize_sap_date_token(birthday_value)
    if normalized_birthday:
        return normalized_birthday

    birthday_age = _normalize_customer_age(birthday_value)
    if birthday_age is not None:
        return f"{birthday_age}岁"

    age_value = _find_payload_value(payload, _CUSTOMER_AGE_PAYLOAD_KEYS)
    age = _normalize_customer_age(age_value)
    if age is not None:
        return f"{age}岁"
    return None


def _normalize_sap_time_token(value: object) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) < 4:
        return None
    if len(digits) >= 6:
        digits = digits[-6:]
        hour = int(digits[0:2])
        minute = int(digits[2:4])
        second = int(digits[4:6])
    else:
        digits = digits[-4:]
        hour = int(digits[0:2])
        minute = int(digits[2:4])
        second = 0
    if hour > 23 or minute > 59 or second > 59:
        return None
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def _normalize_datetime_to_strings(value: datetime | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    normalized = value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    return normalized.date().isoformat(), normalized.strftime("%H:%M:%S")


def _code_label(value: object, mapping: dict[str, str]) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    return mapping.get(text, text)


def _normalized_date_set(date_strings: set[str]) -> set[str]:
    return {normalized for item in date_strings if (normalized := _normalize_sap_date_token(item))}


def _coerce_iso_date(value: object) -> date | None:
    normalized = _normalize_sap_date_token(value)
    if not normalized:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return None


def _split_transition_date_sets(date_strings: set[str]) -> tuple[set[str], set[str]]:
    legacy_dates: set[str] = set()
    sap_dates: set[str] = set()
    for item in date_strings:
        normalized = _normalize_sap_date_token(item)
        if not normalized:
            continue
        parsed = _coerce_iso_date(normalized)
        if not parsed:
            continue
        if parsed <= _LEGACY_VISIT_ORDER_END_DATE:
            legacy_dates.add(normalized)
        elif parsed >= _SAP_HANA_VISIT_ORDER_START_DATE:
            sap_dates.add(normalized)
    return legacy_dates, sap_dates


def _extract_sap_triage_rows(row: SapHanaVisitOrder) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for item in row.fzdata or []:
        if isinstance(item, dict):
            result.append(item)
    return result


def _derive_visit_order_segment(dzdh: str, fzdh: str | None, index: int) -> str:
    triage_no = _clean_text(fzdh)
    if triage_no:
        if triage_no.startswith(f"{dzdh}-"):
            suffix = triage_no[len(dzdh) + 1 :].strip()
            if suffix:
                return suffix[:9]
        if "-" in triage_no:
            suffix = triage_no.rsplit("-", 1)[-1].strip()
            if suffix:
                return suffix[:9]
    return f"{index:03d}"


def _sap_row_advisor_codes(row: SapHanaVisitOrder) -> set[str]:
    codes = {
        code
        for code in (
            _clean_text(row.fzuer),
            _clean_text(row.d_fzuer),
            _clean_text(row.advyq),
            _clean_text(row.yyuer),
            _clean_text(row.vipkf),
            _clean_text(row.d_vipkf),
        )
        if code
    }
    for item in _extract_sap_triage_rows(row):
        for key in ("ADVXC", "ASSXC"):
            code = _clean_text(item.get(key))
            if code:
                codes.add(code)
    return codes


def _sap_row_matches_context(
    row: SapHanaVisitOrder,
    *,
    date_strings: set[str],
    institution_codes: set[str],
    advisor_codes: set[str],
) -> bool:
    del advisor_codes
    institution_code = _clean_text(row.jgbm)
    if not institution_code or institution_code not in institution_codes:
        return False

    row_date = _normalize_sap_date_token(row.crtdt)
    if date_strings and row_date not in date_strings:
        return False

    return True


def _normalize_recording_contexts(
    contexts: set[tuple[str, str, str]] | list[tuple[str, str, str]],
) -> set[tuple[str, str, str]]:
    normalized: set[tuple[str, str, str]] = set()
    for date_string, institution_code, advisor_code in contexts:
        normalized_date = _normalize_sap_date_token(date_string)
        normalized_institution = _clean_text(institution_code)
        normalized_advisor = _clean_text(advisor_code)
        if not normalized_date or not normalized_institution:
            continue
        normalized.add((normalized_date, normalized_institution, normalized_advisor))
    return normalized


def _legacy_row_advisor_codes(row: dict[str, object]) -> set[str]:
    return {
        code
        for code in (
            _clean_text(row.get("fzuer")),
            _clean_text(row.get("fzr_id_dq")),
            _clean_text(row.get("advxc")),
            _clean_text(row.get("assxc")),
            _clean_text(row.get("advyq")),
            _clean_text(row.get("yyuer")),
            _clean_text(row.get("vipkf")),
            _clean_text(row.get("d_vipkf")),
            _clean_text(row.get("kf_id")),
            _clean_text(row.get("kf_id_dq")),
        )
        if code
    }


def _visit_order_advisor_codes(order: VisitOrder) -> set[str]:
    return {
        code
        for code in (
            _clean_text(order.fzuer),
            _clean_text(order.d_fzuer),
            _clean_text(order.fzr_id_dq),
            _clean_text(order.advxc),
            _clean_text(order.assxc),
            _clean_text(order.advyq),
            _clean_text(order.yyuer),
            _clean_text(order.vipkf),
            _clean_text(order.d_vipkf),
        )
        if code
    }


def _matches_any_recording_context(
    *,
    row_date: str | None,
    institution_code: str | None,
    advisor_codes: set[str],
    contexts: set[tuple[str, str, str]],
) -> bool:
    del advisor_codes
    if not row_date or not institution_code:
        return False
    return any(
        ctx_date == row_date and ctx_institution == institution_code
        for ctx_date, ctx_institution, _ctx_advisor in contexts
    )


def _legacy_rows_matching_recording_contexts(
    rows: list[dict[str, object]],
    *,
    contexts: set[tuple[str, str, str]],
) -> list[dict[str, object]]:
    if not rows:
        return []
    normalized_contexts = _normalize_recording_contexts(contexts)
    if not normalized_contexts:
        return []

    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        key = (_clean_text(row.get("jgbm")) or _clean_text(row.get("yybm")) or "", _clean_text(row.get("dzdh")) or "")
        if key[0] and key[1]:
            grouped[key].append(row)

    kept: list[dict[str, object]] = []
    for group_rows in grouped.values():
        matched = False
        for row in group_rows:
            if _matches_any_recording_context(
                row_date=_normalize_sap_date_token(row.get("sjrq") or row.get("crtdt")),
                institution_code=_clean_text(row.get("jgbm")) or _clean_text(row.get("yybm")),
                advisor_codes=_legacy_row_advisor_codes(row),
                contexts=normalized_contexts,
            ):
                matched = True
                break
        if matched:
            kept.extend(group_rows)
    return kept


def _business_date_from_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.date().isoformat()
    return value.astimezone(_BUSINESS_TZ).date().isoformat()


def _recording_business_date(recording: Recording) -> str | None:
    return _business_date_from_datetime(recording.created_at)


async def _discover_recording_sync_contexts(
    db: AsyncSession,
) -> set[tuple[str, str, str]]:
    rows = (
        await db.execute(
            select(Recording.created_at, Staff.external_account, Staff.hospital_code)
            .join(Staff, Staff.id == Recording.staff_id)
            .where(
                Recording.created_at.is_not(None),
                Staff.hospital_code.is_not(None),
            )
        )
    ).all()
    contexts: set[tuple[str, str, str]] = set()
    for created_at, external_account, hospital_code in rows:
        if not created_at:
            continue
        recording_date = _business_date_from_datetime(created_at)
        if not recording_date:
            continue
        contexts.add(
            (
                recording_date,
                str(hospital_code).strip(),
                str(external_account).strip(),
            )
        )
    return contexts


async def sync_visit_orders_for_recording_contexts(
    db: AsyncSession,
    *,
    contexts: set[tuple[str, str, str]],
    validated_dir: Path | None = None,
    staff_directory_dsn: str | None = None,
) -> VisitOrderSyncResult:
    normalized_contexts = _normalize_recording_contexts(contexts)
    if not normalized_contexts:
        return VisitOrderSyncResult(synced_count=0, new_count=0, updated_count=0, date_range="")

    date_strings = {item[0] for item in normalized_contexts}
    institution_codes = {item[1] for item in normalized_contexts}
    advisor_codes = {item[2] for item in normalized_contexts if item[2]}
    legacy_dates, sap_dates = _split_transition_date_sets(date_strings)
    remote_records: list[dict[str, object]] = []

    if legacy_dates:
        legacy_rows = await asyncio.to_thread(
            _fetch_legacy_remote_visit_orders,
            date_strings=legacy_dates,
            advisor_codes=advisor_codes,
            hospital_codes=institution_codes,
            staff_directory_dsn=staff_directory_dsn,
        )
        remote_records.extend(
            _legacy_rows_matching_recording_contexts(legacy_rows, contexts=normalized_contexts)
        )

    if sap_dates:
        sap_rows = (
            await db.execute(
                select(SapHanaVisitOrder)
                .where(SapHanaVisitOrder.jgbm.in_(sorted(institution_codes)))
                .where(SapHanaVisitOrder.crtdt.in_(sorted(sap_dates)))
                .order_by(SapHanaVisitOrder.updated_at.desc(), SapHanaVisitOrder.created_at.desc())
            )
        ).scalars().all()
        institution_name_by_code, staff_name_by_external_code = await _load_staff_support_maps(db)
        for row in sap_rows:
            if not _matches_any_recording_context(
                row_date=_normalize_sap_date_token(row.crtdt),
                institution_code=_clean_text(row.jgbm),
                advisor_codes=_sap_row_advisor_codes(row),
                contexts=normalized_contexts,
            ):
                continue
            remote_records.extend(
                _build_visit_order_records_from_sap_row(
                    row,
                    institution_name_by_code=institution_name_by_code,
                    staff_name_by_external_code=staff_name_by_external_code,
                )
            )

    if not remote_records:
        return VisitOrderSyncResult(
            synced_count=0,
            new_count=0,
            updated_count=0,
            date_range=f"{min(date_strings)} ~ {max(date_strings)}",
        )

    new_count, updated_count = await _upsert_compatible_visit_orders(db, remote_records)
    await sync_customer_center_from_visit_orders(db, validated_dir=validated_dir)
    return VisitOrderSyncResult(
        synced_count=len(remote_records),
        new_count=new_count,
        updated_count=updated_count,
        date_range=f"{min(date_strings)} ~ {max(date_strings)}",
    )


async def retry_visit_order_sync(
    operation: Callable[[], Awaitable[VisitOrderSyncResult]],
    *,
    label: str,
    attempts: int = 3,
    initial_delay_seconds: float = 1.0,
) -> VisitOrderSyncResult:
    """Run a visit-order sync operation with short retries for transient DB/API failures."""
    resolved_attempts = max(attempts, 1)
    delay = max(initial_delay_seconds, 0)
    for attempt in range(1, resolved_attempts + 1):
        try:
            return await operation()
        except Exception:
            if attempt >= resolved_attempts:
                logger.exception(
                    "visit order sync failed after retries label=%s attempts=%d",
                    label,
                    resolved_attempts,
                )
                raise
            logger.warning(
                "visit order sync failed; retrying label=%s attempt=%d/%d delay=%.1fs",
                label,
                attempt,
                resolved_attempts,
                delay,
                exc_info=True,
            )
            if delay > 0:
                await asyncio.sleep(delay)
            delay *= 2


async def cleanup_out_of_context_visit_orders(
    db: AsyncSession,
    *,
    validated_dir: Path | None = None,
) -> dict[str, int]:
    contexts = await _discover_recording_sync_contexts(db)
    normalized_contexts = _normalize_recording_contexts(contexts)
    if not normalized_contexts:
        return {
            "kept_visit_orders": 0,
            "deleted_visit_orders": 0,
            "deleted_visits": 0,
        }

    all_orders = (await db.execute(select(VisitOrder).order_by(VisitOrder.dzdh, VisitOrder.dzseg))).scalars().all()
    grouped_orders: dict[tuple[str, str], list[VisitOrder]] = defaultdict(list)
    for order in all_orders:
        key = (_clean_text(order.jgbm) or "", _clean_text(order.dzdh) or "")
        if key[0] and key[1]:
            grouped_orders[key].append(order)

    keep_order_ids: set[str] = set()
    keep_dzdh: set[str] = set()
    for (_, dzdh), group_orders in grouped_orders.items():
        matched = False
        for order in group_orders:
            if _matches_any_recording_context(
                row_date=_normalize_sap_date_token(order.crtdt or order.sjrq),
                institution_code=_clean_text(order.jgbm),
                advisor_codes=_visit_order_advisor_codes(order),
                contexts=normalized_contexts,
            ):
                matched = True
                break
        if matched:
            keep_dzdh.add(dzdh)
            for order in group_orders:
                keep_order_ids.add(order.id)

    stale_orders = [order for order in all_orders if order.id not in keep_order_ids]
    stale_dzdh = {str(order.dzdh).strip() for order in stale_orders if str(order.dzdh).strip()}
    for order in stale_orders:
        await db.delete(order)

    deleted_visit_ids: set[str] = set()
    if stale_dzdh:
        all_visits = (
            await db.execute(select(Visit).where(Visit.external_visit_order_no.is_not(None)))
        ).scalars().all()
        linked_visit_ids = set(
            (await db.execute(select(RecordingVisitLink.visit_id))).scalars().all()
        )
        stale_visits = [
            visit
            for visit in all_visits
            if str(visit.external_visit_order_no or "").strip() in stale_dzdh
            and str(visit.external_visit_order_no or "").strip() not in keep_dzdh
            and visit.id not in linked_visit_ids
        ]
        for visit in stale_visits:
            await db.execute(
                Recording.__table__.update()
                .where(Recording.__table__.c.visit_id == visit.id)
                .values(visit_id=None)
            )
            deleted_visit_ids.add(visit.id)
            await db.delete(visit)

    if stale_orders or deleted_visit_ids:
        await db.commit()
        await sync_customer_center_from_visit_orders(db, validated_dir=validated_dir)

    return {
        "kept_visit_orders": len(keep_order_ids),
        "deleted_visit_orders": len(stale_orders),
        "deleted_visits": len(deleted_visit_ids),
    }


async def _load_staff_support_maps(
    db: AsyncSession,
) -> tuple[dict[str, str], dict[str, str]]:
    rows = (await db.execute(select(Staff.external_account, Staff.name, Staff.hospital_code, Staff.hospital_short_name))).all()
    institution_name_by_code: dict[str, str] = {}
    staff_name_by_external_code: dict[str, str] = {}
    for external_account, name, hospital_code, hospital_short_name in rows:
        if hospital_code and hospital_short_name and hospital_code not in institution_name_by_code:
            institution_name_by_code[str(hospital_code).strip()] = str(hospital_short_name).strip()
        if external_account and name:
            staff_name_by_external_code[str(external_account).strip()] = str(name).strip()
    return institution_name_by_code, staff_name_by_external_code


def _build_visit_order_records_from_sap_row(
    row: SapHanaVisitOrder,
    *,
    institution_name_by_code: dict[str, str],
    staff_name_by_external_code: dict[str, str],
    customer_birthdays_by_code: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    institution_code = _clean_text(row.jgbm) or ""
    created_date = _normalize_sap_date_token(row.crtdt)
    created_time = _normalize_sap_time_token(row.crttm)
    customer_gender = _normalize_customer_gender(row.kusex)
    customer_code = _clean_text(row.kunr)
    customer_birthday = (
        _extract_sap_hana_kubsd_birthdate(row) or
        _normalize_sap_date_token(row.customer_birthday) or
        (customer_birthdays_by_code or {}).get(customer_code or "")
        or _extract_sap_hana_customer_birthdate_or_age(row)
    )
    triage_rows = _extract_sap_triage_rows(row)

    if not triage_rows:
        triage_rows = [{}]

    result: list[dict[str, object]] = []
    for index, triage in enumerate(triage_rows, start=1):
        current_fzuer = _first_non_empty(_clean_text(row.d_fzuer), _clean_text(row.fzuer))
        triage_code = _clean_text(triage.get("ADVXC"))
        triage_name = _clean_text(triage.get("ADVXC_LONG")) or staff_name_by_external_code.get(triage_code or "")
        fzdh = _clean_text(triage.get("FZDH"))
        fzsj = _normalize_sap_time_token(triage.get("FZSJ")) or created_time
        dzseg = _derive_visit_order_segment(str(row.dzdh), fzdh, index)
        jdrq = created_date.replace("-", "") if created_date else None

        record: dict[str, object] = {
            "dzdh": row.dzdh,
            "dzseg": dzseg,
            "sjrq": created_date,
            "jgbm": institution_code,
            "fzuer": _clean_text(row.fzuer),
            "fzuer_long": _clean_text(row.fzuer_long),
            "advxc": triage_code,
            "advxc_long": triage_name,
            "advyq": _clean_text(row.advyq),
            "advyq_name": staff_name_by_external_code.get(_clean_text(row.advyq) or ""),
            "kunr": _clean_text(row.kunr),
            "ninam": _clean_text(row.ninam),
            "kusex": _clean_text(row.kusex),
            "kusex_txt": customer_gender,
            "yydh": _clean_text(row.yydh),
            "yyuer": _clean_text(row.yyuer),
            "kutyp_dq": _clean_text(row.kutyp_dq),
            "kutyp_dq_txt": _code_label(row.kutyp_dq, _KUTYP_DQ_LABELS),
            "kut30_dq": _clean_text(row.kut30_dq),
            "kut30_dq_txt": _code_label(row.kut30_dq, _KUT30_DQ_LABELS),
            "kusta_dq": _clean_text(row.kusta_dq),
            "kusta_dq_txt": _code_label(row.kusta_dq, _KUSTA_DQ_LABELS),
            "khlx": _clean_text(row.kutyp_dq),
            "khlx_yg": None,
            "khlx_t30": _clean_text(row.kut30_dq),
            "khlx2": _clean_text(row.kusta_dq),
            "kulvl_dq": _clean_text(row.kulvl_dq),
            "vipkf": _clean_text(row.vipkf),
            "d_fzuer": _clean_text(row.d_fzuer),
            "fzr_id_dq": current_fzuer,
            "d_vipkf": _clean_text(row.d_vipkf),
            "fzdh": fzdh,
            "fzsj": fzsj,
            "fzrq": created_date,
            "fzsta": _clean_text(triage.get("FZSTA")),
            "fzsta_txt": _code_label(triage.get("FZSTA"), _FZSTA_LABELS),
            "fzrid": triage_code or current_fzuer,
            "ddsc": _clean_text(triage.get("DDSC")),
            "bhkx": _clean_text(row.bhkx),
            "assxc": _clean_text(triage.get("ASSXC")),
            "jzsj": None,
            "jzrq": None,
            "jgks": _clean_text(row.jgks),
            "jgks_txt": _code_label(row.jgks, _JGKS_LABELS),
            "dztyp": _clean_text(row.dztyp),
            "dztyp_txt": _code_label(row.dztyp, _DZTYP_LABELS),
            "dzsta": _clean_text(row.dzsta),
            "dzsta_txt": _code_label(row.dzsta, _DZSTA_LABELS),
            "dzly": _clean_text(row.dzly),
            "dymd": _clean_text(row.dymd),
            "jcsta": _clean_text(triage.get("JCSTA")),
            "jcsta_txt": _code_label(triage.get("JCSTA"), _JCSTA_LABELS),
            "kusrc": _clean_text(row.kusrc),
            "kusrc2": _clean_text(row.kusrc2),
            "qdly1_txt": _clean_text(row.kusrc),
            "qdly2_txt": _clean_text(row.kusrc2),
            "qd1jfl": None,
            "qd2jfl": None,
            "remark_dz": _clean_text(row.remark_dz),
            "bjzx": _clean_text(row.bjzx),
            "hylx_yg": None,
            "dymd_txt": _code_label(row.dymd, _DYMD_LABELS),
            "dzly_txt": _code_label(row.dzly, _DZLY_LABELS),
            "crtdt": created_date,
            "crttm": created_time,
            "fzr_name_dq": staff_name_by_external_code.get(current_fzuer or ""),
            "jdrq": jdrq,
            "customer_gender": customer_gender,
            "customer_birthday": customer_birthday,
        }
        result.append(record)

    return result


async def _build_sap_hana_compatible_visit_orders(
    db: AsyncSession,
    *,
    date_strings: set[str] | None = None,
    advisor_codes: set[str] | None = None,
    institution_codes: set[str] | None = None,
    sap_rows: list[SapHanaVisitOrder] | None = None,
    staff_directory_dsn: str | None = None,
    include_customer_birthdays: bool = True,
) -> list[dict[str, object]]:
    normalized_dates = _normalized_date_set(date_strings or set())
    normalized_advisors = {str(code or "").strip() for code in (advisor_codes or set()) if str(code or "").strip()}
    normalized_institutions = {
        str(code or "").strip() for code in (institution_codes or set()) if str(code or "").strip()
    }

    if sap_rows is None:
        stmt = select(SapHanaVisitOrder)
        if normalized_institutions:
            stmt = stmt.where(SapHanaVisitOrder.jgbm.in_(sorted(normalized_institutions)))
        if normalized_dates:
            stmt = stmt.where(SapHanaVisitOrder.crtdt.in_(sorted(normalized_dates)))
        rows = (
            await db.execute(stmt.order_by(SapHanaVisitOrder.updated_at.desc(), SapHanaVisitOrder.created_at.desc()))
        ).scalars().all()
    else:
        rows = list(sap_rows)
    if not rows:
        return []

    institution_name_by_code, staff_name_by_external_code = await _load_staff_support_maps(db)
    matched_rows: list[SapHanaVisitOrder] = []
    for row in rows:
        if normalized_institutions or normalized_dates or normalized_advisors:
            if not _sap_row_matches_context(
                row,
                date_strings=normalized_dates,
                institution_codes=normalized_institutions or {_clean_text(row.jgbm) or ""},
                advisor_codes=normalized_advisors,
            ):
                continue
        matched_rows.append(row)
    if not matched_rows:
        return []

    customer_birthdays: dict[str, str] = {}
    if include_customer_birthdays:
        customer_codes = {
            _clean_text(row.kunr)
            for row in matched_rows
            if _clean_text(row.kunr)
            and not _normalize_sap_date_token(row.customer_birthday)
            and not _extract_sap_hana_kubsd_birthdate(row)
        }
        if customer_codes:
            customer_birthdays = await asyncio.to_thread(
                _fetch_remote_customer_birthdays,
                customer_codes,
                staff_directory_dsn=staff_directory_dsn,
            )

    result: list[dict[str, object]] = []
    for row in matched_rows:
        result.extend(
            _build_visit_order_records_from_sap_row(
                row,
                institution_name_by_code=institution_name_by_code,
                staff_name_by_external_code=staff_name_by_external_code,
                customer_birthdays_by_code=customer_birthdays,
            )
        )
    return result


async def sync_compatible_visit_orders_for_sap_keys(
    db: AsyncSession,
    *,
    keys: set[tuple[str, str]],
    validated_dir: Path | None = None,
    staff_directory_dsn: str | None = None,
    include_customer_birthdays: bool = True,
) -> tuple[int, int]:
    normalized_keys = sorted(
        {
            (str(jgbm or "").strip(), str(dzdh or "").strip())
            for jgbm, dzdh in keys
            if str(jgbm or "").strip() and str(dzdh or "").strip()
        }
    )
    if not normalized_keys:
        return 0, 0

    sap_rows: list[SapHanaVisitOrder] = []
    for key_batch in _iter_batches(normalized_keys):
        sap_rows.extend(
            (
                await db.execute(
                    select(SapHanaVisitOrder).where(
                        tuple_(SapHanaVisitOrder.jgbm, SapHanaVisitOrder.dzdh).in_(key_batch)
                    )
                )
            ).scalars().all()
        )
    if not sap_rows:
        return 0, 0

    records = await _build_sap_hana_compatible_visit_orders(
        db,
        sap_rows=sap_rows,
        staff_directory_dsn=staff_directory_dsn,
        include_customer_birthdays=include_customer_birthdays,
    )
    if not records:
        return 0, 0

    new_count, updated_count = await _upsert_compatible_visit_orders(db, records)

    record_keys = {
        (
            str(record.get("jgbm") or "").strip(),
            str(record["dzdh"]).strip(),
            _clean_text(record.get("dzseg")),
        )
        for record in records
    }
    existing_rows: list[VisitOrder] = []
    for key_batch in _iter_batches(normalized_keys):
        existing_rows.extend(
            (
                await db.execute(
                    select(VisitOrder).where(
                        tuple_(VisitOrder.jgbm, VisitOrder.dzdh).in_(key_batch)
                    )
                )
            ).scalars().all()
        )
    stale_rows = [
        row for row in existing_rows
        if (str(row.jgbm or "").strip(), str(row.dzdh).strip(), _clean_text(row.dzseg)) not in record_keys
    ]
    for row in stale_rows:
        await db.delete(row)
    if stale_rows:
        await db.commit()

    await sync_customer_center_from_visit_orders(db, validated_dir=validated_dir)
    return new_count, updated_count


async def _upsert_compatible_visit_orders(
    db: AsyncSession,
    records: list[dict[str, object]],
) -> tuple[int, int]:
    if not records:
        return 0, 0

    keys = sorted({(str(item["dzdh"]), _clean_text(item.get("dzseg"))) for item in records})
    existing_rows: list[VisitOrder] = []
    for key_batch in _iter_batches(keys):
        existing_rows.extend(
            (
                await db.execute(
                    select(VisitOrder).where(
                        tuple_(VisitOrder.dzdh, VisitOrder.dzseg).in_(key_batch)
                    )
                )
            ).scalars().all()
        )
    existing_map = {(row.dzdh, row.dzseg): row for row in existing_rows}

    new_count = 0
    updated_count = 0
    for record in records:
        key = (str(record["dzdh"]), _clean_text(record.get("dzseg")))
        existing = existing_map.get(key)
        if existing is None:
            db.add(VisitOrder(**record))
            new_count += 1
            continue

        changed = False
        for col_name in (*_VISIT_ORDER_COLUMNS, "customer_gender", "customer_birthday"):
            new_val = record.get(col_name)
            old_val = getattr(existing, col_name, None)
            if new_val != old_val:
                setattr(existing, col_name, new_val)
                changed = True
        if changed:
            updated_count += 1

    if new_count or updated_count:
        await db.commit()

    return new_count, updated_count


async def sync_visit_orders_for_context(
    db: AsyncSession,
    *,
    date_strings: set[str],
    advisor_codes: set[str],
    hospital_codes: set[str] | None = None,
    validated_dir: Path | None = None,
    staff_directory_dsn: str | None = None,
) -> VisitOrderSyncResult:
    """按过渡期规则同步指定日期/机构上下文的到诊单。"""
    if not date_strings:
        return VisitOrderSyncResult(synced_count=0, new_count=0, updated_count=0, date_range="")

    resolved_hospital_codes = {code for code in (hospital_codes or set()) if code}
    if not resolved_hospital_codes:
        if not advisor_codes:
            return VisitOrderSyncResult(synced_count=0, new_count=0, updated_count=0, date_range="")
        resolved_hospital_codes = await _resolve_hospital_codes_for_advisors(
            db,
            advisor_codes,
            staff_directory_dsn=staff_directory_dsn,
        )
    if not resolved_hospital_codes:
        return VisitOrderSyncResult(synced_count=0, new_count=0, updated_count=0, date_range="")

    legacy_dates, sap_dates = _split_transition_date_sets(date_strings)
    remote_records: list[dict[str, object]] = []

    if legacy_dates:
        remote_records.extend(
            await asyncio.to_thread(
                _fetch_legacy_remote_visit_orders,
                date_strings=legacy_dates,
                advisor_codes=advisor_codes,
                hospital_codes=resolved_hospital_codes,
                staff_directory_dsn=staff_directory_dsn,
            )
        )
    if sap_dates:
        remote_records.extend(
            await _build_sap_hana_compatible_visit_orders(
                db,
                date_strings=sap_dates,
                advisor_codes=advisor_codes,
                institution_codes=resolved_hospital_codes,
                staff_directory_dsn=staff_directory_dsn,
            )
        )

    if not remote_records:
        return VisitOrderSyncResult(
            synced_count=0,
            new_count=0,
            updated_count=0,
            date_range=f"{min(date_strings)} ~ {max(date_strings)}",
        )

    new_count, updated_count = await _upsert_compatible_visit_orders(db, remote_records)
    await sync_customer_center_from_visit_orders(db, validated_dir=validated_dir)

    return VisitOrderSyncResult(
        synced_count=len(remote_records),
        new_count=new_count,
        updated_count=updated_count,
        date_range=f"{min(date_strings)} ~ {max(date_strings)}",
    )


async def sync_visit_orders_for_recording(
    db: AsyncSession,
    recording: Recording,
    *,
    validated_dir: Path | None = None,
    staff_directory_dsn: str | None = None,
) -> VisitOrderSyncResult:
    """录音入库后，同步该机构当天所有 SAP HANA 到诊单到兼容表。"""
    recording_date = _recording_business_date(recording)
    if not recording_date or not recording.staff_id:
        return VisitOrderSyncResult(synced_count=0, new_count=0, updated_count=0, date_range=recording_date or "")

    staff = await db.get(Staff, recording.staff_id)
    advisor_code = _clean_text(staff.external_account if staff else None)
    hospital_code = _clean_text(staff.hospital_code if staff else None)
    if not hospital_code:
        return VisitOrderSyncResult(synced_count=0, new_count=0, updated_count=0, date_range=recording_date)

    return await sync_visit_orders_for_context(
        db,
        date_strings={recording_date},
        advisor_codes={advisor_code} if advisor_code else set(),
        hospital_codes={hospital_code},
        validated_dir=validated_dir,
        staff_directory_dsn=staff_directory_dsn,
    )


async def sync_pushed_sap_hana_visit_orders_for_recording_contexts(
    db: AsyncSession,
    *,
    keys: set[tuple[str, str]],
    validated_dir: Path | None = None,
    include_customer_birthdays: bool = True,
) -> VisitOrderSyncResult:
    """SAP HANA 推送后，只物化命中已有录音日期/机构上下文的到诊单。"""
    normalized_keys = sorted(
        {
            (str(jgbm or "").strip(), str(dzdh or "").strip())
            for jgbm, dzdh in keys
            if str(jgbm or "").strip() and str(dzdh or "").strip()
        }
    )
    if not normalized_keys:
        return VisitOrderSyncResult(synced_count=0, new_count=0, updated_count=0, date_range="")

    sap_rows = (
        await db.execute(
            select(SapHanaVisitOrder).where(
                tuple_(SapHanaVisitOrder.jgbm, SapHanaVisitOrder.dzdh).in_(normalized_keys)
            )
        )
    ).scalars().all()
    if not sap_rows:
        return VisitOrderSyncResult(synced_count=0, new_count=0, updated_count=0, date_range="")

    contexts = _normalize_recording_contexts(await _discover_recording_sync_contexts(db))
    if not contexts:
        return VisitOrderSyncResult(synced_count=0, new_count=0, updated_count=0, date_range="")

    matched_keys: set[tuple[str, str]] = set()
    matched_dates: set[str] = set()
    for row in sap_rows:
        row_date = _normalize_sap_date_token(row.crtdt)
        if _matches_any_recording_context(
            row_date=row_date,
            institution_code=_clean_text(row.jgbm),
            advisor_codes=_sap_row_advisor_codes(row),
            contexts=contexts,
        ):
            matched_keys.add((str(row.jgbm).strip(), str(row.dzdh).strip()))
            if row_date:
                matched_dates.add(row_date)

    if not matched_keys:
        date_range = f"{min(matched_dates)} ~ {max(matched_dates)}" if matched_dates else ""
        return VisitOrderSyncResult(synced_count=0, new_count=0, updated_count=0, date_range=date_range)

    new_count, updated_count = await sync_compatible_visit_orders_for_sap_keys(
        db,
        keys=matched_keys,
        validated_dir=validated_dir,
        staff_directory_dsn=None,
        include_customer_birthdays=include_customer_birthdays,
    )
    date_range = f"{min(matched_dates)} ~ {max(matched_dates)}" if matched_dates else ""
    return VisitOrderSyncResult(
        synced_count=len(matched_keys),
        new_count=new_count,
        updated_count=updated_count,
        date_range=date_range,
    )


async def sync_all_visit_orders_from_sap_hana(
    db: AsyncSession,
    *,
    validated_dir: Path | None = None,
    staff_directory_dsn: str | None = None,
) -> VisitOrderSyncResult:
    records = await _build_sap_hana_compatible_visit_orders(db, staff_directory_dsn=staff_directory_dsn)
    if not records:
        return VisitOrderSyncResult(synced_count=0, new_count=0, updated_count=0, date_range="")

    new_count, updated_count = await _upsert_compatible_visit_orders(db, records)
    await sync_customer_center_from_visit_orders(db, validated_dir=validated_dir)

    date_values = sorted({value for item in records if (value := _clean_text(item.get("crtdt")) or _clean_text(item.get("sjrq")))})
    date_range = f"{date_values[0]} ~ {date_values[-1]}" if date_values else ""
    return VisitOrderSyncResult(
        synced_count=len(records),
        new_count=new_count,
        updated_count=updated_count,
        date_range=date_range,
    )


async def prune_stale_legacy_visit_data(
    db: AsyncSession,
    *,
    preserve_recent_days: int = 3,
) -> dict[str, int | str]:
    sap_records = await _build_sap_hana_compatible_visit_orders(db, include_customer_birthdays=False)
    sap_keys = {(str(item["dzdh"]), _clean_text(item.get("dzseg")) or "") for item in sap_records}
    sap_dzdh = {dzdh for dzdh, _ in sap_keys}

    all_orders = (await db.execute(select(VisitOrder))).scalars().all()
    legacy_orders = [row for row in all_orders if (str(row.dzdh), _clean_text(row.dzseg) or "") not in sap_keys]

    all_visits = (await db.execute(select(Visit))).scalars().all()
    legacy_visits = [
        row for row in all_visits
        if row.external_visit_order_no and str(row.external_visit_order_no) not in sap_dzdh
    ]
    legacy_visit_ids = [row.id for row in legacy_visits]

    recording_backed_visit_ids: set[str] = set()
    if legacy_visit_ids:
        linked_visit_ids = (
            await db.execute(select(RecordingVisitLink.visit_id).where(RecordingVisitLink.visit_id.in_(legacy_visit_ids)))
        ).scalars().all()
        recording_backed_visit_ids.update(str(item) for item in linked_visit_ids if item)

        primary_visit_ids = (
            await db.execute(select(Recording.visit_id).where(Recording.visit_id.in_(legacy_visit_ids)))
        ).scalars().all()
        recording_backed_visit_ids.update(str(item) for item in primary_visit_ids if item)

    recording_backed_dzdh = {
        str(row.external_visit_order_no)
        for row in legacy_visits
        if row.id in recording_backed_visit_ids and row.external_visit_order_no
    }

    cutoff_date = date.today() - timedelta(days=max(preserve_recent_days, 0))

    recent_legacy_dzdh: set[str] = set()
    for row in legacy_orders:
        order_date = _coerce_iso_date(row.crtdt) or _coerce_iso_date(row.sjrq)
        if order_date and order_date >= cutoff_date:
            recent_legacy_dzdh.add(str(row.dzdh))
    for row in legacy_visits:
        visit_date = row.visit_date or (row.created_at.date() if row.created_at else None)
        if visit_date and visit_date >= cutoff_date and row.external_visit_order_no:
            recent_legacy_dzdh.add(str(row.external_visit_order_no))

    historical_recording_dates = {
        created_at.date()
        for created_at in (
            await db.execute(select(Recording.created_at).where(Recording.created_at.is_not(None)))
        ).scalars().all()
        if created_at and created_at.date() <= _LEGACY_VISIT_ORDER_END_DATE
    }

    historical_transition_dzdh: set[str] = set()
    for row in legacy_orders:
        order_date = _coerce_iso_date(row.crtdt) or _coerce_iso_date(row.sjrq)
        if order_date and order_date in historical_recording_dates:
            historical_transition_dzdh.add(str(row.dzdh))
    for row in legacy_visits:
        visit_date = row.visit_date or (row.created_at.date() if row.created_at else None)
        if visit_date and visit_date in historical_recording_dates and row.external_visit_order_no:
            historical_transition_dzdh.add(str(row.external_visit_order_no))

    orders_to_delete = [
        row for row in legacy_orders
        if str(row.dzdh) not in recording_backed_dzdh
        and str(row.dzdh) not in recent_legacy_dzdh
        and str(row.dzdh) not in historical_transition_dzdh
    ]
    visits_to_delete = [
        row for row in legacy_visits
        if row.id not in recording_backed_visit_ids
        and str(row.external_visit_order_no) not in recent_legacy_dzdh
        and str(row.external_visit_order_no) not in historical_transition_dzdh
    ]

    for row in orders_to_delete:
        await db.delete(row)
    for row in visits_to_delete:
        await db.delete(row)

    if orders_to_delete or visits_to_delete:
        await db.commit()

    return {
        "sap_compatible_rows": len(sap_records),
        "legacy_orders_before": len(legacy_orders),
        "legacy_visits_before": len(legacy_visits),
        "recording_backed_legacy_visits": len(recording_backed_visit_ids),
        "recent_legacy_dzdh": len(recent_legacy_dzdh),
        "historical_transition_dzdh": len(historical_transition_dzdh),
        "deleted_visit_orders": len(orders_to_delete),
        "deleted_visits": len(visits_to_delete),
        "cutoff_date": cutoff_date.isoformat(),
    }


async def sync_visit_orders(
    db: AsyncSession,
    *,
    validated_dir: Path | None = None,
    staff_directory_dsn: str | None = None,
) -> VisitOrderSyncResult:
    """同步录音对应的到诊单数据，按历史旧源 + 新 SAP HANA 过渡。"""
    contexts = await _discover_recording_sync_contexts(db)
    if not contexts:
        return VisitOrderSyncResult(synced_count=0, new_count=0, updated_count=0, date_range="")
    return await sync_visit_orders_for_recording_contexts(
        db,
        contexts=contexts,
        validated_dir=validated_dir,
        staff_directory_dsn=staff_directory_dsn,
    )


async def periodic_visit_order_context_sync(
    stop_event: asyncio.Event,
    *,
    interval_seconds: int,
) -> None:
    """Periodically reconcile SAP HANA visit orders for every existing recording context."""
    from smart_badge_api.db.session import _session_factory

    resolved_interval = max(interval_seconds, 1)
    logger.info("starting visit order context sync loop interval_seconds=%d", resolved_interval)

    while not stop_event.is_set():
        try:
            async with _session_factory() as db:
                result = await retry_visit_order_sync(
                    lambda: sync_visit_orders(db),
                    label="periodic-recording-context-reconcile",
                    attempts=3,
                    initial_delay_seconds=2.0,
                )
                if result.synced_count or result.new_count or result.updated_count:
                    logger.info(
                        "periodic visit order context sync finished synced=%d new=%d updated=%d range=%s",
                        result.synced_count,
                        result.new_count,
                        result.updated_count,
                        result.date_range,
                    )
                birthday_retry_result = await retry_visit_order_sync(
                    lambda: sync_due_sap_hana_customer_birthday_retries(db),
                    label="periodic-customer-birthday-retry",
                    attempts=3,
                    initial_delay_seconds=2.0,
                )
                if any(birthday_retry_result.values()):
                    logger.info(
                        "periodic customer birthday retry finished checked=%d found=%d scheduled=%d closed_missing=%d visit_orders_updated=%d",
                        birthday_retry_result.get("checked", 0),
                        birthday_retry_result.get("found", 0),
                        birthday_retry_result.get("scheduled", 0),
                        birthday_retry_result.get("closed_missing", 0),
                        birthday_retry_result.get("visit_orders_updated", 0),
                    )
        except Exception:
            logger.exception("periodic visit order context sync failed; will retry on next interval")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=resolved_interval)
        except asyncio.TimeoutError:
            continue
