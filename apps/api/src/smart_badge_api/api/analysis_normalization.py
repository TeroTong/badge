from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from smart_badge_api.analysis.reference_data import (
    normalize_standardized_indications_payload,
    resolve_indication_reference_item,
)
from smart_badge_api.analysis.schemas import CONSULTATION_PROCESS_EVALUATION_BLUEPRINT
from smart_badge_api.analysis.consultation_evaluation import (
    INDICATION_DIMENSION_NAME,
    LEGACY_INDICATION_DIMENSION_NAME,
    LEGACY_SERVICE_FLOW_DIMENSION_NAME,
    LEGACY_TREATMENT_FLOW_DIMENSION_NAME,
    LEGACY_PROFILE_DIMENSION_NAME,
    PROFILE_DIMENSION_NAME,
    SERVICE_FLOW_DIMENSION_NAME,
    TREATMENT_FLOW_DIMENSION_NAME,
    normalize_consultation_dimension_name,
)
from smart_badge_api.db.models import AnalysisTask
from smart_badge_api.schemas.customers import CustomerMergedThemeOut
from smart_badge_api.schemas.tasks import TaskDetailOut
from smart_badge_api.tag_catalog_reference import (
    BIRTHDATE_TAG_CATEGORY,
    canonicalize_profile_tag_value,
    is_valid_profile_tag_value,
    NEGATIVE_PROJECT_EMPTY_VALUE,
    NEGATIVE_PROJECT_PLACEHOLDER_VALUE,
    NEGATIVE_PROJECT_TAG_CATEGORY,
    canonicalize_profile_tag_category,
)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


_BUDGET_CATEGORY_MARKERS = ("本次消费预算", "消费预算")
_EMPTY_BUDGET_VALUES = {"", "未明确", "未提及", "未知", "无", "N/A", "-"}
_EMPTY_NEGATIVE_PROJECT_VALUES = {
    "",
    "未明确",
    "未提及",
    "未知",
    "N/A",
    "-",
    NEGATIVE_PROJECT_EMPTY_VALUE,
    NEGATIVE_PROJECT_PLACEHOLDER_VALUE,
}
_PRIOR_TREATMENT_CONTEXT_CATEGORIES = frozenset({"治疗项目", "历史用的设备/原材料名称", "治疗历史"})
_HISTORY_DEVICE_TAG_CATEGORY = "历史用的设备/原材料名称"
_NO_PRIOR_TREATMENT_DEPENDENT_CATEGORIES = (
    _HISTORY_DEVICE_TAG_CATEGORY,
    NEGATIVE_PROJECT_TAG_CATEGORY,
)
_EMPTY_TREATMENT_CONTEXT_VALUES = {
    "",
    "未明确",
    "未提及",
    "未知",
    "无",
    "N/A",
    "-",
}
_INVALID_PROFILE_TAG_VALUES = {
    "",
    "未明确",
    "未提及",
    "未知",
    "N/A",
    "-",
}
_EXPLICIT_NO_PRIOR_TREATMENT_PATTERNS = (
    r"(?:没|未|没有)(?:有)?做过(?:医美)?(?:项目|治疗|整形)?",
    r"从(?:来)?没做过(?:医美)?(?:项目|治疗|整形)?",
    r"(?:医美|医美项目|项目|治疗|整形).{0,6}(?:就是)?第一次",
    r"第一次做(?:医美|医美项目|项目|治疗|整形)",
    r"无既往(?:医美)?(?:项目|治疗|整形)?",
    r"无医美史",
)
_NO_RISK_HEALTH_TAG_VALUE = "无风险禁忌"
_SINGLE_SELECT_PROFILE_CATEGORIES = frozenset(
    {
        BIRTHDATE_TAG_CATEGORY,
        "常驻城市",
        "价格敏感度",
        "决策主体",
        "个人情况",
        "亲属/子女情况",
        "创伤倾向",
        "疼痛耐受度",
        "效果要求",
        "恢复期要求",
        "治疗频次",
        "倾向回访方式",
        "教育程度",
        "交通工具",
        "职位",
        "行业",
        "居住地址",
    }
)
_PROFILE_TAG_VALUE_PRIORITY: dict[str, dict[str, int]] = {
    "价格敏感度": {"高": 30, "中": 20, "低": 10},
    "常驻城市": {"外地": 20, "本地": 10},
    "决策主体": {"父母": 50, "伴侣": 40, "儿女": 30, "自主": 20, "其它": 10},
    "个人情况": {"已婚": 30, "有恋人": 20, "单身": 10},
    "亲属/子女情况": {"2孩及以上": 30, "1孩": 20, "无孩": 10},
    "倾向回访方式": {"微信": 30, "电话": 20, "短信": 10},
    "创伤倾向": {"手术": 30, "微创": 20, "皮肤": 10},
    "疼痛耐受度": {"高": 30, "中": 20, "低": 10},
    "效果要求": {"长期": 20, "即刻": 10},
    "恢复期要求": {"1个月以上": 40, "半个月": 30, "1周": 20, "1-3天": 10},
    "治疗频次": {"高频(1月1次)": 30, "中频（季度1次）": 20, "低频（半年以上1次）": 10},
}
_AGE_FUTURE_OR_HYPOTHETICAL_HINTS = (
    "以后",
    "之后",
    "将来",
    "未来",
    "哪怕",
    "等到",
    "到了",
    "到时候",
)
_PRIMARY_DEMAND_BODY_PART_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("面部", ("面部", "面中", "苹果肌", "法令纹", "嘴角囊带", "全脸", "脸")),
    ("眼部", ("眼部", "眼尾", "眼周", "眼睛", "双眼皮", "单眼皮", "泪沟", "眼袋", "眶周", "眉下", "上眼睑", "眼型", "美杜莎")),
    ("鼻部", ("鼻部", "鼻子", "鼻综合", "山根", "鼻头", "鼻翼")),
    ("颈部", ("颈部", "脖子", "颈纹")),
    ("胸部", ("胸部", "胸", "乳房")),
    ("身体", ("身体", "腰腹", "大腿", "手臂", "手部", "手上", "肩颈", "肩膀", "斜方肌", "后背", "背部", "小后背", "大后背", "富贵包")),
)
_PRIMARY_DEMAND_CONCEPT_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("lip_perioral", ("口周", "嘴唇", "嘴巴", "唇部", "嘴角", "口下", "鼻基底", "唇形", "唇纹")),
    ("eye_bag_tear_trough_fatigue", ("眼袋", "泪沟", "疲态", "疲惫", "没精神")),
    ("eyelid_shape", ("双眼皮", "单眼皮", "眼型", "美杜莎")),
    ("facial_laxity", ("松弛", "下垂", "松垮", "提升", "紧致", "抗衰")),
    ("wrinkle_texture", ("皱纹", "纹路", "细纹", "法令纹", "川字纹", "鱼尾纹")),
    ("skin_tone", ("暗黄", "黄气", "提亮", "肤色")),
    ("pores", ("毛孔",)),
    ("acne_marks_texture", ("痘印", "痘坑", "痘痘", "闭口", "肤质")),
    ("hydration", ("水光", "补水", "保湿", "缺水", "干燥")),
    ("nose_shape", ("鼻子", "鼻部", "鼻综合", "隆鼻", "山根", "鼻头", "鼻翼", "鼻型")),
    ("body_liposuction", ("后背", "背部", "小后背", "大后背", "腰腹", "大腿", "手臂", "吸脂", "抽脂", "超脂", "超脂术", "富贵包")),
    ("scar", ("疤痕", "疤", "留疤")),
)
_FUTURE_ONLY_LAXITY_RE = re.compile(r"(?:怕|担心).{0,12}(?:以后|上了?年纪|未来|再晚几年).{0,20}(?:松弛|下垂|皮肤弹性)")
_CURRENT_LAXITY_ACTION_RE = re.compile(r"(?:现在|目前|这次|今天|本次).{0,20}(?:松弛|下垂|抗衰|提升|紧致)")


def _normalize_budget_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text if text and text not in _EMPTY_BUDGET_VALUES else None


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalize_text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := _normalize_text(item))]
    if isinstance(value, str):
        text = _normalize_text(value)
        return [text] if text else []
    return []


_RECOMMENDATION_DETAIL_FIELDS: tuple[tuple[str, str], ...] = (
    ("brand", "品牌"),
    ("material", "材料"),
    ("dosage", "用量"),
    ("price", "报价"),
    ("course_or_frequency", "疗程"),
    ("treatment_steps", "步骤"),
    ("implementation_notes", "要点"),
)


