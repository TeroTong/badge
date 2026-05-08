from __future__ import annotations

from functools import lru_cache
import re
from typing import Any

from smart_badge_api.tag_catalog_reference import (
    canonicalize_profile_tag_category,
    canonicalize_profile_tag_value,
    is_valid_profile_tag_value,
    load_tag_catalog_definitions,
)
from smart_badge_api.analysis.schemas import CONSULTATION_PROCESS_EVALUATION_BLUEPRINT

PROFILE_DIMENSION_NAME = "顾客标签获取"
INDICATION_DIMENSION_NAME = "适应症获取"
LEGACY_INDICATION_DIMENSION_NAME = "".join(("标准", INDICATION_DIMENSION_NAME))
LEGACY_INDICATION_TERM = "".join(("标准", "适应症"))
LEGACY_PROFILE_DIMENSION_NAME = "顾客背景信息获取"
SERVICE_FLOW_DIMENSION_NAME = "服务流程执行"
TREATMENT_FLOW_DIMENSION_NAME = "治疗流程规范"
_LEGACY_SOP_TOKEN = "".join(("S", "O", "P"))
LEGACY_SERVICE_FLOW_DIMENSION_NAME = f"服务 {_LEGACY_SOP_TOKEN}"
LEGACY_TREATMENT_FLOW_DIMENSION_NAME = f"治疗 {_LEGACY_SOP_TOKEN}"

DIMENSION_NAMES = (
    "医美专业知识",
    INDICATION_DIMENSION_NAME,
    PROFILE_DIMENSION_NAME,
    "医院和医生介绍",
    "老带新等特别事项",
    "负面交流检测",
)

_POSITIVE_STATUSES = {"无问题", "有提及", "达标", "已获取", "已介绍", "通过", "pass"}
_PARTIAL_STATUSES = {"部分达标", "部分获取", "待完善", "partial"}
_NEGATIVE_STATUSES = {"有问题", "未提及", "未获取", "未介绍", "未达标", "fail"}
_INVALID_PROFILE_TAG_VALUES = {"", "未明确", "未提及", "未知", "N/A", "-"}

_REFERRAL_KEYWORDS = (
    "老带新",
    "转介绍",
    "转介",
    "会员权益",
    "介绍朋友",
    "带朋友",
    "推荐朋友",
    "返现",
    "奖励",
)

_HOSPITAL_DOCTOR_KEYWORDS = (
    "我们医院",
    "咱们医院",
    "朗姿",
    "我们院",
    "医生",
    "主任",
    "院长",
    "博士",
    "专家",
    "资历",
    "擅长",
)

_PROFESSIONAL_MISSING_KEYWORDS = (
    "未介绍专业知识",
    "未讲解专业知识",
    "没有介绍专业知识",
    "没有讲解专业知识",
    "未体现专业知识",
)

_HOSPITAL_MISSING_KEYWORDS = (
    "未介绍医院",
    "未介绍医生",
    "没有介绍医院",
    "没有介绍医生",
    "未识别到医院介绍",
    "未识别到医生介绍",
)

_LEADING_SCORE_SUMMARY_RE = re.compile(
    r"^(?:(?:六维(?:得分|总分)\s*\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?)[。；\s]*)+"
)

_OPENING_KEYWORDS = ("你好", "您好", "欢迎", "怎么称呼", "请坐", "坐这边")
_ROLE_PROCESS_KEYWORDS = ("我是咨询", "我是客服", "先了解", "先看一下", "接下来", "流程", "安排医生")
_DEMAND_INQUIRY_KEYWORDS = ("想做", "想改善", "哪里不满意", "主要想", "最困扰", "什么需求")
_MOTIVE_CONCERN_KEYWORDS = ("为什么想做", "担心", "顾虑", "害怕", "介意", "影响", "预算")
_CASE_DISPLAY_KEYWORDS = ("案例", "对比图", "前后对比", "真人案例", "给你看一下")
_DOCTOR_HANDOFF_KEYWORDS = ("给医生", "跟医生说", "让医生", "医生面诊", "找医生看", "转给医生")
_PLAN_RECORD_KEYWORDS = ("方案", "记录一下", "帮你记", "术式", "项目组合", "适合做")
_BUDGET_KEYWORDS = ("预算", "多少钱", "费用", "价格", "大概多少", "多钱")
_VALUE_COMPARE_KEYWORDS = ("性价比", "值不值", "区别", "差别", "对比", "为什么选")
_COMBINED_TREATMENT_KEYWORDS = ("联合", "搭配", "一起做", "组合", "配合做")
_CARE_KEYWORDS = ("术前", "术后", "注意事项", "恢复期", "忌口", "护理")
_AUTHENTICITY_KEYWORDS = ("验真", "扫码", "正品", "仪器码", "药品码", "防伪")
_FOLLOWUP_KEYWORDS = ("回访", "回去考虑", "保持联系", "我再联系你", "下次来", "给你发微信")
_ADD_WECOM_KEYWORDS = ("加个微信", "加我微信", "企业微信", "微信联系", "扫我微信", "留个微信")
_INCORRECT_INTRO_KEYWORDS = ("错误", "不正确", "夸大", "误导", "说错", "不准")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _as_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _compute_process_score_bundle(sections: list[dict[str, Any]]) -> tuple[float, float, float]:
    normalized_sections = [item for item in sections if isinstance(item, dict)]
    max_total_score = float(len(normalized_sections))
    if max_total_score <= 0:
        return 0.0, 0.0, 0.0

    total_score = 0.0
    for section in normalized_sections:
        point_score = _as_number(section.get("point_score")) or 0.0
        max_score = _as_number(section.get("max_score")) or 1.0
        if max_score <= 0:
            max_score = 1.0
        total_score += max(0.0, min(point_score, max_score)) / max_score

    total_score = round(total_score, 2)
    overall_score = round((total_score / max_total_score) * 10, 2) if max_total_score > 0 else 0.0
    return total_score, max_total_score, overall_score


