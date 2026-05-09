"""仪表盘统计 API。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, exists, false, func, or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.api.data_scope import (
    build_permission_scope,
    managed_staff_scope_condition,
)
from smart_badge_api.core.config import get_settings
from smart_badge_api.api.analysis_normalization import normalize_analysis_result
from smart_badge_api.analysis.consultation_evaluation import normalize_consultation_dimension_name
from smart_badge_api.analysis.schemas import CONSULTATION_PROCESS_EVALUATION_BLUEPRINT
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.core.permissions import (
    LEGACY_STAFF_PERMISSION_ROLE_MAP,
    PERMISSION_ROLE_LABELS,
    PERMISSION_ROLE_LEVELS,
    PermissionScope,
    is_global_role,
    normalize_permission_role,
)
from smart_badge_api.db.default_data import ensure_tag_categories
from smart_badge_api.db.models import (
    AnalysisTask,
    Device,
    DeviceStaffBinding,
    Recording,
    RecordingVisitLink,
    Segment,
    Staff,
    StaffManagementRelation,
    TagCategory,
    Transcript,
    Visit,
    VisitOrder,
)
from smart_badge_api.db.models import User
from smart_badge_api.db.session import get_db
from smart_badge_api.tag_catalog_reference import (
    NEGATIVE_PROJECT_EMPTY_VALUE,
    NEGATIVE_PROJECT_TAG_CATEGORY,
    is_valid_profile_tag_value,
)

router = APIRouter(prefix="/dashboard", tags=["仪表盘"])
_DISPLAY_TZ = ZoneInfo("Asia/Shanghai")


class ScoreDistItem(BaseModel):
    range: str
    count: int


class DimensionAvg(BaseModel):
    name: str
    avg_score: float


class DialogueTypeItem(BaseModel):
    type: str
    count: int


class ConcernTypeItem(BaseModel):
    type: str
    count: int


class RecentTask(BaseModel):
    id: str
    file_name: str
    overall_score: float | None
    status: str
    created_at: str


class DashboardExampleRecordingItem(BaseModel):
    recording_id: str
    analysis_task_id: str
    file_name: str
    recorded_at: str | None
    duration_seconds: int | None
    staff_id: str | None
    staff_name: str | None
    total_score: float
    max_score: float
    indication_count: int
    tag_count: int
    concern_count: int
    summary: str


class VisitStatusItem(BaseModel):
    status: str
    count: int


class VisitTrendItem(BaseModel):
    week_start: str
    week_end: str
    week_label: str
    range_label: str
    count: int


class ScoreTrendItem(BaseModel):
    date: str
    label: str
    avg_score: float | None
    task_count: int
    dimension_averages: list[DimensionAvg]


class HospitalOptionItem(BaseModel):
    hospital_code: str
    hospital_name: str


class DashboardStaffOptionItem(BaseModel):
    staff_id: str
    staff_name: str
    hospital_code: str | None
    job_label: str


class StaffStatsItem(BaseModel):
    staff_id: str
    staff_name: str
    hospital_code: str | None
    hospital_name: str | None
    job_label: str
    visit_count: int
    closed_won_count: int
    principal_amount: float
    recording_count: int
    linked_visit_count: int
    analyzed_count: int
    avg_score: float | None
    dimension_averages: list[DimensionAvg] = Field(default_factory=list)


class BreakdownValueItem(BaseModel):
    key: str
    label: str
    count: int
    task_count: int
    customer_count: int = 0


class BreakdownItem(BaseModel):
    key: str
    label: str
    count: int
    task_count: int
    customer_count: int = 0
    is_open_value: bool = False
    distinct_value_count: int = 0
    remaining_value_count: int = 0
    department_code: str | None = None
    department_name: str | None = None
    indication_code: str | None = None
    body_part_code: str | None = None
    body_part_name: str | None = None
    detail: str | None = None
    value_breakdown: list[BreakdownValueItem] = Field(default_factory=list)


class ResultAnalysisModuleStats(BaseModel):
    key: str
    label: str
    analyzed_count: int
    covered_count: int
    coverage_rate: float
    avg_item_count: float


class ProcessEvaluationSummaryStats(BaseModel):
    evaluated_count: int
    avg_total_score: float | None
    max_total_score: float
    pass_rate: float
    issue_count: int
    avg_passed_sections: float


class ProcessEvaluationSectionStats(BaseModel):
    code: str
    name: str
    evaluated_count: int
    avg_score: float | None
    max_score: float
    pass_count: int
    pass_rate: float
    issue_count: int


class ProcessEvaluationIssueItem(BaseModel):
    recording_id: str
    analysis_task_id: str
    file_name: str
    recorded_at: str | None
    staff_id: str | None
    staff_name: str | None
    section_code: str
    section_name: str
    checkpoint_code: str | None = None
    checkpoint_name: str | None = None
    description: str
    evidence: str | None = None


class DashboardStats(BaseModel):
    total_deal_amount: float
    total_closed_won_visits: int
    total_closed_won_customers: int
    total_tasks: int
    done_count: int
    running_count: int
    failed_count: int
    total_tag_count: int
    avg_tag_count: float
    total_indication_count: int
    avg_indication_count: float
    avg_score: float
    max_score: float
    min_score: float
    score_distribution: list[ScoreDistItem]
    dimension_averages: list[DimensionAvg]
    dialogue_types: list[DialogueTypeItem]
    concern_types: list[ConcernTypeItem]
    recent_low_scores: list[RecentTask]
    positive_example_recordings: list[DashboardExampleRecordingItem]
    negative_example_recordings: list[DashboardExampleRecordingItem]
    # 业务统计
    total_customers: int
    total_visits: int
    visit_status_dist: list[VisitStatusItem]
    visit_trend: list[VisitTrendItem]
    visit_trend_scope: str
    visit_trend_hospital_code: str | None
    visit_trend_hospital_name: str | None
    visit_trend_can_select_hospital: bool
    visit_trend_hospital_options: list[HospitalOptionItem]
    score_trend: list[ScoreTrendItem]
    dashboard_scope: str
    dashboard_can_select_scope: bool
    dashboard_can_select_hospital: bool
    dashboard_hospital_code: str | None
    dashboard_hospital_name: str | None
    dashboard_hospital_options: list[HospitalOptionItem]
    dashboard_can_select_staff: bool
    dashboard_staff_id: str | None
    dashboard_staff_name: str | None
    dashboard_staff_options: list[DashboardStaffOptionItem]
    staff_stats: list[StaffStatsItem]
    score_staff_stats: list[StaffStatsItem]
    total_recordings: int
    quality_passed_recordings: int
    recordings_with_visits: int
    recordings_uploaded: int
    recordings_transcribed: int
    # 转写 / 片段统计
    total_transcripts: int
    transcripts_completed: int
    transcripts_failed: int
    total_segments: int
    segments_with_visit: int
    tag_breakdown: list[BreakdownItem]
    indication_breakdown: list[BreakdownItem]
    result_analysis_modules: list[ResultAnalysisModuleStats]
    process_evaluation_summary: ProcessEvaluationSummaryStats
    process_evaluation_sections: list[ProcessEvaluationSectionStats]
    process_evaluation_issues: list[ProcessEvaluationIssueItem]


# Simple in-memory cache keyed by permission scope.
_cache: dict[str, tuple[float, DashboardStats]] = {}
_cache_locks: dict[str, asyncio.Lock] = {}
_CACHE_TTL = 60  # seconds
_REDIS_CACHE_TTL = 60  # seconds; shared across all workers
_REDIS_KEY_PREFIX = "dashboard:stats:v1:"
_redis_client = None  # lazy redis.asyncio.Redis
_redis_disabled = False  # set True after repeated errors
_dashboard_cache_logger = logging.getLogger(__name__)


def _redis_cache_key(cache_key: str) -> str:
    digest = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()
    return f"{_REDIS_KEY_PREFIX}{digest}"


async def _get_redis_client():
    global _redis_client, _redis_disabled
    if _redis_disabled:
        return None
    if _redis_client is not None:
        return _redis_client
    try:
        import redis.asyncio as redis_asyncio  # type: ignore
    except Exception as exc:  # pragma: no cover - redis package required
        _dashboard_cache_logger.warning("dashboard L2 cache disabled: %s", exc)
        _redis_disabled = True
        return None
    try:
        _redis_client = redis_asyncio.from_url(
            get_settings().redis_url,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
            health_check_interval=30,
        )
        await _redis_client.ping()
    except Exception as exc:
        _dashboard_cache_logger.warning("dashboard L2 cache disabled (ping failed): %s", exc)
        _redis_client = None
        _redis_disabled = True
        return None
    return _redis_client


async def _redis_cache_get(cache_key: str) -> DashboardStats | None:
    cli = await _get_redis_client()
    if cli is None:
        return None
    try:
        raw = await cli.get(_redis_cache_key(cache_key))
    except Exception as exc:
        _dashboard_cache_logger.warning("dashboard L2 GET failed: %s", exc)
        return None
    if raw is None:
        return None
    try:
        return DashboardStats.model_validate_json(raw)
    except Exception as exc:
        _dashboard_cache_logger.warning("dashboard L2 decode failed: %s", exc)
        return None


async def _redis_cache_set(cache_key: str, value: DashboardStats) -> None:
    cli = await _get_redis_client()
    if cli is None:
        return
    try:
        payload = value.model_dump_json()
        await cli.set(_redis_cache_key(cache_key), payload, ex=_REDIS_CACHE_TTL)
    except Exception as exc:
        _dashboard_cache_logger.warning("dashboard L2 SET failed: %s", exc)


_SUMMARY_ANALYSIS_SAMPLE_LIMIT = 300
_SUMMARY_PROCESS_ISSUE_LIMIT = 80
_IGNORED_TAG_VALUES = {"未明确", "未提及", "未知", "无", "N/A", "-"}
_MAX_OPEN_TAG_VALUE_ITEMS = 8
_CONSULTATION_TOTAL_SCORE_MAX = 6.0
_CONSULTATION_DIMENSION_SCORE_MAX = 1.0
_CONSULTATION_DIMENSION_ORDER = [
    "医美专业知识",
    "适应症获取",
    "顾客标签获取",
    "医院和医生介绍",
    "老带新等特别事项",
    "负面交流检测",
]
_RESULT_ANALYSIS_MODULES: list[tuple[str, str]] = [
    ("chief", "主诉与适应症"),
    ("factors", "成交影响因素"),
    ("recommendations", "推荐方案"),
    ("outcome", "成交情况"),
    ("profile", "顾客标签"),
]
_EMPTY_MEANINGLESS_TEXT = {"", "-", "无", "未明确", "未知", "未提及", "N/A", "n/a", "null", "None"}


def _should_ignore_dashboard_tag_value(category: str, value: str) -> bool:
    if category == NEGATIVE_PROJECT_TAG_CATEGORY and value == NEGATIVE_PROJECT_EMPTY_VALUE:
        return False
    if value in _IGNORED_TAG_VALUES:
        return True
    return not is_valid_profile_tag_value(category, value)


def _extract_recording_id(file_name: str) -> str | None:
    if file_name.startswith("recording_") and file_name.endswith(".json"):
        return file_name.removeprefix("recording_").removesuffix(".json")
    return None


def _dedupe_done_tasks_by_file_name(done_tasks: list[AnalysisTask]) -> list[AnalysisTask]:
    unique_tasks: list[AnalysisTask] = []
    seen_file_names: set[str] = set()
    for task in done_tasks:
        dedupe_key = str(task.file_name or "").strip() or task.id
        if dedupe_key in seen_file_names:
            continue
        seen_file_names.add(dedupe_key)
        unique_tasks.append(task)
    return unique_tasks


def _to_local_date(value: datetime | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.date()
        return value.astimezone(timezone(timedelta(hours=8))).date()
    return None


def _week_label(week_start: date) -> tuple[str, str]:
    iso_year, iso_week, _ = week_start.isocalendar()
    week_end = week_start + timedelta(days=6)
    return (
        f"{iso_year}年第{iso_week}周",
        f"({week_start:%m/%d} - {week_end:%m/%d})",
    )


def _start_of_week(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _score_week_label(week_start: date) -> str:
    week_end = week_start + timedelta(days=6)
    if week_start.month == week_end.month:
        return f"{week_start:%m/%d}-{week_end:%d}"
    return f"{week_start:%m/%d}-{week_end:%m/%d}"


def _score_day_label(day_value: date) -> str:
    return f"{day_value:%m/%d}"


def _resolve_score_trend_mode(date_from: date | None, date_to: date | None) -> str:
    if not date_from and not date_to:
        return "week"

    end = date_to or date.today()
    start = date_from or end
    if start > end:
        start, end = end, start
    return "day" if (end - start).days <= 6 else "week"


def _resolve_score_trend_days(date_from: date | None, date_to: date | None) -> list[date]:
    end = date_to or date.today()
    start = date_from or end
    if start > end:
        start, end = end, start
    day_count = (end - start).days + 1
    return [start + timedelta(days=offset) for offset in range(max(day_count, 0))]


def _resolve_score_trend_weeks(date_from: date | None, date_to: date | None) -> list[date]:
    if date_from or date_to:
        end = date_to or date.today()
        start = date_from or end
    else:
        end = date.today()
        start = _start_of_week(end) - timedelta(weeks=7)

    if start > end:
        start, end = end, start

    start_week = _start_of_week(start)
    end_week = _start_of_week(end)
    week_count = ((end_week - start_week).days // 7) + 1
    return [start_week + timedelta(weeks=offset) for offset in range(max(week_count, 0))]


def _dimension_numeric_score(raw: Any) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _has_meaningful_text(value: Any) -> bool:
    return _clean_text(value) not in _EMPTY_MEANINGLESS_TEXT


def _count_meaningful_strings(items: Any) -> int:
    return sum(1 for item in _as_list(items) if _has_meaningful_text(item))


def _count_dict_items(items: Any, *fields: str) -> int:
    count = 0
    for item in _as_list(items):
        if isinstance(item, dict):
            if fields and not any(_has_meaningful_text(item.get(field)) for field in fields):
                continue
            count += 1
        elif _has_meaningful_text(item):
            count += 1
    return count


def _result_analysis_module_item_counts(result: dict[str, Any]) -> dict[str, int]:
    consultation_result = _as_dict(result.get("consultation_result"))
    chief = _as_dict(consultation_result.get("chief_complaint_and_indications"))
    deal_factors = _as_dict(consultation_result.get("deal_factors"))
    recommended_plan = _as_dict(consultation_result.get("recommended_plan"))
    deal_outcome = _as_dict(consultation_result.get("deal_outcome"))
    profile_summary = _as_dict(consultation_result.get("customer_profile_summary"))

    primary_demand_count = _count_meaningful_strings(chief.get("primary_demands"))
    if primary_demand_count == 0:
        primary_demand_count = _count_dict_items(
            _as_dict(result.get("customer_primary_demands")).get("items"),
            "demand",
            "content",
            "text",
        )
    indication_count = _count_dict_items(
        _as_dict(result.get("standardized_indications")).get("items"),
        "indication_name",
        "indication_code",
    )
    if indication_count == 0:
        indication_count = _count_meaningful_strings(chief.get("standardized_indications"))

    budget_count = 1 if _has_meaningful_text(deal_factors.get("budget") or _as_dict(result.get("consumption_intent")).get("budget")) else 0
    concern_count = _count_meaningful_strings(deal_factors.get("concerns"))
    if concern_count == 0:
        concern_count = _count_dict_items(_as_dict(result.get("customer_concerns")).get("items"), "content", "type")
    decision_factor_count = _count_meaningful_strings(
        deal_factors.get("decision_factors")
        or _as_dict(result.get("consumption_intent")).get("decision_factors")
    )

    recommendation_count = _count_dict_items(recommended_plan.get("items"), "plan", "recommendation", "content")
    if recommendation_count == 0:
        recommendation_count = _count_dict_items(
            _as_dict(result.get("staff_recommendations")).get("items"),
            "product_or_solution",
            "recommendation",
            "content",
        )

    outcome_count = 0
    if _has_meaningful_text(deal_outcome.get("status")):
        outcome_count += 1
    outcome_count += _count_meaningful_strings(deal_outcome.get("deal_items"))
    outcome_count += 1 if _has_meaningful_text(deal_outcome.get("amount")) else 0
    outcome_count += _count_meaningful_strings(deal_outcome.get("loss_reasons"))

    profile = _as_dict(result.get("customer_profile"))
    profile_tag_count = _count_dict_items(profile.get("tags"), "category", "value")
    if profile_tag_count == 0:
        profile_tag_count = _count_dict_items(profile_summary.get("tags"), "category", "value")
    profile_age_count = 1 if _has_meaningful_text(profile_summary.get("age") or profile.get("age")) else 0

    return {
        "chief": primary_demand_count + indication_count,
        "factors": budget_count + concern_count + decision_factor_count,
        "recommendations": recommendation_count,
        "outcome": outcome_count,
        "profile": profile_tag_count + profile_age_count,
    }


def _count_profile_tags(result: dict[str, Any]) -> int:
    profile = _as_dict(result.get("customer_profile"))
    tags = _as_list(profile.get("tags"))
    if tags:
        return sum(
            1
            for item in tags
            if isinstance(item, dict)
            and (_has_meaningful_text(item.get("category")) or _has_meaningful_text(item.get("value")))
        )
    consultation_result = _as_dict(result.get("consultation_result"))
    profile_summary = _as_dict(consultation_result.get("customer_profile_summary"))
    return sum(
        1
        for item in _as_list(profile_summary.get("tags"))
        if isinstance(item, dict)
        and (_has_meaningful_text(item.get("category")) or _has_meaningful_text(item.get("value")))
    )


def _count_indications(result: dict[str, Any]) -> int:
    indications = _as_dict(result.get("standardized_indications"))
    items = [
        item
        for item in _as_list(indications.get("items"))
        if isinstance(item, dict)
        and (_has_meaningful_text(item.get("indication_name")) or _has_meaningful_text(item.get("indication_code")))
    ]
    if items:
        return len(items)
    consultation_result = _as_dict(result.get("consultation_result"))
    chief = _as_dict(consultation_result.get("chief_complaint_and_indications"))
    return _count_meaningful_strings(chief.get("standardized_indications"))


def _count_concerns(result: dict[str, Any]) -> int:
    concerns = _as_dict(result.get("customer_concerns"))
    items = [
        item
        for item in _as_list(concerns.get("items"))
        if isinstance(item, dict) and (_has_meaningful_text(item.get("content")) or _has_meaningful_text(item.get("type")))
    ]
    if items:
        return len(items)
    consultation_result = _as_dict(result.get("consultation_result"))
    deal_factors = _as_dict(consultation_result.get("deal_factors"))
    return _count_meaningful_strings(deal_factors.get("concerns"))


def _build_example_recording_summary(result: dict[str, Any]) -> str:
    consultation_result = _as_dict(result.get("consultation_result"))
    chief = _as_dict(consultation_result.get("chief_complaint_and_indications"))
    outcome = _as_dict(consultation_result.get("deal_outcome"))
    process_evaluation = _as_dict(result.get("consultation_process_evaluation"))
    primary_demands = [
        _clean_text(item)
        for item in _as_list(chief.get("primary_demands"))
        if _has_meaningful_text(item)
    ][:2]
    if not primary_demands:
        primary = _as_dict(result.get("customer_primary_demands"))
        for item in _as_list(primary.get("items")):
            if isinstance(item, dict):
                text = _clean_text(item.get("demand") or item.get("content") or item.get("text"))
            else:
                text = _clean_text(item)
            if text:
                primary_demands.append(text)
            if len(primary_demands) >= 2:
                break

    fragments: list[str] = []
    if primary_demands:
        fragments.append(f"主诉：{'；'.join(primary_demands)}")
    outcome_status = _clean_text(outcome.get("status"))
    if outcome_status:
        fragments.append(f"成交：{outcome_status}")
    process_summary = _clean_text(process_evaluation.get("overall_summary"))
    if process_summary:
        fragments.append(process_summary)
    if not fragments:
        fragments.append("暂无可读摘要，可进入详情查看完整分析。")
    return "；".join(fragments)


def _is_process_section_passed(section: dict[str, Any]) -> bool:
    status = _clean_text(section.get("status"))
    if status in {"达标", "通过", "合格"}:
        return True
    score = _dimension_numeric_score(section.get("point_score"))
    max_score = _dimension_numeric_score(section.get("max_score")) or 1.0
    return score is not None and max_score > 0 and score >= max_score * 0.999


def _process_section_issue_count(section: dict[str, Any]) -> int:
    issue_count = len(_as_list(section.get("issues")))
    for checkpoint in _as_list(section.get("checkpoints")):
        if isinstance(checkpoint, dict):
            issue_count += len(_as_list(checkpoint.get("issues")))
    return issue_count


def _format_issue_evidence(value: Any) -> str | None:
    if isinstance(value, list):
        lines = [_clean_text(item) for item in value if _has_meaningful_text(item)]
        return "\n".join(lines) if lines else None
    if _has_meaningful_text(value):
        return _clean_text(value)
    return None


def _iter_process_section_issues(section: dict[str, Any]) -> list[dict[str, str | None]]:
    section_code = _clean_text(section.get("code"))
    section_name = _clean_text(section.get("name")) or section_code
    rows: list[dict[str, str | None]] = []

    for issue in _as_list(section.get("issues")):
        issue_dict = issue if isinstance(issue, dict) else {"description": issue}
        description = _clean_text(issue_dict.get("description") or issue_dict.get("summary") or issue)
        evidence = _format_issue_evidence(issue_dict.get("evidence"))
        if not description and not evidence:
            continue
        rows.append(
            {
                "section_code": section_code,
                "section_name": section_name,
                "checkpoint_code": None,
                "checkpoint_name": None,
                "description": description or "未填写问题描述",
                "evidence": evidence,
            }
        )

    for checkpoint in _as_list(section.get("checkpoints")):
        if not isinstance(checkpoint, dict):
            continue
        checkpoint_code = _clean_text(checkpoint.get("code"))
        checkpoint_name = _clean_text(checkpoint.get("name")) or checkpoint_code
        checkpoint_summary = _clean_text(checkpoint.get("summary"))
        checkpoint_evidence = _format_issue_evidence(checkpoint.get("evidence"))
        for issue in _as_list(checkpoint.get("issues")):
            issue_dict = issue if isinstance(issue, dict) else {"description": issue}
            description = _clean_text(issue_dict.get("description") or issue_dict.get("summary") or issue) or checkpoint_summary
            evidence = _format_issue_evidence(issue_dict.get("evidence")) or checkpoint_evidence
            if not description and not evidence:
                continue
            rows.append(
                {
                    "section_code": section_code,
                    "section_name": section_name,
                    "checkpoint_code": checkpoint_code or None,
                    "checkpoint_name": checkpoint_name or None,
                    "description": description or "未填写问题描述",
                    "evidence": evidence,
                }
            )

    return rows


def _process_section_key(section: dict[str, Any]) -> str:
    code = _clean_text(section.get("code"))
    if code:
        return code
    return _clean_text(section.get("name"))


def _process_blueprint_sections() -> list[tuple[str, str]]:
    return [
        (_clean_text(item.get("code")), _clean_text(item.get("name")))
        for item in CONSULTATION_PROCESS_EVALUATION_BLUEPRINT
        if isinstance(item, dict)
    ]


def _normalize_consultation_dimension_score(raw_dimension: Any) -> tuple[str, float] | None:
    if not isinstance(raw_dimension, dict):
        return None

    name = normalize_consultation_dimension_name(raw_dimension.get("name"))
    if not name:
        return None

    point_score = _dimension_numeric_score(raw_dimension.get("point_score"))
    if point_score is None:
        raw_score = _dimension_numeric_score(raw_dimension.get("score"))
        max_score = _dimension_numeric_score(raw_dimension.get("max_score"))
        if raw_score is not None:
            if max_score and max_score > 1:
                point_score = raw_score / max_score
            elif raw_score > 1:
                point_score = raw_score / 10.0
            else:
                point_score = raw_score

    if point_score is None:
        return None

    return name, max(0.0, min(point_score, _CONSULTATION_DIMENSION_SCORE_MAX))


def _dimension_order_index(name: str) -> int:
    try:
        return _CONSULTATION_DIMENSION_ORDER.index(name)
    except ValueError:
        return len(_CONSULTATION_DIMENSION_ORDER)


def _consultation_dimension_scores(result: dict[str, Any]) -> list[tuple[str, float]]:
    evaluation = result.get("consultation_evaluation")
    if not isinstance(evaluation, dict):
        return []

    scores: list[tuple[str, float]] = []
    for raw_dimension in evaluation.get("dimensions") or []:
        normalized = _normalize_consultation_dimension_score(raw_dimension)
        if normalized is None:
            continue
        scores.append(normalized)
    return scores


def _consultation_total_score(result: dict[str, Any]) -> float | None:
    process_evaluation = result.get("consultation_process_evaluation")
    if isinstance(process_evaluation, dict):
        process_total = _dimension_numeric_score(process_evaluation.get("total_score"))
        if process_total is not None:
            max_total = _dimension_numeric_score(process_evaluation.get("max_total_score")) or 9.0
            return max(0.0, min(process_total, max_total))

        process_overall = _dimension_numeric_score(process_evaluation.get("overall_score"))
        if process_overall is not None:
            normalized_total = (process_overall / 10.0) * 9.0
            return max(0.0, min(round(normalized_total, 2), 9.0))

    evaluation = result.get("consultation_evaluation")
    if not isinstance(evaluation, dict):
        return None

    total_score = _dimension_numeric_score(evaluation.get("total_score"))
    if total_score is not None:
        return max(0.0, min(total_score, _CONSULTATION_TOTAL_SCORE_MAX))

    dimension_scores = _consultation_dimension_scores(result)
    if dimension_scores:
        return round(sum(score for _name, score in dimension_scores), 2)

    overall_score = _dimension_numeric_score(evaluation.get("overall_score"))
    if overall_score is None:
        return None

    normalized_total = (overall_score / 10.0) * _CONSULTATION_TOTAL_SCORE_MAX
    return max(0.0, min(round(normalized_total, 2), _CONSULTATION_TOTAL_SCORE_MAX))


def _resolve_tag_category_name(raw_category: str, category_options_map: dict[str, set[str]]) -> str:
    normalized = raw_category.strip()
    if not normalized:
        return normalized
    if normalized in category_options_map:
        return normalized
    for category_name in category_options_map:
        if normalized in category_name or category_name in normalized:
            return category_name
    return normalized


def _format_open_tag_detail(value_counter: Counter[str]) -> str | None:
    distinct_values = [value for value, _count in value_counter.most_common() if value]
    if not distinct_values:
        return None
    samples = distinct_values[:3]
    if len(distinct_values) > len(samples):
        return f"开放值，示例：{'、'.join(samples)} 等{len(distinct_values)}类"
    return f"开放值，示例：{'、'.join(samples)}"


def _staff_scope_filters(scope) -> list[Any]:
    if scope.staff_id:
        return [Staff.is_active.is_(True), managed_staff_scope_condition(scope, Staff.id)]
    return [false()]


def _staff_stats_scope(scope) -> PermissionScope:
    return scope


async def _resolve_dashboard_managed_staff_ids(db: AsyncSession, scope: PermissionScope) -> list[str] | None:
    # Global roles (super_admin/system_admin) without an explicitly selected
    # staff see all staff. When a hospital is selected the visible staff are
    # restricted to that hospital. None is the sentinel for "unrestricted".
    if is_global_role(scope.role) and not scope.staff_id:
        if scope.hospital_code:
            stmt = select(Staff.id).where(
                Staff.hospital_code == scope.hospital_code,
                Staff.is_active.is_(True),
            )
            return list((await db.execute(stmt)).scalars().all())
        return None
    if not scope.staff_id:
        return []
    if scope.role == "single_staff":
        return [scope.staff_id]

    role = normalize_permission_role(scope.role)
    actor_level = PERMISSION_ROLE_LEVELS.get(role, PERMISSION_ROLE_LEVELS["staff"])
    role_levels = {
        **PERMISSION_ROLE_LEVELS,
        **{
            legacy_role: PERMISSION_ROLE_LEVELS[normalized_role]
            for legacy_role, normalized_role in LEGACY_STAFF_PERMISSION_ROLE_MAP.items()
        },
    }
    rows = (
        await db.execute(
            select(StaffManagementRelation.subordinate_staff_id, Staff.permission_role)
            .join(Staff, Staff.id == StaffManagementRelation.subordinate_staff_id)
            .where(
                StaffManagementRelation.manager_staff_id == scope.staff_id,
                Staff.is_active.is_(True),
            )
        )
    ).all()
    ids: set[str] = {scope.staff_id}
    for subordinate_id, subordinate_role in rows:
        subordinate_level = role_levels.get(subordinate_role, PERMISSION_ROLE_LEVELS["staff"])
        if role == "super_admin" or subordinate_level <= actor_level:
            ids.add(subordinate_id)
    return list(ids)


def _dashboard_recording_scope_condition(managed_staff_ids: list[str] | None):
    if managed_staff_ids is None:
        from sqlalchemy import true
        return true()
    if not managed_staff_ids:
        return false()
    return Recording.staff_id.in_(managed_staff_ids)


def _dashboard_visit_scope_condition(visible_visit_ids: list[str] | None):
    if visible_visit_ids is None:
        from sqlalchemy import true
        return true()
    if not visible_visit_ids:
        return false()
    return Visit.id.in_(visible_visit_ids)


async def _resolve_dashboard_visible_visit_ids(db: AsyncSession, managed_staff_ids: list[str] | None) -> list[str] | None:
    if managed_staff_ids is None:
        return None
    if not managed_staff_ids:
        return []

    parts = [
        select(Visit.id.label("visit_id")).where(Visit.consultant_id.in_(managed_staff_ids)),
        select(Visit.id.label("visit_id")).where(Visit.doctor_id.in_(managed_staff_ids)),
        select(Visit.id.label("visit_id"))
        .join(Recording, Recording.visit_id == Visit.id)
        .where(Recording.staff_id.in_(managed_staff_ids)),
        select(Visit.id.label("visit_id"))
        .join(RecordingVisitLink, RecordingVisitLink.visit_id == Visit.id)
        .join(Recording, Recording.id == RecordingVisitLink.recording_id)
        .where(Recording.staff_id.in_(managed_staff_ids)),
    ]
    staff_meta = (
        await db.execute(
            select(Staff.external_account, Staff.hospital_code).where(
                Staff.id.in_(managed_staff_ids),
                Staff.external_account.is_not(None),
                Staff.hospital_code.is_not(None),
            )
        )
    ).all()
    if staff_meta:
        staff_by_hospital: dict[str, list[str]] = {}
        for external_account, hospital_code in staff_meta:
            if not external_account or not hospital_code:
                continue
            staff_by_hospital.setdefault(str(hospital_code), []).append(str(external_account))
        for hospital_code, external_accounts in staff_by_hospital.items():
            parts.append(
                select(Visit.id.label("visit_id"))
                .join(VisitOrder, VisitOrder.dzdh == Visit.external_visit_order_no)
                .where(
                    Visit.external_visit_order_no.is_not(None),
                    VisitOrder.jgbm == hospital_code,
                    or_(
                        VisitOrder.fzuer.in_(external_accounts),
                        VisitOrder.d_fzuer.in_(external_accounts),
                        VisitOrder.fzr_id_dq.in_(external_accounts),
                        VisitOrder.advxc.in_(external_accounts),
                        VisitOrder.assxc.in_(external_accounts),
                        VisitOrder.advyq.in_(external_accounts),
                        VisitOrder.yyuer.in_(external_accounts),
                        VisitOrder.vipkf.in_(external_accounts),
                        VisitOrder.d_vipkf.in_(external_accounts),
                    ),
                )
            )

    rows = (await db.execute(union(*parts))).all()
    return [row[0] for row in rows if row[0]]


def _staff_job_label(staff: Staff) -> str:
    if staff.is_doctor:
        return "医生"
    if staff.is_onsite_advisor:
        return "现场咨询"
    if staff.is_pre_advisor:
        return "院前顾问"
    if staff.is_doctor_assistant:
        return "医助"
    if staff.is_advisor_assistant:
        return "咨询助理"
    if staff.is_nurse:
        return "护士"
    if staff.is_cashier:
        return "收银"
    if staff.is_guide:
        return "导医"
    if staff.is_anesthetist:
        return "麻醉"
    if staff.is_vip_service:
        return "客服"

    normalized_permission_role = normalize_permission_role(getattr(staff, "permission_role", None))
    if normalized_permission_role != "staff":
        return PERMISSION_ROLE_LABELS.get(normalized_permission_role, "管理员")

    role_map = {
        "consultant": "咨询师",
        "doctor": "医生",
        "manager": "组长",
    }
    return role_map.get((getattr(staff, "role", "") or "").strip(), "普通员工")


def _current_badge_bound_staff_condition() -> Any:
    now = datetime.now(timezone.utc)
    current_binding_exists = exists().where(
        DeviceStaffBinding.staff_id == Staff.id,
        DeviceStaffBinding.device_id == Device.id,
        Device.is_active.is_(True),
        DeviceStaffBinding.effective_from <= now,
        or_(DeviceStaffBinding.effective_to.is_(None), DeviceStaffBinding.effective_to > now),
    )
    legacy_device_pointer_exists = exists().where(
        Device.staff_id == Staff.id,
        Device.is_active.is_(True),
    )
    legacy_staff_badge_exists = and_(
        Staff.badge_id.is_not(None),
        func.trim(Staff.badge_id) != "",
    )
    return or_(current_binding_exists, legacy_device_pointer_exists, legacy_staff_badge_exists)


def _cache_key_for_scope(scope) -> str:
    return "|".join(
        [
            scope.role or "",
            scope.staff_id or "",
            scope.hospital_code or "",
        ]
    )


def _recording_date_filters(date_from: date | None, date_to: date | None) -> list[Any]:
    filters: list[Any] = []
    if date_from:
        start_dt = datetime.combine(date_from, datetime.min.time(), tzinfo=_DISPLAY_TZ).astimezone(timezone.utc)
        filters.append(Recording.created_at >= start_dt)
    if date_to:
        end_dt = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=_DISPLAY_TZ).astimezone(timezone.utc)
        filters.append(Recording.created_at < end_dt)
    return filters


def _visit_activity_date_expr():
    return func.coalesce(Visit.visit_date, func.date(Visit.created_at))


def _visit_date_filters(date_from: date | None, date_to: date | None) -> list[Any]:
    visit_activity_date = _visit_activity_date_expr()
    filters: list[Any] = []
    if date_from:
        filters.append(visit_activity_date >= date_from)
    if date_to:
        filters.append(visit_activity_date <= date_to)
    return filters


def _visit_order_join_condition():
    visit_seg = func.coalesce(func.nullif(func.trim(Visit.external_visit_order_seg), ""), "")
    order_seg = func.coalesce(func.nullif(func.trim(VisitOrder.dzseg), ""), "")
    return and_(
        VisitOrder.dzdh == Visit.external_visit_order_no,
        visit_seg == order_seg,
    )


def _real_arrival_visit_order_condition():
    purpose_code = func.upper(func.trim(func.coalesce(VisitOrder.dymd, "")))
    purpose_label = func.trim(func.coalesce(VisitOrder.dymd_txt, ""))
    return and_(
        or_(purpose_code != "", purpose_label != ""),
        purpose_code.notin_(("X", "Z")),
        purpose_label.notin_(("未到院购买", "其他")),
    )


def _visit_hospital_condition(hospital_code: str):
    return or_(
        exists(
            select(VisitOrder.id).where(
                VisitOrder.dzdh == Visit.external_visit_order_no,
                VisitOrder.jgbm == hospital_code,
            )
        ),
        exists(
            select(Staff.id).where(
                Staff.id == Visit.consultant_id,
                Staff.hospital_code == hospital_code,
            )
        ),
        exists(
            select(Staff.id).where(
                Staff.id == Visit.doctor_id,
                Staff.hospital_code == hospital_code,
            )
        ),
    )


async def _load_visible_hospitals(
    db: AsyncSession,
    scope,
    current_user: User,
) -> list[HospitalOptionItem]:
    async def load_hospital_name_map(hospital_codes: list[str]) -> dict[str, str]:
        normalized_codes = [str(code).strip() for code in hospital_codes if str(code or "").strip()]
        if not normalized_codes:
            return {}
        rows = (
            await db.execute(
                select(
                    Staff.hospital_code,
                    func.max(Staff.hospital_short_name),
                )
                .where(
                    Staff.hospital_code.in_(normalized_codes),
                    Staff.hospital_code.isnot(None),
                    Staff.hospital_code != "",
                    Staff.hospital_short_name.isnot(None),
                    Staff.hospital_short_name != "",
                )
                .group_by(Staff.hospital_code)
            )
        ).all()
        return {
            str(hospital_code).strip(): str(hospital_name).strip()
            for hospital_code, hospital_name in rows
            if hospital_code and hospital_name
        }

    if not scope.staff_id:
        return []

    rows = (
        await db.execute(
            select(Staff.hospital_code)
            .where(
                Staff.is_active.is_(True),
                managed_staff_scope_condition(scope, Staff.id),
                Staff.hospital_code.isnot(None),
                Staff.hospital_code != "",
            )
            .group_by(Staff.hospital_code)
            .order_by(Staff.hospital_code.asc())
        )
    ).all()

    hospital_codes = [str(hospital_code).strip() for (hospital_code,) in rows if hospital_code]
    hospital_name_map = await load_hospital_name_map(hospital_codes)

    options = [
        HospitalOptionItem(
            hospital_code=hospital_code,
            hospital_name=hospital_name_map.get(hospital_code) or hospital_code,
        )
        for hospital_code in hospital_codes
    ]
    return options


def _normalize_dashboard_scope_mode(raw_scope_mode: str | None) -> str:
    return "mine" if (raw_scope_mode or "").strip() == "mine" else "all"


def _normalize_dashboard_staff_id(raw_staff_id: str | None) -> str | None:
    normalized = (raw_staff_id or "").strip()
    return normalized or None


def _normalize_dashboard_detail_level(raw_detail_level: str | None) -> str:
    return "full" if (raw_detail_level or "").strip().lower() == "full" else "summary"


async def _load_dashboard_staff_options(
    db: AsyncSession,
    *,
    base_scope: PermissionScope,
    selected_hospital_code: str | None,
) -> list[DashboardStaffOptionItem]:
    stmt = select(Staff).where(Staff.is_active.is_(True))

    if not base_scope.staff_id:
        return []
    stmt = stmt.where(managed_staff_scope_condition(base_scope, Staff.id))
    if selected_hospital_code:
        stmt = stmt.where(Staff.hospital_code == selected_hospital_code)

    rows = (
        await db.execute(
            stmt.order_by(Staff.name.asc(), Staff.id.asc())
        )
    ).scalars().all()

    return [
        DashboardStaffOptionItem(
            staff_id=staff.id,
            staff_name=staff.name,
            hospital_code=getattr(staff, "hospital_code", None),
            job_label=_staff_job_label(staff),
        )
        for staff in rows
    ]


def _resolve_dashboard_scope(
    *,
    base_scope: PermissionScope,
    scope_mode: str,
    selected_hospital_code: str | None,
    selected_staff_id: str | None,
) -> PermissionScope:
    normalized_role = normalize_permission_role(base_scope.role)
    if selected_staff_id and (normalized_role != "staff" or selected_staff_id == base_scope.staff_id):
        return PermissionScope(
            role="single_staff",
            staff_id=selected_staff_id,
            hospital_code=selected_hospital_code or base_scope.hospital_code,
        )
    if scope_mode == "mine" and base_scope.staff_id:
        return PermissionScope(
            role="single_staff",
            staff_id=base_scope.staff_id,
            hospital_code=base_scope.hospital_code,
        )
    return PermissionScope(
        role=normalized_role,
        staff_id=base_scope.staff_id,
        hospital_code=selected_hospital_code or base_scope.hospital_code,
    )


async def _build_staff_stats(
    db: AsyncSession,
    *,
    scope: PermissionScope,
    done_tasks: list[AnalysisTask],
    recording_meta_map: dict[str, dict[str, Any]],
    date_from: date | None,
    date_to: date | None,
    sort_mode: str = "business",
    managed_staff_ids: list[str] | None = None,
    visible_visit_ids: list[str] | None = None,
) -> list[StaffStatsItem]:
    if managed_staff_ids is None:
        managed_staff_ids = await _resolve_dashboard_managed_staff_ids(db, scope)
    if not managed_staff_ids:
        return []
    recording_scope_filter = _dashboard_recording_scope_condition(managed_staff_ids)
    if visible_visit_ids is None:
        visible_visit_ids = await _resolve_dashboard_visible_visit_ids(db, managed_staff_ids)
    visit_scope_filter = _dashboard_visit_scope_condition(visible_visit_ids)
    scoped_staff = (
        await db.execute(
            select(Staff)
            .where(Staff.is_active.is_(True), Staff.id.in_(managed_staff_ids))
            .order_by(Staff.hospital_code.asc(), Staff.name.asc())
        )
    ).scalars().all()
    recording_date_filters = _recording_date_filters(date_from, date_to)
    visit_date_filters = _visit_date_filters(date_from, date_to)
    recording_not_filtered = func.lower(func.coalesce(Recording.status, "")) != "filtered"

    staff_stats_map: dict[str, dict[str, Any]] = {
        staff.id: {
            "staff_id": staff.id,
            "staff_name": staff.name,
            "hospital_code": getattr(staff, "hospital_code", None),
            "hospital_name": getattr(staff, "hospital_short_name", None) or getattr(staff, "hospital_code", None),
            "job_label": _staff_job_label(staff),
            "visit_count": 0,
            "closed_won_count": 0,
            "principal_amount": 0.0,
            "recording_count": 0,
            "linked_visit_ids": set(),
            "analyzed_count": 0,
            "score_values": [],
            "dimension_scores": {},
        }
        for staff in scoped_staff
    }

    recording_count_rows = (
        await db.execute(
            select(Recording.staff_id, func.count(Recording.id))
            .where(
                recording_scope_filter,
                recording_not_filtered,
                Recording.staff_id.is_not(None),
                *recording_date_filters,
            )
            .group_by(Recording.staff_id)
        )
    ).all()
    for staff_id, recording_count in recording_count_rows:
        bucket = staff_stats_map.get(staff_id)
        if bucket:
            bucket["recording_count"] = int(recording_count or 0)

    analyzed_count_rows = (
        await db.execute(
            select(Recording.staff_id, func.count(func.distinct(Recording.id)))
            .join(AnalysisTask, AnalysisTask.file_name == func.concat("recording_", Recording.id, ".json"))
            .where(
                recording_scope_filter,
                Recording.staff_id.is_not(None),
                AnalysisTask.status == "done",
                AnalysisTask.result.isnot(None),
                *recording_date_filters,
            )
            .group_by(Recording.staff_id)
        )
    ).all()
    for staff_id, analyzed_count in analyzed_count_rows:
        bucket = staff_stats_map.get(staff_id)
        if bucket:
            bucket["analyzed_count"] = int(analyzed_count or 0)

    visit_staff_rows = (
        await db.execute(
            select(
                Visit.consultant_id,
                Visit.doctor_id,
                Visit.status,
                Visit.deposit_principal,
            ).where(visit_scope_filter, *_visit_date_filters(date_from, date_to))
        )
    ).all()

    for consultant_id, doctor_id, status, deposit_principal in visit_staff_rows:
        participant_ids = {staff_id for staff_id in (consultant_id, doctor_id) if staff_id}
        for participant_id in participant_ids:
            bucket = staff_stats_map.get(participant_id)
            if not bucket:
                continue
            bucket["visit_count"] += 1
            if status == "closed_won":
                bucket["closed_won_count"] += 1
                bucket["principal_amount"] += float(deposit_principal or 0)

    linked_visit_rows = (
        await db.execute(
            select(Recording.staff_id, RecordingVisitLink.visit_id)
            .join(Recording, Recording.id == RecordingVisitLink.recording_id)
            .join(Visit, Visit.id == RecordingVisitLink.visit_id)
            .where(
                recording_scope_filter,
                recording_not_filtered,
                Recording.staff_id.is_not(None),
                RecordingVisitLink.visit_id.is_not(None),
                *recording_date_filters,
                *visit_date_filters,
            )
        )
    ).all()
    direct_visit_rows = (
        await db.execute(
            select(Recording.staff_id, Recording.visit_id)
            .join(Visit, Visit.id == Recording.visit_id)
            .where(
                recording_scope_filter,
                recording_not_filtered,
                Recording.staff_id.is_not(None),
                Recording.visit_id.is_not(None),
                *recording_date_filters,
                *visit_date_filters,
            )
        )
    ).all()
    for staff_id, visit_id in [*linked_visit_rows, *direct_visit_rows]:
        bucket = staff_stats_map.get(staff_id)
        if bucket and visit_id:
            bucket["linked_visit_ids"].add(visit_id)

    for task in done_tasks:
        recording_id = _extract_recording_id(task.file_name)
        if not recording_id:
            continue
        meta = recording_meta_map.get(recording_id) or {}
        staff_id = meta.get("staff_id")
        if not staff_id:
            continue
        bucket = staff_stats_map.get(staff_id)
        if not bucket:
            continue
        result = normalize_analysis_result(task.result) if isinstance(task.result, dict) else None
        score_value = _consultation_total_score(result) if isinstance(result, dict) else None
        if score_value is not None:
            bucket["score_values"].append(score_value)
        dimension_scores = _consultation_dimension_scores(result) if isinstance(result, dict) else []
        for dimension_name, dimension_score in dimension_scores:
            if dimension_score is None:
                continue
            bucket["dimension_scores"].setdefault(dimension_name, []).append(dimension_score)

    rows = [
        StaffStatsItem(
            staff_id=item["staff_id"],
            staff_name=item["staff_name"],
            hospital_code=item["hospital_code"],
            hospital_name=item["hospital_name"],
            job_label=item["job_label"],
            visit_count=int(item["visit_count"]),
            closed_won_count=int(item["closed_won_count"]),
            principal_amount=round(float(item["principal_amount"]), 2),
            recording_count=int(item["recording_count"]),
            linked_visit_count=len(item["linked_visit_ids"]),
            analyzed_count=int(item["analyzed_count"]),
            avg_score=round(sum(item["score_values"]) / len(item["score_values"]), 2)
            if item["score_values"]
            else None,
            dimension_averages=sorted(
                [
                    DimensionAvg(name=name, avg_score=round(sum(values) / len(values), 2))
                    for name, values in item["dimension_scores"].items()
                    if values
                ],
                key=lambda avg: _dimension_order_index(avg.name),
            ),
        )
        for item in staff_stats_map.values()
    ]

    if sort_mode == "score":
        return sorted(
            rows,
            key=lambda item: (
                -(item.avg_score if item.avg_score is not None else -1),
                -item.analyzed_count,
                -item.recording_count,
                -item.linked_visit_count,
                item.staff_name,
            ),
        )

    return sorted(
        rows,
        key=lambda item: (
            -item.principal_amount,
            -item.closed_won_count,
            -item.visit_count,
            -item.recording_count,
            -item.linked_visit_count,
            item.staff_name,
        ),
    )


@router.get("", response_model=DashboardStats)
async def get_dashboard(
    hospital_code: str | None = Query(default=None),
    scope_mode: str | None = Query(default=None),
    staff_id: str | None = Query(default=None),
    detail_level: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    global _cache
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    base_scope = await build_permission_scope(current_user)
    requested_hospital_code = (hospital_code or "").strip() or None
    requested_scope_mode = _normalize_dashboard_scope_mode(scope_mode)
    requested_staff_id = _normalize_dashboard_staff_id(staff_id)
    requested_detail_level = _normalize_dashboard_detail_level(detail_level)
    hospital_options = await _load_visible_hospitals(db, base_scope, current_user)
    hospital_option_map = {item.hospital_code: item for item in hospital_options}
    effective_scope_mode = requested_scope_mode if requested_scope_mode != "mine" or base_scope.staff_id else "all"
    selected_dashboard_hospital_code: str | None = None
    if effective_scope_mode == "all" and hospital_options:
        if requested_hospital_code and requested_hospital_code in hospital_option_map:
            selected_dashboard_hospital_code = requested_hospital_code
        elif base_scope.hospital_code and base_scope.hospital_code in hospital_option_map:
            selected_dashboard_hospital_code = base_scope.hospital_code
        else:
            selected_dashboard_hospital_code = hospital_options[0].hospital_code
    dashboard_staff_options = await _load_dashboard_staff_options(
        db,
        base_scope=base_scope,
        selected_hospital_code=selected_dashboard_hospital_code or base_scope.hospital_code,
    )
    dashboard_staff_option_map = {item.staff_id: item for item in dashboard_staff_options}
    selected_dashboard_staff_id: str | None = None
    if effective_scope_mode == "all" and requested_staff_id and requested_staff_id in dashboard_staff_option_map:
        selected_dashboard_staff_id = requested_staff_id
    scope = _resolve_dashboard_scope(
        base_scope=base_scope,
        scope_mode=effective_scope_mode,
        selected_hospital_code=selected_dashboard_hospital_code,
        selected_staff_id=selected_dashboard_staff_id,
    )
    cache_key = "|".join(
        [
            _cache_key_for_scope(scope),
            effective_scope_mode,
            requested_hospital_code or "",
            requested_staff_id or "",
            requested_detail_level,
            date_from.isoformat() if date_from else "",
            date_to.isoformat() if date_to else "",
        ]
    )
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached is not None and now - cached[0] < _CACHE_TTL:
        return cached[1]

    lock = _cache_locks.get(cache_key)
    if lock is None:
        lock = _cache_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        # Double-check L1 after acquiring lock.
        now = time.monotonic()
        cached = _cache.get(cache_key)
        if cached is not None and now - cached[0] < _CACHE_TTL:
            return cached[1]
        # L2 (Redis) — shared across workers.
        l2_value = await _redis_cache_get(cache_key)
        if l2_value is not None:
            _cache[cache_key] = (time.monotonic(), l2_value)
            return l2_value
        recording_date_filters = _recording_date_filters(date_from, date_to)
        visit_date_filters = _visit_date_filters(date_from, date_to)
        managed_staff_ids = await _resolve_dashboard_managed_staff_ids(db, scope)
        recording_scope_filter = _dashboard_recording_scope_condition(managed_staff_ids)
        visible_visit_ids = await _resolve_dashboard_visible_visit_ids(db, managed_staff_ids)
        visit_scope_filter = _dashboard_visit_scope_condition(visible_visit_ids)

        allowed_recordings_subquery = (
            select(
                Recording.id.label("recording_id"),
                func.concat("recording_", Recording.id, ".json").label("analysis_file_name"),
            )
            .where(recording_scope_filter, *recording_date_filters)
            .subquery()
        )

        # ── Task counts (1 query) ──
        task_row = (await db.execute(
            select(
                func.count(AnalysisTask.id),
                func.count(case((AnalysisTask.status == "done", 1))),
                func.count(case((AnalysisTask.status.in_(["running", "pending"]), 1))),
                func.count(case((AnalysisTask.status == "failed", 1))),
            )
            .join(
                allowed_recordings_subquery,
                allowed_recordings_subquery.c.analysis_file_name == AnalysisTask.file_name,
            )
        )).one()
        total, done, running, failed = int(task_row[0]), int(task_row[1]), int(task_row[2]), int(task_row[3])

        # Aggregate from result JSON: dimensions, dialogue types, concerns
        await ensure_tag_categories(db)
        tag_categories = (
            await db.execute(
                select(TagCategory)
                .where(TagCategory.is_active.is_(True))
                .options(selectinload(TagCategory.tags))
                .order_by(TagCategory.sort_order)
            )
        ).scalars().all()
        tag_category_options_map: dict[str, set[str]] = {
            str(category.name).strip(): {
                str(tag.name).strip()
                for tag in category.tags
                if tag.is_active and str(tag.name).strip()
            }
            for category in tag_categories
            if str(category.name).strip()
        }
        done_tasks_stmt = (
            select(AnalysisTask)
            .join(
                allowed_recordings_subquery,
                allowed_recordings_subquery.c.analysis_file_name == AnalysisTask.file_name,
            )
            .where(AnalysisTask.status == "done", AnalysisTask.result.isnot(None))
            .order_by(
                AnalysisTask.completed_at.desc().nullslast(),
                AnalysisTask.updated_at.desc(),
                AnalysisTask.created_at.desc(),
            )
        )
        done_tasks = (await db.execute(done_tasks_stmt)).scalars().all()
        unique_done_tasks = _dedupe_done_tasks_by_file_name(done_tasks)

        sampled_recording_ids = [
            recording_id
            for recording_id in (_extract_recording_id(task.file_name) for task in unique_done_tasks)
            if recording_id
        ]
        metadata_recordings_subquery = (
            select(Recording.id.label("recording_id"))
            .where(Recording.id.in_(sampled_recording_ids))
            .subquery()
            if requested_detail_level == "summary"
            else allowed_recordings_subquery
        )
        recording_customer_map: dict[str, set[str]] = {}
        primary_customer_rows = (
            await db.execute(
                select(Recording.id, Visit.customer_id)
                .join(metadata_recordings_subquery, metadata_recordings_subquery.c.recording_id == Recording.id)
                .join(Visit, Visit.id == Recording.visit_id)
                .where(Visit.customer_id.is_not(None))
            )
        ).all()
        for recording_id, customer_id in primary_customer_rows:
            if recording_id and customer_id:
                recording_customer_map.setdefault(recording_id, set()).add(customer_id)
        linked_customer_rows = (
            await db.execute(
                select(RecordingVisitLink.recording_id, Visit.customer_id)
                .join(Visit, Visit.id == RecordingVisitLink.visit_id)
                .join(metadata_recordings_subquery, metadata_recordings_subquery.c.recording_id == RecordingVisitLink.recording_id)
                .where(Visit.customer_id.is_not(None))
            )
        ).all()
        for recording_id, customer_id in linked_customer_rows:
            if recording_id and customer_id:
                recording_customer_map.setdefault(recording_id, set()).add(customer_id)

        recording_meta_rows = (
            await db.execute(
                select(
                    Recording.id,
                    Recording.staff_id,
                    Staff.name,
                    Recording.file_name,
                    Recording.created_at,
                    Recording.duration_seconds,
                    Recording.status,
                )
                .join(metadata_recordings_subquery, metadata_recordings_subquery.c.recording_id == Recording.id)
                .outerjoin(Staff, Staff.id == Recording.staff_id)
            )
        ).all()
        recording_meta_map: dict[str, dict[str, Any]] = {
            recording_id: {
                "staff_id": staff_id,
                "staff_name": staff_name,
                "file_name": file_name,
                "created_at": created_at,
                "duration_seconds": duration_seconds,
                "status": status,
            }
            for recording_id, staff_id, staff_name, file_name, created_at, duration_seconds, status in recording_meta_rows
            if recording_id
        }

        dim_scores: dict[str, list[float]] = {}
        scores: list[float] = []
        scored_tasks: list[tuple[AnalysisTask, float]] = []
        example_recording_map: dict[str, DashboardExampleRecordingItem] = {}
        score_trend_mode = _resolve_score_trend_mode(date_from, date_to)
        score_trend_periods = (
            _resolve_score_trend_days(date_from, date_to)
            if score_trend_mode == "day"
            else _resolve_score_trend_weeks(date_from, date_to)
        )
        score_trend_map: dict[date, dict[str, Any]] = {
            period_start: {
                "scores": [],
                "task_count": 0,
                "dimension_scores": {},
            }
            for period_start in score_trend_periods
        }
        dialogue_counter: Counter[str] = Counter()
        concern_counter: Counter[str] = Counter()
        tag_breakdown_map: dict[str, dict[str, Any]] = {}
        indication_breakdown_map: dict[str, dict[str, Any]] = {}
        total_tag_count = 0
        total_indication_count = 0
        normalized_result_count = 0
        result_module_acc: dict[str, dict[str, Any]] = {
            key: {"label": label, "covered_count": 0, "item_count": 0}
            for key, label in _RESULT_ANALYSIS_MODULES
        }
        process_blueprint_sections = _process_blueprint_sections()
        process_section_name_to_code = {name: code for code, name in process_blueprint_sections if name}
        process_section_acc: dict[str, dict[str, Any]] = {
            code: {
                "code": code,
                "name": name,
                "scores": [],
                "max_score": 1.0,
                "evaluated_count": 0,
                "pass_count": 0,
                "issue_count": 0,
            }
            for code, name in process_blueprint_sections
            if code
        }
        process_evaluated_count = 0
        process_total_scores: list[float] = []
        process_max_total_score = 9.0
        process_passed_sections = 0
        process_section_evaluation_count = 0
        process_issue_count = 0
        process_issue_items: list[ProcessEvaluationIssueItem] = []
        process_issue_item_limit = None if requested_detail_level == "full" else _SUMMARY_PROCESS_ISSUE_LIMIT

        for task in unique_done_tasks:
            result = normalize_analysis_result(task.result) if isinstance(task.result, dict) else None
            if not isinstance(result, dict):
                continue
            normalized_result_count += 1
            recording_id = _extract_recording_id(task.file_name) or ""
            recording_meta = recording_meta_map.get(recording_id) or {}
            module_counts = _result_analysis_module_item_counts(result)
            for module_key, item_count in module_counts.items():
                bucket = result_module_acc.get(module_key)
                if bucket is None:
                    continue
                bucket["item_count"] += item_count
                if item_count > 0:
                    bucket["covered_count"] += 1

            process_evaluation = _as_dict(result.get("consultation_process_evaluation"))
            process_sections = [section for section in _as_list(process_evaluation.get("sections")) if isinstance(section, dict)]
            if process_sections:
                process_evaluated_count += 1
                process_total_score = _dimension_numeric_score(process_evaluation.get("total_score"))
                process_max_score = _dimension_numeric_score(process_evaluation.get("max_total_score")) or float(len(process_sections) or 9)
                if process_total_score is None:
                    section_scores = [
                        score
                        for score in (_dimension_numeric_score(section.get("point_score")) for section in process_sections)
                        if score is not None
                    ]
                    process_total_score = sum(section_scores) if section_scores else None
                if process_total_score is not None:
                    process_total_scores.append(process_total_score)
                process_max_total_score = max(process_max_total_score, process_max_score)

                for section in process_sections:
                    section_key = _process_section_key(section)
                    section_name = _clean_text(section.get("name"))
                    if section_key not in process_section_acc and section_name in process_section_name_to_code:
                        section_key = process_section_name_to_code[section_name]
                    if section_key not in process_section_acc:
                        process_section_acc[section_key] = {
                            "code": _clean_text(section.get("code")) or section_key,
                            "name": section_name or section_key,
                            "scores": [],
                            "max_score": _dimension_numeric_score(section.get("max_score")) or 1.0,
                            "evaluated_count": 0,
                            "pass_count": 0,
                            "issue_count": 0,
                        }
                    section_bucket = process_section_acc[section_key]
                    section_score = _dimension_numeric_score(section.get("point_score"))
                    section_max_score = _dimension_numeric_score(section.get("max_score")) or 1.0
                    section_bucket["evaluated_count"] += 1
                    section_bucket["max_score"] = max(float(section_bucket["max_score"]), section_max_score)
                    if section_score is not None:
                        section_bucket["scores"].append(section_score)
                    if _is_process_section_passed(section):
                        section_bucket["pass_count"] += 1
                        process_passed_sections += 1
                    issue_count = _process_section_issue_count(section)
                    section_bucket["issue_count"] += issue_count
                    process_issue_count += issue_count
                    process_section_evaluation_count += 1
                    for issue in _iter_process_section_issues(section):
                        if process_issue_item_limit is not None and len(process_issue_items) >= process_issue_item_limit:
                            continue
                        process_issue_items.append(
                            ProcessEvaluationIssueItem(
                                recording_id=recording_id,
                                analysis_task_id=task.id,
                                file_name=str(recording_meta.get("file_name") or task.file_name),
                                recorded_at=(
                                    recording_meta["created_at"].isoformat()
                                    if isinstance(recording_meta.get("created_at"), datetime)
                                    else None
                                ),
                                staff_id=recording_meta.get("staff_id"),
                                staff_name=recording_meta.get("staff_name"),
                                section_code=str(issue["section_code"] or section_key),
                                section_name=str(issue["section_name"] or section_name or section_key),
                                checkpoint_code=issue["checkpoint_code"],
                                checkpoint_name=issue["checkpoint_name"],
                                description=str(issue["description"] or "未填写问题描述"),
                                evidence=issue["evidence"],
                            )
                        )

            trend_day = _to_local_date(recording_meta.get("created_at")) or _to_local_date(task.created_at)
            trend_period = (
                trend_day
                if score_trend_mode == "day"
                else (_start_of_week(trend_day) if trend_day else None)
            )
            trend_bucket = score_trend_map.get(trend_period) if trend_period else None
            total_score = _consultation_total_score(result)
            if trend_bucket is not None:
                trend_bucket["task_count"] += 1
                if total_score is not None:
                    trend_bucket["scores"].append(total_score)
            if total_score is not None:
                scores.append(total_score)
                scored_tasks.append((task, total_score))
                if recording_id and recording_meta:
                    candidate = DashboardExampleRecordingItem(
                        recording_id=recording_id,
                        analysis_task_id=task.id,
                        file_name=str(recording_meta.get("file_name") or task.file_name),
                        recorded_at=(
                            recording_meta["created_at"].isoformat()
                            if isinstance(recording_meta.get("created_at"), datetime)
                            else None
                        ),
                        duration_seconds=(
                            int(recording_meta["duration_seconds"])
                            if recording_meta.get("duration_seconds") is not None
                            else None
                        ),
                        staff_id=recording_meta.get("staff_id"),
                        staff_name=recording_meta.get("staff_name"),
                        total_score=round(total_score, 2),
                        max_score=round(
                            _dimension_numeric_score(_as_dict(result.get("consultation_process_evaluation")).get("max_total_score"))
                            or 9.0,
                            2,
                        ),
                        indication_count=_count_indications(result),
                        tag_count=_count_profile_tags(result),
                        concern_count=_count_concerns(result),
                        summary=_build_example_recording_summary(result),
                    )
                    previous = example_recording_map.get(recording_id)
                    if previous is None:
                        example_recording_map[recording_id] = candidate
            # Dimensions
            for name, score_value in _consultation_dimension_scores(result):
                dim_scores.setdefault(name, []).append(score_value)
                if trend_bucket is not None:
                    trend_bucket["dimension_scores"].setdefault(name, []).append(score_value)
            # Dialogue type
            dt = result.get("customer_demands", {}).get("expectation", {}).get("dialogue_type")
            if dt:
                dialogue_counter[dt] += 1
            task_customer_ids = recording_customer_map.get(recording_id, set())
            # Concerns
            for c in result.get("customer_concerns", {}).get("items", []):
                ct = c.get("type")
                if ct:
                    concern_counter[ct] += 1
            profile = result.get("customer_profile", {})
            if isinstance(profile, dict):
                for item in profile.get("tags") or []:
                    if not isinstance(item, dict):
                        continue
                    category = str(item.get("category") or "").strip()
                    value = str(item.get("value") or "").strip()
                    if not category and not value:
                        continue
                    if _should_ignore_dashboard_tag_value(category, value):
                        continue
                    resolved_category = _resolve_tag_category_name(category, tag_category_options_map)
                    selectable_values = tag_category_options_map.get(resolved_category)
                    is_open_value_category = not selectable_values
                    if not value and not is_open_value_category:
                        continue
                    category_key = resolved_category or value
                    category_label = resolved_category or value
                    if not category_key:
                        continue
                    bucket = tag_breakdown_map.setdefault(
                        category_key,
                        {
                            "key": category_key,
                            "label": category_label,
                            "count": 0,
                            "task_ids": set(),
                            "customer_ids": set(),
                            "is_open_value": is_open_value_category,
                            "value_breakdown_map": {},
                        },
                    )
                    bucket["count"] += 1
                    bucket["task_ids"].add(task.id)
                    bucket["customer_ids"].update(task_customer_ids)
                    value_key = value or "未填写"
                    value_label = value or "未填写"
                    value_bucket = bucket["value_breakdown_map"].setdefault(
                        value_key,
                        {
                            "key": value_key,
                            "label": value_label,
                            "count": 0,
                            "task_ids": set(),
                            "customer_ids": set(),
                        },
                    )
                    value_bucket["count"] += 1
                    value_bucket["task_ids"].add(task.id)
                    value_bucket["customer_ids"].update(task_customer_ids)
                    total_tag_count += 1
            indications = result.get("standardized_indications", {})
            if isinstance(indications, dict):
                for item in indications.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    department_code = str(item.get("department_code") or "").strip()
                    department_name = str(item.get("department_name") or "").strip()
                    indication_code = str(item.get("indication_code") or "").strip()
                    indication_name = str(item.get("indication_name") or "").strip()
                    body_part_code = str(item.get("body_part_code") or "").strip()
                    body_part_name = str(item.get("body_part_name") or "").strip()
                    if not indication_name and not indication_code:
                        continue
                    indication_key = "|".join(
                        [
                            department_code or department_name,
                            indication_code or indication_name,
                            body_part_code or body_part_name,
                        ]
                    ).strip("|")
                    if not indication_key:
                        indication_key = indication_name
                    bucket = indication_breakdown_map.setdefault(
                        indication_key,
                        {
                            "key": indication_key,
                            "label": indication_name,
                            "count": 0,
                            "task_ids": set(),
                            "customer_ids": set(),
                            "department_code": department_code or None,
                            "department_name": department_name or None,
                            "indication_code": indication_code or None,
                            "body_part_code": body_part_code or None,
                            "body_part_name": body_part_name or None,
                        },
                    )
                    bucket["count"] += 1
                    bucket["task_ids"].add(task.id)
                    bucket["customer_ids"].update(task_customer_ids)
                    total_indication_count += 1

        avg_score = sum(scores) / len(scores) if scores else 0
        max_score = max(scores) if scores else 0
        min_score = min(scores) if scores else 0

        score_distribution_buckets = {f"{index}-{index + 1}": 0 for index in range(int(_CONSULTATION_TOTAL_SCORE_MAX))}
        for score in scores:
            normalized_bucket = min(int(score), int(_CONSULTATION_TOTAL_SCORE_MAX) - 1)
            bucket_label = f"{normalized_bucket}-{normalized_bucket + 1}"
            score_distribution_buckets[bucket_label] += 1
        score_dist = [ScoreDistItem(range=label, count=count) for label, count in score_distribution_buckets.items()]

        avg_tag_count = total_tag_count / len(unique_done_tasks) if unique_done_tasks else 0
        avg_indication_count = total_indication_count / len(unique_done_tasks) if unique_done_tasks else 0
        tag_breakdown = sorted(
            [
                (
                    lambda sorted_values: BreakdownItem(
                        key=item["key"],
                        label=item["label"],
                        count=int(item["count"]),
                        task_count=len(item["task_ids"]),
                        customer_count=len(item["customer_ids"]),
                        is_open_value=bool(item["is_open_value"]),
                        distinct_value_count=len(sorted_values),
                        remaining_value_count=max(
                            len(sorted_values)
                            - (
                                _MAX_OPEN_TAG_VALUE_ITEMS
                                if item["is_open_value"]
                                else len(sorted_values)
                            ),
                            0,
                        ),
                        detail=(
                            (
                                f"开放值，共识别{len(sorted_values)}种取值，仅展示前{min(len(sorted_values), _MAX_OPEN_TAG_VALUE_ITEMS)}种"
                                if len(sorted_values) > _MAX_OPEN_TAG_VALUE_ITEMS
                                else f"开放值，共识别{len(sorted_values)}种取值"
                            )
                            if item["is_open_value"] and sorted_values
                            else (
                                f"已命中{len(sorted_values)}个取值"
                                if sorted_values
                                else None
                            )
                        ),
                        value_breakdown=[
                            BreakdownValueItem(
                                key=value_item["key"],
                                label=value_item["label"],
                                count=int(value_item["count"]),
                                task_count=len(value_item["task_ids"]),
                                customer_count=len(value_item["customer_ids"]),
                            )
                            for value_item in (
                                sorted_values[:_MAX_OPEN_TAG_VALUE_ITEMS]
                                if item["is_open_value"]
                                else sorted_values
                            )
                        ],
                    )
                )(
                    sorted(
                        item["value_breakdown_map"].values(),
                        key=lambda value_item: (
                            -len(value_item["customer_ids"]),
                            -int(value_item["count"]),
                            value_item["label"],
                        ),
                    )
                )
                for item in tag_breakdown_map.values()
            ],
            key=lambda item: (-item.customer_count, -item.task_count, -item.count, item.label),
        )
        indication_breakdown = sorted(
            [
                BreakdownItem(
                    key=item["key"],
                    label=item["label"],
                    count=int(item["count"]),
                    task_count=len(item["task_ids"]),
                    customer_count=len(item["customer_ids"]),
                    department_code=item["department_code"],
                    department_name=item["department_name"],
                    indication_code=item["indication_code"],
                    body_part_code=item["body_part_code"],
                    body_part_name=item["body_part_name"],
                    detail=" · ".join(
                        part
                        for part in [
                            f"科室：{item['department_name']}" if item["department_name"] else None,
                            f"部位：{item['body_part_name']}" if item["body_part_name"] else None,
                            f"编码：{item['indication_code']}" if item["indication_code"] else None,
                        ]
                        if part
                    )
                    or None,
                )
                for item in indication_breakdown_map.values()
            ],
            key=lambda item: (-item.customer_count, -item.task_count, -item.count, item.label),
        )

        result_analysis_modules = [
            ResultAnalysisModuleStats(
                key=key,
                label=str(bucket["label"]),
                analyzed_count=normalized_result_count,
                covered_count=int(bucket["covered_count"]),
                coverage_rate=round((int(bucket["covered_count"]) / normalized_result_count) * 100, 1)
                if normalized_result_count
                else 0.0,
                avg_item_count=round((int(bucket["item_count"]) / normalized_result_count), 1)
                if normalized_result_count
                else 0.0,
            )
            for key, bucket in result_module_acc.items()
        ]
        process_evaluation_summary = ProcessEvaluationSummaryStats(
            evaluated_count=process_evaluated_count,
            avg_total_score=round(sum(process_total_scores) / len(process_total_scores), 2)
            if process_total_scores
            else None,
            max_total_score=round(process_max_total_score, 2),
            pass_rate=round((process_passed_sections / process_section_evaluation_count) * 100, 1)
            if process_section_evaluation_count
            else 0.0,
            issue_count=process_issue_count,
            avg_passed_sections=round(process_passed_sections / process_evaluated_count, 1)
            if process_evaluated_count
            else 0.0,
        )
        process_evaluation_sections = [
            ProcessEvaluationSectionStats(
                code=str(bucket["code"]),
                name=str(bucket["name"]),
                evaluated_count=int(bucket["evaluated_count"]),
                avg_score=round(sum(bucket["scores"]) / len(bucket["scores"]), 2)
                if bucket["scores"]
                else None,
                max_score=round(float(bucket["max_score"]), 2),
                pass_count=int(bucket["pass_count"]),
                pass_rate=round((int(bucket["pass_count"]) / int(bucket["evaluated_count"])) * 100, 1)
                if int(bucket["evaluated_count"])
                else 0.0,
                issue_count=int(bucket["issue_count"]),
            )
            for bucket in process_section_acc.values()
        ]

        dimension_avgs = sorted(
            [DimensionAvg(name=n, avg_score=round(sum(ss) / len(ss), 1)) for n, ss in dim_scores.items() if ss],
            key=lambda x: -x.avg_score,
        )
        score_trend = [
            ScoreTrendItem(
                date=period_start.isoformat(),
                label=_score_day_label(period_start) if score_trend_mode == "day" else _score_week_label(period_start),
                avg_score=(
                    round(sum(bucket["scores"]) / len(bucket["scores"]), 2)
                    if bucket["scores"]
                    else None
                ),
                task_count=int(bucket["task_count"]),
                dimension_averages=sorted(
                    [
                        DimensionAvg(name=name, avg_score=round(sum(values) / len(values), 2))
                        for name, values in bucket["dimension_scores"].items()
                        if values
                    ],
                    key=lambda item: item.name,
                ),
            )
            for period_start, bucket in score_trend_map.items()
        ]
        dialogue_types = [DialogueTypeItem(type=t, count=c) for t, c in dialogue_counter.most_common(10)]
        concern_types = [ConcernTypeItem(type=t, count=c) for t, c in concern_counter.most_common(10)]

        # Low-score alerts: six-dimension total score lower than 3 / 6
        low_tasks = sorted(
            (item for item in scored_tasks if item[1] < (_CONSULTATION_TOTAL_SCORE_MAX / 2)),
            key=lambda item: (item[1], item[0].created_at),
        )[:10]
        recent_low = [
            RecentTask(
                id=t.id,
                file_name=t.file_name,
                overall_score=round(score, 2),
                status=t.status,
                created_at=t.created_at.isoformat(),
            )
            for t, score in low_tasks
        ]
        example_recordings = list(example_recording_map.values())
        positive_example_limit = min(3, max(1, (len(example_recordings) + 1) // 2)) if example_recordings else 0
        positive_example_recordings = sorted(
            example_recordings,
            key=lambda item: (-item.total_score, item.recorded_at or "", item.staff_name or ""),
        )[:positive_example_limit]
        positive_example_recording_ids = {item.recording_id for item in positive_example_recordings}
        negative_example_limit = min(3, max(0, len(example_recordings) - len(positive_example_recordings)))
        negative_example_recordings = sorted(
            [item for item in example_recordings if item.recording_id not in positive_example_recording_ids],
            key=lambda item: (item.total_score, item.recorded_at or "", item.staff_name or ""),
        )[:negative_example_limit]

        # ── Deal KPIs (1 query) ──
        deal_row = (
            await db.execute(
                select(
                    func.coalesce(
                        func.sum(func.coalesce(Visit.deposit_principal, 0) + func.coalesce(Visit.deposit_bonus, 0)),
                        0,
                    ),
                    func.count(Visit.id),
                    func.count(func.distinct(Visit.customer_id)),
                ).where(Visit.status == "closed_won", visit_scope_filter, *visit_date_filters)
            )
        ).one()
        total_deal_amount = float(deal_row[0] or 0)
        total_closed_won_visits = int(deal_row[1] or 0)
        total_closed_won_customers = int(deal_row[2] or 0)

        # Only visit orders whose arrival purpose represents an actual hospital arrival
        # count toward arrival count; remote purchase and miscellaneous orders do not.
        total_customers_stmt = (
            select(func.count(func.distinct(VisitOrder.id)))
            .select_from(Visit)
            .join(VisitOrder, _visit_order_join_condition())
            .where(
                visit_scope_filter,
                _real_arrival_visit_order_condition(),
                *visit_date_filters,
            )
        )
        total_customers = (await db.execute(total_customers_stmt)).scalar() or 0
        # 合并 total_visits 与 visit_status_rows：通过对 GROUP BY 的结果求和得到总数，省一次扫表。
        visit_status_rows = (await db.execute(
            select(Visit.status, func.count(Visit.id))
            .where(visit_scope_filter, *visit_date_filters)
            .group_by(Visit.status)
        )).all()
        total_visits = sum(int(row[1] or 0) for row in visit_status_rows)
        visit_status_dist = [VisitStatusItem(status=row[0], count=row[1]) for row in visit_status_rows]

        dashboard_can_select_scope = normalize_permission_role(base_scope.role) != "staff" and bool(base_scope.staff_id)
        dashboard_can_select_hospital = is_global_role(base_scope.role) and effective_scope_mode == "all" and bool(hospital_options)
        dashboard_can_select_staff = normalize_permission_role(base_scope.role) != "staff" and bool(dashboard_staff_options)
        dashboard_hospital_name: str | None = None
        if selected_dashboard_hospital_code and selected_dashboard_hospital_code in hospital_option_map:
            dashboard_hospital_name = hospital_option_map[selected_dashboard_hospital_code].hospital_name
        elif scope.role == "hospital_admin" and scope.hospital_code:
            dashboard_hospital_name = (
                hospital_option_map.get(scope.hospital_code).hospital_name
                if scope.hospital_code in hospital_option_map
                else ((getattr(current_user, "hospital_name", None) or "").strip() or scope.hospital_code)
            )
        dashboard_staff_name = (
            dashboard_staff_option_map[selected_dashboard_staff_id].staff_name
            if selected_dashboard_staff_id and selected_dashboard_staff_id in dashboard_staff_option_map
            else None
        )

        visit_trend_scope = "staff"
        selected_trend_hospital_code: str | None = None
        selected_trend_hospital_name: str | None = None
        visit_trend_can_select_hospital = False

        if is_global_role(scope.role):
            visit_trend_scope = "hospital"
            visit_trend_can_select_hospital = bool(hospital_options)
            if selected_dashboard_hospital_code and selected_dashboard_hospital_code in hospital_option_map:
                selected_trend_hospital_code = selected_dashboard_hospital_code
            elif requested_hospital_code and requested_hospital_code in hospital_option_map:
                selected_trend_hospital_code = requested_hospital_code
        elif scope.role == "hospital_admin":
            visit_trend_scope = "hospital"
            selected_trend_hospital_code = scope.hospital_code

        if selected_trend_hospital_code and selected_trend_hospital_code in hospital_option_map:
            selected_trend_hospital_name = hospital_option_map[selected_trend_hospital_code].hospital_name
        elif selected_trend_hospital_code:
            selected_trend_hospital_name = (
                (getattr(current_user, "hospital_name", None) or "").strip()
                or selected_trend_hospital_code
            )

        trend_conditions: list[Any] = [visit_scope_filter, *visit_date_filters]
        if selected_trend_hospital_code:
            trend_conditions.append(_visit_hospital_condition(selected_trend_hospital_code))

        current_date = date.today()
        current_week_start = current_date - timedelta(days=current_date.weekday())
        trend_week_starts = [current_week_start - timedelta(weeks=offset) for offset in reversed(range(6))]
        trend_counts: Counter[date] = Counter({week_start: 0 for week_start in trend_week_starts})
        trend_period_start = trend_week_starts[0]
        trend_period_end = current_week_start + timedelta(days=7)
        trend_period_start_dt = datetime.combine(trend_period_start, datetime.min.time(), tzinfo=timezone.utc)
        trend_period_end_dt = datetime.combine(trend_period_end, datetime.min.time(), tzinfo=timezone.utc)

        trend_rows = (
            await db.execute(
                select(Visit.visit_date, Visit.created_at)
                .where(
                    *trend_conditions,
                    or_(
                        Visit.visit_date.between(trend_period_start, trend_period_end - timedelta(days=1)),
                        and_(
                            Visit.visit_date.is_(None),
                            Visit.created_at >= trend_period_start_dt,
                            Visit.created_at < trend_period_end_dt,
                        ),
                    ),
                )
            )
        ).all()

        for visit_date, created_at in trend_rows:
            resolved_date = visit_date or _to_local_date(created_at)
            if not resolved_date:
                continue
            week_start = resolved_date - timedelta(days=resolved_date.weekday())
            if week_start in trend_counts:
                trend_counts[week_start] += 1

        visit_trend = []
        for week_start in trend_week_starts:
            label, range_label = _week_label(week_start)
            visit_trend.append(
                VisitTrendItem(
                    week_start=week_start.isoformat(),
                    week_end=(week_start + timedelta(days=6)).isoformat(),
                    week_label=label,
                    range_label=range_label,
                    count=int(trend_counts.get(week_start, 0)),
                )
            )

        staff_stats_scope = scope if selected_dashboard_staff_id else _staff_stats_scope(scope)
        staff_stats = await _build_staff_stats(
            db,
            scope=staff_stats_scope,
            done_tasks=unique_done_tasks,
            recording_meta_map=recording_meta_map,
            date_from=date_from,
            date_to=date_to,
            sort_mode="business",
            managed_staff_ids=managed_staff_ids,
            visible_visit_ids=visible_visit_ids,
        )
        score_staff_stats = await _build_staff_stats(
            db,
            scope=scope,
            done_tasks=unique_done_tasks,
            recording_meta_map=recording_meta_map,
            date_from=date_from,
            date_to=date_to,
            sort_mode="score",
            managed_staff_ids=managed_staff_ids,
            visible_visit_ids=visible_visit_ids,
        )

        # ── Recording counts (1 query) ──
        rec_row = (await db.execute(
            select(
                func.count(Recording.id),
                func.count(case((Recording.status != "filtered", 1))),
                func.count(case((Recording.status == "uploaded", 1))),
                func.count(case((Recording.status.in_(["transcribed", "analyzing", "analyzed"]), 1))),
            )
            .where(recording_scope_filter, *recording_date_filters)
        )).one()
        total_recordings = int(rec_row[1])
        quality_passed_recordings = int(rec_row[1])
        recordings_uploaded = int(rec_row[2])
        recordings_transcribed = int(rec_row[3])

        linked_rec_row = (
            await db.execute(
                select(func.count(func.distinct(Recording.id)))
                .join(allowed_recordings_subquery, allowed_recordings_subquery.c.recording_id == Recording.id)
                .outerjoin(RecordingVisitLink, RecordingVisitLink.recording_id == Recording.id)
                .where(
                    Recording.status != "filtered",
                    or_(Recording.visit_id.is_not(None), RecordingVisitLink.visit_id.is_not(None)),
                )
            )
        ).scalar()
        recordings_with_visits = int(linked_rec_row or 0)

        # ── Transcript & segment counts (1 query each) ──
        ts_row = (await db.execute(
            select(
                func.count(Transcript.id),
                func.count(case((Transcript.status == "completed", 1))),
                func.count(case((Transcript.status == "failed", 1))),
            )
            .join(Recording, Recording.id == Transcript.recording_id)
            .where(recording_scope_filter, *recording_date_filters)
        )).one()
        total_transcripts = int(ts_row[0])
        transcripts_completed = int(ts_row[1])
        transcripts_failed = int(ts_row[2])

        seg_row = (await db.execute(
            select(
                func.count(Segment.id),
                func.count(case((Segment.visit_id.isnot(None), 1))),
            )
            .join(Recording, Recording.id == Segment.recording_id)
            .where(recording_scope_filter, *recording_date_filters)
        )).one()
        total_segments = int(seg_row[0])
        segments_with_visit = int(seg_row[1])

        result = DashboardStats(
            total_deal_amount=round(total_deal_amount, 2),
            total_closed_won_visits=total_closed_won_visits,
            total_closed_won_customers=total_closed_won_customers,
            total_tasks=total,
            done_count=done,
            running_count=running,
            failed_count=failed,
            total_tag_count=total_tag_count,
            avg_tag_count=round(avg_tag_count, 1),
            total_indication_count=total_indication_count,
            avg_indication_count=round(avg_indication_count, 1),
            avg_score=round(avg_score, 1),
            max_score=round(max_score, 1),
            min_score=round(min_score, 1),
            score_distribution=score_dist,
            dimension_averages=dimension_avgs,
            dialogue_types=dialogue_types,
            concern_types=concern_types,
            recent_low_scores=recent_low,
            positive_example_recordings=positive_example_recordings,
            negative_example_recordings=negative_example_recordings,
            total_customers=total_customers,
            total_visits=total_visits,
            visit_status_dist=visit_status_dist,
            visit_trend=visit_trend,
            visit_trend_scope=visit_trend_scope,
            visit_trend_hospital_code=selected_trend_hospital_code,
            visit_trend_hospital_name=selected_trend_hospital_name,
            visit_trend_can_select_hospital=visit_trend_can_select_hospital,
            visit_trend_hospital_options=hospital_options,
            score_trend=score_trend,
            dashboard_scope=effective_scope_mode,
            dashboard_can_select_scope=dashboard_can_select_scope,
            dashboard_can_select_hospital=dashboard_can_select_hospital,
            dashboard_hospital_code=selected_dashboard_hospital_code or scope.hospital_code,
            dashboard_hospital_name=dashboard_hospital_name,
            dashboard_hospital_options=hospital_options,
            dashboard_can_select_staff=dashboard_can_select_staff,
            dashboard_staff_id=selected_dashboard_staff_id,
            dashboard_staff_name=dashboard_staff_name,
            dashboard_staff_options=dashboard_staff_options,
            staff_stats=staff_stats,
            score_staff_stats=score_staff_stats,
            total_recordings=total_recordings,
            quality_passed_recordings=quality_passed_recordings,
            recordings_with_visits=recordings_with_visits,
            recordings_uploaded=recordings_uploaded,
            recordings_transcribed=recordings_transcribed,
            total_transcripts=total_transcripts,
            transcripts_completed=transcripts_completed,
            transcripts_failed=transcripts_failed,
            total_segments=total_segments,
            segments_with_visit=segments_with_visit,
            tag_breakdown=tag_breakdown,
            indication_breakdown=indication_breakdown,
            result_analysis_modules=result_analysis_modules,
            process_evaluation_summary=process_evaluation_summary,
            process_evaluation_sections=process_evaluation_sections,
            process_evaluation_issues=process_issue_items,
        )
        _cache[cache_key] = (now, result)
        await _redis_cache_set(cache_key, result)
        return result