def _format_recommendation_plan_text(item: dict[str, Any]) -> str:
    plan = (
        _normalize_text(item.get("recommendation"))
        or _normalize_text(item.get("product_or_solution"))
        or _normalize_text(item.get("plan"))
    )
    if not plan:
        return ""
    compact_plan = re.sub(r"\s+", "", plan)
    details: list[str] = []
    seen_values: set[str] = set()
    for field, label in _RECOMMENDATION_DETAIL_FIELDS:
        value = "；".join(_normalize_text_list(item.get(field))) if isinstance(item.get(field), list) else _normalize_text(item.get(field))
        compact_value = re.sub(r"\s+", "", value)
        if not compact_value or compact_value in compact_plan or compact_value in seen_values:
            continue
        seen_values.add(compact_value)
        details.append(f"{label}：{value}")
    if not details:
        return plan
    return f"{plan}（{'；'.join(details)}）"


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _dedupe_text_list(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _normalize_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


def _primary_demand_concepts(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", _normalize_text(text))
    if not compact:
        return set()
    return {
        concept
        for concept, keywords in _PRIMARY_DEMAND_CONCEPT_HINTS
        if any(keyword in compact for keyword in keywords)
    }


def _primary_demand_body_part(item: dict[str, Any]) -> str:
    body_part = _normalize_text(item.get("body_part"))
    if body_part:
        return body_part
    demand = _normalize_text(item.get("demand"))
    for candidate, keywords in _PRIMARY_DEMAND_BODY_PART_HINTS:
        if any(keyword in demand for keyword in keywords):
            return candidate
    return ""


def _primary_demand_item_score(item: dict[str, Any]) -> tuple[int, int, int, int]:
    demand = _normalize_text(item.get("demand"))
    evidence = _normalize_text(item.get("evidence"))
    priority = item.get("priority")
    try:
        normalized_priority = int(priority)
    except (TypeError, ValueError):
        normalized_priority = 999
    return (
        1 if evidence else 0,
        len(_primary_demand_concepts(demand)),
        len(demand),
        -normalized_priority,
    )


def _primary_demand_items_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_compact = re.sub(r"[；，。、“”‘’（）()：:、,.\s]+", "", _normalize_text(left.get("demand")))
    right_compact = re.sub(r"[；，。、“”‘’（）()：:、,.\s]+", "", _normalize_text(right.get("demand")))
    if not left_compact or not right_compact:
        return False
    if left_compact == right_compact:
        return True

    shared_anchor_groups = (
        ("鼻基底", "口下", "口周", "嘴角", "嘴唇", "唇部", "唇形"),
        ("眼袋", "泪沟", "眼下", "疲态", "疲惫"),
        ("山根", "鼻背", "鼻头", "鼻尖", "鼻翼", "鼻型"),
        ("后背", "背部", "小后背", "大后背", "吸脂", "超脂"),
    )
    for anchors in shared_anchor_groups:
        if any(anchor in left_compact for anchor in anchors) and any(anchor in right_compact for anchor in anchors):
            return True

    left_body_part = _primary_demand_body_part(left)
    right_body_part = _primary_demand_body_part(right)
    if left_body_part and right_body_part and left_body_part != right_body_part:
        return False

    left_concepts = _primary_demand_concepts(_normalize_text(left.get("demand")))
    right_concepts = _primary_demand_concepts(_normalize_text(right.get("demand")))
    if left_concepts and right_concepts and left_concepts.intersection(right_concepts):
        return True

    return left_compact in right_compact or right_compact in left_compact


def _is_future_only_laxity_demand(item: dict[str, Any]) -> bool:
    demand = _normalize_text(item.get("demand"))
    evidence = _normalize_text(item.get("evidence"))
    haystack = f"{demand} {evidence}"
    if not any(keyword in haystack for keyword in ("松弛", "下垂", "抗衰", "提升", "紧致")):
        return False
    if not _FUTURE_ONLY_LAXITY_RE.search(haystack):
        return False
    return not bool(_CURRENT_LAXITY_ACTION_RE.search(evidence))


def _dedupe_primary_demand_items(items: list[Any]) -> tuple[list[dict[str, Any]], bool]:
    kept: list[dict[str, Any]] = []
    changed = False
    for raw_item in items:
        if not isinstance(raw_item, dict):
            changed = True
            continue
        demand = _normalize_text(raw_item.get("demand"))
        if not demand:
            changed = True
            continue
        item = dict(raw_item)
        if _is_future_only_laxity_demand(item):
            changed = True
            continue
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(kept)
                if _primary_demand_items_overlap(existing, item)
            ),
            None,
        )
        if duplicate_index is None:
            kept.append(item)
            continue

        changed = True
        if _primary_demand_item_score(item) > _primary_demand_item_score(kept[duplicate_index]):
            kept[duplicate_index] = item

    for priority, item in enumerate(kept, start=1):
        if item.get("priority") != priority:
            item["priority"] = priority
            changed = True
    return kept, changed


_DECISION_FACTOR_FROM_CONCERN_HINTS: dict[str, tuple[str, ...]] = {
    "价格": ("价格", "预算", "太贵", "有点贵", "便宜", "性价比", "划算"),
    "恢复期": ("恢复", "恢复期", "肿", "消肿", "上班", "请假", "拆线"),
    "效果": ("效果", "自然", "不明显", "明显", "维持多久", "太假"),
    "疼痛": ("疼", "痛", "怕疼", "麻药", "耐受"),
    "风险": ("风险", "副作用", "安全", "过敏", "失败"),
    "时间/到院限制": ("赶时间", "路程", "外地", "高铁", "飞机", "反复到院", "无法到院", "过几天要回去"),
    "支付/流程限制": ("支付", "付不了", "刷不了", "扫码", "下载", "流程", "身份证"),
    "治疗条件限制": ("禁忌", "不能做", "不能打", "不适合", "怀孕", "备孕", "哺乳", "生理期", "过敏"),
}
_FAMILY_DECISION_RELATION_HINTS = ("老公", "老公那边", "男朋友", "父母", "爸妈", "家里", "家人", "对象", "女朋友", "丈夫", "老婆")
_FAMILY_DECISION_ACTION_HINTS = ("商量", "考虑", "决定", "拍板", "同意", "出钱", "付款", "问一下", "沟通", "确认")


def _infer_decision_factors_from_concerns(concern_texts: list[str]) -> set[str]:
    inferred: set[str] = set()
    for text in concern_texts:
        normalized = _normalize_text(text)
        if not normalized:
            continue
        for factor, keywords in _DECISION_FACTOR_FROM_CONCERN_HINTS.items():
            if any(keyword in normalized for keyword in keywords):
                inferred.add(factor)
    return inferred