def extract_preferred_overall_score(result_dict: dict[str, Any]) -> float | None:
    process_evaluation = _as_dict(result_dict.get("consultation_process_evaluation"))
    process_score = _as_number(process_evaluation.get("overall_score"))
    if process_score is not None:
        return process_score

    evaluation = _as_dict(result_dict.get("consultation_evaluation"))
    legacy_score = _as_number(evaluation.get("overall_score"))
    if legacy_score is not None:
        return legacy_score
    return None


def normalize_consultation_dimension_name(value: Any) -> str:
    name = _as_text(value)
    if name == LEGACY_INDICATION_DIMENSION_NAME:
        return INDICATION_DIMENSION_NAME
    if name == LEGACY_PROFILE_DIMENSION_NAME:
        return PROFILE_DIMENSION_NAME
    if name == LEGACY_SERVICE_FLOW_DIMENSION_NAME:
        return SERVICE_FLOW_DIMENSION_NAME
    if name == LEGACY_TREATMENT_FLOW_DIMENSION_NAME:
        return TREATMENT_FLOW_DIMENSION_NAME
    return name


def _unique_texts(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _as_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _strip_leading_score_summary(value: Any) -> str:
    text = _as_text(value)
    if not text:
        return ""
    return _LEADING_SCORE_SUMMARY_RE.sub("", text).strip()


def _sanitize_dimension_summary(value: Any) -> str:
    text = _as_text(value)
    if not text:
        return ""
    replacements = (
        ("只要能从对话语义稳定映射到适应症，即视为获取成功，不要求咨询师或医生直接说出标准名称。", ""),
        (f"只要能从对话语义稳定映射到{LEGACY_INDICATION_TERM}，即视为获取成功，不要求咨询师或医生直接说出标准名称。", ""),
        ("该维度按 14 个必问/重要标签的累计完成度计分，当前得分 0.00/1。", ""),
    )
    for source, target in replacements:
        text = text.replace(source, target)
    text = re.sub(r"该维度按\s*\d+\s*个必问/重要标签的累计完成度计分，当前得分\s*\d+(?:\.\d+)?/1。?", "", text)
    text = re.sub(r"按评分规则记\s*0\s*分。?", "", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"([。；])(?:\s*[。；])+", r"\1", text)
    return text.strip()


def _normalize_existing_point_score(dimension: dict[str, Any]) -> float | None:
    point_score = _as_number(dimension.get("point_score"))
    if point_score is not None:
        return max(0.0, min(point_score, 1.0))

    score = _as_number(dimension.get("score"))
    max_score = _as_number(dimension.get("max_score"))
    if score is not None:
        if max_score and max_score > 1:
            return max(0.0, min(score / max_score, 1.0))
        if score > 1:
            return max(0.0, min(score / 10.0, 1.0))
        return max(0.0, min(score, 1.0))

    status = _as_text(dimension.get("status")).lower()
    if status in {item.lower() for item in _POSITIVE_STATUSES}:
        return 1.0
    if status in {item.lower() for item in _PARTIAL_STATUSES}:
        return 0.5
    if status:
        return 0.0
    return None


@lru_cache(maxsize=1)
def _profile_weight_by_category() -> dict[str, int]:
    return {
        item.name: int(item.weight_level)
        for item in load_tag_catalog_definitions()
        if item.weight_level is not None
    }


@lru_cache(maxsize=1)
def important_profile_category_names() -> tuple[str, ...]:
    return tuple(
        item.name
        for item in load_tag_catalog_definitions()
        if int(item.weight_level or 0) in (1, 2)
    )


def important_profile_category_count() -> int:
    return len(important_profile_category_names())


def _normalize_profile_tags(raw_tags: Any) -> list[dict[str, Any]]:
    weight_by_category = _profile_weight_by_category()
    normalized: list[dict[str, Any]] = []
    for item in _as_list(raw_tags):
        tag = _as_dict(item)
        category = canonicalize_profile_tag_category(tag.get("category"))
        if not category:
            continue
        weight_level = int(_as_number(tag.get("weight_level")) or weight_by_category.get(category) or 0)
        value = canonicalize_profile_tag_value(category, tag.get("value"))
        if value is None:
            continue
        if value in _INVALID_PROFILE_TAG_VALUES:
            continue
        if not is_valid_profile_tag_value(category, value):
            continue
        evidence = _as_text(tag.get("evidence"))
        normalized.append(
            {
                "category": category,
                "value": value,
                "weight_level": weight_level,
                "evidence": evidence,
            }
        )
    return normalized


def _important_profile_categories(tags: list[dict[str, Any]]) -> list[str]:
    important_names = set(important_profile_category_names())
    categories: list[str] = []
    seen: set[str] = set()
    for item in tags:
        category = _as_text(item.get("category"))
        weight_level = int(_as_number(item.get("weight_level")) or 0)
        if not category or category in seen:
            continue
        if category not in important_names or weight_level not in (1, 2):
            continue
        seen.add(category)
        categories.append(category)
    return categories


def _normalize_issues(value: Any, *, replace_doctor_confirmation: bool = False) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for item in _as_list(value):
        issue = _as_dict(item)
        description = _as_text(issue.get("description"))
        evidence = _as_text(issue.get("evidence"))
        if replace_doctor_confirmation and "医生确认" in description:
            description = description.replace("需要医生确认", "当前未能稳定映射到适应症")
            description = description.replace("需医生确认", "当前未能稳定映射到适应症")
            description = description.replace("医生确认", "稳定映射适应症")
        if not description:
            continue
        issues.append({"description": description, "evidence": evidence})
    return issues


def _dimension_payload(
    *,
    name: str,
    point_score: float,
    summary: str,
    issues: list[dict[str, str]],
    max_score: float = 1.0,
) -> dict[str, Any]:
    normalized_point = max(0.0, min(point_score, max_score))
    score_10 = round((normalized_point / max_score) * 10, 2) if max_score > 0 else 0.0
    if normalized_point <= 0:
        status = "未达标"
    elif normalized_point >= max_score:
        status = "达标"
    else:
        status = "部分达标"
    return {
        "name": name,
        "point_score": round(normalized_point, 2),
        "max_score": round(max_score, 2),
        "score": score_10,
        "status": status,
        "summary": _as_text(summary),
        "issues": issues,
    }


def _find_keyword_evidence(dialogue: str | None, keywords: tuple[str, ...]) -> str:
    if not dialogue:
        return ""
    for line in dialogue.splitlines():
        text = line.strip()
        if text and any(keyword in text for keyword in keywords):
            return text
    return ""


def _find_keyword_evidences(dialogue: str | None, keywords: tuple[str, ...], *, limit: int = 2) -> list[str]:
    if not dialogue:
        return []
    results: list[str] = []
    seen: set[str] = set()
    for line in dialogue.splitlines():
        text = line.strip()
        if not text or text in seen:
            continue
        if any(keyword in text for keyword in keywords):
            seen.add(text)
            results.append(text)
            if len(results) >= limit:
                break
    return results


def _issue_payload(description: str, evidence: str = "") -> dict[str, str]:
    return {"description": _as_text(description), "evidence": _as_text(evidence)}


def _status_from_point(point_score: float, max_score: float = 1.0) -> str:
    if point_score <= 0:
        return "未达标"
    if point_score >= max_score:
        return "达标"
    return "部分达标"


def _checkpoint_payload(
    *,
    code: str,
    name: str,
    point_score: float,
    summary: str,
    evidence: list[str] | None = None,
    issues: list[dict[str, str]] | None = None,
    max_score: float = 1.0,
) -> dict[str, Any]:
    normalized_point = max(0.0, min(point_score, max_score))
    return {
        "code": code,
        "name": name,
        "point_score": round(normalized_point, 2),
        "max_score": round(max_score, 2),
        "status": _status_from_point(normalized_point, max_score),
        "summary": _as_text(summary),
        "evidence": _unique_texts(evidence or []),
        "issues": issues or [],
    }


def _section_payload(
    *,
    code: str,
    name: str,
    checkpoints: list[dict[str, Any]],
    summary: str = "",
) -> dict[str, Any]:
    point_values = [float(item.get("point_score") or 0) for item in checkpoints]
    max_values = [float(item.get("max_score") or 1) for item in checkpoints]
    max_total = sum(max_values) or 1.0
    total = sum(point_values)
    point_score = round(total / max_total, 2)
    computed_summary = _as_text(summary)
    if not computed_summary:
        if point_score >= 1.0:
            computed_summary = f"{name}动作整体完成度较好。"
        elif point_score <= 0:
            computed_summary = f"{name}阶段未识别到明确动作。"
        else:
            computed_summary = f"{name}阶段有部分动作完成，但仍有缺口。"
    return {
        "code": code,
        "name": name,
        "point_score": point_score,
        "max_score": 1.0,
        "status": _status_from_point(point_score),
        "summary": computed_summary,
        "checkpoints": checkpoints,
    }


def _build_existing_process_maps(existing_process: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    section_map: dict[str, dict[str, Any]] = {}
    checkpoint_map: dict[tuple[str, str], dict[str, Any]] = {}
    for section in _as_list(existing_process.get("sections")):
        section_dict = _as_dict(section)
        section_code = _as_text(section_dict.get("code"))
        section_name = _as_text(section_dict.get("name"))
        if section_code:
            section_map[section_code] = section_dict
        if section_name:
            section_map[section_name] = section_dict
        for checkpoint in _as_list(section_dict.get("checkpoints")):
            checkpoint_dict = _as_dict(checkpoint)
            checkpoint_code = _as_text(checkpoint_dict.get("code"))
            checkpoint_name = _as_text(checkpoint_dict.get("name"))
            if section_code and checkpoint_code:
                checkpoint_map[(section_code, checkpoint_code)] = checkpoint_dict
            if section_code and checkpoint_name:
                checkpoint_map[(section_code, checkpoint_name)] = checkpoint_dict
            if section_name and checkpoint_code:
                checkpoint_map[(section_name, checkpoint_code)] = checkpoint_dict
    return section_map, checkpoint_map


_SUMMARY_NEGATIVE_MARKERS = (
    "未识别", "未发现", "尚未", "未提及", "未明确", "未说明", "未进行",
    "未告知", "未将", "未协助", "未探寻", "未讲解", "未围绕", "未结合",
    "未给出", "未展示", "未主动", "未做", "检测到负面", "检测到不正确",
)


def _summary_polarity(text: str) -> str | None:
    """Return 'neg' if summary indicates failure, 'pos' if it indicates success, else None."""
    if not text:
        return None
    if any(marker in text for marker in _SUMMARY_NEGATIVE_MARKERS):
        return "neg"
    if text.startswith(("已", "保持", "完成")) or "完成度较好" in text:
        return "pos"
    return None


def _merge_process_checkpoint(
    computed: dict[str, Any],
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    if not existing:
        return computed
    point_score = _normalize_existing_point_score(existing)
    max_score = _as_number(existing.get("max_score")) or float(computed.get("max_score") or 1)
    merged = dict(computed)
    if point_score is not None:
        merged["point_score"] = round(point_score, 2)
        merged["status"] = _as_text(existing.get("status")) or _status_from_point(point_score, max_score)
    existing_summary = _as_text(existing.get("summary"))
    if existing_summary:
        merged["summary"] = existing_summary
    merged["evidence"] = _unique_texts([*_as_list(existing.get("evidence")), *_as_list(computed.get("evidence"))])
    final_score = float(merged.get("point_score") or 0)
    final_max = float(merged.get("max_score") or max_score or 1)
    # Reconcile summary polarity with score
    summary_polarity = _summary_polarity(_as_text(merged.get("summary")))
    computed_summary = _as_text(computed.get("summary"))
    if final_score > 0 and summary_polarity == "neg" and computed_summary:
        merged["summary"] = computed_summary
    elif final_score == 0 and summary_polarity == "pos" and computed_summary:
        merged["summary"] = computed_summary
    if final_score >= final_max:
        # Passed checkpoint should not carry failure issues
        merged["issues"] = []
    else:
        existing_issues = _normalize_issues(existing.get("issues"))
        if existing_issues:
            merged["issues"] = existing_issues
        elif final_score == 0 and not _as_list(merged.get("issues")):
            # Failed checkpoint with no explanation -> use computed default issue
            computed_issues = _as_list(computed.get("issues"))
            if computed_issues:
                merged["issues"] = computed_issues
    # Reconcile status text with final_score in case existing.status is stale
    existing_status = _as_text(existing.get("status"))
    if existing_status:
        canonical_status = _status_from_point(final_score, final_max)
        if final_score >= final_max and existing_status != canonical_status and "达标" not in existing_status:
            merged["status"] = canonical_status
        elif final_score == 0 and existing_status in ("达标", "完成", "通过"):
            merged["status"] = canonical_status
    return merged


def _merge_process_section(
    computed: dict[str, Any],
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    if not existing:
        return computed
    point_score = _normalize_existing_point_score(existing)
    max_score = _as_number(existing.get("max_score")) or float(computed.get("max_score") or 1)
    merged = dict(computed)
    if point_score is not None:
        merged["point_score"] = round(point_score, 2)
        merged["status"] = _as_text(existing.get("status")) or _status_from_point(point_score, max_score)
    if _as_text(existing.get("summary")):
        merged["summary"] = _as_text(existing.get("summary"))
    return merged


def _build_dimension_map(existing_evaluation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in _as_list(existing_evaluation.get("dimensions")):
        dimension = _as_dict(item)
        name = normalize_consultation_dimension_name(dimension.get("name"))
        if name:
            result[name] = dimension
    return result


def _build_professional_dimension(existing: dict[str, Any]) -> dict[str, Any]:
    summary = _as_text(existing.get("summary") or existing.get("comment"))
    issues = _normalize_issues(existing.get("issues"))
    point_score = _normalize_existing_point_score(existing)
    missing = any(keyword in summary for keyword in _PROFESSIONAL_MISSING_KEYWORDS)

    if issues or missing or point_score == 0:
        if not issues:
            issues = [{"description": "未识别到明确的医美专业知识讲解，或专业知识表达存在问题。", "evidence": ""}]
        return _dimension_payload(
            name="医美专业知识",
            point_score=0.0,
            summary=_sanitize_dimension_summary(summary) or "未识别到明确的医美专业知识讲解。",
            issues=issues,
        )

    if point_score is None and not summary:
        return _dimension_payload(
            name="医美专业知识",
            point_score=0.0,
            summary="未识别到明确的医美专业知识讲解。",
            issues=[{"description": "对话中未形成明确的医美专业知识讲解。", "evidence": ""}],
        )

    return _dimension_payload(
        name="医美专业知识",
        point_score=1.0,
        summary=_sanitize_dimension_summary(summary) or "未发现专业知识错误，且存在明确的专业知识讲解。",
        issues=[],
    )


def _build_indication_dimension(existing: dict[str, Any], result_dict: dict[str, Any]) -> dict[str, Any]:
    standardized_indications = _as_dict(result_dict.get("standardized_indications"))
    primary_demands = _as_dict(result_dict.get("customer_primary_demands"))
    items = [_as_dict(item) for item in _as_list(standardized_indications.get("items"))]
    indication_names = _unique_texts([_as_text(item.get("indication_name")) for item in items])

    if indication_names:
        summary = f"已成功获取 {len(items)} 项适应症：{'、'.join(indication_names)}。"
        return _dimension_payload(
            name=INDICATION_DIMENSION_NAME,
            point_score=1.0,
            summary=summary,
            issues=[],
        )

    existing_issues = _normalize_issues(existing.get("issues"), replace_doctor_confirmation=True)
    primary_count = len(_as_list(primary_demands.get("items")))
    description = (
        "已识别到客户主诉，但当前还未稳定映射出适应症。"
        if primary_count > 0
        else "当前对话中未获取到可标准化的适应症。"
    )
    issues = existing_issues or [{"description": description, "evidence": ""}]
    return _dimension_payload(
        name=INDICATION_DIMENSION_NAME,
        point_score=0.0,
        summary="当前未从对话中稳定映射出适应症。",
        issues=issues,
    )


def _build_profile_dimension(
    result_dict: dict[str, Any],
    historical_profile_tags: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    customer_profile = _as_dict(result_dict.get("customer_profile"))
    current_tags = _normalize_profile_tags(customer_profile.get("tags"))
    related_tags = _normalize_profile_tags(historical_profile_tags or [])
    current_categories = _important_profile_categories(current_tags)
    historical_categories = _important_profile_categories(related_tags)
    merged_categories = _unique_texts([*current_categories, *historical_categories])
    local_count = len(current_categories)
    merged_count = len(merged_categories)
    total_required = max(important_profile_category_count(), 1)
    point_score = min(round(merged_count / float(total_required), 2), 1.0)
    duplicate_count = len(set(current_categories) & set(historical_categories))
    evidences = _unique_texts(
        [
            _as_text(item.get("evidence"))
            for item in [*current_tags, *related_tags]
            if _as_text(item.get("evidence"))
        ]
    )[:2]

    if merged_count <= 0:
        return _dimension_payload(
            name=PROFILE_DIMENSION_NAME,
            point_score=0.0,
            summary=f"未获取到必问或重要的顾客背景标签，当前累计完成度 0/{total_required}。",
            issues=[{"description": "当前未获取到必问/重要顾客标签。", "evidence": ""}],
        )

    if historical_categories:
        summary = f"本次录音获取 {local_count} 个必问/重要标签"
        if duplicate_count > 0:
            summary += f"，其中 {duplicate_count} 个与历史重复"
        summary += f"；关联客户历史后累计获取 {merged_count}/{total_required} 个。"
    else:
        summary = f"当前已获取 {merged_count}/{total_required} 个必问/重要标签。"

    issues: list[dict[str, str]] = []
    if point_score < 1.0:
        issues.append(
            {
                "description": f"顾客标签获取还不够完整，目前累计获取 {merged_count}/{total_required} 个必问/重要标签。",
                "evidence": "；".join(evidences),
            }
        )

    return _dimension_payload(
        name=PROFILE_DIMENSION_NAME,
        point_score=point_score,
        summary=summary,
        issues=issues,
    )


def _build_hospital_dimension(existing: dict[str, Any], dialogue: str | None) -> dict[str, Any]:
    summary = _as_text(existing.get("summary") or existing.get("comment"))
    issues = _normalize_issues(existing.get("issues"))
    point_score = _normalize_existing_point_score(existing)
    missing = any(keyword in summary for keyword in _HOSPITAL_MISSING_KEYWORDS)
    keyword_evidence = _find_keyword_evidence(dialogue, _HOSPITAL_DOCTOR_KEYWORDS)

    if issues or missing or point_score == 0:
        if not issues:
            issues = [{"description": "未识别到明确且准确的医院或医生介绍。", "evidence": keyword_evidence}]
        return _dimension_payload(
            name="医院和医生介绍",
            point_score=0.0,
            summary=_sanitize_dimension_summary(summary) or "当前对话中未识别到明确且准确的医院或医生介绍。",
            issues=issues,
        )

    if point_score is None and not keyword_evidence and not summary:
        return _dimension_payload(
            name="医院和医生介绍",
            point_score=0.0,
            summary="当前对话中未识别到明确且准确的医院或医生介绍。",
            issues=[{"description": "未介绍医院资质或医生背景。", "evidence": ""}],
        )

    return _dimension_payload(
        name="医院和医生介绍",
        point_score=1.0,
        summary=_sanitize_dimension_summary(summary) or "已识别到准确的医院或医生介绍。",
        issues=[],
    )


def _build_referral_dimension(existing: dict[str, Any], dialogue: str | None) -> dict[str, Any]:
    summary = _as_text(existing.get("summary") or existing.get("comment"))
    evidence = _find_keyword_evidence(dialogue, _REFERRAL_KEYWORDS)
    point_score = _normalize_existing_point_score(existing)
    negative_summary = ("提及" in summary and ("未" in summary or "没有" in summary)) or "未涉及" in summary
    negative_status = _as_text(existing.get("status")) in _NEGATIVE_STATUSES
    mentioned = (
        not negative_summary
        and not negative_status
        and (bool(evidence) or point_score == 1.0 or any(keyword in summary for keyword in _REFERRAL_KEYWORDS))
    )

    if mentioned:
        return _dimension_payload(
            name="老带新等特别事项",
            point_score=1.0,
            summary=_sanitize_dimension_summary(summary) or "对话中已提及老带新、转介绍或会员权益等特别事项。",
            issues=[],
        )

    return _dimension_payload(
        name="老带新等特别事项",
        point_score=0.0,
        summary="对话中未提及老带新、转介绍或会员权益等特别事项。",
        issues=[{"description": "未提及老带新、转介绍或会员权益等特别事项。", "evidence": ""}],
    )


def _build_negative_dimension(existing: dict[str, Any]) -> dict[str, Any]:
    summary = _as_text(existing.get("summary") or existing.get("comment"))
    issues = _normalize_issues(existing.get("issues"))
    filtered_issues = []
    for issue in issues:
        description = _as_text(issue.get("description"))
        evidence = _as_text(issue.get("evidence"))
        customer_only_negative = (
            ("客户" in description or "主客户" in evidence or "客户：" in evidence)
            and any(keyword in description for keyword in ("表达不满", "抱怨", "投诉", "有情绪", "不满"))
            and not any(keyword in description for keyword in ("辱骂", "不礼貌", "贬低", "威胁", "攻击"))
        )
        if customer_only_negative:
            continue
        filtered_issues.append(issue)
    point_score = _normalize_existing_point_score(existing)
    customer_only_summary = (
        "客户" in summary
        and any(keyword in summary for keyword in ("不满", "抱怨", "投诉"))
        and not any(keyword in summary for keyword in ("辱骂", "不礼貌", "贬低", "威胁", "攻击"))
    )

    if filtered_issues or (point_score == 0 and not customer_only_summary):
        return _dimension_payload(
            name="负面交流检测",
            point_score=0.0,
            summary=_sanitize_dimension_summary(summary) or "检测到负面交流。",
            issues=filtered_issues or [{"description": "检测到负面交流。", "evidence": ""}],
        )

    return _dimension_payload(
        name="负面交流检测",
        point_score=1.0,
        summary=_sanitize_dimension_summary(summary) or "未发现负面交流。",
        issues=[],
    )


def rebuild_consultation_process_evaluation(
    result_dict: dict[str, Any],
    *,
    dialogue: str | None = None,
) -> dict[str, Any]:
    consultation_result = _as_dict(result_dict.get("consultation_result"))
    customer_primary_demands = _as_dict(result_dict.get("customer_primary_demands"))
    customer_concerns = _as_dict(result_dict.get("customer_concerns"))
    customer_demands = _as_dict(result_dict.get("customer_demands"))
    staff_recommendations = _as_dict(result_dict.get("staff_recommendations"))
    consumption_intent = _as_dict(result_dict.get("consumption_intent"))
    consultation_evaluation = _as_dict(result_dict.get("consultation_evaluation"))
    existing_process = _as_dict(result_dict.get("consultation_process_evaluation"))
    section_map, checkpoint_map = _build_existing_process_maps(existing_process)
    legacy_dimensions = _build_dimension_map(consultation_evaluation)

    chief = _as_dict(consultation_result.get("chief_complaint_and_indications"))
    deal_factors = _as_dict(consultation_result.get("deal_factors"))
    recommended_plan = _as_dict(consultation_result.get("recommended_plan"))
    deal_outcome = _as_dict(consultation_result.get("deal_outcome"))

    demand_items = _as_list(customer_primary_demands.get("items"))
    recommendation_items = _as_list(staff_recommendations.get("items")) or _as_list(recommended_plan.get("items"))
    concern_items = _as_list(customer_concerns.get("items"))

    has_primary_demands = bool(demand_items or _as_text(chief.get("summary")))
    has_concerns = bool(concern_items or _as_list(deal_factors.get("concerns")))
    has_recommendations = bool(recommendation_items or _as_text(recommended_plan.get("summary")))
    has_budget = bool(_as_text(consumption_intent.get("budget")) or _as_text(deal_factors.get("budget")))
    has_profile_signals = bool(_as_list(_as_dict(result_dict.get("customer_profile")).get("tags")) or _as_list(customer_demands.get("focus_areas")))
    deal_status = _as_text(deal_outcome.get("status"))

    negative_dimension = legacy_dimensions.get("负面交流检测", {})
    hospital_dimension = legacy_dimensions.get("医院和医生介绍", {})
    professional_dimension = legacy_dimensions.get("医美专业知识", {})

    def build_checkpoint(section_code: str, code: str, name: str) -> dict[str, Any]:
        existing_checkpoint = (
            checkpoint_map.get((section_code, code))
            or checkpoint_map.get((section_code, name))
        )
        evidence: list[str] = []
        issues: list[dict[str, str]] = []
        point_score = 0.0
        summary = ""

        if code == "1.1":
            evidence = _find_keyword_evidences(dialogue, _OPENING_KEYWORDS)
            point_score = 1.0 if evidence else 0.0
            summary = "已完成基本称呼与开场。" if point_score else "未识别到明确的称呼与开场。"
            if not point_score:
                issues = [_issue_payload("未识别到明确的称呼与开场。")]
        elif code == "1.2":
            evidence = _find_keyword_evidences(dialogue, _HOSPITAL_DOCTOR_KEYWORDS)
            hospital_score = _normalize_existing_point_score(hospital_dimension)
            point_score = 1.0 if evidence or hospital_score == 1.0 else 0.0
            summary = "已提及医院品牌、实力或相关背书。" if point_score else "未识别到明确的医院品牌和实力介绍。"
            if not point_score:
                issues = [_issue_payload("未识别到明确的医院品牌和实力介绍。")]
        elif code == "1.3":
            evidence = _find_keyword_evidences(dialogue, _ROLE_PROCESS_KEYWORDS)
            point_score = 1.0 if evidence else 0.0
            summary = "已说明角色或接诊流程。" if point_score else "未识别到明确的角色与流程说明。"
            if not point_score:
                issues = [_issue_payload("未识别到明确的角色与流程说明。")]
        elif code == "2.1":
            evidence = _find_keyword_evidences(dialogue, _DEMAND_INQUIRY_KEYWORDS)
            point_score = 1.0 if has_primary_demands or evidence else 0.0
            summary = "已围绕顾客主诉进行问诊。" if point_score else "未识别到充分的主诉问诊动作。"
            if not point_score:
                issues = [_issue_payload("未识别到充分的主诉问诊动作。")]
        elif code == "2.2":
            evidence = _find_keyword_evidences(dialogue, _MOTIVE_CONCERN_KEYWORDS)
            point_score = 1.0 if has_concerns or evidence else 0.0
            summary = "已追问顾客动机或顾虑。" if point_score else "未识别到对顾客动机和顾虑的深入追问。"
            if not point_score:
                issues = [_issue_payload("未识别到对顾客动机和顾虑的深入追问。")]
        elif code == "3.1":
            point_score = 1.0 if has_profile_signals or _as_text(customer_demands.get("inference_note")) else 0.0
            summary = "已结合顾客情况进行初步分析。" if point_score else "未识别到明确的客户情况分析。"
            if not point_score:
                issues = [_issue_payload("未识别到明确的客户情况分析。")]
        elif code == "3.2":
            point_score = 1.0 if has_recommendations else 0.0
            summary = "已给出结合顾客偏好的专业建议。" if point_score else "未识别到结合顾客偏好的明确专业建议。"
            if not point_score:
                issues = [_issue_payload("未识别到结合顾客偏好的明确专业建议。")]
        elif code == "3.3":
            evidence = _find_keyword_evidences(dialogue, _CASE_DISPLAY_KEYWORDS)
            point_score = 1.0 if evidence else 0.0
            summary = "已展示案例或参考对比。" if point_score else "未识别到案例展示动作。"
            if not point_score:
                issues = [_issue_payload("未识别到案例展示动作。")]
        elif code == "4.1":
            doctor_evidence = _find_keyword_evidences(dialogue, ("医生", "主任", "院长", "博士", "专家", "擅长"))
            hospital_score = _normalize_existing_point_score(hospital_dimension)
            point_score = 1.0 if doctor_evidence or hospital_score == 1.0 else 0.0
            evidence = doctor_evidence
            summary = "已进行医生专业化介绍。" if point_score else "未识别到医生的专业化介绍。"
            if not point_score:
                issues = [_issue_payload("未识别到医生的专业化介绍。")]
        elif code == "4.2":
            evidence = _find_keyword_evidences(dialogue, _DOCTOR_HANDOFF_KEYWORDS)
            point_score = 1.0 if evidence else 0.0
            summary = "已将顾客需求转述给医生。" if point_score else "未识别到清晰转述顾客需求给医生的动作。"
            if not point_score:
                issues = [_issue_payload("未识别到清晰转述顾客需求给医生的动作。")]
        elif code == "4.3":
            evidence = _find_keyword_evidences(dialogue, _PLAN_RECORD_KEYWORDS)
            point_score = 1.0 if evidence or has_recommendations else 0.0
            summary = "已协助讲解或记录方案。" if point_score else "未识别到协助讲解并记录方案的动作。"
            if not point_score:
                issues = [_issue_payload("未识别到协助讲解并记录方案的动作。")]
        elif code == "5.1":
            evidence = _find_keyword_evidences(dialogue, _BUDGET_KEYWORDS)
            point_score = 1.0 if has_budget or evidence else 0.0
            summary = "已探寻顾客预算与意向。" if point_score else "未识别到对预算与成交意向的明确探寻。"
            if not point_score:
                issues = [_issue_payload("未识别到对预算与成交意向的明确探寻。")]
        elif code == "5.2":
            evidence = _find_keyword_evidences(dialogue, _VALUE_COMPARE_KEYWORDS)
            point_score = 1.0 if evidence else 0.0
            summary = "已讲解方案价值或进行对比。" if point_score else "未识别到方案价值与对比讲解。"
            if not point_score:
                issues = [_issue_payload("未识别到方案价值与对比讲解。")]
        elif code == "5.3":
            evidence = _find_keyword_evidences(dialogue, _COMBINED_TREATMENT_KEYWORDS)
            multi_plans = len(recommendation_items) > 1
            point_score = 1.0 if evidence or multi_plans else 0.0
            summary = "已提及联合治疗或组合方案。" if point_score else "未识别到联合治疗项目的介绍。"
            if not point_score:
                issues = [_issue_payload("未识别到联合治疗项目的介绍。")]
        elif code == "6.1":
            evidence = _find_keyword_evidences(dialogue, _CARE_KEYWORDS)
            point_score = 1.0 if evidence else 0.0
            summary = "已告知术前/术后注意事项。" if point_score else "未识别到术前/术后注意事项说明。"
            if not point_score:
                issues = [_issue_payload("未识别到术前/术后注意事项说明。")]
        elif code == "6.2":
            evidence = _find_keyword_evidences(dialogue, _AUTHENTICITY_KEYWORDS)
            point_score = 1.0 if evidence else 0.0
            summary = "已提及仪器或药品验真提示。" if point_score else "未识别到仪器/药品验真提示。"
            if not point_score:
                issues = [_issue_payload("未识别到仪器/药品验真提示。")]
        elif code == "7.1":
            evidence = _find_keyword_evidences(dialogue, _FOLLOWUP_KEYWORDS) if deal_status == "未成交" else []
            point_score = 1.0 if (deal_status == "未成交" and evidence) else 0.0
            summary = "未成交后仍保持专业热情并进行了跟进。" if point_score else "未识别到未成交后的有效跟进行为。"
            if deal_status == "未成交" and not point_score:
                issues = [_issue_payload("未识别到未成交后的有效跟进行为。")]
        elif code == "8.1":
            evidence = _find_keyword_evidences(dialogue, _ADD_WECOM_KEYWORDS)
            point_score = 1.0 if evidence else 0.0
            summary = "已主动引导添加企业微信。" if point_score else "未识别到主动添加企业微信的动作。"
            if not point_score:
                issues = [_issue_payload("未识别到主动添加企业微信的动作。")]
        elif code == "8.2":
            evidence = _find_keyword_evidences(dialogue, _REFERRAL_KEYWORDS)
            referral_score = _normalize_existing_point_score(legacy_dimensions.get("老带新等特别事项", {}))
            point_score = 1.0 if evidence or referral_score == 1.0 else 0.0
            summary = "已进行老带新或转介绍种草。" if point_score else "未识别到老带新开口种草。"
            if not point_score:
                issues = [_issue_payload("未识别到老带新开口种草。")]
        elif code == "9.1":
            negative_issues = _normalize_issues(negative_dimension.get("issues"))
            point_score = 0.0 if negative_issues or _normalize_existing_point_score(negative_dimension) == 0 else 1.0
            evidence = [issue.get("evidence", "") for issue in negative_issues if _as_text(issue.get("evidence"))]
            summary = "未发现负面语言。" if point_score else (_sanitize_dimension_summary(negative_dimension.get("summary")) or "检测到负面语言或负面交流。")
            if not point_score:
                issues = negative_issues or [_issue_payload("检测到负面语言或负面交流。")]
        elif code == "9.2":
            professional_issues = _normalize_issues(professional_dimension.get("issues"))
            hospital_issues = _normalize_issues(hospital_dimension.get("issues"))
            incorrect_issues = [
                issue
                for issue in [*professional_issues, *hospital_issues]
                if any(keyword in _as_text(issue.get("description")) for keyword in _INCORRECT_INTRO_KEYWORDS)
            ]
            point_score = 0.0 if incorrect_issues else 1.0
            summary = "未发现不正确的医院、医生或产品介绍。" if point_score else "检测到不正确的医院、医生或产品介绍。"
            issues = incorrect_issues

        computed = _checkpoint_payload(
            code=code,
            name=name,
            point_score=point_score,
            summary=summary,
            evidence=evidence,
            issues=issues,
        )
        return _merge_process_checkpoint(computed, existing_checkpoint)

    sections: list[dict[str, Any]] = []
    for blueprint in CONSULTATION_PROCESS_EVALUATION_BLUEPRINT:
        section_code = blueprint["code"]
        section_name = blueprint["name"]
        checkpoints = [
            build_checkpoint(section_code, item["code"], item["name"])
            for item in blueprint["checkpoints"]
        ]
        computed_section = _section_payload(code=section_code, name=section_name, checkpoints=checkpoints)
        existing_section = section_map.get(section_code) or section_map.get(section_name)
        sections.append(_merge_process_section(computed_section, existing_section))

    total_score, max_total_score, overall_score = _compute_process_score_bundle(sections)

    overall_summary = _as_text(existing_process.get("overall_summary"))
    if not overall_summary:
        passed = [section["name"] for section in sections if float(section.get("point_score") or 0) >= 1.0]
        pending = [section["name"] for section in sections if float(section.get("point_score") or 0) < 1.0]
        summary_parts = []
        if passed:
            summary_parts.append(f"已完成：{'、'.join(passed)}。")
        if pending:
            summary_parts.append(f"待提升：{'、'.join(pending)}。")
        if not summary_parts:
            summary_parts.append("当前尚未识别到明确的问诊过程动作。")
        overall_summary = "".join(summary_parts)

    return {
        "total_score": total_score,
        "max_total_score": max_total_score,
        "overall_score": overall_score,
        "overall_summary": overall_summary,
        "sections": sections,
    }


def rebuild_consultation_evaluation(
    result_dict: dict[str, Any],
    *,
    dialogue: str | None = None,
    historical_profile_tags: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    existing_evaluation = _as_dict(result_dict.get("consultation_evaluation"))
    existing_dimensions = _build_dimension_map(existing_evaluation)

    dimensions = [
        _build_professional_dimension(existing_dimensions.get("医美专业知识", {})),
        _build_indication_dimension(existing_dimensions.get(INDICATION_DIMENSION_NAME, {}), result_dict),
        _build_profile_dimension(result_dict, historical_profile_tags),
        _build_hospital_dimension(existing_dimensions.get("医院和医生介绍", {}), dialogue),
        _build_referral_dimension(existing_dimensions.get("老带新等特别事项", {}), dialogue),
        _build_negative_dimension(existing_dimensions.get("负面交流检测", {})),
    ]

    total_score = round(sum(float(item.get("point_score") or 0) for item in dimensions), 2)
    max_total_score = float(len(DIMENSION_NAMES))
    overall_score = round((total_score / max_total_score) * 10, 2) if max_total_score > 0 else 0.0

    passed = [item["name"] for item in dimensions if float(item.get("point_score") or 0) >= float(item.get("max_score") or 1)]
    pending = [item["name"] for item in dimensions if float(item.get("point_score") or 0) < float(item.get("max_score") or 1)]

    existing_summary = _strip_leading_score_summary(existing_evaluation.get("overall_summary"))
    if existing_summary:
        overall_summary = f"六维得分 {total_score:.2f}/6。{existing_summary}"
    else:
        summary_parts = [f"六维得分 {total_score:.2f}/6。"]
        if passed:
            summary_parts.append(f"已达标：{'、'.join(passed)}。")
        if pending:
            summary_parts.append(f"待提升：{'、'.join(pending)}。")
        overall_summary = "".join(summary_parts)

    return {
        "total_score": total_score,
        "max_total_score": max_total_score,
        "overall_score": overall_score,
        "overall_summary": overall_summary,
        "dimensions": dimensions,
    }
