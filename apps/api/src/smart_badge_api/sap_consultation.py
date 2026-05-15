"""
构建 SAP HANA 咨询单接口 (ZMC_FM_INT_YMC_SET / YMC_2013) 的回传数据。

核心逻辑：
  1. 从录音关联的到诊单中获取客户信息和接诊人信息
  2. 从录音分析结果中提取适应症、客户主诉、推荐方案、顾虑点
  3. 按要求格式拼装咨询备注(text)
  4. 按录音关联的每一张到诊单分别生成 RFC 请求体，TAB_SYZ 内包含全部适应症
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.analysis.llm_client import chat_completion, parse_json_response
from smart_badge_api.analysis.pipeline import sanitize_analysis_result_with_raw
from smart_badge_api.analysis.reference_data import normalize_standardized_indications_payload
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import (
    AnalysisTask,
    Recording,
    RecordingVisitAnalysis,
    RecordingVisitLink,
    SapConsultationReview,
    Staff,
    Visit,
    VisitOrder,
    WecomTenant,
)

logger = logging.getLogger(__name__)

CN_TZ = ZoneInfo("Asia/Shanghai")
SAP_CONSULTATION_PREVIEW_RESULT_KEY = "sap_consultation_preview"
SPARSE_MAIN_FACT_FALLBACK_NOTE = "低内容量医美业务场景兜底"

_VISIT_RESULT_FUSION_SYSTEM_PROMPT = """\
你是医美到诊单级 SAP 回写融合分析员。输入是同一张到诊单关联的多条录音已有面诊分析结果，不含完整录音原文，也不含面诊过程评价。

任务：融合多条录音的面诊结果，输出这张到诊单最终应回写 SAP 的结构化 JSON。不要机械合并，要判断主次、时间推进、冲突和最终状态。

规则：
1. 正确率优先，只保留已有分析结果能支撑的结论；不确定则省略。
2. 按 recording_index 理解时间线。后续医生面诊、报价、客户表态、付款/放弃动作可修正前序“待定/犹豫/考虑”。
3. 主诉/适应症按本次到诊单归纳去重；历史项目、否定表达、第三方案例、方案机制不能变成客户主诉。
4. standardized_indications 只能从输入 allowed_standardized_indications 复制已有编码整组，不得新增、猜测或改码。
5. 鼻基底/面中/苹果肌/八字纹在填充、注射、玻尿酸、胶原、瑞德喜语境下归面部填充；泪沟/卧蚕注射复配归塑美（眼部D）；否定、历史、机构闲聊或弱证据项目不要进入适应症。
6. deal_outcome 以最终落地动作为准：付款、定金、下单、锁档、确定治疗/日期为已成交；仅咨询、考虑、对比、未付款为未成交或未明确。
7. recommended_plan 输出到诊单级最终推荐方案清单，只保留针对本次主诉的解决方案。治疗目标、材料/产品族、项目组合相同或高度相近的方案要合并，保留更清楚、更可执行的名称，并按时间线保留最终或最有信息量的客户反馈；不要同时输出“主方案”和“后续补充/加强版”这类重复表达。
8. seed_plan 输出到诊单级最终种草方案清单，只保留主诉之外的顺带建议、下次可做或后续维护升级方向；不要和 recommended_plan 重复。
9. sap_summary_materials 写自然业务复盘，优先输出 sections；若输入已有机构级模板段落，sections.name 必须沿用模板段落名和顺序，每个 content 写一个准确、流畅、可跟进的自然段。总结要基于已有分析证据和多录音时间线归纳，不要只改写前置字段；只引用合并后的方案名称，同一方案不要在同一段反复出现，不要把“认可程度”等字段标签写成流水账，也不要把多个编号段落挤在 summary 的同一行。冲突信息以后续录音或最终落地动作为准。