def _looks_like_explicit_family_decision(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    return any(relation in normalized for relation in _FAMILY_DECISION_RELATION_HINTS) and any(
        action in normalized for action in _FAMILY_DECISION_ACTION_HINTS
    )


# Subjective concern labels belong in customer_concerns, not in
# decision_factors. Always strip them from "其他影响因素".
_CONCERN_CATEGORY_LABELS = {"价格", "恢复期", "效果", "疼痛", "风险", "家庭决策", "对比机构"}
_OBJECTIVE_DECISION_FACTOR_KEYWORDS = (
    "生理期",
    "经期",
    "月经",
    "姨妈",
    "例假",
    "妊娠",
    "怀孕",
    "备孕",
    "哺乳",
    "禁忌",
    "身体条件",
    "竞对",
    "竞品",
    "同行机构",
    "黑名单",
    "特殊身份",
    "支付",
    "流程",
    "系统",
    "扫码",
    "到院",
    "路程",
    "外地",
    "高铁",
    "飞机",
    "赶时间",
)
_SUBJECTIVE_DECISION_FACTOR_KEYWORDS = (
    "价格",
    "预算",
    "太贵",
    "恢复",
    "肿",
    "效果",
    "自然",
    "疼",
    "风险",
    "副作用",
    "安全",
    "商量",
    "考虑",
    "对比",
    "家人",
    "老公",
    "男朋友",
)


def _looks_like_subjective_decision_factor(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    if any(keyword in normalized for keyword in _OBJECTIVE_DECISION_FACTOR_KEYWORDS):
        return False
    return any(keyword in normalized for keyword in _SUBJECTIVE_DECISION_FACTOR_KEYWORDS)


def _filter_decision_factors(
    decision_factors: list[str],
    *,
    concern_texts: list[str],
    evidence_texts: list[str],
    loss_reasons: list[str],
) -> list[str]:
    concern_implied = _infer_decision_factors_from_concerns(concern_texts)
    has_family_decision_evidence = any(
        _looks_like_explicit_family_decision(text)
        for text in [*concern_texts, *evidence_texts, *loss_reasons]
    )

    filtered: list[str] = []
    for factor in _dedupe_text_list(decision_factors):
        if factor in _CONCERN_CATEGORY_LABELS:
            continue
        if _looks_like_subjective_decision_factor(factor):
            continue
        if factor in concern_implied:
            continue
        if factor == "家庭决策" and not has_family_decision_evidence:
            continue
        filtered.append(factor)
    return filtered


def _rename_legacy_consultation_text(value: Any) -> str:
    text = _normalize_text(value)
    if not text:
        return text
    replacements = (
        (LEGACY_INDICATION_DIMENSION_NAME, INDICATION_DIMENSION_NAME),
        ("".join(("标准", "适应症")), "适应症"),
        (LEGACY_PROFILE_DIMENSION_NAME, PROFILE_DIMENSION_NAME),
        (LEGACY_SERVICE_FLOW_DIMENSION_NAME, SERVICE_FLOW_DIMENSION_NAME),
        (LEGACY_TREATMENT_FLOW_DIMENSION_NAME, TREATMENT_FLOW_DIMENSION_NAME),
    )
    for source, target in replacements:
        text = text.replace(source, target)
    return text


def _format_indication_brief(item: dict[str, Any]) -> str:
    department_name = _normalize_text(item.get("department_name"))
    department_code = _normalize_text(item.get("department_code"))
    indication_name = _normalize_text(item.get("indication_name"))
    indication_code = _normalize_text(item.get("indication_code"))
    body_part_name = _normalize_text(item.get("body_part_name"))
    body_part_code = _normalize_text(item.get("body_part_code"))

    department_part = department_name or ""
    if department_name and department_code:
        department_part = f"{department_name}（{department_code}）"

    indication_part = indication_name or ""
    if indication_name and indication_code:
        indication_part = f"{indication_name}（{indication_code}）"

    body_part = body_part_name or ""
    if body_part_name and body_part_code:
        body_part = f"{body_part_name}（{body_part_code}）"

    parts = [part for part in (department_part, indication_part, body_part) if part]
    return "｜".join(parts)


_LEADING_RESULT_SCORE_SUMMARY_RE = re.compile(
    r"^(?:(?:(?:六维(?:得分|总分)\s*\d+(?:\.\d+)?\s*\/\s*\d+(?:\.\d+)?|九点评价\s*\d+(?:\.\d+)?\s*\/\s*10(?:\.\d+)?|旧版评分[:：]?\s*\d+(?:\.\d+)?))[。；\s]*)+"
)


def _looks_like_stale_outcome_summary(value: str) -> bool:
    text = _normalize_text(value)
    if not text:
        return False
    stripped = _LEADING_RESULT_SCORE_SUMMARY_RE.sub("", text).strip()
    if not stripped:
        return True
    return stripped != text and not any(keyword in stripped for keyword in ("成交", "未成交", "方案", "金额", "顾虑", "预算"))


def _build_outcome_summary(
    *,
    status: str,
    deal_items: list[str],
    amount: str | None,
    loss_reasons: list[str],
    concern_texts: list[str],
    has_plan_context: bool,
) -> str:
    if status == "已成交":
        parts = ["对话中体现出明确成交倾向。"]
        if deal_items:
            parts.append(f"成交方案：{'；'.join(deal_items)}")
        if amount:
            parts.append(f"成交金额：{amount}")
        return " ".join(parts)

    if status == "未成交":
        parts = ["对话中未形成成交。"]
        if loss_reasons:
            parts.append(f"未成交原因：{'；'.join(loss_reasons)}")
        elif concern_texts:
            parts.append(f"主要顾虑：{'；'.join(concern_texts)}")
        return " ".join(parts)

    if deal_items or amount:
        parts = ["对话中形成了明确方案沟通，但最终是否成交未完全明确。"]
        if deal_items:
            parts.append(f"沟通方案：{'；'.join(deal_items)}")
        if amount:
            parts.append(f"提及金额：{amount}")
        return " ".join(parts)

    if loss_reasons or concern_texts or has_plan_context:
        parts = ["对话中已形成方案沟通与决策讨论，最终成交结果未明确。"]
        if loss_reasons:
            parts.append(f"当前阻力：{'；'.join(loss_reasons)}")
        elif concern_texts:
            parts.append(f"主要顾虑：{'；'.join(concern_texts)}")
        return " ".join(parts)

    return "对话中未明确体现最终成交结果。"


_SUCCESS_OUTCOME_SUMMARY_RE = re.compile(
    r"(?:已成交|成交方案|成交金额|已付款|付款|付了|已付|已交|交(?:了)?定金|定金(?:已)?到账|下单|锁档|确定(?:日期|时间)?|确认.{0,8}(?:手术|治疗|项目)|安排(?:治疗|手术)|排(?:了)?手术|办病历|体检|术前)"
)
_LOSS_OUTCOME_SUMMARY_RE = re.compile(
    r"(?:未成交|未形成成交|未能成交|没有成交|未付款|没有付款|暂未支付|未支付(?:定金)?|未交(?:定金)?|没有交(?:定金)?|未下单|未锁档|再考虑|回去(?:考虑|商量)|商量.{0,8}再决定|仍需(?:考虑|商量|比较)|先不做|暂不做)"
)
_DEAL_ACTION_SUMMARY_RE = re.compile(r"(?:已成交|下单|核销|锁档|付款|付了|定金|当天先做|今天先做|先做|直接做|安排治疗)")
_DEAL_UNKNOWN_AMOUNT_TEXT = "未明确"
_PRIMARY_DEMAND_INDICATION_RULES: tuple[tuple[tuple[str, ...], tuple[str, str, str]], ...] = (
    (("水光", "补水", "缺水", "干燥"), ("Y3", "SYZ3006", "BW3001")),
    (("肤色提亮", "提亮", "暗黄", "暗沉", "肤色暗"), ("Y3", "SYZ3007", "BW3001")),
    (("痘印", "痘痘", "痤疮"), ("Y3", "SYZ3005", "BW3001")),
    (("色斑", "色素", "斑点", "黄褐斑", "雀斑"), ("Y3", "SYZ3003", "BW3001")),
)


def _outcome_summary_conflicts_with_status(summary: str, status: str) -> bool:
    text = _normalize_text(summary)
    if not text:
        return False
    if status == "未成交":
        return bool(_SUCCESS_OUTCOME_SUMMARY_RE.search(text)) and not bool(_LOSS_OUTCOME_SUMMARY_RE.search(text))
    if status == "已成交":
        return bool(_LOSS_OUTCOME_SUMMARY_RE.search(text)) and not bool(_SUCCESS_OUTCOME_SUMMARY_RE.search(text))
    return bool(_SUCCESS_OUTCOME_SUMMARY_RE.search(text) or _LOSS_OUTCOME_SUMMARY_RE.search(text))


def _refresh_standardized_indication_summary(payload: dict[str, Any]) -> None:
    items = [_as_dict(item) for item in _as_list(payload.get("items"))]
    if not items:
        payload["summary"] = "对话中未识别出可标准化的适应症"
        return
    payload["summary"] = "识别出{}项适应症：{}".format(
        len(items),
        "；".join(
            f"{_normalize_text(item.get('indication_name'))}（{_normalize_text(item.get('body_part_name'))}）"
            for item in items
        ),
    )


def _backfill_indications_from_primary_demands(normalized: dict[str, Any]) -> bool:
    primary_demands = _as_dict(normalized.get("customer_primary_demands"))
    indication_payload = _as_dict(normalized.get("standardized_indications"))
    demand_items = [_as_dict(item) for item in _as_list(primary_demands.get("items"))]
    indication_items = [_as_dict(item) for item in _as_list(indication_payload.get("items"))]
    if not demand_items or not isinstance(normalized.get("standardized_indications"), dict):
        return False

    seen_pairs = {
        (_normalize_text(item.get("indication_code")), _normalize_text(item.get("body_part_code")))
        for item in indication_items
    }
    changed = False
    for demand in demand_items:
        evidence = _normalize_text(demand.get("evidence"))
        if not evidence:
            continue
        haystack = f"{_normalize_text(demand.get('demand'))} {_normalize_text(demand.get('body_part'))} {evidence}"
        for keywords, (department_code, indication_code, body_part_code) in _PRIMARY_DEMAND_INDICATION_RULES:
            if not any(keyword in haystack for keyword in keywords):
                continue
            if (indication_code, body_part_code) in seen_pairs:
                break
            matched = resolve_indication_reference_item(
                department_code=department_code,
                indication_code=indication_code,
                body_part_code=body_part_code,
            )
            if matched is None:
                break
            indication_items.append(
                {
                    "department_code": matched.department_code,
                    "department_name": matched.department_name,
                    "indication_code": matched.indication_code,
                    "indication_name": matched.indication_name,
                    "body_part_code": matched.body_part_code,
                    "body_part_name": matched.body_part_name,
                    "evidence": evidence,
                }
            )
            seen_pairs.add((matched.indication_code, matched.body_part_code))
            changed = True
            break

    if changed:
        indication_payload["items"] = indication_items
        _refresh_standardized_indication_summary(indication_payload)
        normalized["standardized_indications"] = indication_payload
    return changed


def _infer_closed_deal_items(
    *,
    deal_items: list[str],
    recommended_items: list[Any],
    outcome_summary: str,
    plan_summary: str,
    chief_demands: list[str],
) -> list[str]:
    if deal_items:
        return deal_items
    if not _DEAL_ACTION_SUMMARY_RE.search(outcome_summary):
        return deal_items

    text = " ".join(
        [
            outcome_summary,
            plan_summary,
            " ".join(chief_demands),
            " ".join(
                _normalize_text(_as_dict(item).get("plan")) + " " + _normalize_text(_as_dict(item).get("evidence"))
                for item in recommended_items
            ),
        ]
    )
    candidates: list[str] = []
    if "黄金超光子" in text:
        candidates.append("黄金超光子")
    elif "光子" in text:
        candidates.append("光子嫩肤")
    if "水光" in text and ("套餐" in text or "核销" in text or "下单" in text):
        candidates.append("水光/嗨体")
    if not candidates:
        return deal_items
    return _dedupe_text_list(candidates)


def _build_consultation_result(normalized: dict[str, Any]) -> dict[str, Any]:
    existing = _as_dict(normalized.get("consultation_result"))
    chief_existing = _as_dict(existing.get("chief_complaint_and_indications"))
    profile_existing = _as_dict(existing.get("customer_profile_summary"))
    deal_factors_existing = _as_dict(existing.get("deal_factors"))
    plan_existing = _as_dict(existing.get("recommended_plan"))
    outcome_existing = _as_dict(existing.get("deal_outcome"))

    primary_demands = _as_dict(normalized.get("customer_primary_demands"))
    standardized_indications = _as_dict(normalized.get("standardized_indications"))
    consumption_intent = _as_dict(normalized.get("consumption_intent"))
    staff_recommendations = _as_dict(normalized.get("staff_recommendations"))
    customer_concerns = _as_dict(normalized.get("customer_concerns"))
    customer_profile = _as_dict(normalized.get("customer_profile"))

    has_primary_payload = isinstance(normalized.get("customer_primary_demands"), dict)
    has_indication_payload = isinstance(normalized.get("standardized_indications"), dict)
    has_consumption_payload = isinstance(normalized.get("consumption_intent"), dict)
    has_concern_payload = isinstance(normalized.get("customer_concerns"), dict)
    has_recommendation_payload = isinstance(normalized.get("staff_recommendations"), dict)

    chief_summary = _rename_legacy_consultation_text(
        _normalize_text(primary_demands.get("summary"))
        or _normalize_text(standardized_indications.get("summary"))
        or (
            _normalize_text(chief_existing.get("summary"))
            if not has_primary_payload and not has_indication_payload
            else ""
        )
    )
    chief_demand_items = [_normalize_text(_as_dict(item).get("demand")) for item in _as_list(primary_demands.get("items"))]
    chief_demands = (
        _dedupe_text_list(chief_demand_items)
        if has_primary_payload
        else _dedupe_text_list(_normalize_text_list(chief_existing.get("primary_demands")))
    )
    indication_items = [_format_indication_brief(_as_dict(item)) for item in _as_list(standardized_indications.get("items"))]
    indication_briefs = (
        _dedupe_text_list(indication_items)
        if has_indication_payload
        else _dedupe_text_list(_normalize_text_list(chief_existing.get("standardized_indications")))
    )
    indication_briefs = [_rename_legacy_consultation_text(item) for item in indication_briefs]

    normalized_tags = [item for item in _as_list(customer_profile.get("tags")) if isinstance(item, dict)]
    existing_summary_tags = [item for item in _as_list(profile_existing.get("tags")) if isinstance(item, dict)]
    normalized_tag_pairs = {
        (_normalize_text(item.get("category")), _normalize_text(item.get("value")))
        for item in normalized_tags
    }
    existing_summary_tag_pairs = {
        (_normalize_text(item.get("category")), _normalize_text(item.get("value")))
        for item in existing_summary_tags
    }
    profile_age = _normalize_text(customer_profile.get("age"))
    profile_age_evidence = _normalize_text(customer_profile.get("age_evidence"))
    profile_existing_age = _normalize_text(profile_existing.get("age"))
    profile_existing_age_evidence = _normalize_text(profile_existing.get("age_evidence"))
    if profile_existing_age_evidence:
        supported_existing_age = _extract_supported_age_from_evidence(profile_existing_age_evidence)
        profile_existing_age = supported_existing_age
        if not supported_existing_age:
            profile_existing_age_evidence = None
    profile_summary = _normalize_text(profile_existing.get("summary"))
    existing_tag_count = int(profile_existing.get("extracted_tag_count") or 0)
    summary_is_stale_empty = bool(
        normalized_tags
        and (
            not profile_summary
            or "暂未提取" in profile_summary
            or "0 个画像标签" in profile_summary
            or existing_tag_count <= 0
        )
    )
    if summary_is_stale_empty:
        profile_summary = ""
    if normalized_tags and (
        existing_tag_count != len(normalized_tags)
        or existing_summary_tag_pairs != normalized_tag_pairs
        or not profile_summary.startswith("本次录音共提取")
    ):
        profile_summary = ""
    if not normalized_tags:
        # `customer_profile_summary` is a presentation mirror of the verified
        # `customer_profile.tags`. If validation removes every tag, do not keep
        # an older/free-text summary that still claims tags were found.
        profile_summary = ""
        existing_tag_count = 0
    if not profile_summary:
        if normalized_tags:
            profile_summary = f"本次录音共提取 {len(normalized_tags)} 个画像标签。"
        else:
            profile_summary = "本次录音暂未提取出明确画像标签。"

    concern_texts = (
        _dedupe_text_list(
            [
                _normalize_text(_as_dict(item).get("content"))
                for item in _as_list(customer_concerns.get("items"))
            ]
        )
        if has_concern_payload
        else _dedupe_text_list(_normalize_text_list(deal_factors_existing.get("concerns")))
    )
    raw_decision_factors = (
        _dedupe_text_list(_normalize_text_list(consumption_intent.get("decision_factors")))
        if has_consumption_payload
        else _dedupe_text_list(_normalize_text_list(deal_factors_existing.get("decision_factors")))
    )
    concern_evidence_texts = _dedupe_text_list(
        [
            _normalize_text(_as_dict(item).get("evidence"))
            for item in _as_list(customer_concerns.get("items"))
            if _normalize_text(_as_dict(item).get("evidence"))
        ]
        + _normalize_text_list(consumption_intent.get("evidence"))
    )
    loss_reasons = _dedupe_text_list(
        _normalize_text_list(outcome_existing.get("loss_reasons"))
    )
    decision_factors = _filter_decision_factors(
        raw_decision_factors,
        concern_texts=concern_texts,
        evidence_texts=concern_evidence_texts,
        loss_reasons=loss_reasons,
    )
    parts = []
    if _normalize_text(consumption_intent.get("budget")):
        parts.append(f"预算：{_normalize_text(consumption_intent.get('budget'))}")
    if concern_texts:
        parts.append(f"客户顾虑：{'；'.join(concern_texts)}")
    if decision_factors:
        parts.append(f"其他影响：{'；'.join(decision_factors)}")
    deal_factors_summary = "；".join(parts)

    recommended_items = (
        [
            {
                "plan": _format_recommendation_plan_text(_as_dict(item)),
                "acceptance": _normalize_text(_as_dict(item).get("customer_response")) or "未明确回应",
                "evidence": _normalize_text(_as_dict(item).get("evidence")),
            }
            for item in _as_list(staff_recommendations.get("items"))
            if _normalize_text(_as_dict(item).get("recommendation"))
            or _normalize_text(_as_dict(item).get("product_or_solution"))
        ]
        if has_recommendation_payload
        else _as_list(plan_existing.get("items"))
    )
    plan_summary = (
        _normalize_text(staff_recommendations.get("summary"))
        if has_recommendation_payload
        else _normalize_text(plan_existing.get("summary"))
    )

    outcome_status = _normalize_text(outcome_existing.get("status")) or "未明确"
    if outcome_status not in {"已成交", "未成交", "未明确"}:
        outcome_status = "未明确"
    existing_deal_items: list[str] = []
    for raw_item in _as_list(outcome_existing.get("deal_items")):
        if isinstance(raw_item, dict):
            text = (
                _normalize_text(raw_item.get("item"))
                or _normalize_text(raw_item.get("plan"))
                or _normalize_text(raw_item.get("name"))
                or _normalize_text(raw_item.get("content"))
            )
            amount = _normalize_text(raw_item.get("amount"))
            if text and amount:
                existing_deal_items.append(f"{text}（{amount}）")
            elif text:
                existing_deal_items.append(text)
        else:
            text = _normalize_text(raw_item)
            if text:
                existing_deal_items.append(text)

    deal_items = _dedupe_text_list(
        existing_deal_items
        or [
            _normalize_text(_as_dict(item).get("plan"))
            for item in recommended_items
            if _normalize_text(_as_dict(item).get("acceptance")) == "接受"
        ]
    )
    outcome_amount = _normalize_text(outcome_existing.get("amount")) or None
    outcome_summary = _normalize_text(outcome_existing.get("summary"))
    if outcome_status == "已成交" and _outcome_summary_conflicts_with_status(outcome_summary, outcome_status):
        outcome_status = "未成交"
    if outcome_status == "已成交":
        deal_items = _infer_closed_deal_items(
            deal_items=deal_items,
            recommended_items=recommended_items,
            outcome_summary=outcome_summary,
            plan_summary=plan_summary,
            chief_demands=chief_demands,
        )
        if not outcome_amount:
            outcome_amount = _DEAL_UNKNOWN_AMOUNT_TEXT
        loss_reasons = []
    elif outcome_status == "未成交":
        deal_items = []
        outcome_amount = None
    else:
        deal_items = []
        outcome_amount = None
        loss_reasons = []
    if _looks_like_stale_outcome_summary(outcome_summary) or _outcome_summary_conflicts_with_status(outcome_summary, outcome_status):
        outcome_summary = ""
    if not outcome_summary:
        outcome_summary = _build_outcome_summary(
            status=outcome_status,
            deal_items=deal_items,
            amount=outcome_amount,
            loss_reasons=loss_reasons,
            concern_texts=concern_texts,
            has_plan_context=bool(recommended_items or _normalize_text(plan_summary)),
        )

    return {
        "chief_complaint_and_indications": {
            "summary": chief_summary,
            "primary_demands": chief_demands,
            "seeding_points": [],
            "standardized_indications": indication_briefs,
        },
        "deal_factors": {
            "summary": deal_factors_summary,
            "budget": (
                _normalize_text(consumption_intent.get("budget"))
                if has_consumption_payload
                else _normalize_text(deal_factors_existing.get("budget"))
            )
            or None,
            "concerns": concern_texts,
            "decision_factors": decision_factors,
        },
        "recommended_plan": {
            "summary": plan_summary,
            "items": recommended_items,
        },
        "deal_outcome": {
            "status": outcome_status,
            "summary": outcome_summary,
            "deal_items": deal_items,
            "amount": outcome_amount,
            "loss_reasons": loss_reasons,
        },
        "customer_profile_summary": {
            "summary": profile_summary,
            "extracted_tag_count": len(normalized_tags) if normalized_tags else existing_tag_count,
            "age": profile_age or profile_existing_age or None,
            "age_evidence": profile_age_evidence or profile_existing_age_evidence or None,
            "tags": normalized_tags,
        },
    }


_LEGACY_PROCESS_DIMENSION_TO_SECTION = {
    "医院和医生介绍": ("doctor_consultation", "4.1"),
    "老带新等特别事项": ("required_actions", "8.2"),
    "负面交流检测": ("negative_feedback", "9.1"),
}


_PROCESS_NEGATIVE_SUMMARY_MARKERS = (
    "未识别", "未发现", "尚未", "未提及", "未明确", "未说明", "未进行",
    "未告知", "未将", "未协助", "未探寻", "未讲解", "未围绕", "未结合",
    "未给出", "未展示", "未主动", "未做", "检测到负面", "检测到不正确",
)


def _process_summary_polarity(text: str) -> str | None:
    if not text:
        return None
    if any(marker in text for marker in _PROCESS_NEGATIVE_SUMMARY_MARKERS):
        return "neg"
    if text.startswith(("已", "保持", "完成")) or "完成度较好" in text:
        return "pos"
    return None


def _build_consultation_process_evaluation(normalized: dict[str, Any]) -> dict[str, Any]:
    existing = _as_dict(normalized.get("consultation_process_evaluation"))
    existing_sections = {}
    for section in _as_list(existing.get("sections")):
        section_dict = _as_dict(section)
        section_key = _normalize_text(section_dict.get("code")) or _normalize_text(section_dict.get("name"))
        if section_key:
            existing_sections[section_key] = section_dict

    legacy_dimensions = {
        _normalize_text(_as_dict(item).get("name")): _as_dict(item)
        for item in _as_list(_as_dict(normalized.get("consultation_evaluation")).get("dimensions"))
        if _normalize_text(_as_dict(item).get("name"))
    }

    sections: list[dict[str, Any]] = []
    for section_blueprint in CONSULTATION_PROCESS_EVALUATION_BLUEPRINT:
        section_code = section_blueprint["code"]
        section_name = section_blueprint["name"]
        existing_section = existing_sections.get(section_code) or existing_sections.get(section_name) or {}
        existing_checkpoints = {
            _normalize_text(_as_dict(item).get("code")) or _normalize_text(_as_dict(item).get("name")): _as_dict(item)
            for item in _as_list(existing_section.get("checkpoints"))
        }

        legacy_dim_name = next(
            (
                legacy_name
                for legacy_name, target in _LEGACY_PROCESS_DIMENSION_TO_SECTION.items()
                if target[0] == section_code
            ),
            "",
        )
        legacy_dimension = legacy_dimensions.get(legacy_dim_name, {})

        checkpoints: list[dict[str, Any]] = []
        for checkpoint_blueprint in section_blueprint["checkpoints"]:
            checkpoint_code = checkpoint_blueprint["code"]
            checkpoint_name = checkpoint_blueprint["name"]
            existing_checkpoint = (
                existing_checkpoints.get(checkpoint_code)
                or existing_checkpoints.get(checkpoint_name)
                or {}
            )

            _seed_point_score = existing_checkpoint.get("point_score")
            _seed_passed = bool(_seed_point_score) and float(_seed_point_score or 0) > 0
            seeded_checkpoint = {
                "code": checkpoint_code,
                "name": checkpoint_name,
                "point_score": _seed_point_score,
                "max_score": existing_checkpoint.get("max_score", 1),
                "status": _normalize_text(existing_checkpoint.get("status")),
                "summary": _normalize_text(existing_checkpoint.get("summary")),
                "evidence": _normalize_text_list(existing_checkpoint.get("evidence")),
                "issues": [] if _seed_passed else [
                    {
                        "description": _normalize_text(_as_dict(issue).get("description")),
                        "evidence": _normalize_text(_as_dict(issue).get("evidence")),
                    }
                    for issue in _as_list(existing_checkpoint.get("issues"))
                    if _normalize_text(_as_dict(issue).get("description"))
                ],
            }

            if not existing_checkpoint and legacy_dimension and _LEGACY_PROCESS_DIMENSION_TO_SECTION.get(legacy_dim_name) == (
                section_code,
                checkpoint_code,
            ):
                _legacy_point_score = legacy_dimension.get("point_score")
                _legacy_passed = bool(_legacy_point_score) and float(_legacy_point_score or 0) > 0
                seeded_checkpoint["point_score"] = _legacy_point_score
                seeded_checkpoint["max_score"] = legacy_dimension.get("max_score", 1)
                seeded_checkpoint["status"] = _normalize_text(legacy_dimension.get("status"))
                seeded_checkpoint["summary"] = _normalize_text(legacy_dimension.get("summary"))
                seeded_checkpoint["issues"] = [] if _legacy_passed else [
                    {
                        "description": _normalize_text(_as_dict(issue).get("description")),
                        "evidence": _normalize_text(_as_dict(issue).get("evidence")),
                    }
                    for issue in _as_list(legacy_dimension.get("issues"))
                    if _normalize_text(_as_dict(issue).get("description"))
                ]
                seeded_checkpoint["evidence"] = _dedupe_text_list(
                    [issue["evidence"] for issue in seeded_checkpoint["issues"] if issue.get("evidence")]
                )

            # Reconcile summary/status with point_score to remove contradictions
            _final_score_val = _as_number(seeded_checkpoint.get("point_score")) or 0.0
            _final_max_val = _as_number(seeded_checkpoint.get("max_score")) or 1.0
            if _final_max_val <= 0:
                _final_max_val = 1.0
            _summary_pol = _process_summary_polarity(_normalize_text(seeded_checkpoint.get("summary")))
            if _final_score_val > 0 and _summary_pol == "neg":
                # Drop misleading negative summary on a passed checkpoint; let downstream rebuild fill it
                seeded_checkpoint["summary"] = ""
            elif _final_score_val == 0 and _summary_pol == "pos":
                seeded_checkpoint["summary"] = ""
            # Reconcile status text
            _status_text = _normalize_text(seeded_checkpoint.get("status"))
            if _status_text:
                if _final_score_val >= _final_max_val and "达标" not in _status_text and _status_text not in ("完成", "通过"):
                    seeded_checkpoint["status"] = "达标"
                elif _final_score_val == 0 and _status_text in ("达标", "完成", "通过"):
                    seeded_checkpoint["status"] = "未达标"
            # Final guard: passed checkpoints never carry failure issues
            if _final_score_val >= _final_max_val:
                seeded_checkpoint["issues"] = []

            checkpoints.append(seeded_checkpoint)

        sections.append(
            {
                "code": section_code,
                "name": section_name,
                "point_score": existing_section.get("point_score"),
                "max_score": existing_section.get("max_score", 1),
                "status": _normalize_text(existing_section.get("status")),
                "summary": _normalize_text(existing_section.get("summary")),
                "checkpoints": checkpoints,
            }
        )

    max_total_score = float(len(sections))
    total_score = 0.0
    for section in sections:
        point_score = _as_number(section.get("point_score")) or 0.0
        max_score = _as_number(section.get("max_score")) or 1.0
        if max_score <= 0:
            max_score = 1.0
        total_score += max(0.0, min(point_score, max_score)) / max_score
    total_score = round(total_score, 2)
    overall_score = round((total_score / max_total_score) * 10, 2) if max_total_score > 0 else 0.0

    return {
        "total_score": _as_number(existing.get("total_score")) if _as_number(existing.get("total_score")) is not None else total_score,
        "max_total_score": _as_number(existing.get("max_total_score")) if _as_number(existing.get("max_total_score")) is not None else max_total_score,
        "overall_score": _as_number(existing.get("overall_score")) if _as_number(existing.get("overall_score")) is not None else overall_score,
        "overall_summary": _normalize_text(existing.get("overall_summary"))
        or _rename_legacy_consultation_text(_as_dict(normalized.get("consultation_evaluation")).get("overall_summary")),
        "sections": sections,
    }


def _is_explicit_no_prior_treatment(value: Any) -> bool:
    text = _normalize_text(value)
    if not text:
        return False
    normalized = text.replace(" ", "")
    return any(re.search(pattern, normalized) for pattern in _EXPLICIT_NO_PRIOR_TREATMENT_PATTERNS)


def _is_budget_profile_tag(item: dict[str, Any]) -> bool:
    category = str(item.get("category") or "").strip()
    return bool(category and any(marker in category for marker in _BUDGET_CATEGORY_MARKERS))


def _is_invalid_profile_tag_value(category: str, value: str) -> bool:
    if category == NEGATIVE_PROJECT_TAG_CATEGORY and value == NEGATIVE_PROJECT_EMPTY_VALUE:
        return False
    return value in _INVALID_PROFILE_TAG_VALUES


def _pick_canonical_budget(values: list[str]) -> str | None:
    if not values:
        return None

    def _score(value: str) -> tuple[int, int]:
        digit_count = sum(1 for ch in value if ch.isdigit())
        return (digit_count, len(value))

    return max(values, key=_score)


def _normalize_profile_tag_item(item: dict[str, Any]) -> dict[str, Any] | None:
    category = canonicalize_profile_tag_category(item.get("category"))
    if category is None:
        return None
    if category == "治疗历史":
        category = "治疗项目"
    value = canonicalize_profile_tag_value(category, item.get("value"))
    if value is None:
        return None
    if _is_invalid_profile_tag_value(category, value):
        return None
    if not is_valid_profile_tag_value(category, value):
        return None
    return {**item, "category": category, "value": value}


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _normalize_negative_project_tags(tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_tags: list[dict[str, Any]] = []
    category_items: list[tuple[dict[str, Any], str]] = []
    concrete_values: list[str] = []
    insert_index: int | None = None
    has_prior_treatment_context = False

    for item in tags:
        category = _normalize_text(item.get("category"))
        value = _normalize_text(item.get("value"))
        if (
            category in _PRIOR_TREATMENT_CONTEXT_CATEGORIES
            and value
            and value not in _EMPTY_TREATMENT_CONTEXT_VALUES
            and not _is_explicit_no_prior_treatment(value)
        ):
            has_prior_treatment_context = True
        if category != NEGATIVE_PROJECT_TAG_CATEGORY:
            normalized_tags.append(item)
            continue

        if insert_index is None:
            insert_index = len(normalized_tags)
        category_items.append((item, value))
        if not value or value in _EMPTY_NEGATIVE_PROJECT_VALUES:
            continue
        concrete_values.extend(
            part.strip()
            for part in re.split(r"[、,，;；]\s*", value)
            if part and part.strip()
        )

    concrete_values = _dedupe_preserve_order(concrete_values)
    concrete_template = next((item for item, value in category_items if value not in _EMPTY_NEGATIVE_PROJECT_VALUES), None)
    empty_template = category_items[0][0] if category_items else {}

    if concrete_values:
        negative_item = {
            **(concrete_template or empty_template),
            "category": NEGATIVE_PROJECT_TAG_CATEGORY,
            "value": "；".join(concrete_values),
        }
    elif has_prior_treatment_context:
        negative_item = {
            **empty_template,
            "category": NEGATIVE_PROJECT_TAG_CATEGORY,
            "value": NEGATIVE_PROJECT_EMPTY_VALUE,
        }
        if not category_items:
            negative_item.pop("evidence", None)
            negative_item.pop("weight_level", None)
    if insert_index is None:
        if concrete_values or has_prior_treatment_context:
            normalized_tags.append(negative_item)
    else:
        if concrete_values or has_prior_treatment_context:
            normalized_tags.insert(insert_index, negative_item)

    return normalized_tags


def _normalize_explicit_no_prior_treatment_tags(tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    explicit_no_prior = any(
        _normalize_text(item.get("category")) == "治疗项目"
        and _is_explicit_no_prior_treatment(item.get("value"))
        for item in tags
    )
    if not explicit_no_prior:
        return tags

    normalized_tags: list[dict[str, Any]] = []
    existing_by_category: dict[str, dict[str, Any]] = {}

    for item in tags:
        category = _normalize_text(item.get("category"))
        if category in _NO_PRIOR_TREATMENT_DEPENDENT_CATEGORIES:
            value = _normalize_text(item.get("value"))
            if value == "无" and category not in existing_by_category:
                existing_by_category[category] = {**item, "category": category, "value": "无"}
            continue
        normalized_tags.append(item)

    insertion_index = next(
        (
            idx + 1
            for idx, item in enumerate(normalized_tags)
            if _normalize_text(item.get("category")) == "治疗项目"
            and _is_explicit_no_prior_treatment(item.get("value"))
        ),
        len(normalized_tags),
    )

    dependent_items: list[dict[str, Any]] = []
    for category in _NO_PRIOR_TREATMENT_DEPENDENT_CATEGORIES:
        concrete_item = existing_by_category.get(category)
        if concrete_item is not None:
            dependent_items.append(concrete_item)
            continue
        dependent_items.append({"category": category, "value": "无"})

    for offset, item in enumerate(dependent_items):
        normalized_tags.insert(insertion_index + offset, item)
    return normalized_tags


def _profile_tag_score(category: str, item: dict[str, Any]) -> tuple[int, int, int, int]:
    value = _normalize_text(item.get("value"))
    evidence = _normalize_text(item.get("evidence"))
    priority = _PROFILE_TAG_VALUE_PRIORITY.get(category, {}).get(value, 0)
    return (
        priority,
        1 if evidence else 0,
        len(evidence),
        len(value),
    )


def _pick_best_profile_tag(category: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    best = items[0]
    best_score = _profile_tag_score(category, best)
    for item in items[1:]:
        score = _profile_tag_score(category, item)
        if score > best_score:
            best = item
            best_score = score
    return best


def _resolve_profile_tag_conflicts(tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    category_order: list[str] = []
    for item in tags:
        category = _normalize_text(item.get("category"))
        if not category:
            continue
        if category not in grouped:
            category_order.append(category)
            grouped[category] = []
        grouped[category].append(item)

    resolved: list[dict[str, Any]] = []
    for category in category_order:
        items = grouped.get(category, [])
        if not items:
            continue

        if category == "治疗项目":
            explicit_no_prior = [
                item for item in items if _is_explicit_no_prior_treatment(item.get("value"))
            ]
            if explicit_no_prior:
                resolved.append(_pick_best_profile_tag(category, explicit_no_prior))
                continue
            resolved.extend(items)
            continue

        if category == "健康风险/禁忌":
            concrete_items = [
                item
                for item in items
                if _normalize_text(item.get("value")) != _NO_RISK_HEALTH_TAG_VALUE
            ]
            if concrete_items:
                resolved.extend(concrete_items)
            else:
                resolved.append(_pick_best_profile_tag(category, items))
            continue

        if category in _SINGLE_SELECT_PROFILE_CATEGORIES:
            resolved.append(_pick_best_profile_tag(category, items))
            continue

        resolved.extend(items)

    return resolved


def _normalize_negative_project_themes(items: list[CustomerMergedThemeOut]) -> list[CustomerMergedThemeOut]:
    negative_items = [item for item in items if item.label.startswith(f"{NEGATIVE_PROJECT_TAG_CATEGORY}：")]
    if len(negative_items) <= 1:
        return items

    concrete_items = [item for item in negative_items if item.detail and item.detail != NEGATIVE_PROJECT_EMPTY_VALUE]
    if concrete_items:
        return [item for item in items if item not in negative_items or item in concrete_items]

    best_empty = max(
        negative_items,
        key=lambda item: (
            item.count,
            item.latest_seen_at or "",
        ),
    )
    return [item for item in items if item not in negative_items or item == best_empty]


def _extract_age_phrase(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    text = " ".join(value.split())
    if not text:
        return None

    patterns = [
        r"(\d+\s*-\s*\d+岁)",
        r"(\d+多岁)",
        r"(\d+岁左右)",
        r"(约\d+岁)",
        r"(\d+岁以上)",
        r"(\d+岁以下)",
        r"(\d+岁)",
    ]

    import re

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).replace(" ", "")
    return None


def _extract_birthdate_phrase(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    text = " ".join(value.split())
    if not text:
        return None

    patterns = (
        (r"(?P<year>\d{4})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})", "full"),
        (r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})[日号]?", "full"),
        (r"(?P<year>\d{4})[-/.](?P<month>\d{1,2})", "month"),
        (r"(?P<year>\d{4})年(?P<month>\d{1,2})月", "month"),
        (r"(?P<year>\d{4})年", "year"),
    )
    for pattern, mode in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        year = int(match.group("year"))
        if mode == "year":
            return f"{year:04d}"
        month = int(match.group("month"))
        if mode == "month":
            return f"{year:04d}-{month:02d}"
        day = int(match.group("day"))
        return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _age_specificity_score(value: str) -> int:
    normalized = value.replace(" ", "")

    import re

    bounded_range = re.match(r"(\d+)-(\d+)岁", normalized)
    if bounded_range:
        start = int(bounded_range.group(1))
        end = int(bounded_range.group(2))
        return 200 - max(end - start, 0)

    if re.search(r"\d+岁左右|约\d+岁|\d+岁$|\d+多岁$", normalized):
        return 180
    if re.search(r"\d+岁以上|\d+岁以下", normalized):
        return 120
    return 60


def _birthdate_specificity_score(value: str) -> int:
    normalized = value.replace(" ", "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        return 320
    if re.fullmatch(r"\d{4}-\d{2}", normalized):
        return 260
    if re.fullmatch(r"\d{4}", normalized):
        return 220
    return 0


def _pick_canonical_age(values: list[str | None]) -> str | None:
    best: str | None = None
    best_score = -1
    for value in values:
        if not value:
            continue
        score = _age_specificity_score(value)
        if score > best_score:
            best = value
            best_score = score
    return best


def _age_evidence_mention_is_future_or_hypothetical(text: str, start: int, end: int, age_text: str) -> bool:
    window = text[max(0, start - 28): min(len(text), end + 28)]
    compact_window = re.sub(r"\s+", "", window)
    compact_prefix = re.sub(r"\s+", "", text[max(0, start - 16): start])
    compact_suffix = re.sub(r"\s+", "", text[end: min(len(text), end + 18)])
    compact_age = re.sub(r"\s+", "", age_text)
    age_question_like = re.search(r"(?:今年)?(?:多大|几岁|多少岁)|年龄|身份证", window)

    if re.search(r"(?:我要是|要是我|如果我|假如我|换成我|像我|我当时|我那时候|我以前|我之前).{0,18}" + re.escape(compact_age), compact_window):
        return True
    if re.search(r"(?:我也要|我还要|我要|要).{0,4}" + re.escape(compact_age), compact_window) and not age_question_like:
        return True
    if re.search(re.escape(compact_age) + r"(?:的时候|那时候|当时|以前|之前|左右|上下)", compact_window) and not age_question_like:
        return True
    if re.search(re.escape(compact_age) + r"(?:离开|离|走|跑)", compact_window) and not age_question_like:
        return True
    if re.search(r"(?:像|看着像|看起来像|显得像).{0,6}" + re.escape(compact_age), compact_window) and not age_question_like:
        return True
    if compact_suffix.startswith(("以后", "之后", "后")):
        return True
    if re.search(re.escape(compact_age) + r"(?:以后|之后|后)", compact_window):
        return True
    if any(hint in compact_window for hint in _AGE_FUTURE_OR_HYPOTHETICAL_HINTS) and not age_question_like:
        return True
    if any(hint in compact_prefix for hint in ("以后", "之后", "将来", "未来")) and not age_question_like:
        return True
    if re.search(r"(?:还可以|可以|能|还能)$", compact_prefix) and not age_question_like:
        return True
    return False


def _extract_supported_age_from_evidence(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    text = " ".join(value.split())
    if not text:
        return None

    candidates: list[tuple[int, int, str]] = []
    age_matches: list[tuple[int, int, int, str]] = []
    for match in re.finditer(r"(?<!\d)(\d{2,3})\s*(多?岁)", text):
        age_matches.append((int(match.group(1)), match.start(), match.end(), f"{int(match.group(1))}{match.group(2)}"))
    for match in re.finditer(r"(?:身份证号年龄|年龄|今年多大|多大|几岁)[^，。；;]{0,10}?(?<!\d)(\d{2,3})(?!\d)", text):
        age_matches.append((int(match.group(1)), match.start(1), match.end(1), f"{int(match.group(1))}岁"))
    for match in re.finditer(r"(?:还没有|还没|没|没有)?(?:满|到)\s*(\d{2})\s*(?:岁)?", text):
        age = int(match.group(1))
        if any(cue in match.group(0) for cue in ("还没有", "还没", "没", "没有")):
            age -= 1
        age_matches.append((age, match.start(1), match.end(1), f"{age}岁"))

    for age, start, end, age_text in age_matches:
        if not (10 <= age <= 100):
            continue

        window = text[max(0, start - 28): min(len(text), end + 28)]
        prefix = text[max(0, start - 10): start]

        if _age_evidence_mention_is_future_or_hypothetical(text, start, end, age_text):
            continue
        if re.search(r"(?:到|到了|等到|变到|再到)\s*$", prefix):
            continue
        if any(marker in window for marker in ("看不出来", "你可以叫我", "您可以叫我", "叫我", "比你大", "比您大")):
            continue
        if any(marker in window for marker in ("不像", "不是", "不到", "案例", "顾客", "别人", "人家", "很多人")) and not re.search(
            r"(?:今年多大|年龄|你现在|您现在|身份证)", window
        ):
            continue
        if any(marker in prefix for marker in ("不可能让你到", "变到", "再到")):
            continue

        score = 0
        if re.search(r"(?:今年)?(?:多大|几岁|多少岁)|年龄|身份证", window):
            score += 10
        if re.search(r"(?:我|你|您|她|他)(?:今年|现在)?[^，。；;]{0,14}" + re.escape(age_text), window):
            score += 8
        if re.search(r"(?:今年多大|年龄|身份证号年龄)[^，。；;]{0,12}(?:\d{2,3}\s*)?" + re.escape(age_text), window):
            score += 8
        if "相当于" in window or "还没有满" in window or "还没满" in window or "没满" in window:
            score += 6
        if score <= 0:
            continue
        candidates.append((score, -start, age_text))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def _pick_canonical_birthdate(values: list[str | None]) -> str | None:
    best: str | None = None
    best_score = -1
    for value in values:
        if not value:
            continue
        score = _birthdate_specificity_score(value)
        if score > best_score:
            best = value
            best_score = score
    return best


def _extract_profile_birthdate(tags: list[dict[str, Any]]) -> str | None:
    candidates: list[str] = []
    for item in tags:
        category = str(item.get("category") or "")
        value = str(item.get("value") or "")
        if (
            category == BIRTHDATE_TAG_CATEGORY
            or "出生日期" in category
            or "出生" in value
        ):
            birthdate = (
                _extract_birthdate_phrase(value)
                or _extract_birthdate_phrase(category)
            )
            if birthdate:
                candidates.append(birthdate)
    return _pick_canonical_birthdate(candidates)


def _extract_profile_age(tags: list[dict[str, Any]]) -> str | None:
    candidates: list[str] = []
    for item in tags:
        category = str(item.get("category") or "")
        value = str(item.get("value") or "")
        if "年龄" in category or "年龄" in value or "岁" in value:
            age = _extract_age_phrase(value) or _extract_age_phrase(category)
            if age:
                candidates.append(age)
    return _pick_canonical_age(candidates)


def _extract_profile_age_evidence(tags: list[dict[str, Any]], age: str | None) -> str | None:
    if not age:
        return None
    for item in tags:
        category = str(item.get("category") or "")
        value = str(item.get("value") or "")
        evidence = _normalize_text(item.get("evidence"))
        if not evidence:
            continue
        extracted_age = _extract_age_phrase(value) or _extract_age_phrase(category)
        if extracted_age == age:
            return evidence
    return None


def _extract_canonical_birthdate(result: dict[str, Any]) -> str | None:
    original = _as_dict(result.get("_original"))
    strategy = _as_dict(_as_dict(original.get("strategyAnalyzeResult")).get("strategy"))
    consult_summary = _as_dict(_as_dict(original.get("consultAnalyzeResult")).get("summary"))
    profile_tags = _as_list(_as_dict(result.get("customer_profile")).get("tags"))

    characteristics = _as_dict(strategy.get("customer_characteristics"))
    strategy_birthdate = (
        _extract_birthdate_phrase(characteristics.get("birthdate"))
        or _extract_birthdate_phrase(characteristics.get("birthday"))
        or _extract_birthdate_phrase(characteristics.get("出生日期"))
    )
    consult_birthdate = (
        _extract_birthdate_phrase(_as_dict(consult_summary.get(BIRTHDATE_TAG_CATEGORY)).get("content"))
        or _extract_birthdate_phrase(_as_dict(consult_summary.get("出生日期")).get("content"))
        or _extract_birthdate_phrase(_as_dict(consult_summary.get("年龄")).get("content"))
    )
    consult_profile_birthdate = (
        _extract_birthdate_phrase(_as_dict(consult_summary.get("客户档案")).get("content"))
    )
    profile_birthdate = _extract_profile_birthdate([item for item in profile_tags if isinstance(item, dict)])

    return _pick_canonical_birthdate([strategy_birthdate, consult_birthdate, consult_profile_birthdate, profile_birthdate])


def _extract_canonical_age(result: dict[str, Any]) -> str | None:
    original = _as_dict(result.get("_original"))
    strategy = _as_dict(_as_dict(original.get("strategyAnalyzeResult")).get("strategy"))
    consult_summary = _as_dict(_as_dict(original.get("consultAnalyzeResult")).get("summary"))
    profile = _as_dict(result.get("customer_profile"))
    profile_tags = _as_list(profile.get("tags"))

    characteristics = _as_dict(strategy.get("customer_characteristics"))
    evidence_age = _extract_supported_age_from_evidence(profile.get("age_evidence"))
    direct_age = (
        evidence_age
        or _extract_age_phrase(profile.get("age"))
        or _extract_age_phrase(characteristics.get("age"))
        or _extract_age_phrase(characteristics.get("年龄"))
    )
    consult_age = (
        _extract_age_phrase(_as_dict(consult_summary.get("年龄")).get("content"))
        or _extract_age_phrase(_as_dict(consult_summary.get("出生日期/年龄")).get("content"))
        or _extract_age_phrase(_as_dict(consult_summary.get("客户档案")).get("content"))
    )
    profile_age = _extract_profile_age([item for item in profile_tags if isinstance(item, dict)])
    if _normalize_text(profile.get("age_evidence")) and not evidence_age:
        direct_age = None
        profile_age = None
    return _pick_canonical_age([direct_age, consult_age, profile_age])


def normalize_analysis_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return result

    normalized = deepcopy(result)

    # Normalize demand_priority: int|None → list[int] for backward compat
    recs = normalized.get("staff_recommendations")
    if isinstance(recs, dict):
        for item in _as_list(recs.get("items")):
            if isinstance(item, dict):
                dp = item.get("demand_priority")
                if isinstance(dp, list):
                    item["demand_priority"] = [int(x) for x in dp if isinstance(x, (int, float))]
                elif isinstance(dp, (int, float)):
                    item["demand_priority"] = [int(dp)]
                else:
                    item["demand_priority"] = []

    standardized_indications = normalized.get("standardized_indications")
    if isinstance(standardized_indications, dict):
        normalized_indications = normalize_standardized_indications_payload(standardized_indications)
        if "summary" in normalized_indications:
            normalized_indications["summary"] = _rename_legacy_consultation_text(normalized_indications.get("summary"))
        if "inference_note" in normalized_indications:
            normalized_indications["inference_note"] = _rename_legacy_consultation_text(
                normalized_indications.get("inference_note")
            )
        normalized["standardized_indications"] = normalized_indications

    primary_demands = normalized.get("customer_primary_demands")
    if isinstance(primary_demands, dict):
        deduped_items, deduped_changed = _dedupe_primary_demand_items(_as_list(primary_demands.get("items")))
        if deduped_changed:
            primary_demands = dict(primary_demands)
            primary_demands["items"] = deduped_items
            primary_demands["summary"] = "；".join(_normalize_text(item.get("demand")) for item in deduped_items[:3])
            normalized["customer_primary_demands"] = primary_demands
        _backfill_indications_from_primary_demands(normalized)

    customer_profile = _as_dict(normalized.setdefault("customer_profile", {}))
    raw_tags = [item for item in _as_list(customer_profile.get("tags")) if isinstance(item, dict)]
    consumption_intent = _as_dict(normalized.setdefault("consumption_intent", {}))

    normalized_tags: list[dict[str, Any]] = []
    budget_candidates: list[str] = []
    seen_tag_pairs: set[tuple[str, str]] = set()
    for item in raw_tags:
        if _is_budget_profile_tag(item):
            budget_value = _normalize_budget_text(item.get("value"))
            if budget_value:
                budget_candidates.append(budget_value)
            continue
        normalized_item = _normalize_profile_tag_item(item)
        if normalized_item is None:
            continue
        dedupe_key = (
            str(normalized_item.get("category") or "").strip(),
            str(normalized_item.get("value") or "").strip(),
        )
        if dedupe_key in seen_tag_pairs:
            continue
        seen_tag_pairs.add(dedupe_key)
        normalized_tags.append(normalized_item)

    current_budget = _normalize_budget_text(consumption_intent.get("budget"))
    if current_budget is None:
        canonical_budget = _pick_canonical_budget(budget_candidates)
        if canonical_budget is not None:
            consumption_intent["budget"] = canonical_budget
    else:
        consumption_intent["budget"] = current_budget

    normalized["consumption_intent"] = consumption_intent

    canonical_age = _extract_canonical_age(normalized)
    if canonical_age:
        customer_profile["age"] = canonical_age
        if not _normalize_text(customer_profile.get("age_evidence")):
            age_evidence = _extract_profile_age_evidence(raw_tags, canonical_age)
            if age_evidence:
                customer_profile["age_evidence"] = age_evidence
    elif _normalize_text(customer_profile.get("age_evidence")):
        customer_profile.pop("age", None)
        customer_profile.pop("age_evidence", None)

    canonical_birthdate = _extract_canonical_birthdate(normalized)
    if canonical_birthdate:
        birthdate_adjusted_tags: list[dict[str, Any]] = []
        birthdate_written = False
        for item in normalized_tags:
            category = str(item.get("category") or "")
            value = str(item.get("value") or "")
            if category == BIRTHDATE_TAG_CATEGORY or "出生日期" in category or _extract_birthdate_phrase(value):
                if not birthdate_written:
                    birthdate_adjusted_tags.append(
                        {**item, "category": BIRTHDATE_TAG_CATEGORY, "value": canonical_birthdate}
                    )
                    birthdate_written = True
                continue
            birthdate_adjusted_tags.append(item)

        if not birthdate_written:
            birthdate_adjusted_tags.append({"category": BIRTHDATE_TAG_CATEGORY, "value": canonical_birthdate})
        normalized_tags = birthdate_adjusted_tags

    normalized_tags = _normalize_negative_project_tags(normalized_tags)
    normalized_tags = _normalize_explicit_no_prior_treatment_tags(normalized_tags)
    normalized_tags = _resolve_profile_tag_conflicts(normalized_tags)
    customer_profile["tags"] = normalized_tags
    normalized["customer_profile"] = customer_profile

    consultation_evaluation = _as_dict(normalized.setdefault("consultation_evaluation", {}))
    consultation_evaluation["overall_summary"] = _rename_legacy_consultation_text(
        consultation_evaluation.get("overall_summary")
    )
    normalized_dimensions: list[dict[str, Any]] = []
    for item in _as_list(consultation_evaluation.get("dimensions")):
        if not isinstance(item, dict):
            continue
        normalized_item = dict(item)
        normalized_item["name"] = normalize_consultation_dimension_name(item.get("name"))
        if "summary" in normalized_item:
            normalized_item["summary"] = _rename_legacy_consultation_text(normalized_item.get("summary"))
        if "comment" in normalized_item:
            normalized_item["comment"] = _rename_legacy_consultation_text(normalized_item.get("comment"))
        issues = []
        for raw_issue in _as_list(normalized_item.get("issues")):
            if not isinstance(raw_issue, dict):
                continue
            issues.append(
                {
                    **raw_issue,
                    "description": _rename_legacy_consultation_text(raw_issue.get("description")),
                    "evidence": _normalize_text(raw_issue.get("evidence")),
                }
            )
        normalized_item["issues"] = issues
        normalized_dimensions.append(normalized_item)
    consultation_evaluation["dimensions"] = normalized_dimensions
    normalized["consultation_evaluation"] = consultation_evaluation
    normalized["consultation_result"] = _build_consultation_result(normalized)
    normalized["consultation_process_evaluation"] = _build_consultation_process_evaluation(normalized)
    return normalized


def normalize_task_detail(task: AnalysisTask) -> TaskDetailOut:
    normalized = TaskDetailOut.model_validate(task)
    normalized.result = normalize_analysis_result(task.result)
    return normalized


def normalize_profile_themes(items: list[CustomerMergedThemeOut]) -> list[CustomerMergedThemeOut]:
    items = _normalize_negative_project_themes(items)
    birthdate_items = [
        item
        for item in items
        if "出生日期" in item.label
        or "年龄" in item.label
        or (item.detail and ("出生日期" in item.detail or "年龄" in item.detail))
    ]
    if len(birthdate_items) <= 1:
        return items

    best_item = max(
        birthdate_items,
        key=lambda item: _age_specificity_score(
            _extract_age_phrase(item.label) or _extract_age_phrase(item.detail) or ""
        ) + _birthdate_specificity_score(
            _extract_birthdate_phrase(item.label) or _extract_birthdate_phrase(item.detail) or item.label
        ),
    )
    return [item for item in items if item not in birthdate_items or item == best_item]