输出严格 JSON，键结构如下：
{
  "consultation_result": {
    "chief_complaint_and_indications": {"primary_demands": [], "standardized_indications": []},
    "deal_factors": {"budget": null, "concerns": [], "decision_factors": []},
    "recommended_plan": {"items": [{"plan": "", "acceptance": "未明确回应"}]},
    "seed_plan": {"items": [{"plan": "", "acceptance": "未明确回应"}]},
    "deal_outcome": {"status": "未明确", "deal_items": [], "amount": null, "loss_reasons": [], "summary": ""},
    "customer_profile_summary": {"tags": []}
  },
  "standardized_indications": {"items": []},
  "sap_summary_materials": {"summary": "", "sections": [{"name": "", "content": "", "covered_points": []}]}
}
"""


def _text_or_none(value: str | None) -> str:
    text = str(value or "").strip()
    return text or "无"


def _join_non_empty(values: list[str]) -> str:
    items = [str(value).strip() for value in values if str(value or "").strip()]
    return "；".join(items) if items else "无"


_SAP_FIELD_CONTINUATION_INDENT = " "
_SAP_ITEM_MARKERS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_SAP_BULLET_FIELD_RE = re.compile(r"^●\s*([^：:\n]+?)\s*[：:]\s*(.*)$")
_SAP_MULTILINE_FIELD_LABELS = {"顾客主诉", "顾客顾虑", "推荐方案", "种草方案", "未成交原因"}


def _strip_sap_item_separator(value: str) -> str:
    text = str(value or "").strip().strip("；;").strip()
    return re.sub(rf"^\s*(?:[{_SAP_ITEM_MARKERS}]|\d+\s*[、.．])\s*", "", text).strip()


def _is_empty_analysis_placeholder(value: str | None) -> bool:
    text = re.sub(r"\s+", "", str(value or ""))
    if not text:
        return False
    placeholder_fragments = (
        "未识别出可标准化的适应症",
        "未获取到可标准化的适应症",
        "未识别出明确适应症",
        "未识别到明确适应症",
        "未识别出顾客主诉",
        "未识别到顾客主诉",
        "未识别出明确主诉",
        "未识别到明确主诉",
        "没有识别出顾客主诉",
        "没有识别出适应症",
    )
    return any(fragment in text for fragment in placeholder_fragments)


def _sap_item_marker(index: int) -> str:
    if 1 <= index <= len(_SAP_ITEM_MARKERS):
        return _SAP_ITEM_MARKERS[index - 1]
    return f"{index}、"


def _split_top_level_sap_items(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []

    items: list[str] = []
    current: list[str] = []
    paren_depth = 0
    for char in text:
        if char in "（(":
            paren_depth += 1
        elif char in "）)" and paren_depth > 0:
            paren_depth -= 1

        if char in "；;" and paren_depth == 0:
            item = _strip_sap_item_separator("".join(current))
            if item:
                items.append(item)
            current = []
            continue
        current.append(char)

    item = _strip_sap_item_separator("".join(current))
    if item:
        items.append(item)
    return items


def _normalize_sap_field_items(values: list[str]) -> list[str]:
    return _dedupe_preserve_order(
        [
            item
            for item in (_strip_sap_item_separator(value) for value in values)
            if item and item not in {"无", "暂无", "未明确", "-"} and not _is_empty_analysis_placeholder(item)
        ]
    )


def _format_sap_multiline_field(title: str, values: list[str]) -> str:
    items = _normalize_sap_field_items(values)
    if not items:
        return f"●{title}：无"

    lines: list[str] = []
    for index, item in enumerate(items, 1):
        prefix = f"●{title}：" if index == 1 else _SAP_FIELD_CONTINUATION_INDENT
        suffix = "；" if index < len(items) else ""
        lines.append(f"{prefix}{_sap_item_marker(index)}{item}{suffix}")
    return "\n".join(lines)


def _format_sap_multiline_fields_in_text(text: str) -> str:
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip().splitlines()
    if not lines:
        return ""

    blocks: list[str] = []
    index = 0
    while index < len(lines):
        raw_line = lines[index].rstrip()
        match = _SAP_BULLET_FIELD_RE.match(raw_line.strip())
        if not match:
            if raw_line.strip():
                blocks.append(raw_line)
            index += 1
            continue

        title = match.group(1).strip()
        if title not in _SAP_MULTILINE_FIELD_LABELS:
            block_lines = [raw_line]
            next_index = index + 1
            while next_index < len(lines):
                next_line = lines[next_index].rstrip()
                if _SAP_BULLET_FIELD_RE.match(next_line.strip()):
                    break
                if next_line.strip():
                    block_lines.append(next_line)
                next_index += 1
            blocks.append("\n".join(block_lines).strip())
            index = next_index
            continue

        first_value = _strip_sap_item_separator(match.group(2))
        continuation_values: list[str] = []
        next_index = index + 1
        while next_index < len(lines):
            next_line = lines[next_index].rstrip()
            if _SAP_BULLET_FIELD_RE.match(next_line.strip()):
                break
            if next_line.strip():
                continuation_values.append(_strip_sap_item_separator(next_line))
            next_index += 1

        values = [first_value, *continuation_values] if continuation_values else _split_top_level_sap_items(first_value)
        blocks.append(_format_sap_multiline_field(title, values))
        index = next_index

    return "\n\n".join(block for block in blocks if block.strip()).strip()


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


_RECOMMENDATION_ACCEPTANCE_META_RE = re.compile(r"（认可程度：([^）]+)）")

_RECOMMENDATION_PLAN_METHOD_PATTERNS: tuple[tuple[str, str], ...] = (
    ("超声炮", "超声炮"),
    ("热玛吉", "热玛吉"),
    ("热拉提", "热拉提"),
    ("黄金微针", "黄金微针"),
    ("水光", "水光"),
    ("肉毒素", "肉毒"),
    ("肉毒", "肉毒"),
    ("除皱针", "肉毒"),
    ("瘦脸针", "肉毒"),
    ("玻尿酸", "玻尿酸"),
    ("胶原蛋白", "胶原"),
    ("胶原", "胶原"),
    ("瑞德喜", "瑞德喜"),
    ("嗨体", "嗨体"),
    ("少女针", "少女针"),
    ("童颜针", "童颜针"),
    ("线雕", "线雕"),
    ("埋线", "线雕"),
    ("光子", "光子"),
    ("皮秒", "皮秒"),
    ("深层支撑", "深层支撑"),
    ("支撑", "深层支撑"),
    ("填充", "填充"),
    ("注射", "注射"),
)

_RECOMMENDATION_PLAN_BODY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("法令纹", "法令纹"),
    ("鼻基底", "鼻基底"),
    ("面中", "面中"),
    ("苹果肌", "苹果肌"),
    ("八字纹", "八字纹"),
    ("泪沟", "泪沟"),
    ("卧蚕", "卧蚕"),
    ("眼周", "眼周"),
    ("眼下", "眼周"),
    ("鱼尾纹", "鱼尾纹"),
    ("川字纹", "川字纹"),
    ("眉间纹", "川字纹"),
    ("抬头纹", "额纹"),
    ("额纹", "额纹"),
    ("颈纹", "颈纹"),
    ("颈部", "颈部"),
    ("下巴", "下巴"),
    ("下颌", "下颌"),
    ("咬肌", "咬肌"),
    ("瘦脸", "瘦脸"),
    ("轮廓", "轮廓"),
)

_RECOMMENDATION_PLAN_EFFECT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("除皱", "除皱"),
    ("动态纹", "除皱"),
    ("提升", "提升"),
    ("提拉", "提升"),
    ("抗衰", "抗衰"),
    ("松弛", "抗衰"),
    ("紧致", "紧致"),
    ("塑形", "塑形"),
    ("补水", "补水"),
    ("淡斑", "淡斑"),
    ("祛斑", "淡斑"),
)

_RECOMMENDATION_PLAN_NOISE_WORDS: tuple[str, ...] = (
    "后续",
    "后期",
    "后面",
    "当下",
    "本次",
    "可以",
    "可",
    "建议",
    "考虑",
    "补充",
    "加强",
    "改善",
    "治疗",
    "项目",
    "方案",
    "效果",
    "方向",
    "继续",
    "进行",
    "配合",
    "联合",
    "以及",
    "或者",
    "或",
    "先",
    "再",
    "做",
)


def _recommendation_plan_key(plan: str) -> str:
    return re.sub(r"[\s，,。；;（）()【】\\[\\]：:、/+＋&]+", "", str(plan or "").strip().lower())


def _recommendation_concepts(text: str, patterns: tuple[tuple[str, str], ...]) -> list[str]:
    concepts: list[str] = []
    for pattern, concept in patterns:
        if pattern in text and concept not in concepts:
            concepts.append(concept)
    return concepts


def _split_recommendation_plan_and_acceptance(plan: str, acceptance: str | None = None) -> tuple[str, str]:
    text = str(plan or "").strip()
    explicit_acceptance = str(acceptance or "").strip()
    match = _RECOMMENDATION_ACCEPTANCE_META_RE.search(text)
    if match and not explicit_acceptance:
        explicit_acceptance = match.group(1).strip()
    text = _RECOMMENDATION_ACCEPTANCE_META_RE.sub("", text).strip()
    return text, explicit_acceptance


def _recommendation_plan_semantic_key(plan: str) -> str:
    compact = _recommendation_plan_key(_split_recommendation_plan_and_acceptance(plan)[0])
    if not compact:
        return ""

    method_tokens = _recommendation_concepts(compact, _RECOMMENDATION_PLAN_METHOD_PATTERNS)
    body_tokens = _recommendation_concepts(compact, _RECOMMENDATION_PLAN_BODY_PATTERNS)
    effect_tokens = _recommendation_concepts(compact, _RECOMMENDATION_PLAN_EFFECT_PATTERNS)
    if method_tokens:
        tokens = [f"m:{token}" for token in method_tokens]
        tokens.extend(f"b:{token}" for token in body_tokens)
        if len(method_tokens) == 1:
            tokens.extend(f"e:{token}" for token in effect_tokens)
        return "|".join(tokens)

    cleaned = compact
    for word in _RECOMMENDATION_PLAN_NOISE_WORDS:
        cleaned = cleaned.replace(word, "")
    generic_tokens = [*body_tokens, *effect_tokens]
    if generic_tokens:
        return "g:" + "|".join(generic_tokens)
    return "x:" + cleaned[:80]


def _recommendation_plan_specificity_score(plan: str) -> int:
    raw = str(plan or "").strip()
    compact = _recommendation_plan_key(raw)
    method_count = len(_recommendation_concepts(compact, _RECOMMENDATION_PLAN_METHOD_PATTERNS))
    body_count = len(_recommendation_concepts(compact, _RECOMMENDATION_PLAN_BODY_PATTERNS))
    score = method_count * 12 + body_count * 4 + min(len(compact), 30)
    if any(word in compact for word in ("后续", "后期", "后面", "补充", "加强", "可", "考虑", "建议")):
        score -= 8
    if any(marker in raw for marker in ("+", "＋", "/", "联合")):
        score += 4
    if "方案" in raw:
        score += 2
    return score


def _prefer_recommendation_plan(existing: str, incoming: str) -> str:
    if not existing:
        return incoming
    if not incoming:
        return existing
    if _recommendation_plan_specificity_score(incoming) > _recommendation_plan_specificity_score(existing):
        return incoming
    return existing


def _is_uninformative_acceptance(acceptance: str) -> bool:
    text = str(acceptance or "").strip()
    return not text or text in {"无", "暂无", "未明确", "未明确回应", "-", "null", "None"}


def _merge_recommendation_acceptance(existing: str, incoming: str) -> str:
    current = str(existing or "").strip()
    new_value = str(incoming or "").strip()
    if _is_uninformative_acceptance(new_value):
        return current or "未明确回应"
    return new_value


def _merge_recommendation_name_values(values: list[str]) -> list[str]:
    by_key: dict[str, str] = {}
    order: list[str] = []
    for value in values:
        plan, _ = _split_recommendation_plan_and_acceptance(value)
        plan = plan.strip()
        if not plan:
            continue
        key = _recommendation_plan_semantic_key(plan)
        if not key:
            continue
        if key not in by_key:
            order.append(key)
            by_key[key] = plan
        else:
            by_key[key] = _prefer_recommendation_plan(by_key[key], plan)
    return [by_key[key] for key in order if by_key.get(key)]


def _merge_recommendation_display_items(values: list[str]) -> list[str]:
    by_key: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for value in values:
        plan, acceptance = _split_recommendation_plan_and_acceptance(value)
        if not plan:
            continue
        key = _recommendation_plan_semantic_key(plan)
        if not key:
            continue
        if key not in by_key:
            order.append(key)
            by_key[key] = {
                "plan": plan,
                "acceptance": str(acceptance or "").strip(),
            }
            continue
        by_key[key]["plan"] = _prefer_recommendation_plan(by_key[key]["plan"], plan)
        by_key[key]["acceptance"] = _merge_recommendation_acceptance(by_key[key].get("acceptance", ""), acceptance)

    merged: list[str] = []
    for key in order:
        item = by_key.get(key) or {}
        plan = item.get("plan", "")
        if not plan:
            continue
        acceptance = item.get("acceptance", "")
        merged.append(f"{plan}（认可程度：{acceptance}）" if acceptance else plan)
    return merged


def _format_numbered_block(title: str, items: list[str]) -> list[str]:
    if not items:
        return [f"●{title}：无"]
    lines = [f"●{title}："]
    for index, item in enumerate(items, 1):
        lines.append(f"{index}、{item}")
    return lines


def _collect_primary_demand_items(result: dict) -> list[str]:
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    chief = consultation_result.get("chief_complaint_and_indications", {})
    if isinstance(chief, dict):
        primary_demands = chief.get("primary_demands")
        if isinstance(primary_demands, list):
            values = _dedupe_preserve_order(
                [
                    str(item or "").strip()
                    for item in primary_demands
                    if str(item or "").strip() and not _is_empty_analysis_placeholder(str(item or ""))
                ]
            )
            if values:
                return values
        chief_summary = str(chief.get("summary") or "").strip()
        if chief_summary and not _is_empty_analysis_placeholder(chief_summary):
            return _dedupe_preserve_order(chief_summary.replace("；", "\n").splitlines())

    cpd = result.get("customer_primary_demands", {})
    if isinstance(cpd, dict):
        items = cpd.get("items", [])
        if isinstance(items, list):
            values = [
                str((item or {}).get("demand") or "").strip()
                for item in items
                if isinstance(item, dict)
                and str((item or {}).get("demand") or "").strip()
                and not _is_empty_analysis_placeholder(str((item or {}).get("demand") or ""))
            ]
            values = _dedupe_preserve_order(values)
            if values:
                return values
        cpd_summary = str(cpd.get("summary") or "").strip()
        if cpd_summary and not _is_empty_analysis_placeholder(cpd_summary):
            return _dedupe_preserve_order(cpd_summary.replace("；", "\n").splitlines())
    return []


def _collect_primary_demand_text(result: dict) -> str:
    items = _collect_primary_demand_items(result)
    return "；".join(items) if items else ""


def _collect_indication_items(result: dict) -> list[str]:
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    chief = consultation_result.get("chief_complaint_and_indications", {})
    if isinstance(chief, dict):
        items = chief.get("standardized_indications")
        if isinstance(items, list):
            values = _dedupe_preserve_order([str(item or "").strip() for item in items])
            if values:
                return values

    standardized = result.get("standardized_indications", {})
    if isinstance(standardized, dict):
        items = standardized.get("items")
        if isinstance(items, list):
            values: list[str] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                department_name = str(item.get("department_name") or "").strip()
                department_code = str(item.get("department_code") or "").strip()
                indication_name = str(item.get("indication_name") or "").strip()
                indication_code = str(item.get("indication_code") or "").strip()
                body_part_name = str(item.get("body_part_name") or "").strip()
                body_part_code = str(item.get("body_part_code") or "").strip()
                parts = []
                if department_name or department_code:
                    parts.append(f"{department_name or '未明确科室'}（{department_code or '无编码'}）")
                if indication_name or indication_code:
                    parts.append(f"{indication_name or '未明确适应症'}（{indication_code or '无编码'}）")
                if body_part_name or body_part_code:
                    parts.append(f"{body_part_name or '未明确部位'}（{body_part_code or '无编码'}）")
                text = "｜".join(parts).strip("｜")
                if text:
                    values.append(text)
            values = _dedupe_preserve_order(values)
            if values:
                return values
    return []


def _collect_budget_text(result: dict) -> str:
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    deal_factors = consultation_result.get("deal_factors", {})
    if isinstance(deal_factors, dict) and str(deal_factors.get("budget") or "").strip():
        return str(deal_factors.get("budget") or "").strip()

    consumption_intent = result.get("consumption_intent", {})
    if isinstance(consumption_intent, dict) and str(consumption_intent.get("budget") or "").strip():
        return str(consumption_intent.get("budget") or "").strip()
    return ""


def _collect_concern_items(result: dict) -> list[str]:
    values: list[str] = []
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    deal_factors = consultation_result.get("deal_factors", {})
    if isinstance(deal_factors, dict):
        for text in deal_factors.get("concerns", []) or []:
            desc = str(text or "").strip()
            if not desc:
                continue
            values.append(desc)

    cc = result.get("customer_concerns", {})
    if isinstance(cc, dict):
        for item in cc.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            desc = str(item.get("content") or "").strip()
            if not desc:
                continue
            values.append(desc)
    return _dedupe_preserve_order(values)


def _collect_recommendation_text(result: dict) -> str:
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    recommended_plan = consultation_result.get("recommended_plan", {})
    if isinstance(recommended_plan, dict):
        if str(recommended_plan.get("summary") or "").strip():
            return str(recommended_plan.get("summary") or "").strip()
        items = recommended_plan.get("items", [])
        if isinstance(items, list):
            values = [
                _format_recommendation_plan_for_sap(item, "plan", "recommendation", "content")
                for item in items
                if isinstance(item, dict)
            ]
            values = [value for value in values if value]
            if values:
                return "；".join(values)

    sr = result.get("staff_recommendations", {})
    if isinstance(sr, dict):
        if str(sr.get("summary") or "").strip():
            return str(sr.get("summary") or "").strip()
        items = sr.get("items", [])
        if isinstance(items, list):
            values = [
                _format_recommendation_plan_for_sap(item, "recommendation", "product_or_solution")
                for item in items
                if isinstance(item, dict)
            ]
            values = [value for value in values if value]
            if values:
                return "；".join(values)
    return ""


_RECOMMENDATION_DETAIL_FIELDS: tuple[tuple[str, str], ...] = (
    ("brand", "品牌"),
    ("material", "材料"),
    ("dosage", "用量"),
    ("price", "报价"),
    ("course_or_frequency", "疗程"),
    ("treatment_steps", "步骤"),
    ("implementation_notes", "要点"),
)


def _recommendation_detail_value(value: object) -> str:
    if isinstance(value, list):
        return "；".join(str(item or "").strip() for item in value if str(item or "").strip())
    if isinstance(value, tuple):
        return "；".join(str(item or "").strip() for item in value if str(item or "").strip())
    return str(value or "").strip()


def _format_recommendation_plan_for_sap(item: dict, *keys: str) -> str:
    plan = next((str(item.get(key) or "").strip() for key in keys if str(item.get(key) or "").strip()), "")
    if not plan:
        return ""
    compact_plan = re.sub(r"\s+", "", plan)
    details: list[str] = []
    seen_values: set[str] = set()
    for field, label in _RECOMMENDATION_DETAIL_FIELDS:
        value = _recommendation_detail_value(item.get(field))
        compact_value = re.sub(r"\s+", "", value)
        if not compact_value or compact_value in compact_plan or compact_value in seen_values:
            continue
        seen_values.add(compact_value)
        details.append(f"{label}：{value}")
    if not details:
        return plan
    return f"{plan}（{'；'.join(details)}）"


def _collect_recommendation_items(result: dict) -> list[str]:
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    recommended_plan = consultation_result.get("recommended_plan", {})
    if isinstance(recommended_plan, dict):
        items = recommended_plan.get("items")
        if isinstance(items, list):
            values: list[str] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                plan, acceptance = _split_recommendation_plan_and_acceptance(
                    _format_recommendation_plan_for_sap(item, "plan", "recommendation", "content"),
                    str(item.get("acceptance") or "").strip(),
                )
                if not plan:
                    continue
                if acceptance:
                    values.append(f"{plan}（认可程度：{acceptance}）")
                else:
                    values.append(plan)
            values = _merge_recommendation_display_items(values)
            if values:
                return values

    deal_items = _collect_deal_items(result)
    if deal_items:
        return deal_items

    sr = result.get("staff_recommendations", {})
    if isinstance(sr, dict):
        items = sr.get("items")
        if isinstance(items, list):
            values = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                plan, response = _split_recommendation_plan_and_acceptance(
                    _format_recommendation_plan_for_sap(item, "recommendation", "product_or_solution"),
                    str(item.get("customer_response") or "").strip(),
                )
                if not plan:
                    continue
                if response:
                    values.append(f"{plan}（认可程度：{response}）")
                else:
                    values.append(plan)
            values = _merge_recommendation_display_items(values)
            if values:
                return values
    return []


def _collect_seed_recommendation_items(result: dict) -> list[str]:
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    seed_plan = consultation_result.get("seed_plan", {})
    if isinstance(seed_plan, dict):
        items = seed_plan.get("items")
        if isinstance(items, list):
            values: list[str] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                plan, acceptance = _split_recommendation_plan_and_acceptance(
                    _format_recommendation_plan_for_sap(item, "plan", "recommendation", "content"),
                    str(item.get("acceptance") or "").strip(),
                )
                if not plan:
                    continue
                if acceptance:
                    values.append(f"{plan}（认可程度：{acceptance}）")
                else:
                    values.append(plan)
            values = _merge_recommendation_display_items(values)
            if values:
                return values

    seed_recommendations = result.get("staff_seed_recommendations", {})
    if isinstance(seed_recommendations, dict):
        items = seed_recommendations.get("items")
        if isinstance(items, list):
            values = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                plan, response = _split_recommendation_plan_and_acceptance(
                    _format_recommendation_plan_for_sap(item, "recommendation", "product_or_solution"),
                    str(item.get("customer_response") or "").strip(),
                )
                if not plan:
                    continue
                if response:
                    values.append(f"{plan}（认可程度：{response}）")
                else:
                    values.append(plan)
            values = _merge_recommendation_display_items(values)
            if values:
                return values
    return []


def _transcript_text_for_price_quotes(
    transcript_full_text: str | None,
    transcript_utterances: list[dict] | None,
) -> str:
    if transcript_utterances:
        parts = [
            str(item.get("text") or "").strip()
            for item in transcript_utterances
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        if parts:
            return " ".join(parts)
    return str(transcript_full_text or "").strip()


def _collect_transcript_price_quote_recommendation_items(
    transcript_full_text: str | None,
    transcript_utterances: list[dict] | None,
) -> list[str]:
    text = _transcript_text_for_price_quotes(transcript_full_text, transcript_utterances)
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return []

    has_price = bool(re.search(r"(?:\d{4,6}|[一二三四五六七八九十两]+万|十几万)", compact))
    has_breast_implant_context = any(
        keyword in compact
        for keyword in (
            "隆胸",
            "丰胸",
            "胸假体",
            "乳房假体",
            "假体",
            "水滴型",
            "圆形",
            "宝俪",
            "保利",
            "爱思美",
            "艾思美",
            "优思利",
            "优思丽",
            "欧若拉",
            "傲诺拉",
            "母提瓦",
            "魔滴",
            "Motiva",
            "星钻",
        )
    )
    if not (has_price and has_breast_implant_context):
        return []

    quotes: list[str] = []

    def add_quote(value: str) -> None:
        text_value = value.strip("，,；;。 ")
        if text_value and text_value not in quotes:
            quotes.append(text_value)

    if re.search(r"(?:保利|宝俪|宝丽).{0,10}水滴型.{0,30}(?:69800|七万)", compact):
        add_quote("宝俪/保利水滴型约69800元")
    if re.search(r"(?:保利|宝俪|宝丽).{0,45}圆形.{0,30}(?:46800|47000|4万7|四万七)", compact):
        add_quote("宝俪/保利圆形约46800元")
    elif re.search(r"圆形.{0,30}(?:46800|47000|4万7|四万七)", compact):
        add_quote("圆形假体约46800元")

    if re.search(r"(?:爱思美|艾思美).{0,12}(?:5万|五万|50000)", compact):
        add_quote("爱思美/艾思美约5万元")
    if re.search(r"(?:优思利|优思丽).{0,30}(?:保利|宝俪|宝丽).{0,20}(?:价格是一样|价格一样)", compact):
        add_quote("优思利/优思丽圆形与宝俪/保利圆形价格相同")
    if re.search(r"(?:欧若拉|傲诺拉).{0,25}(?:69800|七万)", compact):
        add_quote("欧若拉/傲诺拉约69800元")
    if re.search(r"(?:母提瓦|魔滴|Motiva).{0,16}(?:12万8|十二万八|128000)", compact, flags=re.IGNORECASE):
        add_quote("母提瓦/Motiva魔滴约12.8万元")
    elif re.search(r"(?:母提瓦|魔滴|Motiva).{0,16}(?:12万|十二万|120000)", compact, flags=re.IGNORECASE):
        add_quote("母提瓦/Motiva魔滴约12万元")
    if re.search(r"星钻.{0,12}(?:14万|十四万|140000)", compact):
        add_quote("星钻约14万元")

    if not quotes:
        return []
    return [f"胸假体/隆胸方案报价：{'；'.join(quotes)}"]


def _collect_profile_tag_items(result: dict) -> list[str]:
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    profile = consultation_result.get("customer_profile_summary", {})
    tags = profile.get("tags") if isinstance(profile, dict) else None
    if not isinstance(tags, list):
        return []

    normalized: list[str] = []
    seen_category: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    has_no_history = False
    for item in tags:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip()
        value = str(item.get("value") or "").strip()
        if not category or not value:
            continue
        if value in {"无", "未明确", "暂无", "-"}:
            continue
        if category == "治疗项目" and value == "无医美史":
            has_no_history = True
        pair = (category, value)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        normalized.append(f"{category}：{value}")

    if has_no_history:
        normalized = [item for item in normalized if item != "治疗项目：第一次做医美"]

    deduped: list[str] = []
    for item in normalized:
        category = item.split("：", 1)[0]
        if category in seen_category and category not in {"治疗项目", "健康风险/禁忌"}:
            continue
        seen_category.add(category)
        deduped.append(item)
    return deduped


def _collect_profile_tag_pairs(result: dict) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    sources = []
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    profile_summary = consultation_result.get("customer_profile_summary", {})
    if isinstance(profile_summary, dict):
        sources.append(profile_summary.get("tags"))
    customer_profile = result.get("customer_profile", {})
    if isinstance(customer_profile, dict):
        sources.append(customer_profile.get("tags"))

    seen: set[tuple[str, str]] = set()
    for tags in sources:
        if not isinstance(tags, list):
            continue
        for item in tags:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category") or "").strip()
            value = str(item.get("value") or "").strip()
            if not category or not value or value in {"无", "未明确", "暂无", "-"}:
                continue
            pair = (category, value)
            if pair in seen:
                continue
            seen.add(pair)
            pairs.append(pair)
    return pairs


def _profile_values_by_category(result: dict) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for category, value in _collect_profile_tag_pairs(result):
        values.setdefault(category, []).append(value)
    return {key: _dedupe_preserve_order(items) for key, items in values.items()}


def _collect_profile_tag_values(result: dict) -> list[str]:
    values: list[str] = []
    for item in _collect_profile_tag_items(result):
        if "：" not in item:
            continue
        _, value = item.split("：", 1)
        text = value.strip()
        if text:
            values.append(text)
    return _dedupe_preserve_order(values)


def _collect_customer_age_text(result: dict, transcript_signals: dict[str, str | bool]) -> str:
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    profile_summary = consultation_result.get("customer_profile_summary", {})
    age = str((profile_summary or {}).get("age") or "").strip() if isinstance(profile_summary, dict) else ""
    customer_profile = result.get("customer_profile", {})
    if not age and isinstance(customer_profile, dict):
        age = str(customer_profile.get("age") or "").strip()
    if not age:
        age = str(transcript_signals.get("age") or "").strip()
    if not age:
        return ""
    return age if "岁" in age else f"{age}岁"


def _collect_expectation_phrases(result: dict) -> list[str]:
    customer_demands = result.get("customer_demands", {})
    if not isinstance(customer_demands, dict):
        return []
    expectation = customer_demands.get("expectation", {})
    phrases: list[str] = []
    if isinstance(expectation, dict):
        for key in ("specific_standards", "exit_state"):
            value = str(expectation.get(key) or "").strip()
            if value and value not in {"无", "未明确", "本段未涉及"}:
                phrases.append(value)
    product_preference = customer_demands.get("product_preference", {})
    if isinstance(product_preference, dict):
        influence = str(product_preference.get("consultant_influence") or "").strip()
        if influence and influence not in {"无", "未明确", "本段未涉及"}:
            phrases.append(influence)
        factors = [
            str(item or "").strip()
            for item in product_preference.get("comparison_factors", []) or []
            if str(item or "").strip()
        ]
        if factors:
            phrases.append(f"比较因素包括{'、'.join(_dedupe_preserve_order(factors)[:3])}")
    return _dedupe_preserve_order(phrases)


def _collect_recommendation_acceptance_items(result: dict) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    recommended_plan = consultation_result.get("recommended_plan", {})
    if isinstance(recommended_plan, dict):
        for item in recommended_plan.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            plan, acceptance = _split_recommendation_plan_and_acceptance(
                _format_recommendation_plan_for_sap(item, "plan", "recommendation", "content"),
                str(item.get("acceptance") or "").strip(),
            )
            if plan:
                items.append((plan, acceptance or "未明确回应"))

    staff_recommendations = result.get("staff_recommendations", {})
    if isinstance(staff_recommendations, dict):
        for item in staff_recommendations.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            plan, response = _split_recommendation_plan_and_acceptance(
                _format_recommendation_plan_for_sap(item, "recommendation", "product_or_solution"),
                str(item.get("customer_response") or "").strip(),
            )
            if plan:
                items.append((plan, response or "未明确回应"))

    by_key: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for plan, acceptance in items:
        key = _recommendation_plan_semantic_key(plan)
        if not key:
            continue
        if key not in by_key:
            order.append(key)
            by_key[key] = {"plan": plan, "acceptance": acceptance}
            continue
        by_key[key]["plan"] = _prefer_recommendation_plan(by_key[key]["plan"], plan)
        by_key[key]["acceptance"] = _merge_recommendation_acceptance(by_key[key]["acceptance"], acceptance)
    return [
        (by_key[key]["plan"], by_key[key]["acceptance"] or "未明确回应")
        for key in order
        if by_key.get(key, {}).get("plan")
    ]


def _collect_seed_recommendation_acceptance_items(result: dict) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    seed_plan = consultation_result.get("seed_plan", {})
    if isinstance(seed_plan, dict):
        for item in seed_plan.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            plan, acceptance = _split_recommendation_plan_and_acceptance(
                _format_recommendation_plan_for_sap(item, "plan", "recommendation", "content"),
                str(item.get("acceptance") or "").strip(),
            )
            if plan:
                items.append((plan, acceptance or "未明确回应"))

    staff_seed_recommendations = result.get("staff_seed_recommendations", {})
    if isinstance(staff_seed_recommendations, dict):
        for item in staff_seed_recommendations.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            plan, response = _split_recommendation_plan_and_acceptance(
                _format_recommendation_plan_for_sap(item, "recommendation", "product_or_solution"),
                str(item.get("customer_response") or "").strip(),
            )
            if plan:
                items.append((plan, response or "未明确回应"))

    by_key: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for plan, acceptance in items:
        key = _recommendation_plan_semantic_key(plan)
        if not key:
            continue
        if key not in by_key:
            order.append(key)
            by_key[key] = {"plan": plan, "acceptance": acceptance}
            continue
        by_key[key]["plan"] = _prefer_recommendation_plan(by_key[key]["plan"], plan)
        by_key[key]["acceptance"] = _merge_recommendation_acceptance(by_key[key]["acceptance"], acceptance)
    return [
        (by_key[key]["plan"], by_key[key]["acceptance"] or "未明确回应")
        for key in order
        if by_key.get(key, {}).get("plan")
    ]


def _collect_deal_outcome(result: dict) -> dict:
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    outcome = consultation_result.get("deal_outcome", {})
    return outcome if isinstance(outcome, dict) else {}


def _compact_sentence(prefix: str, values: list[str], fallback: str = "") -> str:
    items = _dedupe_preserve_order(values)
    if items:
        return f"{prefix}{'，'.join(items)}。"
    return fallback


def _natural_join(values: list[str]) -> str:
    return "、".join(_dedupe_preserve_order(values))


def _clean_summary_value(value: str) -> str:
    return str(value or "").strip().rstrip("。；;，,")


def _sentence_or_empty(text: str) -> str:
    cleaned = _clean_summary_value(text)
    return f"{cleaned}。" if cleaned else ""


def _collect_deal_items(result: dict) -> list[str]:
    outcome = _collect_deal_outcome(result)
    return _dedupe_preserve_order(
        [
            str(item or "").strip()
            for item in outcome.get("deal_items", []) or []
            if str(item or "").strip() not in {"无", "暂无", "未明确", "-"}
        ]
    )


def _collect_deal_amount(result: dict) -> str:
    amount = str(_collect_deal_outcome(result).get("amount") or "").strip()
    return "" if amount in {"无", "暂无", "未明确", "-", "null", "None"} else amount


def _summary_paragraph(index: int, title: str, sentences: list[str], fallback: str) -> str:
    parts = [_sentence_or_empty(sentence) for sentence in sentences if _clean_summary_value(sentence)]
    if not parts:
        parts = [_sentence_or_empty(fallback)]
    return f"{index}、{title}：{''.join(parts)}"


_SAP_SUMMARY_INLINE_POINT_RE = re.compile(r"(^|[\s。；;])([1-9]\d{0,1}[、.．]\s*[^：:\n]{2,24}[：:])")


def _split_inline_sap_summary_points(text: str) -> str:
    matches = list(_SAP_SUMMARY_INLINE_POINT_RE.finditer(text))
    if len(matches) <= 1:
        return text

    parts: list[str] = []
    for index, match in enumerate(matches):
        start = match.start(2)
        end = matches[index + 1].start(2) if index + 1 < len(matches) else len(text)
        part = text[start:end].strip()
        if part:
            parts.append(part)
    return "\n".join(parts) if parts else text


def _format_sap_summary_text(text: str | None) -> str:
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"^\s*●?\s*总结信息\s*[：:]\s*", "", cleaned)

    lines: list[str] = []
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lines.extend(part.strip() for part in _split_inline_sap_summary_points(line).splitlines() if part.strip())
    return "\n".join(lines).strip()


_SAP_SUMMARY_SECTION_ORDER = (
    "客户基础信息",
    "需求与动机分析",
    "面诊与设计方案",
    "报价与成交策略",
    "客户画像与标签",
    "后续跟进规划",
    "老带新提及",
)


@dataclass(frozen=True)
class SapSummaryTemplateSection:
    index: int
    title: str
    guidance: str


@dataclass(frozen=True)
class SapSummaryTemplateConfig:
    hospital_code: str
    tenant_name: str
    template_name: str
    template_version: str
    template: str
    prompt: str
    sections: tuple[SapSummaryTemplateSection, ...]
    enabled: bool = True


_SAP_SUMMARY_TEMPLATE_LINE_RE = re.compile(r"^\s*(\d+)\s*[、.．]\s*([^：:\n]+?)\s*[：:]\s*(.*?)\s*$")


def _parse_sap_summary_template_sections(template: str | None) -> tuple[SapSummaryTemplateSection, ...]:
    sections: list[SapSummaryTemplateSection] = []
    for line in str(template or "").splitlines():
        match = _SAP_SUMMARY_TEMPLATE_LINE_RE.match(line)
        if not match:
            continue
        title = _clean_summary_value(match.group(2))
        if not title:
            continue
        sections.append(
            SapSummaryTemplateSection(
                index=int(match.group(1)),
                title=title,
                guidance=_clean_summary_value(match.group(3)),
            )
        )
    return tuple(sorted(sections, key=lambda item: item.index))


def _configured_sap_summary_disabled_hospital_codes() -> set[str]:
    raw = get_settings().sap_rfc_summary_disabled_hospital_codes
    return {code for code in re.split(r"[,;\s]+", str(raw or "")) if code}


def _is_sap_summary_section_enabled(
    visit_order: VisitOrder | None,
    sap_summary_config: SapSummaryTemplateConfig | None,
) -> bool:
    if sap_summary_config is not None:
        return sap_summary_config.enabled
    hospital_code = str(getattr(sap_summary_config, "hospital_code", "") or "").strip()
    if not hospital_code and visit_order is not None:
        hospital_code = str(getattr(visit_order, "jgbm", "") or "").strip()
    if not hospital_code:
        return True
    return hospital_code not in _configured_sap_summary_disabled_hospital_codes()


def _strip_sap_summary_section(text: str) -> str:
    lines: list[str] = []
    skipping_summary = False
    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        stripped = raw_line.strip()
        if re.match(r"^●\s*总结信息\s*[：:]", stripped):
            skipping_summary = True
            continue
        if skipping_summary and re.match(r"^●\s*[^：:\n]+?\s*[：:]", stripped):
            skipping_summary = False
        if not skipping_summary:
            lines.append(raw_line.rstrip())
    return "\n".join(lines).strip()


async def _load_sap_summary_template_config(
    db: AsyncSession,
    hospital_code: str | None,
) -> SapSummaryTemplateConfig | None:
    code = str(hospital_code or "").strip()
    if not code:
        return None
    tenant = (
        await db.execute(
            select(WecomTenant)
            .where(
                WecomTenant.default_hospital_code == code,
                WecomTenant.is_active.is_(True),
            )
            .order_by(WecomTenant.is_default.desc(), WecomTenant.updated_at.desc())
        )
    ).scalars().first()
    if tenant is None:
        return None

    template = _clean_summary_value(getattr(tenant, "sap_summary_template", None))
    prompt = _clean_summary_value(getattr(tenant, "sap_summary_prompt", None))
    summary_enabled = bool(getattr(tenant, "sap_summary_enabled", True))
    sections = _parse_sap_summary_template_sections(template)
    if not summary_enabled:
        return SapSummaryTemplateConfig(
            hospital_code=code,
            tenant_name=_clean_summary_value(getattr(tenant, "name", None)),
            template_name=_clean_summary_value(getattr(tenant, "sap_summary_template_name", None)),
            template_version=_clean_summary_value(getattr(tenant, "sap_summary_template_version", None)),
            template=template,
            prompt=prompt,
            sections=sections,
            enabled=False,
        )
    if not sections:
        return None
    return SapSummaryTemplateConfig(
        hospital_code=code,
        tenant_name=_clean_summary_value(getattr(tenant, "name", None)),
        template_name=_clean_summary_value(getattr(tenant, "sap_summary_template_name", None)),
        template_version=_clean_summary_value(getattr(tenant, "sap_summary_template_version", None)),
        template=template,
        prompt=prompt,
        sections=sections,
        enabled=True,
    )


def _clean_model_summary_section_content(section_name: str, content: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(content or "")).strip()
    if not cleaned or cleaned in {"无", "暂无", "未明确", "未提及", "-"}:
        return ""
    cleaned = re.sub(
        rf"^(?:\d+[、.．]\s*)?{re.escape(section_name)}\s*[：:]\s*",
        "",
        cleaned,
    ).strip()
    return _clean_summary_value(cleaned)


def _collect_model_sap_summary_text_for_sections(
    result: dict,
    section_names: tuple[str, ...] | list[str],
    *,
    require_all: bool = True,
) -> str:
    payload = result.get("sap_summary_materials")
    if not isinstance(payload, dict):
        return ""

    lines: list[str] = []
    by_name: dict[str, str] = {}
    sections = payload.get("sections")
    if isinstance(sections, list):
        for item in sections:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("title") or item.get("section") or "").strip()
            if not name or name in by_name:
                continue
            content = _clean_model_summary_section_content(name, str(item.get("content") or item.get("summary") or ""))
            if not content:
                continue
            by_name[name] = content

    normalized_section_names = tuple(_clean_summary_value(name) for name in section_names if _clean_summary_value(name))
    if not normalized_section_names:
        return ""
    if require_all and not all(section_name in by_name for section_name in normalized_section_names):
        return ""

    for index, section_name in enumerate(normalized_section_names, 1):
        content = by_name.get(section_name, "")
        if content:
            lines.append(f"{index}、{section_name}：{_sentence_or_empty(content)}")

    if lines:
        return "\n".join(lines)

    summary = _clean_summary_value(str(payload.get("summary") or ""))
    return _sentence_or_empty(summary) if summary else ""


def _collect_model_sap_summary_text(result: dict) -> str:
    return _collect_model_sap_summary_text_for_sections(result, _SAP_SUMMARY_SECTION_ORDER, require_all=False)


def _safe_attr_text(obj: object | None, attr: str) -> str:
    return str(getattr(obj, attr, "") or "").strip() if obj is not None else ""


def _collect_visit_customer_type_text(visit_order: VisitOrder | None) -> str:
    text = _safe_attr_text(visit_order, "kut30_dq_txt") or _safe_attr_text(visit_order, "khlx_t30")
    code = _safe_attr_text(visit_order, "kut30_dq") or _safe_attr_text(visit_order, "khlx_t30")
    if text:
        return text
    if code == "Q":
        return "新客"
    if code == "V":
        return "老客"
    if code == "Q":
        return "新客"
    if code == "V":
        return "老客"
    return ""


def _collect_visit_staff_text(visit_order: VisitOrder | None, advisor_name: str | None) -> str:
    advisor = str(advisor_name or "").strip() or _safe_attr_text(visit_order, "advxc_long") or _safe_attr_text(visit_order, "fzuer_long")
    doctor = _safe_attr_text(visit_order, "advyq_name") or _safe_attr_text(visit_order, "yyuer")
    parts: list[str] = []
    if advisor:
        parts.append(f"接诊/咨询人员为{advisor}")
    if doctor:
        parts.append(f"面诊医生为{doctor}")
    return "，".join(parts)


def _all_transcript_text(transcript_utterances: list[dict] | None, transcript_full_text: str | None) -> str:
    texts = [
        str(item.get("text") or "").strip()
        for item in transcript_utterances or []
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    if texts:
        return "\n".join(texts)
    return str(transcript_full_text or "").strip()


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _collect_transcript_strategy_cues(all_transcript_text: str) -> dict[str, bool]:
    compact = re.sub(r"\s+", "", all_transcript_text)
    return {
        "price_strategy": bool(
            _has_any(compact, ("优惠", "活动", "套餐", "组合", "分期", "免息", "特价", "定金", "今天订", "划算"))
        ),
        "trust": bool(_has_any(compact, ("医生资质", "院长", "案例", "认证", "资质", "朋友推荐", "熟人推荐", "口碑", "经验"))),
        "praise": bool(_has_any(compact, ("漂亮", "好看", "基础很好", "很适合", "很精致", "状态很好"))),
        "referral_open": bool(_has_any(compact, ("老带新", "转介绍", "推荐朋友", "介绍朋友", "带朋友", "身边朋友"))),
        "referral_policy": bool(_has_any(compact, ("老带新福利", "新客福利", "老客奖励", "米米", "积分", "奖励", "两人福利", "2人福利"))),
        "phone_capture": bool(_has_any(compact, ("电话", "手机号", "手机号码", "留个号码", "留电话", "套电"))),
        "competitor": bool(_has_any(compact, ("别家", "其他医院", "其他机构", "对比机构", "面诊过其他", "竞品", "竞对"))),
        "immediate_treatment": bool(_has_any(compact, ("今天做", "当日做", "现在做", "马上做", "立即治疗", "现场做"))),
    }


def _format_expectation_summary(text: str) -> str:
    cleaned = _clean_summary_value(text)
    if not cleaned:
        return ""
    if cleaned.startswith(("希望", "想", "期望", "接受", "认可", "担心", "关注", "需要", "比较")):
        return f"客户{cleaned}"
    return f"客户期望{cleaned}"


def _is_followable_acceptance(acceptance: str) -> bool:
    text = str(acceptance or "").strip()
    if not text:
        return False
    if any(keyword in text for keyword in ("拒绝", "不接受", "未接受", "暂未接受")):
        return False
    return any(keyword in text for keyword in ("接受", "犹豫", "兴趣", "可以", "认可", "心动"))


def _strip_recommendation_meta(text: str) -> str:
    return _RECOMMENDATION_ACCEPTANCE_META_RE.sub("", str(text or "")).strip()


def _collect_consultant_evaluation_text(result: dict) -> str:
    process_evaluation = result.get("consultation_process_evaluation", {}) if isinstance(result.get("consultation_process_evaluation"), dict) else {}
    process_summary = str(process_evaluation.get("overall_summary") or "").strip()
    if process_summary:
        return process_summary

    evaluation = result.get("consultation_evaluation", {}) if isinstance(result.get("consultation_evaluation"), dict) else {}
    evaluation_summary = str(evaluation.get("overall_summary") or "").strip()
    if evaluation_summary:
        return evaluation_summary

    dimension_summaries = [
        str(item.get("summary") or "").strip()
        for item in (evaluation.get("dimensions") or [])
        if isinstance(item, dict) and str(item.get("summary") or "").strip()
    ]
    if dimension_summaries:
        return "；".join(dimension_summaries)
    return ""


def _classify_concern(concern_type: str, content: str) -> str:
    """将顾虑归入四类之一：效果类、价格类、对比机构类、其他。"""
    text = f"{concern_type} {content}".lower()
    if any(kw in text for kw in ("效果", "反弹", "自然", "恢复", "疼", "痛", "安全", "失败", "风险", "不明显")):
        return "效果类"
    if any(kw in text for kw in ("价格", "费用", "贵", "钱", "预算", "优惠", "便宜", "划算")):
        return "价格类"
    if any(kw in text for kw in ("机构", "医院", "别家", "对比", "其他地方", "朋友推荐", "竞争")):
        return "对比机构类"
    return "其他"


def _collect_loss_reason_items(result: dict) -> list[str]:
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    outcome = consultation_result.get("deal_outcome", {})
    if not isinstance(outcome, dict):
        return []
    return _dedupe_preserve_order([str(item or "").strip() for item in outcome.get("loss_reasons", []) or []])


def _is_visit_order_final_not_deal(visit_order: VisitOrder | None) -> bool:
    if visit_order is None:
        return False
    status_code = str(visit_order.jcsta or "").strip()
    status_text = str(visit_order.jcsta_txt or "").strip()
    if status_code in {"Y", "Z"} or status_text in {"已成交", "已治疗"}:
        return False
    if str(visit_order.jcsta or "").strip() == "N":
        return True
    if status_text == "未成交":
        return True
    return not status_code and not status_text


def _resolve_effective_deal_status(visit_order: VisitOrder | None, analysis_status: str) -> str:
    status = str(analysis_status or "").strip() or "未明确"
    if visit_order is None:
        return status
    status_code = str(visit_order.jcsta or "").strip()
    status_text = str(visit_order.jcsta_txt or "").strip()
    if status_code in {"Y", "Z"} or status_text in {"已成交", "已治疗"}:
        return "已成交"
    if status_code == "N" or status_text == "未成交":
        return "未成交"
    return status


def _customer_utterance_texts(transcript_utterances: list[dict] | None, transcript_full_text: str | None) -> list[str]:
    utterance_texts: list[str] = []
    for item in transcript_utterances or []:
        if not isinstance(item, dict):
            continue
        role_candidates = [
            str(item.get("speaker_role") or "").strip().lower(),
            str(item.get("speaker") or "").strip().lower(),
            str(item.get("speaker_business_role") or "").strip().lower(),
        ]
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if any(role in {"customer", "visitor", "guest", "patient"} for role in role_candidates if role):
            utterance_texts.append(text)
    if utterance_texts:
        return utterance_texts
    if transcript_full_text:
        return [line.strip() for line in str(transcript_full_text).splitlines() if line.strip()]
    return []


def _extract_transcript_customer_clues(
    transcript_utterances: list[dict] | None,
    transcript_full_text: str | None,
) -> list[str]:
    texts = _customer_utterance_texts(transcript_utterances, transcript_full_text)
    if not texts:
        return []
    joined = "\n".join(texts)
    clues: list[str] = []

    age = _extract_direct_customer_age(texts)
    if age:
        clues.append(f"客户自述年龄约{age}岁")

    if re.search(r"(第一次|没做过|没有做过).{0,8}(医美|抗衰|项目|保养)", joined):
        clues.append("客户明确表示此前未做过相关医美项目")

    if "单身" in joined:
        clues.append("客户当前为单身状态")
    elif "已婚" in joined:
        clues.append("客户当前为已婚状态")

    if "微信" in joined and re.search(r"(加我微信|加微信|微信联系)", joined):
        clues.append("客户接受后续通过微信保持联系")

    if re.search(r"(上班|工作).{0,8}(恢复|肿|休息)", joined) or re.search(r"(恢复|肿|休息).{0,8}(上班|工作)", joined):
        clues.append("客户关注治疗恢复期对工作安排的影响")

    if re.search(r"(怕痛|疼|痛感|疼痛)", joined):
        clues.append("客户对治疗疼痛和耐受度较为敏感")

    return _dedupe_preserve_order(clues)


def _extract_direct_customer_age(texts: list[str]) -> str:
    joined = "\n".join(str(text or "").strip() for text in texts if str(text or "").strip())
    if not joined:
        return ""

    for match in re.finditer(r"(?<![\d~～\-－—–至到])(\d{2})(?!\d)\s*岁", joined):
        age = match.group(1)
        start, end = match.span()
        window = joined[max(0, start - 24) : min(len(joined), end + 24)]
        compact_window = re.sub(r"\s+", "", window)
        if re.search(r"\d{1,3}[~～\-－—–至到]\d{1,3}岁", compact_window):
            continue
        if any(
            cue in compact_window
            for cue in (
                "老了",
                "显得",
                "看起来",
                "不像",
                "比",
                "以后",
                "之后",
                "案例",
                "别人",
                "朋友",
                "顾客",
                "客户",
                "医生",
            )
        ):
            continue
        if re.search(r"(?:今年多大|年龄|多大|几岁|身份证)[^。；;\n]{0,20}" + re.escape(age) + r"岁", compact_window):
            return age
        if re.search(r"(?:我|本人|客户|顾客|她|他)(?:今年|现在)?[^。；;\n]{0,10}" + re.escape(age) + r"岁", compact_window):
            return age
    return ""


def _extract_transcript_customer_signals(
    transcript_utterances: list[dict] | None,
    transcript_full_text: str | None,
) -> dict[str, str | bool]:
    texts = _customer_utterance_texts(transcript_utterances, transcript_full_text)
    if not texts:
        return {}
    joined = "\n".join(texts)

    return {
        "age": _extract_direct_customer_age(texts),
        "no_history": bool(re.search(r"(第一次|没做过|没有做过).{0,8}(医美|抗衰|项目|保养)", joined)),
        "single": "单身" in joined,
        "married": "已婚" in joined,
        "wechat_follow_up": bool("微信" in joined and re.search(r"(加我微信|加微信|微信联系)", joined)),
        "work_recovery_concern": bool(
            re.search(r"(上班|工作).{0,8}(恢复|肿|休息)", joined)
            or re.search(r"(恢复|肿|休息).{0,8}(上班|工作)", joined)
        ),
        "pain_sensitive": bool(re.search(r"(怕痛|疼|痛感|疼痛)", joined)),
    }


def _collect_summary_focus_items(result: dict, visit_order: VisitOrder | None) -> list[str]:
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    deal_factors = consultation_result.get("deal_factors", {})
    focus_values: list[str] = []
    if isinstance(deal_factors, dict):
        focus_values.extend([str(item or "").strip() for item in deal_factors.get("concerns", []) or []])
    if _is_visit_order_final_not_deal(visit_order):
        focus_values.extend(_collect_loss_reason_items(result))
    return _dedupe_preserve_order(focus_values)


def _build_summary_context(
    result: dict,
    visit_order: VisitOrder | None,
    transcript_full_text: str | None = None,
    transcript_utterances: list[dict] | None = None,
    advisor_name: str | None = None,
) -> dict:
    tag_values = _profile_values_by_category(result)
    transcript_signals = _extract_transcript_customer_signals(transcript_utterances, transcript_full_text)
    all_transcript_text = _all_transcript_text(transcript_utterances, transcript_full_text)
    transcript_cues = _collect_transcript_strategy_cues(all_transcript_text)
    recommendation_acceptance = _collect_recommendation_acceptance_items(result)
    recommendation_items = _collect_recommendation_items(result)
    concern_items = _collect_concern_items(result)
    focus_values = _collect_summary_focus_items(result, visit_order)
    loss_reasons = _collect_loss_reason_items(result)
    deal_outcome = _collect_deal_outcome(result)
    deal_status = _resolve_effective_deal_status(
        visit_order,
        str(deal_outcome.get("status") or "未明确").strip() or "未明确",
    )
    deal_items = _collect_deal_items(result)
    deal_amount = _collect_deal_amount(result)
    primary_demands = _collect_primary_demand_items(result)
    budget_text = _collect_budget_text(result)
    price_sensitivity = tag_values.get("价格敏感度", [])
    decision_factors: list[str] = []
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    deal_factors = consultation_result.get("deal_factors", {})
    if isinstance(deal_factors, dict):
        decision_factors = _dedupe_preserve_order(
            [str(item or "").strip() for item in deal_factors.get("decision_factors", []) or []]
        )

    recommendation_names = _merge_recommendation_name_values(
        [
            *[plan for plan, _ in recommendation_acceptance],
            *[_strip_recommendation_meta(item) for item in recommendation_items],
            *deal_items,
        ]
    )
    accepted_or_hesitant = [
        plan
        for plan, acceptance in recommendation_acceptance
        if _is_followable_acceptance(acceptance)
    ]
    history_values = tag_values.get("治疗项目", [])
    material_values = tag_values.get("历史用的设备/原材料名称", [])
    negative_values = [value for value in tag_values.get("负面项目/设备/原材料", []) if value != "无"]
    risks = [value for value in tag_values.get("健康风险/禁忌", []) if value != "无风险禁忌"]

    return {
        "tag_values": tag_values,
        "transcript_signals": transcript_signals,
        "transcript_cues": transcript_cues,
        "customer_type": _collect_visit_customer_type_text(visit_order),
        "age": _collect_customer_age_text(result, transcript_signals),
        "advisor_name": advisor_name or "",
        "staff_text": _collect_visit_staff_text(visit_order, advisor_name),
        "primary_demands": primary_demands,
        "expectation_values": _collect_expectation_phrases(result),
        "budget_text": budget_text,
        "price_sensitivity": price_sensitivity,
        "decision_factors": decision_factors,
        "concern_items": concern_items,
        "focus_values": focus_values,
        "loss_reasons": loss_reasons,
        "deal_status": deal_status,
        "deal_items": deal_items,
        "deal_amount": deal_amount,
        "recommendation_acceptance": recommendation_acceptance,
        "recommendation_names": recommendation_names,
        "accepted_or_hesitant": accepted_or_hesitant,
        "history_values": history_values,
        "material_values": material_values,
        "negative_values": negative_values,
        "risks": risks,
        "no_history": bool(transcript_signals.get("no_history")) or "无医美史" in history_values,
    }


def _section_key_for_template(section: SapSummaryTemplateSection) -> str:
    text = f"{section.title} {section.guidance}"
    if "客户背景" in text or "客户基础信息" in text or ("年龄" in text and "历史" in text):
        return "background"
    if "决策画像" in text or "需求与动机" in text or "核心阻力" in text or "决策主体" in text:
        return "decision"
    if "方案反馈" in text or "面诊与设计" in text or "推荐方案" in text or "客户接受" in text:
        return "plan"
    if "报价" in text or "成交策略" in text:
        return "pricing"
    if "成交与跟进" in text or "未成交" in text or "回访方式" in text:
        return "deal_follow"
    if "客户画像" in text or "信任偏好" in text or "客户类型" in text:
        return "profile"
    if "后续跟进" in text or "长期开发" in text or "互动与关系" in text:
        return "follow"
    if "老带新" in text or "种草" in text or "套电" in text or "米米" in text:
        return "referral"
    return "general"


def _configured_background_sentences(context: dict) -> list[str]:
    tag_values = context["tag_values"]
    identity: list[str] = []
    if context["age"]:
        identity.append(f"年龄{context['age']}")
    if context["customer_type"]:
        identity.append(f"本次到诊类型为{context['customer_type']}")
    if tag_values.get("常驻城市"):
        identity.append(f"常驻区域在{_natural_join(tag_values['常驻城市'][:2])}")
    if tag_values.get("职业"):
        identity.append(f"职业信息与{_natural_join(tag_values['职业'][:2])}相关")
    if tag_values.get("特殊身份"):
        identity.append(f"存在{_natural_join(tag_values['特殊身份'][:2])}等特殊身份线索")

    history: list[str] = []
    if context["no_history"]:
        history.append("既往医美经历相对空白")
    else:
        history_items = [value for value in context["history_values"] if value not in {"第一次做医美", "无医美史"}]
        if history_items:
            history.append(f"既往做过{_natural_join(history_items[:3])}等项目")
    if context["material_values"]:
        history.append(f"过往接触过{_natural_join(context['material_values'][:3])}等材料或设备")
    if context["negative_values"]:
        history.append(f"曾对{_natural_join(context['negative_values'][:2])}有过不满意体验")
    if context["risks"]:
        history.append(f"治疗前还要关注{_natural_join(context['risks'][:2])}等风险禁忌")

    sentences: list[str] = []
    if identity:
        sentences.append(f"这位客户{ '，'.join(identity)}，后续沟通需要结合这些基础信息建立信任")
    if history:
        sentences.append(f"客户过往治疗和身体基础中，{_natural_join(history[:4])}，方案排序要兼顾安全、效果边界和接受门槛")
    return sentences


def _configured_decision_sentences(context: dict) -> list[str]:
    tag_values = context["tag_values"]
    sentences: list[str] = []
    if context["primary_demands"]:
        sentences.append(f"这次真正驱动客户到院的是{_natural_join(context['primary_demands'][:4])}，回访时要围绕这些原始需求承接")
    if context["expectation_values"]:
        sentences.append(f"客户对结果的期待更偏向{_natural_join(context['expectation_values'][:2])}，需要把效果边界和治疗顺序解释清楚")
    decision_parts: list[str] = []
    if context["price_sensitivity"]:
        decision_parts.append(f"价格敏感度偏{_natural_join(context['price_sensitivity'][:1])}")
    if tag_values.get("决策主体"):
        decision_parts.append(f"决策上会受到{_natural_join(tag_values['决策主体'][:2])}影响")
    if context["decision_factors"]:
        decision_parts.append(f"还会参考{_natural_join(context['decision_factors'][:3])}")
    if context["transcript_cues"]["competitor"]:
        decision_parts.append("存在对比其他机构或医生的可能")
    if decision_parts:
        sentences.append(f"影响推进的关键因素是{_natural_join(decision_parts)}")
    if context["concern_items"]:
        sentences.append(f"当前最需要化解的是{_natural_join(context['concern_items'][:3])}，不要只重复项目名称，要把客户为什么会犹豫讲透")
    return sentences


def _configured_plan_sentences(context: dict) -> list[str]:
    sentences: list[str] = []
    advisor_name = context["advisor_name"]
    if advisor_name:
        sentences.append(f"本次由{advisor_name}承接咨询，方案需要从客户原始诉求自然过渡到可执行项目")
    elif context["staff_text"]:
        sentences.append(f"{context['staff_text']}，方案需要从客户原始诉求自然过渡到可执行项目")
    if context["recommendation_names"]:
        if context["primary_demands"]:
            sentences.append(
                f"推荐方向主要围绕{_natural_join(context['primary_demands'][:3])}展开，重点讨论了{_natural_join(context['recommendation_names'][:5])}"
            )
        else:
            sentences.append(f"现场重点讨论了{_natural_join(context['recommendation_names'][:5])}，还需要继续说明方案与客户问题的对应关系")
    if context["recommendation_acceptance"]:
        feedback = [
            f"{plan}{acceptance}"
            for plan, acceptance in context["recommendation_acceptance"][:4]
        ]
        sentences.append(f"客户对方案的反馈表现为{_natural_join(feedback)}，优先推进项应从客户已经认可或愿意继续听的部分切入")
    elif context["accepted_or_hesitant"]:
        sentences.append(f"客户对{_natural_join(context['accepted_or_hesitant'][:3])}存在继续推进空间")
    return sentences


def _configured_pricing_sentences(context: dict) -> list[str]:
    sentences: list[str] = []
    if context["deal_status"] == "已成交":
        if context["deal_items"] and context["deal_amount"]:
            sentences.append(f"本次已成交{_natural_join(context['deal_items'][:3])}，金额为{context['deal_amount']}")
        elif context["deal_items"]:
            sentences.append(f"本次已成交{_natural_join(context['deal_items'][:3])}，成交金额仍需补充")
        elif context["deal_amount"]:
            sentences.append(f"本次已有明确成交金额线索{context['deal_amount']}，成交方案仍需补充")
        else:
            sentences.append("本次已成交，但成交方案和金额仍需在系统记录中补齐")
    elif context["deal_status"] == "未成交":
        if context["loss_reasons"]:
            sentences.append(f"本次未成交，主要卡点是{_natural_join(context['loss_reasons'][:3])}")
        else:
            sentences.append("本次未成交，具体阻力还需要通过回访继续确认")
    else:
        sentences.append("本次成交状态尚未完全明确，需要继续核实是否进入付款、定金或排期")
    if context["budget_text"] and context["deal_status"] != "已成交":
        sentences.append(f"预算和报价线索为{context['budget_text']}，可以拆成阶段方案降低决策压力")
    if context["transcript_cues"]["price_strategy"] or context["price_sensitivity"]:
        strategy_parts = []
        if context["transcript_cues"]["price_strategy"]:
            strategy_parts.append("活动、套餐、分期或定金等方式")
        if context["price_sensitivity"]:
            strategy_parts.append(f"客户价格敏感度偏{_natural_join(context['price_sensitivity'][:1])}")
        sentences.append(f"价格突破口可围绕{_natural_join(strategy_parts)}展开，但仍要回到效果价值和风险收益解释")
    return sentences


def _configured_profile_sentences(context: dict) -> list[str]:
    tag_values = context["tag_values"]
    profile_parts: list[str] = []
    if tag_values.get("客户类型"):
        profile_parts.append(f"客户类型更接近{_natural_join(tag_values['客户类型'][:2])}")
    if context["price_sensitivity"]:
        profile_parts.append(f"价格敏感度为{_natural_join(context['price_sensitivity'][:1])}")
    if tag_values.get("倾向治疗方式"):
        profile_parts.append(f"治疗方式偏好为{_natural_join(tag_values['倾向治疗方式'][:2])}")
    sentences: list[str] = []
    if profile_parts:
        sentences.append(f"客户画像上，{_natural_join(profile_parts)}，后续沟通要把方案价值讲到客户能判断和选择")
    if context["transcript_cues"]["trust"]:
        sentences.append("信任建立可以继续借助医生资质、案例、熟人推荐或既往体验，让客户获得更强确定感")
    elif context["concern_items"]:
        sentences.append(f"客户不是完全没有兴趣，而是需要围绕{_natural_join(context['concern_items'][:2])}建立确定感")
    return sentences


def _configured_follow_sentences(context: dict) -> list[str]:
    sentences: list[str] = []
    if context["deal_status"] == "已成交" and context["deal_items"]:
        sentences.append(f"短期应先保障{_natural_join(context['deal_items'][:3])}的治疗安排、注意事项、效果反馈和复查体验")
        add_on_candidates = [
            plan for plan in context["recommendation_names"] if plan not in set(context["deal_items"])
        ]
        if add_on_candidates:
            sentences.append(f"等客户看到首轮效果后，再自然承接{_natural_join(add_on_candidates[:2])}等附加方案")
    elif context["accepted_or_hesitant"]:
        sentences.append(f"下一步建议优先推进{_natural_join(context['accepted_or_hesitant'][:3])}，先解决客户最明确的犹豫点")
    elif context["recommendation_names"]:
        sentences.append(f"下一步可围绕{_natural_join(context['recommendation_names'][:3])}确认治疗顺序、价格接受度和到院时间")
    else:
        sentences.append("下一步建议继续确认客户对方案、价格、治疗时间和效果预期的接受度")
    if context["focus_values"]:
        sentences.append(f"回访话术重点应回应{_natural_join(context['focus_values'][:3])}")
    if context["transcript_signals"].get("wechat_follow_up") or "微信" in context["tag_values"].get("倾向回访方式", []):
        sentences.append("客户适合通过微信延续沟通，持续维护服务感和专业感")
    return sentences


def _configured_referral_sentences(context: dict) -> list[str]:
    parts: list[str] = []
    if context["transcript_cues"]["referral_open"]:
        parts.append("录音中已经出现老带新、转介绍或推荐朋友相关开口")
    if context["transcript_cues"]["referral_policy"]:
        parts.append("同时宣教了老带新福利、新客福利、老客奖励米米等权益")
    if context["transcript_cues"]["phone_capture"]:
        parts.append("沟通中有电话、号码或套电相关动作，可用于后续触达")
    if parts:
        return [f"{_natural_join(parts)}，后续可以把客户满意度、朋友推荐和福利机制串起来，形成更自然的转介绍入口"]
    return []


def _configured_deal_follow_sentences(context: dict) -> list[str]:
    sentences = _configured_pricing_sentences(context)
    follow_sentences = _configured_follow_sentences(context)
    for sentence in follow_sentences:
        if sentence not in sentences:
            sentences.append(sentence)
    return sentences


def _build_configured_section_sentences(section: SapSummaryTemplateSection, context: dict) -> list[str]:
    section_key = _section_key_for_template(section)
    if section_key == "background":
        return _configured_background_sentences(context)
    if section_key == "decision":
        return _configured_decision_sentences(context)
    if section_key == "plan":
        return _configured_plan_sentences(context)
    if section_key == "pricing":
        return _configured_pricing_sentences(context)
    if section_key == "deal_follow":
        return _configured_deal_follow_sentences(context)
    if section_key == "profile":
        return _configured_profile_sentences(context)
    if section_key == "follow":
        return _configured_follow_sentences(context)
    if section_key == "referral":
        return _configured_referral_sentences(context)

    generic = []
    generic.extend(_configured_background_sentences(context)[:1])
    generic.extend(_configured_decision_sentences(context)[:1])
    generic.extend(_configured_plan_sentences(context)[:1])
    return generic


def _configured_section_fallback(section: SapSummaryTemplateSection) -> str:
    section_key = _section_key_for_template(section)
    if section_key == "background":
        return "录音内未提取到足够的客户年龄、新老客、历史医美、历史用材、负面经历或风险禁忌信息"
    if section_key == "decision":
        return "录音内未形成更明确的价格敏感度、决策主体、对比机构或核心阻力信息"
    if section_key == "plan":
        return "录音内没有形成清晰的客户方案反馈或可优先推进项"
    if section_key == "pricing":
        return "录音内没有提取到明确报价、价格策略或客户接受度"
    if section_key == "deal_follow":
        return "本次成交结果和下一步跟进重点仍需继续确认"
    if section_key == "profile":
        return "除前述诉求和顾虑外，暂未提取到更稳定的客户类型或信任偏好"
    if section_key == "follow":
        return "建议继续确认客户对方案、价格、治疗时间和效果预期的接受度"
    if section_key == "referral":
        return "本次沟通未明确出现老带新开口、机制宣教或主动套电动作"
    return f"录音内暂未提取到足够支撑“{section.title}”的信息"


def _collect_configured_sap_summary_text(
    result: dict,
    visit_order: VisitOrder | None,
    transcript_full_text: str | None,
    transcript_utterances: list[dict] | None,
    advisor_name: str | None,
    sap_summary_config: SapSummaryTemplateConfig | None,
) -> str:
    if sap_summary_config is None or not sap_summary_config.sections:
        return ""

    section_names = tuple(section.title for section in sap_summary_config.sections)
    model_summary = _collect_model_sap_summary_text_for_sections(result, section_names, require_all=True)
    if model_summary:
        return model_summary

    context = _build_summary_context(
        result,
        visit_order,
        transcript_full_text=transcript_full_text,
        transcript_utterances=transcript_utterances,
        advisor_name=advisor_name,
    )
    lines = [
        _summary_paragraph(
            index,
            section.title,
            _build_configured_section_sentences(section, context),
            _configured_section_fallback(section),
        )
        for index, section in enumerate(sap_summary_config.sections, 1)
    ]
    return "\n\n".join(lines)


def _collect_summary_text(
    result: dict,
    visit_order: VisitOrder | None,
    transcript_full_text: str | None = None,
    transcript_utterances: list[dict] | None = None,
    advisor_name: str | None = None,
    sap_summary_config: SapSummaryTemplateConfig | None = None,
) -> str:
    configured_summary = _collect_configured_sap_summary_text(
        result,
        visit_order,
        transcript_full_text,
        transcript_utterances,
        advisor_name,
        sap_summary_config,
    )
    if configured_summary:
        return configured_summary

    model_summary = _collect_model_sap_summary_text(result)
    if model_summary:
        return model_summary

    tag_values = _profile_values_by_category(result)
    transcript_signals = _extract_transcript_customer_signals(transcript_utterances, transcript_full_text)
    recommendation_acceptance = _collect_recommendation_acceptance_items(result)
    concern_items = _collect_concern_items(result)
    focus_values = _collect_summary_focus_items(result, visit_order)
    loss_reasons = _collect_loss_reason_items(result)
    deal_outcome = _collect_deal_outcome(result)
    deal_status = _resolve_effective_deal_status(
        visit_order,
        str(deal_outcome.get("status") or "未明确").strip() or "未明确",
    )
    deal_items = _collect_deal_items(result)
    deal_amount = _collect_deal_amount(result)
    expectation_values = _collect_expectation_phrases(result)
    primary_demands = _collect_primary_demand_items(result)
    budget_text = _collect_budget_text(result)
    recommendation_items = _collect_recommendation_items(result)
    recommendation_names = _merge_recommendation_name_values(
        [
            *[plan for plan, _ in recommendation_acceptance],
            *[_strip_recommendation_meta(item) for item in recommendation_items],
            *deal_items,
        ]
    )
    all_transcript_text = _all_transcript_text(transcript_utterances, transcript_full_text)
    transcript_cues = _collect_transcript_strategy_cues(all_transcript_text)

    customer_type = _collect_visit_customer_type_text(visit_order)
    age = _collect_customer_age_text(result, transcript_signals)
    population_parts: list[str] = []
    if age:
        population_parts.append(f"客户年龄{age}")
    if customer_type:
        population_parts.append(f"到诊类型为{customer_type}")
    if tag_values.get("常驻城市"):
        population_parts.append(f"常驻区域为{_natural_join(tag_values['常驻城市'][:2])}")
    if tag_values.get("特殊身份"):
        population_parts.append(f"特殊身份提到{_natural_join(tag_values['特殊身份'][:2])}")
    if tag_values.get("职业"):
        population_parts.append(f"职业信息为{_natural_join(tag_values['职业'][:2])}")
    if tag_values.get("个人情况"):
        population_parts.append(f"个人情况为{_natural_join(tag_values['个人情况'][:2])}")
    elif transcript_signals.get("single"):
        population_parts.append("客户为单身状态")
    elif transcript_signals.get("married"):
        population_parts.append("客户为已婚状态")

    economic_parts: list[str] = []
    if budget_text:
        economic_parts.append(f"本次预算或金额线索为{budget_text}")
    price_sensitivity = tag_values.get("价格敏感度", [])
    if price_sensitivity:
        economic_parts.append(f"价格敏感度偏{_natural_join(price_sensitivity[:1])}")
    history_values = tag_values.get("治疗项目", [])
    if transcript_signals.get("no_history") or "无医美史" in history_values:
        economic_parts.append("此前暂无明确医美史")
    else:
        history_items = [value for value in history_values if value not in {"第一次做医美", "无医美史"}]
        if history_items:
            economic_parts.append(f"既往做过{_natural_join(history_items[:3])}等医美项目")
    material_values = tag_values.get("历史用的设备/原材料名称", [])
    if material_values:
        economic_parts.append(f"历史用材或设备包括{_natural_join(material_values[:3])}")

    physical_parts: list[str] = []
    negative_values = [value for value in tag_values.get("负面项目/设备/原材料", []) if value != "无"]
    if negative_values:
        physical_parts.append(f"曾对{_natural_join(negative_values[:2])}有负面体验或不满意")
    risks = [value for value in tag_values.get("健康风险/禁忌", []) if value != "无风险禁忌"]
    if risks:
        physical_parts.append(f"需关注{_natural_join(risks[:2])}等风险禁忌")
    for category in ("皮肤类型", "敏感度", "生理期", "创伤倾向", "倾向治疗方式"):
        if tag_values.get(category):
            physical_parts.append(f"{category}为{_natural_join(tag_values[category][:2])}")
    if transcript_signals.get("pain_sensitive"):
        physical_parts.append("对疼痛和耐受度较敏感")

    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    deal_factors = consultation_result.get("deal_factors", {})
    decision_factors = []
    if isinstance(deal_factors, dict):
        decision_factors = [
            str(item or "").strip()
            for item in deal_factors.get("decision_factors", []) or []
            if str(item or "").strip()
        ]

    motivation_parts: list[str] = []
    if expectation_values:
        motivation_parts.append(_format_expectation_summary("；".join(expectation_values[:2])))
    if decision_factors:
        motivation_parts.append(f"影响决策的因素包括{_natural_join(_dedupe_preserve_order(decision_factors)[:3])}")
    if transcript_cues["competitor"]:
        motivation_parts.append("录音中出现对比其他机构或医生的线索")

    decision_parts: list[str] = []
    if tag_values.get("决策主体"):
        decision_parts.append(f"决策上会受到{_natural_join(tag_values['决策主体'][:2])}影响")
    if concern_items:
        decision_parts.append(f"当前阻力主要集中在{_natural_join(concern_items[:3])}")
    if transcript_signals.get("work_recovery_concern"):
        decision_parts.append("客户明确关注恢复期对工作或日常安排的影响")

    staff_text = _collect_visit_staff_text(visit_order, advisor_name)

    accepted_or_hesitant = [
        plan
        for plan, acceptance in recommendation_acceptance
        if _is_followable_acceptance(acceptance)
    ]

    recommendation_plan_text = "、".join(recommendation_names[:5])
    product_parts: list[str] = []
    if deal_items:
        product_parts.append(f"成交或重点沟通项目为{_natural_join(deal_items[:3])}")
    elif material_values:
        product_parts.append(f"对话中提到的典型设备或材料包括{_natural_join(material_values[:3])}")

    quote_parts: list[str] = []
    if deal_amount:
        quote_parts.append(f"成交或金额线索为{deal_amount}")
    if budget_text:
        quote_parts.append(f"客户预算表达为{budget_text}")

    price_strategy_parts: list[str] = []
    if transcript_cues["price_strategy"]:
        price_strategy_parts.append("优惠、套餐、分期、定金或活动")
    if price_sensitivity:
        price_strategy_parts.append(f"客户价格敏感度偏{_natural_join(price_sensitivity[:1])}")
    if not price_strategy_parts and decision_factors:
        price_strategy_parts.append(f"{_natural_join(_dedupe_preserve_order(decision_factors)[:2])}相关价值解释")

    acceptance_parts: list[str] = [f"成交状态为{deal_status}"]
    if recommendation_acceptance:
        readable_feedback = [
            f"{plan}为{acceptance}"
            for plan, acceptance in recommendation_acceptance[:4]
        ]
        acceptance_parts.append(f"客户对{_natural_join(readable_feedback)}")
    if accepted_or_hesitant:
        acceptance_parts.append(f"可优先推进{_natural_join(accepted_or_hesitant[:3])}")
    elif deal_items:
        acceptance_parts.append(f"围绕{_natural_join(deal_items[:2])}继续确认治疗安排和交付预期")

    profile_parts: list[str] = []
    customer_type_values = tag_values.get("客户类型", [])
    if customer_type_values:
        profile_parts.append(f"客户类型更接近{_natural_join(customer_type_values[:2])}")
    if price_sensitivity:
        profile_parts.append(f"价格敏感度为{_natural_join(price_sensitivity[:1])}")
    if tag_values.get("倾向治疗方式"):
        profile_parts.append(f"倾向治疗方式为{_natural_join(tag_values['倾向治疗方式'][:2])}")

    trust_parts: list[str] = []
    if transcript_cues["trust"]:
        trust_parts.append("客户或咨询师沟通中出现医生资质、案例、口碑或熟人推荐等信任线索")
    if transcript_cues["competitor"]:
        trust_parts.append("客户存在对比其他机构或医生的可能")
    trust_related_factors = [
        value
        for value in decision_factors
        if any(keyword in value for keyword in ("医生", "资质", "案例", "口碑", "朋友", "熟人", "机构", "品牌"))
    ]
    if not trust_parts and trust_related_factors:
        trust_parts.append(f"客户信任建立可围绕{_natural_join(_dedupe_preserve_order(trust_related_factors)[:2])}展开")

    treatment_suggestion_parts: list[str] = []
    if deal_status == "已成交" and deal_items:
        treatment_suggestion_parts.append(f"短期建议跟进{_natural_join(deal_items[:3])}的治疗安排、效果反馈和复查")
    elif accepted_or_hesitant:
        treatment_suggestion_parts.append(f"短期建议优先推进{_natural_join(accepted_or_hesitant[:3])}")
    elif recommendation_names:
        treatment_suggestion_parts.append(f"短期建议围绕{_natural_join(recommendation_names[:3])}确认治疗顺序")
    if transcript_cues["immediate_treatment"]:
        treatment_suggestion_parts.append("录音中出现当日治疗或尽快治疗线索")

    long_term_parts: list[str] = []
    if deal_items:
        long_term_parts.append(f"已成交后可围绕{_natural_join(deal_items[:3])}做复查、复购和联合项目延展")
    elif recommendation_names:
        long_term_parts.append(f"可从{_natural_join(recommendation_names[:2])}切入，逐步延展长期方案")
    if transcript_signals.get("wechat_follow_up") or "微信" in tag_values.get("倾向回访方式", []):
        long_term_parts.append("建议通过微信持续跟进")

    relationship_parts: list[str] = []
    if transcript_cues["praise"]:
        relationship_parts.append("沟通中有赞美、肯定基础或审美共识建立")
    if transcript_signals.get("wechat_follow_up") or "微信" in tag_values.get("倾向回访方式", []):
        relationship_parts.append("已建立或适合建立微信跟进关系")

    follow_parts: list[str] = []
    if deal_status == "已成交":
        if deal_items and deal_amount:
            follow_parts.append(f"本次已成交{_natural_join(deal_items[:3])}，金额为{deal_amount}")
        elif deal_items:
            follow_parts.append(f"本次已成交{_natural_join(deal_items[:3])}")
        elif deal_amount:
            follow_parts.append(f"本次已有成交金额线索{deal_amount}")
        else:
            follow_parts.append("本次已成交，但成交方案和金额仍需在后续记录中补齐")
    elif deal_status == "未成交":
        if loss_reasons:
            follow_parts.append(f"本次未成交，主要卡点是{_natural_join(loss_reasons[:3])}")
        else:
            follow_parts.append("本次未成交，具体原因仍需继续确认")
    else:
        follow_parts.append("本次成交状态尚未明确")
        if deal_items or deal_amount:
            detail = _natural_join(deal_items[:3]) if deal_items else "相关方案"
            amount_text = f"，金额线索为{deal_amount}" if deal_amount else ""
            follow_parts.append(f"已讨论{detail}{amount_text}")
    if focus_values:
        follow_parts.append(f"下一步建议重点回应{_natural_join(focus_values[:3])}")
    if transcript_signals.get("wechat_follow_up") or "微信" in tag_values.get("倾向回访方式", []):
        follow_parts.append("建议通过微信延续沟通")
    elif not focus_values and deal_status != "已成交":
        follow_parts.append("建议继续确认价格接受度、治疗时间和方案优先级")

    referral_parts: list[str] = []
    if transcript_cues["referral_open"]:
        referral_parts.append("录音中已经出现老带新、转介绍或推荐朋友相关开口")
    if transcript_cues["referral_policy"]:
        referral_parts.append("同时宣教了老带新、新客福利、老客奖励米米等权益")
    if transcript_cues["phone_capture"]:
        referral_parts.append("沟通中也有电话、号码或套电相关动作，可用于后续转介绍触达")

    plan_narrative_parts: list[str] = []
    if staff_text:
        plan_narrative_parts.append(staff_text)
    if recommendation_plan_text:
        if primary_demands:
            plan_narrative_parts.append(f"现场方案围绕客户表达的问题展开，重点给出{recommendation_plan_text}")
        else:
            plan_narrative_parts.append(f"现场主要给出{recommendation_plan_text}")
    if product_parts:
        plan_narrative_parts.append("；".join(product_parts))
    if recommendation_acceptance:
        readable_feedback = [
            f"{plan}{acceptance}"
            for plan, acceptance in recommendation_acceptance[:4]
        ]
        plan_narrative_parts.append(f"客户对方案的反馈是{_natural_join(readable_feedback)}")

    pricing_narrative_parts: list[str] = []
    if deal_status == "已成交":
        if deal_items and deal_amount:
            pricing_narrative_parts.append(f"本次已成交{_natural_join(deal_items[:3])}，金额为{deal_amount}")
        elif deal_items:
            pricing_narrative_parts.append(f"本次已成交{_natural_join(deal_items[:3])}")
        elif deal_amount:
            pricing_narrative_parts.append(f"本次已有成交金额线索{deal_amount}")
        else:
            pricing_narrative_parts.append("本次已成交，但成交方案和金额仍需在后续记录中补齐")
    elif deal_status == "未成交":
        if loss_reasons:
            pricing_narrative_parts.append(f"本次尚未成交，主要卡点集中在{_natural_join(loss_reasons[:3])}")
        else:
            pricing_narrative_parts.append("本次尚未成交，具体阻力仍需继续确认")
    else:
        pricing_narrative_parts.append("本次成交状态尚未明确")
    if quote_parts and deal_status != "已成交":
        pricing_narrative_parts.append("报价和预算信息显示，" + "；".join(quote_parts))
    if price_strategy_parts:
        price_strategy_text = "；".join(price_strategy_parts).replace("沟通中出现", "出现")
        pricing_narrative_parts.append("价格沟通上，" + price_strategy_text)
    if recommendation_acceptance:
        readable_acceptance = [
            f"{plan}{acceptance}"
            for plan, acceptance in recommendation_acceptance[:4]
        ]
        pricing_narrative_parts.append(f"客户对方案的接受度表现为{_natural_join(readable_acceptance)}")

    profile_narrative_parts: list[str] = []
    if profile_parts:
        profile_narrative_parts.append("，".join(profile_parts))
    if trust_parts:
        profile_narrative_parts.append("；".join(trust_parts))
    if transcript_cues["competitor"]:
        profile_narrative_parts.append("后续沟通需要兼顾对比机构带来的信任和差异化解释")

    follow_narrative_parts: list[str] = []
    if deal_status == "已成交" and deal_items:
        follow_narrative_parts.append(f"后续重点应放在{_natural_join(deal_items[:3])}的治疗安排、术后/治疗后反馈和复查维护上")
        add_on_candidates = [plan for plan in recommendation_names if plan not in set(deal_items)]
        if add_on_candidates:
            follow_narrative_parts.append(f"附加方案可在客户看到首轮效果后，再自然承接到{_natural_join(add_on_candidates[:2])}")
    else:
        follow_narrative_parts.extend(treatment_suggestion_parts)
    follow_narrative_parts.extend(long_term_parts)
    follow_narrative_parts.extend(relationship_parts)
    if deal_status == "已成交":
        if focus_values:
            follow_narrative_parts.append(f"下一步沟通重点是回应{_natural_join(focus_values[:3])}")
        if transcript_signals.get("wechat_follow_up") or "微信" in tag_values.get("倾向回访方式", []):
            follow_narrative_parts.append("可继续通过微信维护治疗体验和复购机会")
    else:
        follow_narrative_parts.extend(follow_parts)

    identity_clauses: list[str] = []
    if age:
        identity_clauses.append(f"年龄{age}")
    if customer_type:
        identity_clauses.append(f"到诊类型是{customer_type}")
    if tag_values.get("职业"):
        identity_clauses.append(f"职业与{_natural_join(tag_values['职业'][:2])}相关")
    if tag_values.get("常驻城市"):
        identity_clauses.append(f"常驻区域在{_natural_join(tag_values['常驻城市'][:2])}")
    if tag_values.get("特殊身份"):
        identity_clauses.append(f"存在{_natural_join(tag_values['特殊身份'][:2])}等特殊身份线索")
    if tag_values.get("个人情况"):
        identity_clauses.append(f"个人情况提到{_natural_join(tag_values['个人情况'][:2])}")
    elif transcript_signals.get("single"):
        identity_clauses.append("当前为单身状态")
    elif transcript_signals.get("married"):
        identity_clauses.append("当前为已婚状态")

    history_clauses: list[str] = []
    if transcript_signals.get("no_history") or "无医美史" in history_values:
        history_clauses.append("既往医美经历相对空白")
    else:
        history_items = [value for value in history_values if value not in {"第一次做医美", "无医美史"}]
        if history_items:
            history_clauses.append(f"既往做过{_natural_join(history_items[:3])}等项目")
    if material_values:
        history_clauses.append(f"过往接触过{_natural_join(material_values[:3])}等材料或设备")
    if negative_values:
        history_clauses.append(f"对{_natural_join(negative_values[:2])}有过不满意体验")

    money_clauses: list[str] = []
    if budget_text:
        money_clauses.append(f"本次已出现{budget_text}的预算或金额线索")
    if price_sensitivity:
        money_clauses.append(f"价格敏感度偏{_natural_join(price_sensitivity[:1])}")

    condition_clauses: list[str] = []
    if risks:
        condition_clauses.append(f"需要继续关注{_natural_join(risks[:2])}等风险禁忌")
    for category in ("皮肤类型", "敏感度", "生理期", "创伤倾向", "倾向治疗方式"):
        if tag_values.get(category):
            condition_clauses.append(f"{category}偏向{_natural_join(tag_values[category][:2])}")
    if transcript_signals.get("pain_sensitive"):
        condition_clauses.append("对疼痛和耐受度比较敏感")

    background_sentences: list[str] = []
    if identity_clauses:
        background_sentences.append(f"这位客户{ '，'.join(identity_clauses)}，这些信息决定了沟通中既要建立信任，也要把方案解释得足够具体")
    if history_clauses or money_clauses:
        context_text = "；".join([*_dedupe_preserve_order(history_clauses), *_dedupe_preserve_order(money_clauses)])
        background_sentences.append(f"从消费基础看，{context_text}，后续报价和项目排序需要兼顾效果价值与接受门槛")
    if condition_clauses:
        background_sentences.append(f"身体与治疗条件方面，{_natural_join(condition_clauses[:4])}，这些点适合在术前确认、风险解释和恢复期沟通中继续跟进")

    demand_sentences: list[str] = []
    if primary_demands:
        demand_sentences.append(f"客户这次的需求主线比较清楚，主要集中在{_natural_join(primary_demands[:5])}，不是泛泛了解项目")
    if expectation_values:
        demand_sentences.append(f"客户对结果的期待更偏向{_natural_join(expectation_values[:2])}，因此沟通重点应放在效果边界、自然度和可落地方案上")
    if decision_factors or transcript_cues["competitor"]:
        factors = _dedupe_preserve_order(decision_factors)
        if transcript_cues["competitor"]:
            factors.append("对比其他机构或医生")
        demand_sentences.append(f"影响决策的因素包括{_natural_join(_dedupe_preserve_order(factors)[:4])}，这些因素会直接影响客户是否愿意当场推进")
    if concern_items or transcript_signals.get("work_recovery_concern"):
        concern_texts = list(concern_items[:3])
        if transcript_signals.get("work_recovery_concern"):
            concern_texts.append("恢复期对工作或日常安排的影响")
        demand_sentences.append(f"真正需要被化解的是{_natural_join(_dedupe_preserve_order(concern_texts)[:4])}，后续话术要围绕这些阻力给出更确定的解释")

    plan_sentences: list[str] = []
    if advisor_name:
        plan_sentences.append(f"本次由{advisor_name}承接咨询，方案沟通需要把客户原始诉求转化为可执行的治疗路径")
    elif staff_text:
        plan_sentences.append(f"{staff_text}，方案沟通需要把客户原始诉求转化为可执行的治疗路径")
    if recommendation_plan_text:
        if primary_demands:
            plan_sentences.append(f"推荐方向围绕{_natural_join(primary_demands[:3])}展开，重点落在{recommendation_plan_text}，避免让客户觉得只是被动追加项目")
        else:
            plan_sentences.append(f"现场主要推荐{recommendation_plan_text}，需要继续说明每个项目和客户问题之间的对应关系")
    if product_parts:
        plan_sentences.append("；".join(product_parts))
    if recommendation_acceptance:
        readable_feedback = [f"{plan}{acceptance}" for plan, acceptance in recommendation_acceptance[:4]]
        plan_sentences.append(f"客户反馈中，{_natural_join(readable_feedback)}，这能判断哪些方案适合优先推进、哪些适合作为后续开发")

    pricing_sentences: list[str] = []
    if deal_status == "已成交":
        if deal_items and deal_amount:
            pricing_sentences.append(f"本次已经落地成交，成交项目为{_natural_join(deal_items[:3])}，金额为{deal_amount}")
        elif deal_items:
            pricing_sentences.append(f"本次已经落地成交，成交项目为{_natural_join(deal_items[:3])}")
        elif deal_amount:
            pricing_sentences.append(f"本次已有明确成交金额线索{deal_amount}")
        else:
            pricing_sentences.append("本次已成交，但成交方案和金额仍需要在后续记录中补齐")
    elif deal_status == "未成交":
        if loss_reasons:
            pricing_sentences.append(f"本次没有当场成交，主要卡点落在{_natural_join(loss_reasons[:3])}")
        else:
            pricing_sentences.append("本次没有当场成交，具体卡点还需要结合回访继续确认")
    else:
        pricing_sentences.append("本次成交状态尚未完全明确，需要继续核实客户是否已进入付款、定金或排期动作")
    if quote_parts and deal_status != "已成交":
        pricing_sentences.append(f"报价与预算信息中，{'；'.join(quote_parts)}，适合在回访时拆解为可接受的阶段方案")
    if price_strategy_parts:
        if len(price_strategy_parts) == 1:
            pricing_sentences.append(f"价格突破口可先围绕{price_strategy_parts[0]}展开，但仍要回到项目价值和风险收益解释")
        else:
            pricing_sentences.append(
                f"价格策略上，可以先结合{price_strategy_parts[0]}降低决策门槛，同时针对{_natural_join(price_strategy_parts[1:3])}把价值解释说透"
            )
    if recommendation_acceptance:
        readable_acceptance = [f"{plan}{acceptance}" for plan, acceptance in recommendation_acceptance[:4]]
        pricing_sentences.append(f"从接受度看，{_natural_join(readable_acceptance)}，成交策略上应优先抓住客户已经认可或心动的部分")

    profile_sentences: list[str] = []
    if profile_parts:
        profile_sentences.append(f"画像上更明显的是{'，'.join(profile_parts)}，单纯强调项目名称不够，需要把方案价值讲到客户能判断和选择")
    if trust_parts:
        profile_sentences.append(f"信任建立上，{'；'.join(trust_parts)}，后续可用案例、医生能力、效果边界和长期陪伴来增强确定感")
    elif concern_items:
        profile_sentences.append(f"客户当前不是完全没有兴趣，而是需要围绕{_natural_join(concern_items[:2])}建立确定感")
    if transcript_cues["competitor"]:
        profile_sentences.append("因为对话里出现对比机构线索，后续要主动讲清本院方案差异、医生经验和交付保障，减少客户外部比较造成的流失")

    follow_sentences: list[str] = []
    if deal_status == "已成交" and deal_items:
        follow_sentences.append(f"短期跟进应先保障{_natural_join(deal_items[:3])}的治疗安排、注意事项、术后反馈和复查体验")
        add_on_candidates = [plan for plan in recommendation_names if plan not in set(deal_items)]
        if add_on_candidates:
            follow_sentences.append(f"等客户看到首轮效果后，再自然承接{_natural_join(add_on_candidates[:2])}等附加方案，避免一次性推得过满")
    else:
        follow_sentences.extend(treatment_suggestion_parts)
        follow_sentences.extend(long_term_parts)
    if relationship_parts:
        follow_sentences.append(f"关系维护上，{_natural_join(relationship_parts[:3])}，可以继续把服务感和专业感做实")
    if deal_status != "已成交":
        follow_sentences.extend(follow_parts)
    elif focus_values:
        follow_sentences.append(f"下一步沟通仍要回应{_natural_join(focus_values[:3])}，把成交后的不确定感提前消化")
    if deal_status == "已成交" and (transcript_signals.get("wechat_follow_up") or "微信" in tag_values.get("倾向回访方式", [])):
        follow_sentences.append("可继续通过微信维护治疗体验、复查节点和复购机会")

    referral_sentences: list[str] = []
    if referral_parts:
        referral_sentences.append(f"{'；'.join(referral_parts)}。后续可以把客户满意度、朋友推荐和福利机制串起来，形成更自然的转介绍入口")

    lines = [
        _summary_paragraph(
            1,
            "客户基础信息",
            background_sentences,
            "录音内未提取到足够的年龄、职业、居住区域、历史医美或身体基础信息",
        ),
        _summary_paragraph(
            2,
            "需求与动机分析",
            demand_sentences,
            "录音内未形成更明确的动机、决策顾虑或核心阻力",
        ),
        _summary_paragraph(
            3,
            "面诊与设计方案",
            plan_sentences,
            "录音内没有形成清晰的面诊角色、推荐方案或客户反馈",
        ),
        _summary_paragraph(
            4,
            "报价与成交策略",
            pricing_sentences,
            "录音内没有提取到明确报价、价格策略或客户接受度",
        ),
        _summary_paragraph(
            5,
            "客户画像与标签",
            profile_sentences,
            "除前述诉求和顾虑外，暂未提取到更稳定的客户类型或信任偏好",
        ),
        _summary_paragraph(
            6,
            "后续跟进规划",
            follow_sentences,
            "建议继续确认客户对方案、价格、治疗时间和效果预期的接受度",
        ),
        _summary_paragraph(
            7,
            "老带新提及",
            referral_sentences,
            "本次沟通未明确出现老带新开口、机制宣教或主动套电动作",
        ),
    ]
    return "\n".join(lines)


def build_consultation_text(
    advisor_name: str,
    result: dict,
    visit_order: VisitOrder | None = None,
    transcript_full_text: str | None = None,
    transcript_utterances: list[dict] | None = None,
    sap_summary_config: SapSummaryTemplateConfig | None = None,
) -> str:
    """
    按生产回传口径拼装咨询备注：
    ●备注人员
    ●顾客主诉
    ●本次预算
    ●顾客顾虑
    ●推荐方案
    ●种草方案
    ●未成交原因（仅当到诊单最终状态为未成交）
    ●总结信息
    """
    lines = [f"●备注人员：{_text_or_none(advisor_name)}"]
    lines.append(_format_sap_multiline_field("顾客主诉", _collect_primary_demand_items(result)))
    lines.append(f"●本次预算：{_text_or_none(_collect_budget_text(result))}")
    lines.append(_format_sap_multiline_field("顾客顾虑", _collect_concern_items(result)))
    recommendation_items = _collect_recommendation_items(result)
    if not recommendation_items:
        recommendation_items = _collect_transcript_price_quote_recommendation_items(
            transcript_full_text,
            transcript_utterances,
        )
    lines.append(_format_sap_multiline_field("推荐方案", recommendation_items))
    lines.append(_format_sap_multiline_field("种草方案", _collect_seed_recommendation_items(result)))
    if _is_visit_order_final_not_deal(visit_order):
        lines.append(_format_sap_multiline_field("未成交原因", _collect_loss_reason_items(result)))
    if _is_sap_summary_section_enabled(visit_order, sap_summary_config):
        summary_text = _format_sap_summary_text(
            _collect_summary_text(
                result,
                visit_order,
                transcript_full_text,
                transcript_utterances,
                advisor_name,
                sap_summary_config,
            )
        )
        lines.append("\u25cf\u603b\u7ed3\u4fe1\u606f\uff1a\n" f"{_text_or_none(summary_text)}")

    return "\n\n".join(lines)


def _normalize_date_token(value: str | None) -> str:
    if not value:
        return ""
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return ""


def _normalize_time_token(value: str | None) -> str:
    if not value:
        return ""
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) >= 6:
        return digits[:6]
    if len(digits) == 4:
        return f"{digits}00"
    return ""


def _resolve_consultation_date_time(
    recording: Recording,
    visit: Visit,
    visit_order: VisitOrder,
) -> tuple[str, str]:
    if recording.created_at:
        recorded_at = recording.created_at
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=timezone.utc)
        recorded_at = recorded_at.astimezone(CN_TZ)
        return recorded_at.strftime("%Y%m%d"), recorded_at.strftime("%H%M%S")

    if visit.visit_date:
        visit_date = visit.visit_date.strftime("%Y%m%d")
        visit_time = _normalize_time_token(visit.visit_time)
        if visit_time:
            return visit_date, visit_time

    for date_value, time_value in (
        (visit_order.jzrq, visit_order.jzsj),
        (visit_order.fzrq, visit_order.fzsj),
        (visit_order.crtdt, visit_order.crttm),
    ):
        normalized_date = _normalize_date_token(date_value)
        normalized_time = _normalize_time_token(time_value)
        if normalized_date and normalized_time:
            return normalized_date, normalized_time

    if visit.visit_date:
        return visit.visit_date.strftime("%Y%m%d"), "000000"

    return "", ""


def _resolve_rfc_field_overrides(visit_order: VisitOrder) -> dict[str, str]:
    settings = get_settings()
    zxdh = settings.sap_rfc_override_zxdh.strip()
    mode = (settings.sap_rfc_mode.strip() or "C").upper()
    if mode == "U" and not zxdh:
        # ZXDH is learned from SAP when an initial create attempt reports an
        # existing consultation order. Without it, start with create mode so the
        # push service can extract the real ZXDH and retry in update mode.
        mode = "C"
    return {
        "user": settings.sap_rfc_override_user.strip() or visit_order.advxc or visit_order.fzuer or "",
        "advxc": settings.sap_rfc_override_advxc.strip() or visit_order.advxc or visit_order.fzuer or "",
        "jgbm": visit_order.jgbm or "",
        "kunr": settings.sap_rfc_override_kunr.strip() or visit_order.kunr or "",
        "mode": mode,
        "zxdh": zxdh,
    }


def _strip_embedded_sap_preview(result: dict | None) -> dict:
    payload = deepcopy(result or {})
    payload.pop(SAP_CONSULTATION_PREVIEW_RESULT_KEY, None)
    return payload


def _build_sap_indication_rows(result: dict) -> list[dict[str, str]]:
    si = result.get("standardized_indications", {})
    indication_items = si.get("items", []) if isinstance(si, dict) else []

    tab_syz: list[dict[str, str]] = []
    seen_syz: set[tuple[str, str, str]] = set()
    for item in indication_items:
        if not isinstance(item, dict):
            continue
        department_code = str(item.get("department_code") or "").strip()
        indication_code = str(item.get("indication_code") or "").strip()
        body_part_code = str(item.get("body_part_code") or "").strip()
        if not (department_code or indication_code or body_part_code):
            continue
        dedupe_key = (department_code, indication_code, body_part_code)
        if dedupe_key in seen_syz:
            continue
        seen_syz.add(dedupe_key)
        tab_syz.append(
            {
                "CCKS": department_code,
                "CCSYZ": indication_code,
                "CCBW": body_part_code,
            }
        )
    return tab_syz


def _resolve_recording_date_time(recording: Recording | None) -> tuple[str, str]:
    if recording is None or not recording.created_at:
        return "", ""
    recorded_at = recording.created_at
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)
    recorded_at = recorded_at.astimezone(CN_TZ)
    return recorded_at.strftime("%Y%m%d"), recorded_at.strftime("%H%M%S")


def _recording_staff_name(recording: Recording | None) -> str:
    if recording is None:
        return ""
    # Only read the relationship when it was eagerly loaded; async lazy loading
    # from a formatter can fail outside greenlet context.
    staff = recording.__dict__.get("staff")
    if isinstance(staff, Staff):
        return str(staff.name or "").strip()
    return ""


def build_unlinked_sap_preview_payload(
    recording: Recording | None,
    result: dict,
    transcript_full_text: str | None = None,
    transcript_utterances: list[dict] | None = None,
    sap_summary_config: SapSummaryTemplateConfig | None = None,
) -> dict:
    result_payload = _normalize_result_payload(_strip_embedded_sap_preview(result))
    text = build_consultation_text(
        _recording_staff_name(recording),
        result_payload,
        visit_order=None,
        transcript_full_text=transcript_full_text,
        transcript_utterances=transcript_utterances,
        sap_summary_config=sap_summary_config,
    )
    consultation_date, consultation_time = _resolve_recording_date_time(recording)
    settings = get_settings()
    mode = settings.sap_rfc_mode.strip() or "C"
    tab_syz = _build_sap_indication_rows(result_payload)
    return {
        "text": text,
        "user": "",
        "zxxx": {
            "JGBM": "",
            "kunr": "",
            "advxc": "",
            "wbtyp": "MX",
            "mode": mode,
            "zxdh": settings.sap_rfc_override_zxdh.strip() if mode == "U" else "",
            "fzdh": "",
            "ZXRQ": consultation_date,
            "ZXSJ": consultation_time,
            "ZXMD": "A",
            "MZYS": "",
            "WMZYY": "",
            "FLG_NEW_SYZ": "X" if tab_syz else "",
        },
        "TAB_SYZ": tab_syz,
    }


def build_sap_payload(
    visit_order: VisitOrder,
    advisor_name: str,
    result: dict,
    consultation_date: str,
    consultation_time: str,
    transcript_full_text: str | None = None,
    transcript_utterances: list[dict] | None = None,
    sap_summary_config: SapSummaryTemplateConfig | None = None,
    consultation_text_override: str | None = None,
) -> dict:
    """
    按接口文档生成单条 SAP 咨询单 payload。
    TAB_SYZ 中可包含多条适应症，仅保留文档要求的编码字段。
    """
    text = str(consultation_text_override or "").strip()
    if not text:
        text = build_consultation_text(
            advisor_name,
            result,
            visit_order=visit_order,
            transcript_full_text=transcript_full_text,
            transcript_utterances=transcript_utterances,
            sap_summary_config=sap_summary_config,
        )
    field_overrides = _resolve_rfc_field_overrides(visit_order)

    tab_syz = _build_sap_indication_rows(result)

    return {
        "text": text,
        "user": field_overrides["user"],
        "zxxx": {
            "JGBM": field_overrides["jgbm"],
            "kunr": field_overrides["kunr"],
            "advxc": field_overrides["advxc"],
            "wbtyp": "MX",
            "mode": field_overrides["mode"],
            "zxdh": field_overrides["zxdh"] if field_overrides["mode"] == "U" else "",
            "fzdh": visit_order.fzdh or f"{visit_order.dzdh}-{visit_order.dzseg or ''}",
            "ZXRQ": consultation_date,
            "ZXSJ": consultation_time,
            "ZXMD": "A",
            "MZYS": visit_order.yyuer or "",
            "WMZYY": "",
            "FLG_NEW_SYZ": "X" if tab_syz else "",
        },
        "TAB_SYZ": tab_syz,
    }


def _extract_sap_preview_text(result: dict | None, recording: Recording | None = None) -> str:
    if not isinstance(result, dict):
        return ""
    preview = result.get(SAP_CONSULTATION_PREVIEW_RESULT_KEY)
    if not isinstance(preview, dict):
        return ""
    payloads = preview.get("payloads")
    if not isinstance(payloads, list):
        return ""
    for item in payloads:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if "●接诊人员" in text:
            return ""
        staff_name = _recording_staff_name(recording)
        if staff_name and text.startswith("●备注人员：无"):
            text = re.sub(r"^●备注人员：[^\n]*", f"●备注人员：{staff_name}", text, count=1).strip()
        return _format_sap_multiline_fields_in_text(text)
    return ""


def _build_recording_consultation_text_for_visit(
    context: dict,
    *,
    sap_summary_config: SapSummaryTemplateConfig | None = None,
) -> str:
    result = context.get("result") if isinstance(context.get("result"), dict) else {}
    recording = context.get("recording")
    recording_obj = recording if isinstance(recording, Recording) else None

    preview_text = _extract_sap_preview_text(result, recording_obj)
    if preview_text:
        if not _is_sap_summary_section_enabled(None, sap_summary_config):
            return _strip_sap_summary_section(preview_text)
        return preview_text

    return build_consultation_text(
        _recording_staff_name(recording_obj),
        result,
        visit_order=None,
        transcript_full_text=str(context.get("transcript_full_text") or "") or None,
        transcript_utterances=context.get("transcript_utterances") if isinstance(context.get("transcript_utterances"), list) else None,
        sap_summary_config=sap_summary_config,
    ).strip()


def _build_multi_recording_consultation_text(
    contexts: list[dict],
    *,
    sap_summary_config: SapSummaryTemplateConfig | None = None,
) -> str:
    blocks = [
        _build_recording_consultation_text_for_visit(
            context,
            sap_summary_config=sap_summary_config,
        )
        for context in contexts
    ]
    return "\n\n".join(block for block in blocks if block.strip()).strip()


def _sanitize_review_editable_body(value: str, *, include_summary: bool = True) -> str:
    lines: list[str] = []
    skipping_summary = False
    for raw_line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("●备注人员") or stripped.startswith("●接诊人员"):
            continue
        if not include_summary:
            if re.match(r"^●\s*总结信息\s*[：:]", stripped):
                skipping_summary = True
                continue
            if skipping_summary and re.match(r"^●\s*[^：:\n]+?\s*[：:]", stripped):
                skipping_summary = False
            if skipping_summary:
                continue
        lines.append(raw_line.rstrip())
    return "\n".join(lines).strip()


def _split_review_consultation_block(text: str, staff_name: str) -> tuple[str, str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    locked_header = f"●备注人员：{staff_name or '无'}"
    if not normalized:
        return locked_header, ""
    lines = normalized.splitlines()
    first = lines[0].strip() if lines else ""
    if first.startswith("●备注人员") or first.startswith("●接诊人员"):
        return locked_header, _sanitize_review_editable_body("\n".join(lines[1:]))
    return locked_header, _sanitize_review_editable_body(normalized)


def _compose_review_consultation_text(blocks: list[dict], *, generated: bool = False) -> str:
    parts: list[str] = []
    for block in sorted(blocks, key=lambda item: int(item.get("sort_index") or 0)):
        header = str(block.get("locked_header") or "").strip()
        body_key = "generated_body" if generated else "effective_body"
        body = str(block.get(body_key) or "").strip()
        if not header and not body:
            continue
        parts.append("\n".join(part for part in (header, body) if part).strip())
    return "\n\n".join(parts).strip()


async def _sync_review_effective_consultation_text(
    db: AsyncSession,
    visit_id: str | None,
    contexts: list[dict],
    *,
    sap_summary_config: SapSummaryTemplateConfig | None = None,
) -> str:
    normalized_visit_id = str(visit_id or "").strip()
    if not normalized_visit_id:
        return ""
    review = (
        await db.execute(
            select(SapConsultationReview).where(SapConsultationReview.visit_id == normalized_visit_id)
        )
    ).scalar_one_or_none()
    if review is None:
        return ""

    existing_blocks = {
        str(block.get("recording_id") or ""): block
        for block in (review.blocks if isinstance(review.blocks, list) else [])
        if isinstance(block, dict)
    }

    blocks: list[dict] = []
    for index, context in enumerate(contexts, start=1):
        recording = context.get("recording")
        if not isinstance(recording, Recording):
            continue
        staff_name = _recording_staff_name(recording) or "无"
        block_text = _build_recording_consultation_text_for_visit(
            context,
            sap_summary_config=sap_summary_config,
        )
        locked_header, generated_body = _split_review_consultation_block(block_text, staff_name)
        previous = existing_blocks.get(recording.id) or {}
        summary_enabled = _is_sap_summary_section_enabled(None, sap_summary_config)
        edited_body = _sanitize_review_editable_body(
            str(previous.get("edited_body") or ""),
            include_summary=summary_enabled,
        )
        blocks.append(
            {
                "recording_id": recording.id,
                "file_name": recording.file_name,
                "staff_id": recording.staff_id,
                "staff_name": staff_name,
                "sap_summary_enabled": summary_enabled,
                "locked_header": locked_header,
                "generated_body": generated_body,
                "edited_body": edited_body or None,
                "effective_body": edited_body or generated_body,
                "sort_index": index,
            }
        )

    if not blocks:
        return str(review.effective_text or "").strip()

    generated_text = _compose_review_consultation_text(blocks, generated=True)
    effective_text = _compose_review_consultation_text(blocks, generated=False)
    recording_ids = [str(block["recording_id"]) for block in blocks]
    changed = (
        list(review.recording_ids or []) != recording_ids
        or list(review.blocks or []) != blocks
        or str(review.generated_text or "") != generated_text
        or str(review.effective_text or "") != effective_text
    )
    if changed:
        review.recording_ids = recording_ids
        review.blocks = blocks
        review.generated_text = generated_text
        review.effective_text = effective_text
        if any(block.get("edited_body") for block in blocks):
            review.status = "modified"
        elif review.status not in {"modified", "sending", "queued"}:
            review.status = "pending"
        await db.flush()

    return effective_text


async def _load_review_effective_consultation_text(db: AsyncSession, visit_id: str | None) -> str:
    normalized_visit_id = str(visit_id or "").strip()
    if not normalized_visit_id:
        return ""
    review = (
        await db.execute(
            select(SapConsultationReview.effective_text).where(SapConsultationReview.visit_id == normalized_visit_id)
        )
    ).scalar_one_or_none()
    return str(review or "").strip()


def _visit_order_lookup_key(dzdh: str | None, dzseg: str | None) -> tuple[str, str | None] | None:
    visit_order_no = str(dzdh or "").strip()
    if not visit_order_no:
        return None
    visit_order_seg = str(dzseg or "").strip() or None
    return visit_order_no, visit_order_seg


def _normalize_result_payload(result: dict | None) -> dict:
    payload = dict(result or {})
    if isinstance(payload.get("standardized_indications"), dict):
        payload["standardized_indications"] = normalize_standardized_indications_payload(
            payload["standardized_indications"]
        )
    return payload


def _is_staged_analysis_result(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    debug = result.get("staged_pipeline_debug")
    if not isinstance(debug, dict):
        return False
    chain = str(debug.get("production_chain") or "")
    return chain.startswith("staged")


def _load_recording_analysis_raw(recording: Recording) -> dict | None:
    settings = get_settings()
    input_path = settings.upload_path / "analysis_input" / f"recording_{recording.id}.json"
    if input_path.exists():
        try:
            return json.loads(input_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("failed to load analysis input for SAP sanitization recording_id=%s: %s", recording.id, exc)
    if recording.transcript and recording.transcript.utterances:
        return {"utterances": recording.transcript.utterances}
    return None


def _collect_tag_items(result: dict) -> list[dict[str, str]]:
    consultation_result = result.get("consultation_result", {}) if isinstance(result.get("consultation_result"), dict) else {}
    profile = consultation_result.get("customer_profile_summary", {})
    if not isinstance(profile, dict):
        return []
    values: list[dict[str, str]] = []
    for item in profile.get("tags", []) or []:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip()
        value = str(item.get("value") or "").strip()
        if category or value:
            values.append({"category": category, "value": value})
    return values


def _merge_tag_items(results: list[dict]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    merged: list[dict[str, str]] = []
    for result in results:
        for item in _collect_tag_items(result):
            key = (item.get("category", ""), item.get("value", ""))
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _merge_standardized_indication_items(results: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for result in results:
        standardized = result.get("standardized_indications", {})
        if not isinstance(standardized, dict):
            continue
        for item in standardized.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("department_code") or "").strip(),
                str(item.get("indication_code") or "").strip(),
                str(item.get("body_part_code") or "").strip(),
            )
            if not any(key) or key in seen:
                continue
            seen.add(key)
            merged.append(dict(item))
    return merged


def _merge_deal_status(results: list[dict]) -> str:
    statuses = [str(_collect_deal_outcome(result).get("status") or "").strip() for result in results]
    for status in reversed(statuses):
        if status in {"已成交", "未成交"}:
            return status
    return next((status for status in reversed(statuses) if status), "未明确")


def _recent_non_empty_values(values: list[str], *, limit: int | None = None) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in reversed(values):
        text = str(value or "").strip()
        if not text or text in {"无", "暂无", "未明确", "-", "null", "None"} or text in seen:
            continue
        seen.add(text)
        merged.append(text)
        if limit is not None and len(merged) >= limit:
            break
    return list(reversed(merged))


def _latest_non_empty_value(values: list[str]) -> str:
    recent_values = _recent_non_empty_values(values, limit=1)
    return recent_values[0] if recent_values else ""


def _merge_recommendation_plan_items(results: list[dict]) -> list[dict[str, str]]:
    by_key: dict[str, dict[str, str]] = {}
    for result in results:
        for plan, acceptance in _collect_recommendation_acceptance_items(result):
            normalized_plan = str(plan or "").strip()
            if not normalized_plan:
                continue
            key = _recommendation_plan_semantic_key(normalized_plan)
            if key in by_key:
                existing = by_key.pop(key)
                normalized_plan = _prefer_recommendation_plan(existing.get("plan", ""), normalized_plan)
                acceptance = _merge_recommendation_acceptance(existing.get("acceptance", ""), acceptance)
            by_key[key] = {
                "plan": normalized_plan,
                "acceptance": str(acceptance or "").strip() or "未明确回应",
            }
    return list(by_key.values())


def _merge_seed_plan_items(results: list[dict]) -> list[dict[str, str]]:
    by_key: dict[str, dict[str, str]] = {}
    for result in results:
        for plan, acceptance in _collect_seed_recommendation_acceptance_items(result):
            normalized_plan = str(plan or "").strip()
            if not normalized_plan:
                continue
            key = _recommendation_plan_semantic_key(normalized_plan)
            if key in by_key:
                existing = by_key.pop(key)
                normalized_plan = _prefer_recommendation_plan(existing.get("plan", ""), normalized_plan)
                acceptance = _merge_recommendation_acceptance(existing.get("acceptance", ""), acceptance)
            by_key[key] = {
                "plan": normalized_plan,
                "acceptance": str(acceptance or "").strip() or "未明确回应",
            }
    return list(by_key.values())


def _normalize_result_recommendation_plan_items(result: dict) -> dict:
    consultation_result = result.get("consultation_result")
    if not isinstance(consultation_result, dict):
        return result
    recommended_plan = consultation_result.get("recommended_plan")
    if not isinstance(recommended_plan, dict):
        return result
    items = [
        {"plan": plan, "acceptance": acceptance or "未明确回应"}
        for plan, acceptance in _collect_recommendation_acceptance_items(result)
        if str(plan or "").strip()
    ]
    recommended_plan["items"] = items
    return result


def _normalize_result_seed_plan_items(result: dict) -> dict:
    consultation_result = result.get("consultation_result")
    if not isinstance(consultation_result, dict):
        return result
    seed_plan = consultation_result.get("seed_plan")
    if not isinstance(seed_plan, dict):
        return result
    items = [
        {"plan": plan, "acceptance": acceptance or "未明确回应"}
        for plan, acceptance in _collect_seed_recommendation_acceptance_items(result)
        if str(plan or "").strip()
    ]
    seed_plan["items"] = items
    return result


def _result_has_sparse_main_fact_fallback(result: dict, key: str) -> bool:
    payload = result.get(key)
    if not isinstance(payload, dict):
        return False
    return SPARSE_MAIN_FACT_FALLBACK_NOTE in str(payload.get("inference_note") or "")


def _prefer_non_sparse_main_fact_results(results: list[dict], key: str) -> list[dict]:
    strong_results = [result for result in results if not _result_has_sparse_main_fact_fallback(result, key)]
    return strong_results or results


def _merge_analysis_results(results: list[dict]) -> dict:
    normalized_results = [_normalize_result_payload(result) for result in results if isinstance(result, dict)]
    if len(normalized_results) <= 1:
        return normalized_results[0] if normalized_results else {}

    primary_source_results = _prefer_non_sparse_main_fact_results(normalized_results, "customer_primary_demands")
    indication_source_results = _prefer_non_sparse_main_fact_results(normalized_results, "standardized_indications")

    primary_demands = _dedupe_preserve_order(
        [
            demand
            for result in primary_source_results
            for demand in _collect_primary_demand_items(result)
        ]
    )
    indication_texts = _dedupe_preserve_order(
        [
            indication
            for result in indication_source_results
            for indication in _collect_indication_items(result)
        ]
    )
    budget = "；".join(
        _recent_non_empty_values([_collect_budget_text(result) for result in normalized_results], limit=3)
    )
    concerns = _dedupe_preserve_order(
        [
            concern
            for result in normalized_results
            for concern in _collect_concern_items(result)
        ]
    )
    recommendation_items = _merge_recommendation_plan_items(normalized_results)
    seed_items = _merge_seed_plan_items(normalized_results)
    loss_reasons = _dedupe_preserve_order(
        [
            reason
            for result in normalized_results
            for reason in _collect_loss_reason_items(result)
        ]
    )
    deal_items = _dedupe_preserve_order(
        [
            item
            for result in normalized_results
            for item in _collect_deal_items(result)
        ]
    )
    deal_amount = _latest_non_empty_value([_collect_deal_amount(result) for result in normalized_results])
    standardized_items = _merge_standardized_indication_items(indication_source_results)
    tags = _merge_tag_items(normalized_results)
    deal_status = _merge_deal_status(normalized_results)

    return {
        "consultation_result": {
            "chief_complaint_and_indications": {
                "primary_demands": primary_demands,
                "standardized_indications": indication_texts,
            },
            "customer_profile_summary": {
                "tags": tags,
            },
            "deal_factors": {
                "budget": budget,
                "concerns": concerns,
            },
            "recommended_plan": {
                "items": recommendation_items,
            },
            "seed_plan": {
                "items": seed_items,
            },
            "deal_outcome": {
                "status": deal_status,
                "deal_items": deal_items,
                "amount": deal_amount,
                "loss_reasons": loss_reasons,
                "summary": "已按录音时间综合该到诊单关联的多条录音分析结果。",
            },
        },
        "standardized_indications": {
            "items": standardized_items,
        },
        "merged_recording_count": len(normalized_results),
        "visit_level_synthesis": {
            "source": "deterministic_timeline",
            "note": "多条录音按时间顺序合并，后序明确状态覆盖前序待定状态",
        },
    }


def _json_clone(value: dict) -> dict:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _compact_for_visit_result_fusion(value: object, *, depth: int = 0) -> object:
    if depth > 6:
        return None
    if isinstance(value, str):
        text = re.sub(r"\s+", " ", value).strip()
        return text if len(text) <= 500 else f"{text[:240]} ...[省略]... {text[-240:]}"
    if isinstance(value, list):
        compacted_items = []
        for item in value[:16]:
            compacted = _compact_for_visit_result_fusion(item, depth=depth + 1)
            if compacted not in (None, "", [], {}):
                compacted_items.append(compacted)
        return compacted_items
    if isinstance(value, dict):
        compacted_dict: dict[str, object] = {}
        for key, item in value.items():
            if key in {"consultation_evaluation", "consultation_process_evaluation"}:
                continue
            compacted = _compact_for_visit_result_fusion(item, depth=depth + 1)
            if compacted not in (None, "", [], {}):
                compacted_dict[str(key)] = compacted
        return compacted_dict
    return value


def _analysis_result_for_visit_fusion(result: dict) -> dict:
    normalized = _normalize_result_payload(result)
    selected = {
        "customer_primary_demands": normalized.get("customer_primary_demands"),
        "standardized_indications": normalized.get("standardized_indications"),
        "consumption_intent": normalized.get("consumption_intent"),
        "customer_demands": normalized.get("customer_demands"),
        "customer_concerns": normalized.get("customer_concerns"),
        "customer_profile": normalized.get("customer_profile"),
        "staff_recommendations": normalized.get("staff_recommendations"),
        "staff_seed_recommendations": normalized.get("staff_seed_recommendations"),
        "consultation_result": normalized.get("consultation_result"),
        "sap_summary_materials": normalized.get("sap_summary_materials"),
    }
    compacted = _compact_for_visit_result_fusion(selected)
    return compacted if isinstance(compacted, dict) else {}


def _build_visit_result_fusion_prompt(
    contexts: list[dict],
    fallback: dict,
    visit_order: VisitOrder | None,
) -> str:
    fallback_indications = (
        (fallback.get("standardized_indications") or {}).get("items", [])
        if isinstance(fallback.get("standardized_indications"), dict)
        else []
    )
    recordings: list[dict[str, object]] = []
    for index, context in enumerate(contexts, 1):
        recording = context.get("recording")
        recordings.append(
            {
                "recording_index": index,
                "recording_id": getattr(recording, "id", None),
                "file_name": getattr(recording, "file_name", None),
                "created_at": (
                    recording.created_at.isoformat()
                    if isinstance(recording, Recording) and recording.created_at
                    else None
                ),
                "analysis_result": _analysis_result_for_visit_fusion(context.get("result") or {}),
            }
        )

    payload = {
        "visit_order": {
            "visit_order_no": getattr(visit_order, "dzdh", None),
            "visit_order_seg": getattr(visit_order, "dzseg", None),
            "customer_name": getattr(visit_order, "ninam", None),
            "customer_code": getattr(visit_order, "kunr", None),
            "order_status_code": getattr(visit_order, "jcsta", None),
            "order_status_text": getattr(visit_order, "jcsta_txt", None),
        },
        "allowed_standardized_indications": _compact_for_visit_result_fusion(fallback_indications),
        "timeline_merge_reference": _compact_for_visit_result_fusion(fallback),
        "recordings": recordings,
    }
    return "请融合以下同一到诊单的多条录音面诊分析结果，只输出 JSON：\n" + json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _indication_key(item: dict) -> tuple[str, str, str]:
    return (
        str(item.get("department_code") or "").strip(),
        str(item.get("indication_code") or "").strip(),
        str(item.get("body_part_code") or "").strip(),
    )


def _resolve_fused_indications(fallback: dict, fused: dict) -> dict:
    fallback_payload = fallback.get("standardized_indications")
    fallback_items = fallback_payload.get("items", []) if isinstance(fallback_payload, dict) else []
    allowed_by_key = {
        _indication_key(item): dict(item)
        for item in fallback_items
        if isinstance(item, dict) and any(_indication_key(item))
    }
    if not allowed_by_key:
        return {"items": []}

    fused_payload = fused.get("standardized_indications")
    if not isinstance(fused_payload, dict) or "items" not in fused_payload:
        return {"items": list(allowed_by_key.values())}

    fused_items = fused_payload.get("items", [])
    if not isinstance(fused_items, list):
        return {"items": list(allowed_by_key.values())}
    if not fused_items:
        return {"items": []}

    selected: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for item in fused_items:
        if not isinstance(item, dict):
            continue
        key = _indication_key(item)
        if key not in allowed_by_key or key in seen:
            continue
        seen.add(key)
        selected.append(allowed_by_key[key])
    return {"items": selected or list(allowed_by_key.values())}


def _merge_present_dict_fields(target: dict, source: dict) -> dict:
    merged = _json_clone(target)
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_present_dict_fields(merged[key], value)
        else:
            merged[key] = value
    return merged


def _apply_visit_result_fusion(fallback: dict, fused: dict) -> dict:
    merged = _json_clone(fallback)
    if isinstance(fused.get("consultation_result"), dict):
        base_consultation = merged.get("consultation_result")
        if not isinstance(base_consultation, dict):
            base_consultation = {}
        merged["consultation_result"] = _merge_present_dict_fields(base_consultation, fused["consultation_result"])
    if isinstance(fused.get("sap_summary_materials"), dict):
        merged["sap_summary_materials"] = fused["sap_summary_materials"]

    merged["standardized_indications"] = _resolve_fused_indications(fallback, fused)
    indication_texts = _collect_indication_items({"standardized_indications": merged["standardized_indications"]})
    consultation_result = merged.setdefault("consultation_result", {})
    chief = consultation_result.setdefault("chief_complaint_and_indications", {})
    if isinstance(chief, dict):
        chief["standardized_indications"] = indication_texts
    merged["merged_recording_count"] = fallback.get("merged_recording_count")
    merged["visit_level_synthesis"] = {
        "source": "llm_result_fusion",
        "note": "已基于同一到诊单多条录音的既有面诊分析结果做融合分析",
    }
    _normalize_result_recommendation_plan_items(merged)
    _normalize_result_seed_plan_items(merged)
    return _normalize_result_payload(merged)


async def _synthesize_visit_analysis_results(
    contexts: list[dict],
    visit_order: VisitOrder | None,
) -> dict:
    fallback = _merge_analysis_results([context["result"] for context in contexts])
    if len(contexts) <= 1:
        return fallback

    user_prompt = _build_visit_result_fusion_prompt(contexts, fallback, visit_order)
    logger.info(
        "Fusing visit-level analysis results for SAP: recordings=%d system=%d user=%d total=%d chars",
        len(contexts),
        len(_VISIT_RESULT_FUSION_SYSTEM_PROMPT),
        len(user_prompt),
        len(_VISIT_RESULT_FUSION_SYSTEM_PROMPT) + len(user_prompt),
    )
    try:
        response_text = await asyncio.to_thread(
            chat_completion,
            _VISIT_RESULT_FUSION_SYSTEM_PROMPT,
            user_prompt,
            temperature=0.1,
            max_tokens=6000,
        )
        fused = parse_json_response(response_text)
    except Exception as exc:
        logger.warning("visit-level analysis result fusion failed, fallback to timeline merge: %s", exc)
        return fallback

    if not isinstance(fused, dict):
        return fallback
    return _apply_visit_result_fusion(fallback, fused)


async def _load_latest_base_analysis_task(db: AsyncSession, recording_id: str) -> AnalysisTask | None:
    analysis_file_name = f"recording_{recording_id}.json"
    return (
        await db.execute(
            select(AnalysisTask)
            .where(AnalysisTask.file_name == analysis_file_name, AnalysisTask.status == "done")
            .order_by(AnalysisTask.created_at.desc())
        )
    ).scalars().first()


async def _load_recording_for_unlinked_preview(db: AsyncSession, recording_id: str) -> Recording | None:
    return (
        await db.execute(
            select(Recording)
            .where(Recording.id == recording_id)
            .options(selectinload(Recording.transcript))
            .options(selectinload(Recording.staff))
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _load_unlinked_preview_summary_config(
    db: AsyncSession,
    recording: Recording,
) -> SapSummaryTemplateConfig | None:
    if not recording.staff_id:
        return None
    hospital_code = (
        await db.execute(
            select(Staff.hospital_code)
            .where(Staff.id == recording.staff_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    normalized = str(hospital_code or "").strip()
    if not normalized:
        return None
    return await _load_sap_summary_template_config(db, normalized)


def _build_unlinked_sap_preview_response(recording: Recording, payload: dict) -> dict:
    return {
        "recording_id": recording.id,
        "visit_order_no": "",
        "visit_order_seg": None,
        "customer_name": "",
        "customer_code": "",
        "advisor_name": _recording_staff_name(recording),
        "indication_count": len(payload.get("TAB_SYZ") or []),
        "recording_count": 1,
        "target_count": 0,
        "targets": [],
        "payloads": [payload],
    }


async def build_unlinked_sap_preview_for_recording(
    db: AsyncSession,
    recording: Recording,
    result: dict,
) -> dict:
    raw_payload = _load_recording_analysis_raw(recording)
    result_payload = _strip_embedded_sap_preview(result)
    if raw_payload and not _is_staged_analysis_result(result_payload):
        sanitize_analysis_result_with_raw(result_payload, raw=raw_payload)
    result_payload = _normalize_result_payload(result_payload)
    summary_config = await _load_unlinked_preview_summary_config(db, recording)
    payload = build_unlinked_sap_preview_payload(
        recording,
        result_payload,
        transcript_full_text=recording.transcript.full_text if recording.transcript else None,
        transcript_utterances=recording.transcript.utterances if recording.transcript else None,
        sap_summary_config=summary_config,
    )
    return _build_unlinked_sap_preview_response(recording, payload)


async def attach_unlinked_sap_preview_to_result(
    db: AsyncSession,
    recording_id: str | None,
    result: dict | None,
) -> dict | None:
    if not recording_id or not isinstance(result, dict):
        return result
    recording = await _load_recording_for_unlinked_preview(db, recording_id)
    if recording is None:
        return result
    enriched = _strip_embedded_sap_preview(result)
    preview = await build_unlinked_sap_preview_for_recording(db, recording, enriched)
    enriched[SAP_CONSULTATION_PREVIEW_RESULT_KEY] = preview
    return enriched


async def _generate_unlinked_sap_preview_payloads(
    db: AsyncSession,
    recording: Recording,
) -> dict:
    task = await _load_latest_base_analysis_task(db, recording.id)
    if task is None or not task.result:
        return {
            "error": "no_analysis",
            "message": f"录音 {recording.file_name or recording.id} 暂无可用于 SAP 预览的分析结果",
        }
    return await build_unlinked_sap_preview_for_recording(db, recording, dict(task.result))


async def _load_visit_recording_contexts(db: AsyncSession, visit_id: str) -> tuple[list[dict], dict | None]:
    recordings = (
        await db.execute(
            select(Recording)
            .join(RecordingVisitLink, RecordingVisitLink.recording_id == Recording.id)
            .where(RecordingVisitLink.visit_id == visit_id, Recording.status != "filtered")
            .options(
                selectinload(Recording.transcript),
                selectinload(Recording.staff),
                selectinload(Recording.visit_links),
            )
            .order_by(Recording.created_at.asc(), Recording.id.asc())
            .execution_options(populate_existing=True)
        )
    ).scalars().unique().all()

    contexts: list[dict] = []
    for linked_recording in recordings:
        linked_visit_ids = {
            link.visit_id
            for link in linked_recording.visit_links
            if str(link.visit_id or "").strip()
        }
        result_payload: dict | None = None
        if len(linked_visit_ids) > 1:
            scoped = (
                await db.execute(
                    select(RecordingVisitAnalysis).where(
                        RecordingVisitAnalysis.recording_id == linked_recording.id,
                        RecordingVisitAnalysis.visit_id == visit_id,
                    )
                )
            ).scalar_one_or_none()
            if scoped is not None and scoped.mapping_status == "confirmed":
                if scoped.analysis_status != "done" or not scoped.analysis_result:
                    return [], {
                        "error": "multi_customer_analysis_pending",
                        "message": "该到诊单关联的多客户录音尚未全部完成到诊单级分析",
                    }
                result_payload = dict(scoped.analysis_result)

        if result_payload is None:
            task = await _load_latest_base_analysis_task(db, linked_recording.id)
            if task is None or not task.result:
                return [], {
                    "error": "no_analysis",
                    "message": f"到诊单关联的录音 {linked_recording.file_name or linked_recording.id} 尚无已完成的分析结果",
                }
            result_payload = dict(task.result)
            raw_payload = _load_recording_analysis_raw(linked_recording)
            if raw_payload and not _is_staged_analysis_result(result_payload):
                sanitize_analysis_result_with_raw(result_payload, raw=raw_payload)

        contexts.append(
            {
                "recording": linked_recording,
                # Visit-level SAP content must be regenerated from the latest
                # analysis facts.  A recording can carry an older unlinked
                # preview snapshot, and using it here would keep stale remarks
                # after the analysis result has been repaired.
                "result": _normalize_result_payload(_strip_embedded_sap_preview(result_payload)),
                "transcript_full_text": linked_recording.transcript.full_text if linked_recording.transcript else None,
                "transcript_utterances": linked_recording.transcript.utterances if linked_recording.transcript else None,
            }
        )

    if not contexts:
        return [], {"error": "no_analysis", "message": "该到诊单尚无可用于回传的已分析录音"}
    return contexts, None


def _merge_transcript_full_text(contexts: list[dict]) -> str | None:
    parts: list[str] = []
    for index, context in enumerate(contexts, start=1):
        recording = context.get("recording")
        file_name = recording.file_name if isinstance(recording, Recording) else f"录音{index}"
        full_text = str(context.get("transcript_full_text") or "").strip()
        if full_text:
            parts.append(f"【录音{index}：{file_name}】\n{full_text}")
    return "\n\n".join(parts) if parts else None


def _merge_transcript_utterances(contexts: list[dict]) -> list[dict] | None:
    utterances: list[dict] = []
    for index, context in enumerate(contexts, start=1):
        recording = context.get("recording")
        recording_id = recording.id if isinstance(recording, Recording) else None
        for item in context.get("transcript_utterances") or []:
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            copied["source_recording_index"] = index
            if recording_id:
                copied["source_recording_id"] = recording_id
            utterances.append(copied)
    return utterances or None


async def generate_sap_consultation_payloads(
    db: AsyncSession,
    recording_id: str,
    target_visit_id: str | None = None,
    allow_unlinked_preview: bool = False,
) -> dict:
    """
    主入口：根据录音 ID 生成 SAP 咨询单回传数据。

    返回结构：
    {
        "recording_id": ...,
        "visit_order_no": ...,  # 主关联到诊单摘要
        "customer_name": ...,
        "indication_count": ...,
        "target_count": ...,
        "targets": [...],       # 所有关联到诊单的逐条目标
        "payloads": [...],
    }
    """
    # 1. 加载录音及其关联的 visit
    recording = (
        await db.execute(
            select(Recording)
            .where(Recording.id == recording_id)
            .options(
                selectinload(Recording.transcript),
                selectinload(Recording.staff),
                selectinload(Recording.visit_links)
                .selectinload(RecordingVisitLink.visit),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if recording is None:
        return {"error": "recording_not_found", "message": "录音不存在"}

    # 2. 找到关联的 visit（主关联优先）
    links = sorted(recording.visit_links, key=lambda lk: (not lk.is_primary, lk.created_at))
    if not links:
        if allow_unlinked_preview and not target_visit_id:
            return await _generate_unlinked_sap_preview_payloads(db, recording)
        return {"error": "no_visit_linked", "message": "该录音尚未关联到诊单"}

    valid_links: list[tuple[RecordingVisitLink, Visit, str, str | None]] = []
    for link in links:
        visit = link.visit
        if visit is None:
            continue
        ref_key = _visit_order_lookup_key(visit.external_visit_order_no, visit.external_visit_order_seg)
        if ref_key is None:
            continue
        valid_links.append((link, visit, ref_key[0], ref_key[1]))

    if not valid_links:
        if allow_unlinked_preview and not target_visit_id:
            return await _generate_unlinked_sap_preview_payloads(db, recording)
        return {"error": "no_visit_linked", "message": "该录音尚未关联到诊单"}

    # 3. 通过 visit 的 external_visit_order_no 找到 VisitOrder
    conditions = []
    for _, _, visit_order_no, visit_order_seg in valid_links:
        if visit_order_seg:
            conditions.append(and_(VisitOrder.dzdh == visit_order_no, VisitOrder.dzseg == visit_order_seg))
        else:
            conditions.append(and_(VisitOrder.dzdh == visit_order_no, VisitOrder.dzseg.is_(None)))

    visit_orders = (
        await db.execute(select(VisitOrder).where(or_(*conditions)))
    ).scalars().all()
    visit_order_by_key = {
        _visit_order_lookup_key(item.dzdh, item.dzseg): item
        for item in visit_orders
        if _visit_order_lookup_key(item.dzdh, item.dzseg) is not None
    }

    linked_targets: list[tuple[RecordingVisitLink, Visit, VisitOrder]] = []
    for link, visit, visit_order_no, visit_order_seg in valid_links:
        visit_order = visit_order_by_key.get((visit_order_no, visit_order_seg))
        if visit_order is None:
            continue
        linked_targets.append((link, visit, visit_order))

    if not linked_targets:
        if allow_unlinked_preview and not target_visit_id:
            return await _generate_unlinked_sap_preview_payloads(db, recording)
        first_visit_order_no = valid_links[0][2]
        return {"error": "visit_order_not_found", "message": f"到诊单 {first_visit_order_no} 不存在"}

    if target_visit_id:
        linked_targets = [
            (link, visit, visit_order)
            for link, visit, visit_order in linked_targets
            if link.visit_id == target_visit_id
        ]
        if not linked_targets:
            return {"error": "visit_order_not_found", "message": "目标到诊单未关联当前录音"}

    payloads: list[dict] = []
    targets: list[dict[str, str | int | bool | None]] = []
    summary_config_cache: dict[str, SapSummaryTemplateConfig | None] = {}
    for link, visit, visit_order in linked_targets:
        contexts, error = await _load_visit_recording_contexts(db, visit.id)
        if error:
            return error
        result_payload = await _synthesize_visit_analysis_results(contexts, visit_order)
        source_recording = contexts[0]["recording"] if contexts else recording
        advisor_name = _recording_staff_name(recording) or _recording_staff_name(source_recording)
        consultation_date, consultation_time = _resolve_consultation_date_time(source_recording, visit, visit_order)
        hospital_code = str(visit_order.jgbm or "").strip()
        if hospital_code not in summary_config_cache:
            summary_config_cache[hospital_code] = await _load_sap_summary_template_config(db, hospital_code)
        summary_config = summary_config_cache[hospital_code]
        review_text_override = await _sync_review_effective_consultation_text(
            db,
            visit.id,
            contexts,
            sap_summary_config=summary_config,
        )
        consultation_text_override = (
            review_text_override
            or (
                _build_multi_recording_consultation_text(contexts, sap_summary_config=summary_config)
                if len(contexts) > 1
                else None
            )
        )
        payload = build_sap_payload(
            visit_order=visit_order,
            advisor_name=advisor_name,
            result=result_payload,
            consultation_date=consultation_date,
            consultation_time=consultation_time,
            transcript_full_text=_merge_transcript_full_text(contexts),
            transcript_utterances=_merge_transcript_utterances(contexts),
            sap_summary_config=summary_config,
            consultation_text_override=consultation_text_override,
        )
        payloads.append(payload)
        targets.append(
            {
                "visit_id": visit.id,
                "visit_order_no": visit_order.dzdh,
                "visit_order_seg": visit_order.dzseg,
                "customer_name": visit_order.ninam or "",
                "customer_code": visit_order.kunr or "",
                "advisor_name": advisor_name,
                "indication_count": len(payload["TAB_SYZ"]),
                "recording_count": len(contexts),
                "is_primary": bool(link.is_primary),
            }
        )

    primary_target = targets[0]

    return {
        "recording_id": recording_id,
        "visit_order_no": primary_target["visit_order_no"],
        "visit_order_seg": primary_target["visit_order_seg"],
        "customer_name": primary_target["customer_name"],
        "customer_code": primary_target["customer_code"],
        "advisor_name": primary_target["advisor_name"],
        "indication_count": primary_target["indication_count"],
        "recording_count": primary_target["recording_count"],
        "target_count": len(targets),
        "targets": targets,
        "payloads": payloads,
    }
