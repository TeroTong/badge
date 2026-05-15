"""Agent-style backup analysis pipeline.

This module is intentionally not used by the production worker by default.
It runs a higher-token, multi-agent chain for side-by-side comparison against
the current production staged pipeline.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smart_badge_api.analysis.staged_pipeline import (
    STAGED_LLM_MODEL,
    _INDICATION_ADJUDICATION_SYSTEM_PROMPT,
    _INDICATION_ADJUDICATION_USER_TEMPLATE,
    _build_analysis_result_from_fact_graph,
    _build_line_speaker_metadata,
    _build_preprocess_context,
    _call_json,
    _candidate_indications_from_text,
    _clean_text,
    _compact_fact_graph_for_indications,
    _estimate_payload_chars,
    _extract_correction_patch,
    _extract_evidence_graph,
    _extract_fact_graph,
    _extract_indication_adjudication,
    _format_candidate_indications,
    _format_staff_context,
    _first_text,
    _number_dialogue_lines,
    _apply_correction_patch,
    _apply_indication_adjudication,
    _merge_profile_facts_from_evidence_graph,
    _repair_empty_fact_graph_from_evidence_graph,
)
from smart_badge_api.analysis.transcript import prepare_transcript

logger = logging.getLogger(__name__)

PIPELINE_NAME = "agent_pipeline_v2_gpt52"
EVIDENCE_CHUNK_TARGET_CHARS = 12000
EVIDENCE_CHUNK_OVERLAP_LINES = 3


_CORRECTION_AGENT_SYSTEM_PROMPT = """\
You are Agent 1 in a Chinese medical-aesthetic recording analysis chain:
the transcript correction and speaker-role agent.

Task:
1. Correct only high-confidence ASR term mistakes.
2. Correct only clearly wrong speaker roles.
3. Preserve timestamps and original wording as evidence.

Hard rules:
1. Do not summarize the transcript.
2. Do not extract demands, indications, recommendations, or SAP remarks.
3. Do not rewrite whole lines. Return patch operations only.
4. If uncertain, do not correct. Put the concern in uncertain_notes.
5. First infer a stable role for each ASR speaker id in speaker_role_map.
   The same asr_speaker should normally keep one business role across the
   transcript. Use line-level speaker_corrections only for true exceptions
   such as diarization errors, mixed speakers, or a different person taking
   over the same ASR speaker id.
6. Also infer a stable participant label and customer_scope for each ASR
   speaker id. This is critical when two or more customers/companions are
   present. Use participant_label="主咨询客户" for the person whose visit/order
   is being handled, "同行客户A"/"同行客户B" for other people asking about their
   own treatment, "陪同人员" for family/friends speaking about the main customer,
   and staff labels such as "医生", "咨询师", "专家助理", "前台".
   customer_scope must be one of primary_customer, other_customer,
   companion_or_family, staff, unknown.
7. Do not label two different customer speakers simply as "客户" when the
   transcript gives enough evidence to distinguish main customer vs同行客户.
   If unsure which customer is primary, choose the best-supported primary and
   add an uncertain_notes item.
8. Speaker must be one of:
   customer, companion, consultant, doctor, expert_assistant, frontdesk,
   staff_peer, other.
9. A person self-identifying as expert assistant / doctor assistant /
   dean assistant must not be labeled doctor.
10. Professional explanation alone does not prove doctor; consultants and
   expert assistants can explain anatomy, plans, dosage, risks, and prices.
11. Customer speech usually contains personal goals, feelings, questions,
   consent/refusal, hesitation, budget or price concerns.
12. Correct medical-aesthetic terms only when context is strong, especially:
   瑞德喜, 艾维岚, 艾拉斯提, 贝丽菲尔, 双美胶原蛋白, 玻尿酸, 胶原蛋白,
   肉毒, 除皱针, 溶解酶, 眶外C线, 眉弓线, 额颞, 鼻基底, 泪沟,
   妈生鼻, 黑曜双波, 黄金微针, 富贵包, 副乳.

Return JSON only:
{
  "correction_patch": {
    "speaker_role_map": [
      {
        "asr_speaker": "speaker_0",
        "role": "customer|companion|consultant|doctor|expert_assistant|frontdesk|staff_peer|other",
        "participant_label": "主咨询客户|同行客户A|同行客户B|陪同人员|咨询师|医生|专家助理|前台|员工|其他",
        "customer_scope": "primary_customer|other_customer|companion_or_family|staff|unknown",
        "confidence": 0.0,
        "reason": ""
      }
    ],
    "speaker_corrections": [
      {
        "line_id": "L0001",
        "corrected_speaker": "customer|companion|consultant|doctor|expert_assistant|frontdesk|staff_peer|other",
        "participant_label": "",
        "customer_scope": "primary_customer|other_customer|companion_or_family|staff|unknown",
        "confidence": 0.0,
        "reason": ""
      }
    ],
    "term_corrections": [
      {
        "line_id": "L0001",
        "original": "",
        "corrected": "",
        "confidence": 0.0,
        "reason": ""
      }
    ],
    "uncertain_notes": []
  }
}
"""


_CORRECTION_AGENT_USER_TEMPLATE = """\
Staff / recording context:
{staff_context}

Code-side preprocessing hints:
{preprocess_context}

Numbered transcript:
{numbered_dialogue}

Output correction_patch JSON only.
"""


_EVIDENCE_AGENT_SYSTEM_PROMPT = """\
You are Agent 2 in a Chinese medical-aesthetic recording analysis chain:
the evidence extraction agent.

Your job is evidence only. Do not decide final SAP indications and do not
render final analysis_result.

Extraction rules:
1. Keep customer evidence, staff/doctor diagnosis evidence, recommendation
   evidence, seed/next-visit evidence, concerns, budget, price, deal actions,
   and medical history separate.
2. Every useful item must include short original evidence, turn ids/timestamps
   when available, speaker, and confidence.
3. Customer demand evidence must be customer-spoken, customer-confirmed, or a
   staff restatement accepted by the customer.
4. Staff/doctor observations are diagnosis evidence, not customer demand, unless
   customer confirmation is present.
Participant rules:
- When the corrected transcript distinguishes 主咨询客户, 同行客户A/同行客户B, or
  陪同人员, preserve that participant label in every evidence item and add
  participant_scope: primary_customer, other_customer, companion_or_family,
  staff, or unknown.
- Extract evidence for every consulting customer. Independent demands from
  同行客户A/同行客户B must be marked other_customer and kept separate from 主咨询客户,
  because the same recording may later be linked to multiple SAP visit orders.
- Do not merge one customer's demand, concern, budget, recommendation, medical
  history, deal status, or indication support into another customer's facts.
- 陪同人员 may provide supporting information for 主咨询客户, but if the wording is
  about the companion's own treatment need, mark it other_customer.
5. Keep explicit customer-raised deferred/cross-department demands such as 美白,
   毛孔, 痘印, 暗沉, 水光, 光电. Mark handling_status as referral_or_deferred
   when the transcript says it is not handled in this consultation.
6. Recommendation evidence must preserve brand, material, dosage, price, course,
   treatment steps, implementation notes, and customer response when present.
7. Do not promote standalone pre-op checks, postoperative medicine, wound care,
   scar gel, dressing change, or consumables into treatment recommendations.
8. Mark comparison-only, unsuitable, rejected, or non-priority options as
   alternative_not_recommended.
9. For body contouring, preserve 副乳, 富贵包, 手臂, 后背, 腰腹 separately.
10. For skin anti-aging, distinguish 松弛/紧致/抗衰 from 毛孔、痘印、暗沉.
11. Do not infer 鼻综合 from nose-tip/nose-wing pores, blackheads, oil,
    acne, or skin texture without explicit nose contour/surgery/injection plan.
12. Concern evidence must be from customer/companion wording or explicit
    customer confirmation, not staff reassurance alone.
13. Extract budget_evidence aggressively. Any customer-spoken or
    customer-confirmed budget, acceptable price range, quote reaction, "贵",
    "打不起", "能不能便宜/优惠/打折", "最多/顶死", "几千/几万/xxxx元",
    deposit/payment/price-comparison signal must be represented in
    budget_evidence. Keep project quote fields on recommendation_evidence too.
14. Extract profile_evidence for customer labels even when they are not SAP
    indications: prior treatments/materials/devices, current budget, price
    sensitivity, pain tolerance, children/family situation, industry/special
    identity, comparison institution, decision maker, treatment preference,
    recovery/time constraint, and product/project preference. Preserve
    participant/participant_scope and exact evidence.
15. When a recommendation has multiple material/product choices, preserve all
    named choices. Mark the main recommendation and store backup choices in
    implementation_notes instead of dropping them. Example: "双美胶原蛋白"
    can be a backup to "瑞德喜" even when not the main recommendation.
16. Do not create separate customer_demand_evidence for process questions such
    as instrument version/generation, verification, doctor assignment, surgery
    time, incision, recovery, driving, payment, discount, or price only. Attach
    those to concern_evidence, budget_evidence, deal_evidence, or
    implementation_notes unless they also state a concrete body problem/goal.
17. Keep demand evidence concise and normalized: one item per body problem/goal,
    not one item per repeated question.

Return JSON only:
{
  "evidence_graph": {
    "customer_demand_evidence": [
      {
        "id": "E_D1",
        "content": "",
        "body_part": "",
        "speaker": "customer|companion|staff_restated_confirmed",
        "participant": "主咨询客户|同行客户A|同行客户B|陪同人员|unknown",
        "participant_scope": "primary_customer|other_customer|companion_or_family|unknown",
        "handling_status": "current_handled|referral_or_deferred|unclear",
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "diagnosis_evidence": [],
    "recommendation_evidence": [
      {
        "id": "E_R1",
        "content": "",
        "body_part": "",
        "participant": "主咨询客户|同行客户A|同行客户B|unknown",
        "participant_scope": "primary_customer|other_customer|unknown",
        "brand": "",
        "material": "",
        "dosage": "",
        "price": "",
        "course_or_frequency": "",
        "treatment_steps": [],
        "implementation_notes": "",
        "customer_response": "",
        "relation_to_current_demand": "current_main_plan|possible_current_plan|planting_or_later|alternative_not_recommended|auxiliary_or_care|not_current_or_referral|unclear",
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "concern_evidence": [],
    "budget_evidence": [],
    "medical_history_evidence": [],
    "profile_evidence": [
      {
        "id": "E_P1",
        "category": "",
        "value": "",
        "participant": "主咨询客户|同行客户A|同行客户B|陪同人员|unknown",
        "participant_scope": "primary_customer|other_customer|companion_or_family|unknown",
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "deal_evidence": [],
    "speaker_corrections": [],
    "quality_notes": []
  }
}
"""


_EVIDENCE_AGENT_USER_TEMPLATE = """\
Staff / recording context:
{staff_context}

Code-side preprocessing hints:
{preprocess_context}

Corrected transcript:
{dialogue}

Extract evidence_graph JSON only.
"""


_EVIDENCE_AGENT_CHUNK_USER_TEMPLATE = """\
Staff / recording context:
{staff_context}

Code-side preprocessing hints:
{preprocess_context}

This is transcript chunk {chunk_index}/{chunk_count}.
Line range: {line_range}.
The chunk may overlap with adjacent chunks. Extract evidence only from this
chunk and keep line ids in evidence_turn_ids so deterministic merge can dedupe.

Corrected transcript chunk:
{dialogue}

Extract evidence_graph JSON only.
"""


_JUDGMENT_AGENT_SYSTEM_PROMPT = """\
You are Agent 3 in a Chinese medical-aesthetic recording analysis chain:
the structured fact-graph judgment agent.

You receive evidence_graph and candidate indications recalled from the local
SAP indication dictionary. Build a fact_graph. Application code will render
final analysis_result, so do not write final prose.

Judgment rules:
1. Demands: include current customer problems/goals with customer-side evidence
   or customer confirmation. Also keep explicit deferred/referral demands for
   SAP remarks and follow-up, but do not create final SAP indications from them
   unless a current plan supports them.
   When participant_scope is present, build facts for every consulting customer
   and keep participant/participant_scope on each item. Do not convert
   同行客户A/同行客户B independent needs into 主咨询客户 facts. Companion/family speech
   may support the main customer's demand only when it clearly describes the
   main customer rather than the companion's own treatment need.
2. Diagnoses: keep staff/doctor observations separate from demands unless the
   customer confirmed them.
3. Recommendations: plans solving current demands. Link related_demand_ids.
4. Seed recommendations: additional, maintenance, lower-priority, next-visit, or
   outside-current-demand plans. A staged sequence remains a recommendation if it
   is necessary to solve the current demand.
5. Preserve concrete details: brand, material, dosage, price, course, steps,
   implementation notes, and customer_response.
6. Convert all budget_evidence into budget_facts. Do not drop price/discount/
   acceptable-range/deposit/payment evidence merely because it is also inside
   recommendation_evidence. Use concise content such as
   "预算上限约7000-8000元" or "对26800元方案价格敏感，要求优惠".
7. If recommendation_evidence uses a nested "details" style or contains
   multiple named options, the fact_graph recommendation must still expose flat
   fields brand/material/dosage/price/course_or_frequency/treatment_steps/
   implementation_notes/customer_response. Application code renders flat fields.
8. Concerns and deal_factors must be concrete. Avoid vague labels without the
   actual limitation.
9. Indication candidates are preliminary. Copy exact standardized_indication
   strings from candidate_indications only. Prefer high precision over recall.
10. For 副乳, prefer specific 副乳整形 when supported. For 富贵包, keep demand or
   diagnosis unless there is a clear suction/fat-reduction treatment plan.
11. Do not select 痤疮 from mouth-closing wording such as 闭口时/闭上嘴.
12. Do not select 面部除皱 from 咬肌肉毒/瘦脸 unless wrinkle/动态纹/除皱 evidence is explicit.
13. If transcript is internal staff/order/payment discussion without a main
    customer demand or current-customer diagnosis/plan, return empty business
    facts and deal_outcome.status = "未明确".
14. Convert profile_evidence into profile_facts. Also keep profile signals from
    medical_history, budget, concern, and deal evidence when they describe prior
    treatment, material/device, budget, price sensitivity, pain tolerance,
    family/children, industry/special identity, comparison institution,
    decision maker, treatment preference, recovery/time constraint, or product
    preference. These profile_facts are used for customer tags and should not be
    dropped merely because they are not SAP indications.
15. Demand facts must be normalized to the fewest concrete customer goals.
    Merge repeated wording and keep usually 3-6 demands for a single customer.
    Do not output instrument-version, 发数, 医生, 验证, 恢复, 切口, 排期,
    付款, 优惠, or pure price questions as demands; preserve them in concerns,
    budget_facts, deal_factors, or recommendation implementation_notes.

Return JSON only:
{
  "fact_graph": {
    "demands": [],
    "doctor_diagnoses": [],
    "indication_candidates": [],
    "recommendations": [],
    "seed_recommendations": [],
    "concerns": [],
    "budget_facts": [],
    "medical_history": [],
    "profile_facts": [],
    "deal_factors": [],
    "deal_outcome": {},
    "uncertainties": []
  }
}
"""


_JUDGMENT_AGENT_USER_TEMPLATE = """\
Evidence graph:
{evidence_graph}

Candidate indications recalled from local dictionary:
{candidate_indications}

Build fact_graph JSON only.
"""


_PLAN_AGENT_SYSTEM_PROMPT = """\
You are Agent 4 in a Chinese medical-aesthetic recording analysis chain:
the recommendation vs seed-plan adjudication agent.

Your job is only to improve recommendation classification and detail
completeness. Do not choose final SAP indications.

Rules:
1. recommendation = plan solving the customer's current demand.
2. seed_recommendation = additional, future, maintenance, lower-priority, or
   outside-current-demand plan.
   If participant_scope is present, classify recommendations separately for each
   customer. Do not classify a 同行客户A/同行客户B independent plan as 主咨询客户's
   recommendation or seed recommendation, and do not lose the同行客户's own plan.
3. A multi-step plan remains recommendation when all steps are needed to solve
   the current demand, even if one step is later.
4. Move comparison-only, unsuitable, explicitly not recommended, auxiliary care,
   pre-op checks, postop medicine, scar gel, or dressing changes out of both
   recommendations and seed_recommendations.
5. Preserve concrete details from evidence: brand, material, dosage, price,
   course, steps, notes, customer_response.
6. Output recommendation and seed_recommendation items with flat fields:
   content/body_part/brand/material/dosage/price/course_or_frequency/
   treatment_steps/implementation_notes/customer_response/related_demand_ids/
   evidence_ids. Do not put these only inside a nested details object.
7. When two materials/products are offered as choices, keep the selected/main
   option in brand/material and keep backup choices in implementation_notes.
   Do not drop the backup choice if it is clinically meaningful.
8. If an item is rewritten or merged, keep the most concrete supported wording
   and cite evidence.
9. If a current main recommendation contains comparison or backup wording in
   implementation_notes, keep the main recommendation as recommendation; only
   move the backup/comparison choice into implementation_notes or seed when it
   is a separate later plan.

Return JSON only:
{
  "recommendation_adjudication": {
    "recommendations": [],
    "seed_recommendations": [],
    "rejected_recommendations": [
      {"source_id": "", "reason": ""}
    ],
    "notes": []
  }
}
"""


_PLAN_AGENT_USER_TEMPLATE = """\
Current fact_graph:
{fact_graph}

Evidence graph:
{evidence_graph}

Relevant corrected transcript excerpts:
{dialogue}

Return recommendation_adjudication JSON only.
"""


_AUDIT_AGENT_SYSTEM_PROMPT = """\
You are Agent 6 in a Chinese medical-aesthetic recording analysis chain:
the final audit and repair agent.

Audit the fact_graph before code renders analysis_result. You may return a
corrected_fact_graph only when there is a clear evidence-backed issue.

Audit priorities:
1. SAP indications must be evidence-backed and exact to body area/project.
   When participant_scope exists, each indication/recommendation/concern/budget
   must stay attached to its own participant. Do not merge 同行客户A/同行客户B facts
   into 主咨询客户, but do not delete valid同行客户 facts either.
2. Do not miss explicit current customer demands such as 副乳、富贵包、美白、毛孔、
   痘印、暗沉 when the customer raised them.
3. Do not turn referral/deferred demands into final SAP indications without a
   current plan.
4. Recommendations and seed recommendations must be separated correctly.
5. Do not keep standalone pre/post-op care as recommendation.
6. Do not infer nose surgery from nasal skin texture.
7. Do not infer acne from 闭口时/闭上嘴.
8. Deal outcome requires direct payment/order/deposit evidence.
9. Do not delete valid profile_facts such as budget, price sensitivity, prior
   treatment/material, children/family, industry, decision maker, treatment
   preference, or comparison institution when evidence supports them.
10. If budget_evidence exists but budget_facts are empty or less specific, repair
    budget_facts. If recommendation evidence has price/dosage/material in a
    nested detail but final recommendation lacks it, repair the flat fields.
11. If a current main recommendation in fact_graph would be lost from rendered
    output due to backup/comparison wording, preserve it and only demote the
    backup option.
12. If you cannot confidently repair an issue, leave fact_graph unchanged and
   add an audit issue instead of inventing.

Return JSON only:
{
  "audit": {
    "revision_required": false,
    "issues": [
      {
        "severity": "high|medium|low",
        "type": "",
        "description": "",
        "evidence": ""
      }
    ],
    "unresolved_risks": []
  },
  "corrected_fact_graph": null
}
"""


_AUDIT_AGENT_USER_TEMPLATE = """\
Fact graph after recommendation and indication adjudication:
{fact_graph}

Evidence graph:
{evidence_graph}

Candidate indications:
{candidate_indications}

Relevant corrected transcript excerpts:
{dialogue}

Return audit JSON only. Include corrected_fact_graph only if a repair is clearly evidence-backed.
"""


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


_AGENT_RECOMMENDATION_DETAIL_KEYS = (
    "brand",
    "material",
    "dosage",
    "price",
    "course_or_frequency",
    "treatment_steps",
    "implementation_notes",
    "customer_response",
)

_AGENT_PRICE_TERMS = (
    "预算",
    "价格",
    "报价",
    "费用",
    "金额",
    "元",
    "块",
    "万",
    "千",
    "贵",
    "便宜",
    "优惠",
    "打折",
    "折扣",
    "承受",
    "顶死",
    "最多",
    "打不起",
    "付",
    "定金",
    "订金",
)

_AGENT_BUDGET_CATEGORY_ALIASES = {
    "budget",
    "current_budget",
    "本次预算",
    "本次消费预算",
    "消费预算",
    "价格预算",
}

_AGENT_PROFILE_CATEGORY_ALIASES = {
    "price_sensitivity": "价格敏感度",
    "budget": "本次消费预算",
    "current_budget": "本次消费预算",
    "prior_treatment_experience": "治疗项目",
    "treatment_experience": "治疗项目",
    "prior_material_or_device": "历史用的设备/原材料名称",
    "material_or_device": "历史用的设备/原材料名称",
    "pain_tolerance": "疼痛耐受度",
    "decision_maker": "决策主体",
    "comparison_institution": "对比机构",
    "industry_or_identity": "行业",
    "occupation": "行业",
    "children_status": "亲属/子女情况",
}


def _agent_join_text(*values: object) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, list):
            parts.extend(_clean_text(item) for item in value if _clean_text(item))
        elif isinstance(value, dict):
            parts.append(json.dumps(value, ensure_ascii=False))
        else:
            text = _clean_text(value)
            if text:
                parts.append(text)
    return "；".join(part for part in parts if part)


def _agent_evidence_text(item: dict[str, Any]) -> str:
    return _agent_join_text(item.get("quote"), item.get("evidence"), item.get("content"), item.get("text"))


def _agent_item_content(item: dict[str, Any]) -> str:
    return _first_text(item, "content", "demand", "recommendation", "plan", "text", "summary", "value")


def _agent_item_key(item: dict[str, Any]) -> str:
    return _compact_key_text(
        _agent_item_content(item)
        or item.get("quote")
        or item.get("value")
        or item.get("amount")
    )


def _agent_has_price_signal(text: str) -> bool:
    text = _clean_text(text)
    if not text:
        return False
    if not any(term in text for term in _AGENT_PRICE_TERMS):
        return False
    if re.search(r"\d", text):
        return True
    if re.search(r"[一二三四五六七八九十两俩]+[千百]?多?万", text):
        return True
    if re.search(r"[一二三四五六七八九十两俩]+千", text):
        return True
    if re.search(r"[一二三四五六七八九十两俩]+百", text):
        return True
    if re.search(r"[一二三四五六七八九十两俩]+(块钱|元)", text):
        return True
    return any(term in text for term in ("价格高", "价格偏高", "太贵", "贵了", "打不起", "预算有限"))


def _agent_has_budget_or_price_reaction(text: str) -> bool:
    text = _clean_text(text)
    if not text:
        return False
    return any(
        term in text
        for term in (
            "预算",
            "贵",
            "便宜",
            "打折",
            "优惠",
            "申请",
            "承受",
            "顶死",
            "最多",
            "打不起",
            "太高",
            "价格",
            "多少钱",
            "几千",
            "几万",
        )
    )


def _agent_has_affordability_reaction(text: str) -> bool:
    text = _clean_text(text)
    if not text:
        return False
    return any(
        term in text
        for term in (
            "预算",
            "贵",
            "太高",
            "打折",
            "优惠",
            "申请",
            "承受",
            "顶死",
            "最多",
            "打不起",
            "不够",
            "没那么多",
            "价格偏高",
            "价格高",
        )
    )


def _agent_next_id(prefix: str, items: list[dict[str, Any]]) -> str:
    max_index = 0
    for item in items:
        raw = _clean_text(item.get("id") or item.get(f"{prefix.lower()}_id"))
        match = re.search(r"(\d+)$", raw)
        if match:
            max_index = max(max_index, int(match.group(1)))
    return f"{prefix}{max_index + 1}"


def _agent_participant_key(item: dict[str, Any]) -> tuple[str, str]:
    return (
        _clean_text(item.get("participant_scope") or item.get("customer_scope")),
        _clean_text(item.get("participant") or item.get("participant_label")),
    )


def _agent_flatten_recommendation_details(fact_graph: dict[str, Any]) -> dict[str, Any]:
    updated = dict(fact_graph)
    for section in ("recommendations", "seed_recommendations"):
        flattened: list[dict[str, Any]] = []
        for item in _as_list(updated.get(section)):
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            details = _as_dict(copied.get("details"))
            for key in _AGENT_RECOMMENDATION_DETAIL_KEYS:
                if copied.get(key) in (None, "", [], {}) and details.get(key) not in (None, "", [], {}):
                    copied[key] = details[key]
            if copied.get("material") in (None, "") and details.get("brand_or_material"):
                copied["material"] = details["brand_or_material"]
            if copied.get("brand") in (None, "") and details.get("brand_or_product"):
                copied["brand"] = details["brand_or_product"]
            if copied.get("price") in (None, "") and details.get("amount"):
                copied["price"] = details["amount"]
            flattened.append(copied)
        updated[section] = flattened
    return updated


def _agent_existing_item_keys(items: list[dict[str, Any]]) -> set[tuple[str, tuple[str, str]]]:
    return {
        (_agent_item_key(item), _agent_participant_key(item))
        for item in items
        if _agent_item_key(item)
    }


def _agent_ensure_demands_from_evidence_graph(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(fact_graph)
    demands = [dict(item) for item in _as_list(updated.get("demands")) if isinstance(item, dict)]
    seen = _agent_existing_item_keys(demands)
    for item in _as_list(evidence_graph.get("customer_demand_evidence")):
        if not isinstance(item, dict):
            continue
        content = _agent_item_content(item)
        if not content:
            continue
        confidence = item.get("confidence")
        if isinstance(confidence, (int, float)) and confidence < 0.62:
            continue
        scope = _clean_text(item.get("participant_scope") or item.get("customer_scope"))
        if scope == "staff":
            continue
        key = (_compact_key_text(content), _agent_participant_key(item))
        if key in seen:
            continue
        seen.add(key)
        next_id = _agent_next_id("D", demands)
        demands.append(
            {
                "id": next_id,
                "content": content,
                "body_part": _first_text(item, "body_part", "body_part_name"),
                "participant": _clean_text(item.get("participant") or item.get("participant_label")) or None,
                "participant_scope": scope or None,
                "handling_status": _clean_text(item.get("handling_status")) or None,
                "evidence_ids": [_clean_text(item.get("id"))] if _clean_text(item.get("id")) else [],
                "evidence": [_agent_evidence_text(item)] if _agent_evidence_text(item) else [],
                "confidence": item.get("confidence"),
            }
        )
    updated["demands"] = demands
    return updated


def _agent_budget_fact_from_item(
    item: dict[str, Any],
    *,
    source_id: str,
    content: str | None = None,
) -> dict[str, Any] | None:
    content = _clean_text(content) or _first_text(item, "content", "amount", "price", "quote", "text", "summary")
    quote = _agent_evidence_text(item)
    combined = _agent_join_text(content, quote, item.get("customer_response"))
    if not _agent_has_price_signal(combined):
        return None
    return {
        "id": "",
        "content": content or quote,
        "participant": _clean_text(item.get("participant") or item.get("participant_label")) or None,
        "participant_scope": _clean_text(item.get("participant_scope") or item.get("customer_scope")) or None,
        "evidence_ids": [_clean_text(item.get("id")) or source_id],
        "evidence": [quote] if quote else [],
        "confidence": item.get("confidence"),
    }


def _agent_ensure_budget_facts_from_evidence_graph(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(fact_graph)
    budget_facts = [dict(item) for item in _as_list(updated.get("budget_facts")) if isinstance(item, dict)]
    seen = _agent_existing_item_keys(budget_facts)

    def append_fact(fact: dict[str, Any] | None) -> None:
        if not fact:
            return
        key = (_agent_item_key(fact), _agent_participant_key(fact))
        if not key[0] or key in seen:
            return
        fact["id"] = _agent_next_id("B", budget_facts)
        seen.add(key)
        budget_facts.append(fact)

    for item in _as_list(evidence_graph.get("budget_evidence")):
        if isinstance(item, dict):
            append_fact(_agent_budget_fact_from_item(item, source_id="budget_evidence"))

    for item in _as_list(evidence_graph.get("concern_evidence")):
        if not isinstance(item, dict):
            continue
        text = _agent_join_text(_agent_item_content(item), item.get("quote"))
        if _agent_has_budget_or_price_reaction(text):
            append_fact(_agent_budget_fact_from_item(item, source_id="concern_evidence"))

    for item in _as_list(evidence_graph.get("recommendation_evidence")):
        if not isinstance(item, dict):
            continue
        price = _first_text(item, "price")
        response = _first_text(item, "customer_response", "response")
        quote = _first_text(item, "quote")
        if not price:
            continue
        if not _agent_has_affordability_reaction(_agent_join_text(response, quote)):
            continue
        plan = _agent_item_content(item)
        content = f"{plan}价格反馈：{price}"
        if response:
            content = f"{content}；{response}"
        append_fact(_agent_budget_fact_from_item(item, source_id="recommendation_evidence", content=content))

    if budget_facts:
        updated["budget_facts"] = budget_facts
    return updated


def _agent_option_terms(text: str) -> list[str]:
    terms = [
        "瑞德喜",
        "双美胶原蛋白",
        "双美",
        "芭比针",
        "弗缦",
        "尊雅",
        "海媚",
        "思奥美",
        "艾拉斯提",
        "贝丽菲尔",
    ]
    found: list[str] = []
    for term in terms:
        if term in text and term not in found:
            found.append(term)
    return found


def _agent_should_preserve_as_backup_option(item: dict[str, Any]) -> bool:
    relation = _clean_text(item.get("relation_to_current_demand"))
    text = _agent_join_text(item.get("content"), item.get("quote"), item.get("implementation_notes"))
    if relation not in {"alternative_not_recommended", "unclear"}:
        return False
    return any(term in text for term in ("备选", "二选一", "选择", "维持时间偏短", "非主要推荐"))


def _agent_same_plan_area(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if _agent_participant_key(left) != _agent_participant_key(right):
        return False
    left_body = _compact_key_text(_first_text(left, "body_part", "body_part_name"))
    right_body = _compact_key_text(_first_text(right, "body_part", "body_part_name"))
    if left_body and right_body and (left_body in right_body or right_body in left_body):
        return True
    left_text = _compact_key_text(_agent_item_content(left))
    right_text = _compact_key_text(_agent_item_content(right))
    return bool(left_text and right_text and (left_text[:8] in right_text or right_text[:8] in left_text))


def _agent_preserve_backup_options(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
) -> dict[str, Any]:
    alternatives = [
        item
        for item in _as_list(evidence_graph.get("recommendation_evidence"))
        if isinstance(item, dict) and _agent_should_preserve_as_backup_option(item)
    ]
    if not alternatives:
        return fact_graph
    updated = dict(fact_graph)
    recs = [dict(item) for item in _as_list(updated.get("recommendations")) if isinstance(item, dict)]
    for alt in alternatives:
        alt_text = _agent_join_text(_agent_item_content(alt), _first_text(alt, "brand"), _first_text(alt, "material"))
        terms = _agent_option_terms(_agent_join_text(alt_text, alt.get("quote")))
        if not terms:
            continue
        note = f"备选/对比材料：{'/'.join(terms)}"
        extra = _first_text(alt, "implementation_notes")
        if extra:
            note = f"{note}（{extra}）"
        for rec in recs:
            if not _agent_same_plan_area(rec, alt):
                continue
            rec_text = json.dumps(rec, ensure_ascii=False)
            if all(term in rec_text for term in terms):
                break
            current_notes = _first_text(rec, "implementation_notes", "notes")
            if note not in current_notes:
                rec["implementation_notes"] = "；".join(part for part in (current_notes, note) if part)
            brand = _first_text(rec, "brand")
            missing_terms = [term for term in terms if term not in brand]
            if brand and missing_terms and any(term in brand for term in _agent_option_terms(brand)):
                rec["brand"] = f"{brand}/{'/'.join(missing_terms)}"
            break
    updated["recommendations"] = recs
    return updated


def _agent_normalize_profile_facts(fact_graph: dict[str, Any]) -> dict[str, Any]:
    updated = dict(fact_graph)
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, str]]] = set()
    for item in _as_list(updated.get("profile_facts")):
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        category = _first_text(copied, "category", "tag_category", "type")
        category = _AGENT_PROFILE_CATEGORY_ALIASES.get(category, category)
        value = _first_text(copied, "value", "tag_value", "content", "text")
        evidence = _agent_join_text(copied.get("evidence"), copied.get("quote"))
        combined = _agent_join_text(category, value, evidence)
        if category in _AGENT_BUDGET_CATEGORY_ALIASES or category == "本次消费预算":
            if not _agent_has_price_signal(combined):
                continue
            category = "本次消费预算"
        if category == "价格敏感度":
            if any(term in combined for term in ("高", "贵", "太高", "打不起", "顶死", "预算有限")):
                value = "高"
            elif any(term in combined for term in ("价格", "预算", "费用", "报价")):
                value = "中"
        if not category or not value:
            continue
        key = (_compact_key_text(category), _compact_key_text(value), _agent_participant_key(copied))
        if key in seen:
            continue
        seen.add(key)
        copied["category"] = category
        copied["value"] = value
        copied["content"] = value
        normalized.append(copied)
    updated["profile_facts"] = normalized
    return updated


def _agent_repair_fact_graph(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
) -> dict[str, Any]:
    repaired = _agent_flatten_recommendation_details(fact_graph)
    repaired = _agent_ensure_demands_from_evidence_graph(repaired, evidence_graph)
    repaired = _agent_ensure_budget_facts_from_evidence_graph(repaired, evidence_graph)
    repaired = _agent_preserve_backup_options(repaired, evidence_graph)
    repaired = _agent_normalize_profile_facts(repaired)
    return repaired


_EVIDENCE_LIST_SECTIONS = (
    "customer_demand_evidence",
    "diagnosis_evidence",
    "recommendation_evidence",
    "concern_evidence",
    "budget_evidence",
    "medical_history_evidence",
    "profile_evidence",
    "deal_evidence",
    "speaker_corrections",
    "quality_notes",
)

_EVIDENCE_ID_PREFIX = {
    "customer_demand_evidence": "E_D",
    "diagnosis_evidence": "E_X",
    "recommendation_evidence": "E_R",
    "concern_evidence": "E_C",
    "budget_evidence": "E_B",
    "medical_history_evidence": "E_H",
    "profile_evidence": "E_P",
    "deal_evidence": "E_DEAL",
    "speaker_corrections": "E_SPK",
    "quality_notes": "E_Q",
}


def _line_id_from_text(line: str) -> str:
    match = re.match(r"^\s*(L\d{4})\b", line)
    return match.group(1) if match else ""


def _split_corrected_dialogue_for_evidence(
    dialogue: str,
    *,
    target_chars: int = EVIDENCE_CHUNK_TARGET_CHARS,
    overlap_lines: int = EVIDENCE_CHUNK_OVERLAP_LINES,
) -> list[dict[str, Any]]:
    lines = [line for line in dialogue.splitlines() if line.strip()]
    if not lines:
        return []
    if len(dialogue) <= target_chars:
        return [
            {
                "chunk_index": 1,
                "chunk_count": 1,
                "line_range": f"{_line_id_from_text(lines[0]) or 'start'}-{_line_id_from_text(lines[-1]) or 'end'}",
                "line_count": len(lines),
                "char_count": len(dialogue),
                "dialogue": "\n".join(lines),
            }
        ]

    chunks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        start = max(0, index - overlap_lines) if chunks else index
        current: list[str] = []
        current_len = 0
        cursor = start
        while cursor < len(lines):
            line = lines[cursor]
            next_len = current_len + len(line) + 1
            if current and next_len > target_chars and cursor > index:
                break
            current.append(line)
            current_len = next_len
            cursor += 1
            if cursor > index and current_len >= target_chars:
                break
        if not current:
            current = [lines[index]]
            cursor = index + 1
        first_line = current[0]
        last_line = current[-1]
        chunks.append(
            {
                "chunk_index": len(chunks) + 1,
                "chunk_count": 0,
                "line_range": f"{_line_id_from_text(first_line) or 'start'}-{_line_id_from_text(last_line) or 'end'}",
                "line_count": len(current),
                "char_count": len("\n".join(current)),
                "dialogue": "\n".join(current),
            }
        )
        index = max(cursor, index + 1)
    total = len(chunks)
    for chunk in chunks:
        chunk["chunk_count"] = total
    return chunks


def _compact_key_text(value: object) -> str:
    text = _clean_text(value)
    return re.sub(r"[\s,，;；。.!！?？、/\\|（）()\"'“”‘’]+", "", text).lower()


def _evidence_turn_ids(item: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for value in _as_list(item.get("evidence_turn_ids")):
        text = _clean_text(value)
        if text and text not in ids:
            ids.append(text)
    blob = json.dumps(item, ensure_ascii=False)
    for line_id in re.findall(r"\bL\d{4}\b", blob):
        if line_id not in ids:
            ids.append(line_id)
    return ids


def _evidence_merge_key(section: str, item: object) -> tuple[str, ...]:
    if isinstance(item, dict):
        turn_ids = ",".join(_evidence_turn_ids(item))
        content = _compact_key_text(
            item.get("content")
            or item.get("quote")
            or item.get("text")
            or item.get("value")
            or item.get("description")
        )
        body = _compact_key_text(item.get("body_part") or item.get("body_part_name"))
        participant = _compact_key_text(item.get("participant") or item.get("participant_label"))
        scope = _compact_key_text(item.get("participant_scope") or item.get("customer_scope"))
        category = _compact_key_text(item.get("category") or item.get("type"))
        if turn_ids:
            return (section, turn_ids, category, content[:80], body, participant, scope)
        return (section, category, content[:140], body, participant, scope)
    return (section, _compact_key_text(item))


def _merge_evidence_graphs(
    graphs: list[dict[str, Any]],
    chunk_debug: list[dict[str, Any]],
) -> dict[str, Any]:
    merged: dict[str, Any] = {section: [] for section in _EVIDENCE_LIST_SECTIONS}
    seen: set[tuple[str, ...]] = set()
    counters: dict[str, int] = {section: 0 for section in _EVIDENCE_LIST_SECTIONS}

    for chunk_index, graph in enumerate(graphs, start=1):
        if not isinstance(graph, dict):
            continue
        for section in _EVIDENCE_LIST_SECTIONS:
            for item in _as_list(graph.get(section)):
                key = _evidence_merge_key(section, item)
                if key in seen:
                    continue
                seen.add(key)
                counters[section] += 1
                if isinstance(item, dict):
                    copied = dict(item)
                    copied["source_chunk"] = chunk_index
                    copied["source_evidence_id"] = _clean_text(copied.get("id"))
                    copied["id"] = f"{_EVIDENCE_ID_PREFIX.get(section, 'E')}{counters[section]}"
                    merged[section].append(copied)
                else:
                    merged[section].append(item)

    merged["_merge_stats"] = {
        "chunk_count": len(graphs),
        "chunks": chunk_debug,
        "section_counts": {section: len(_as_list(merged.get(section))) for section in _EVIDENCE_LIST_SECTIONS},
    }
    return merged


def _extract_evidence_by_chunks(
    corrected_dialogue: str,
    *,
    staff_text: str,
    preprocess_context: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    chunks = _split_corrected_dialogue_for_evidence(corrected_dialogue)
    evidence_graphs: list[dict[str, Any]] = []
    chunk_debug: list[dict[str, Any]] = []
    preprocess_text = json.dumps(preprocess_context, ensure_ascii=False, indent=2)
    for chunk in chunks:
        evidence_user_prompt = _EVIDENCE_AGENT_CHUNK_USER_TEMPLATE.format(
            staff_context=staff_text,
            preprocess_context=preprocess_text,
            chunk_index=chunk["chunk_index"],
            chunk_count=chunk["chunk_count"],
            line_range=chunk["line_range"],
            dialogue=chunk["dialogue"],
        )
        evidence_parsed = _call_agent(
            f"evidence_chunk_{chunk['chunk_index']}",
            _EVIDENCE_AGENT_SYSTEM_PROMPT,
            evidence_user_prompt,
            max_tokens=9000,
        )
        evidence_graph = _extract_evidence_graph(evidence_parsed)
        evidence_graphs.append(evidence_graph)
        chunk_debug.append(
            {
                "chunk_index": chunk["chunk_index"],
                "line_range": chunk["line_range"],
                "line_count": chunk["line_count"],
                "char_count": chunk["char_count"],
                "evidence_counts": {
                    section: len(_as_list(evidence_graph.get(section)))
                    for section in _EVIDENCE_LIST_SECTIONS
                },
            }
        )
    return _merge_evidence_graphs(evidence_graphs, chunk_debug), chunk_debug


def _collect_referenced_line_ids(evidence_graph: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for section in _EVIDENCE_LIST_SECTIONS:
        for item in _as_list(evidence_graph.get(section)):
            if isinstance(item, dict):
                ids.update(_evidence_turn_ids(item))
            else:
                for line_id in re.findall(r"\bL\d{4}\b", _clean_text(item)):
                    ids.add(line_id)
    return ids


def _relevant_dialogue_excerpt(
    corrected_dialogue: str,
    evidence_graph: dict[str, Any],
    *,
    context_lines: int = 1,
    max_lines: int = 120,
) -> str:
    wanted = _collect_referenced_line_ids(evidence_graph)
    if not wanted:
        return "No full transcript provided. Use evidence quotes in evidence_graph."
    lines = [line for line in corrected_dialogue.splitlines() if line.strip()]
    indexed: list[tuple[str, str]] = [(_line_id_from_text(line), line) for line in lines]
    positions = {line_id: index for index, (line_id, _line) in enumerate(indexed) if line_id}
    selected: set[int] = set()
    for line_id in wanted:
        if line_id not in positions:
            continue
        pos = positions[line_id]
        for offset in range(-context_lines, context_lines + 1):
            next_pos = pos + offset
            if 0 <= next_pos < len(indexed):
                selected.add(next_pos)
    selected_positions = sorted(selected)
    if len(selected_positions) > max_lines:
        selected_positions = selected_positions[:max_lines]
    return "\n".join(indexed[pos][1] for pos in selected_positions)


def _has_participant_scope(evidence_graph: dict[str, Any], scope: str) -> bool:
    for section in _EVIDENCE_LIST_SECTIONS:
        for item in _as_list(evidence_graph.get(section)):
            if isinstance(item, dict) and _clean_text(item.get("participant_scope") or item.get("customer_scope")) == scope:
                return True
    return False


def _audit_needed(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
    correction_metadata: dict[str, Any],
    indication_adjudication: dict[str, Any],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    demands = _as_list(fact_graph.get("demands"))
    recommendations = _as_list(fact_graph.get("recommendations"))
    seed_recommendations = _as_list(fact_graph.get("seed_recommendations"))
    indications = _as_list(fact_graph.get("indication_candidates"))
    if _as_list(evidence_graph.get("customer_demand_evidence")) and not demands:
        reasons.append("demand_evidence_without_fact")
    if (demands or recommendations or seed_recommendations) and not indications:
        reasons.append("business_facts_without_indication")
    if _as_list(evidence_graph.get("recommendation_evidence")) and not (recommendations or seed_recommendations):
        reasons.append("recommendation_evidence_without_fact")
    if recommendations and not any(_as_list(item.get("related_demand_ids")) for item in recommendations if isinstance(item, dict)):
        reasons.append("recommendations_without_demand_links")
    if _as_list(evidence_graph.get("profile_evidence")) and not _as_list(fact_graph.get("profile_facts")):
        reasons.append("profile_evidence_without_profile_facts")
    if _as_list(evidence_graph.get("budget_evidence")) and not _as_list(fact_graph.get("budget_facts")):
        reasons.append("budget_evidence_without_budget_facts")
    for item in recommendations:
        if not isinstance(item, dict):
            continue
        details = _as_dict(item.get("details"))
        if details and any(details.get(key) for key in _AGENT_RECOMMENDATION_DETAIL_KEYS) and not any(item.get(key) for key in _AGENT_RECOMMENDATION_DETAIL_KEYS):
            reasons.append("recommendation_details_not_flattened")
            break
    if _has_participant_scope(evidence_graph, "other_customer"):
        reasons.append("multi_customer_scope")
    if len(_as_list(correction_metadata.get("applied_speaker_corrections"))) >= 3:
        reasons.append("many_speaker_corrections")
    if _as_list(correction_metadata.get("uncertain_notes")):
        reasons.append("speaker_or_term_uncertainty")
    rejected = _as_list(indication_adjudication.get("rejected_indications"))
    if len(rejected) >= 3:
        reasons.append("many_rejected_indications")
    # Keep the expensive audit call for structural/data-loss risks. Speaker
    # uncertainty and many rejected candidates are useful diagnostics but are
    # common on long recordings and do not by themselves justify another full
    # LLM pass.
    diagnostic_only = {
        "many_speaker_corrections",
        "speaker_or_term_uncertainty",
        "many_rejected_indications",
    }
    actionable = [reason for reason in reasons if reason not in diagnostic_only]
    return bool(actionable), reasons


def _extract_plan_adjudication(parsed: dict[str, Any]) -> dict[str, Any]:
    payload = parsed.get("recommendation_adjudication")
    if isinstance(payload, dict):
        return payload
    return parsed


def _normalize_fact_item_list(value: object) -> list[dict[str, Any]]:
    return [dict(item) for item in _as_list(value) if isinstance(item, dict)]


def _apply_plan_adjudication(fact_graph: dict[str, Any], adjudication: dict[str, Any]) -> dict[str, Any]:
    recommendations = _normalize_fact_item_list(adjudication.get("recommendations"))
    seed_recommendations = _normalize_fact_item_list(adjudication.get("seed_recommendations"))
    if not recommendations and not seed_recommendations:
        return fact_graph
    updated = dict(fact_graph)
    updated["recommendations"] = recommendations
    updated["seed_recommendations"] = seed_recommendations
    updated["_recommendation_adjudication"] = {
        "rejected_recommendations": _as_list(adjudication.get("rejected_recommendations")),
        "notes": _as_list(adjudication.get("notes")),
    }
    return updated


def _extract_audit(parsed: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    audit = parsed.get("audit") if isinstance(parsed.get("audit"), dict) else {}
    corrected = parsed.get("corrected_fact_graph")
    return audit, corrected if isinstance(corrected, dict) else None


def _apply_audit_repair(fact_graph: dict[str, Any], corrected: dict[str, Any] | None) -> dict[str, Any]:
    if not corrected:
        return fact_graph
    updated = dict(fact_graph)
    replaceable_sections = (
        "demands",
        "doctor_diagnoses",
        "indication_candidates",
        "recommendations",
        "seed_recommendations",
        "concerns",
        "budget_facts",
        "medical_history",
        "profile_facts",
        "deal_factors",
        "uncertainties",
    )
    for section in replaceable_sections:
        if section in corrected and isinstance(corrected.get(section), list):
            updated[section] = _normalize_fact_item_list(corrected.get(section))
    if isinstance(corrected.get("deal_outcome"), dict):
        updated["deal_outcome"] = dict(corrected["deal_outcome"])
    updated["_audit_repaired"] = True
    return updated


def _compact_for_prompt(value: object, *, max_chars: int = 22000) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...<truncated>"


def _call_agent(agent_name: str, system_prompt: str, user_prompt: str, *, max_tokens: int) -> dict[str, Any]:
    logger.info(
        "agent pipeline %s prompt chars system=%d user=%d",
        agent_name,
        len(system_prompt),
        len(user_prompt),
    )
    return _call_json(system_prompt, user_prompt, max_tokens=max_tokens)


def analyze_transcript_agent(
    path: str | Path,
    *,
    system_prompt: str | None = None,
    staff_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the higher-token agent backup chain for one transcript.

    The returned payload mirrors ``analyze_transcript_staged``: it contains an
    ``analysis_result`` suitable for comparison, but callers should decide
    whether to persist it. This function itself does not update DB state.
    """
    del system_prompt  # Current agent prompts carry task-specific rules directly.
    dialogue, raw = prepare_transcript(path)
    if not dialogue.strip():
        raise ValueError(f"Transcript file {Path(path).name} has no valid dialogue")

    staff_text = _format_staff_context(staff_context)
    preprocess_context = _build_preprocess_context(dialogue, staff_context)
    line_speaker_metadata = _build_line_speaker_metadata(dialogue, raw)
    numbered_dialogue, numbered_line_map = _number_dialogue_lines(dialogue, line_speaker_metadata)

    correction_user_prompt = _CORRECTION_AGENT_USER_TEMPLATE.format(
        staff_context=staff_text,
        preprocess_context=json.dumps(preprocess_context, ensure_ascii=False, indent=2),
        numbered_dialogue=numbered_dialogue,
    )
    correction_parsed = _call_agent(
        "correction",
        _CORRECTION_AGENT_SYSTEM_PROMPT,
        correction_user_prompt,
        max_tokens=6000,
    )
    correction_patch = _extract_correction_patch(correction_parsed)
    corrected_dialogue, correction_metadata = _apply_correction_patch(
        numbered_dialogue,
        numbered_line_map,
        correction_patch,
        line_speaker_metadata,
    )

    evidence_graph, evidence_chunk_debug = _extract_evidence_by_chunks(
        corrected_dialogue,
        staff_text=staff_text,
        preprocess_context=preprocess_context,
    )
    evidence_call_count = max(1, len(evidence_chunk_debug))
    relevant_dialogue_excerpt = _relevant_dialogue_excerpt(corrected_dialogue, evidence_graph)

    evidence_text = json.dumps(evidence_graph, ensure_ascii=False)
    candidate_rows = _candidate_indications_from_text(f"{evidence_text}\n{corrected_dialogue}", max_items=50)
    candidate_indications = _format_candidate_indications(candidate_rows)

    judgment_user_prompt = _JUDGMENT_AGENT_USER_TEMPLATE.format(
        evidence_graph=_compact_for_prompt(evidence_graph),
        candidate_indications=json.dumps(candidate_indications, ensure_ascii=False, indent=2),
    )
    judgment_parsed = _call_agent(
        "judgment",
        _JUDGMENT_AGENT_SYSTEM_PROMPT,
        judgment_user_prompt,
        max_tokens=14000,
    )
    fact_graph = _extract_fact_graph(judgment_parsed)
    fact_graph = _repair_empty_fact_graph_from_evidence_graph(fact_graph, evidence_graph)
    fact_graph = _merge_profile_facts_from_evidence_graph(fact_graph, evidence_graph)
    fact_graph = _agent_repair_fact_graph(fact_graph, evidence_graph)

    plan_call_count = 0
    if _as_list(evidence_graph.get("recommendation_evidence")) or _as_list(fact_graph.get("recommendations")) or _as_list(fact_graph.get("seed_recommendations")):
        plan_user_prompt = _PLAN_AGENT_USER_TEMPLATE.format(
            fact_graph=_compact_for_prompt(fact_graph),
            evidence_graph=_compact_for_prompt(evidence_graph),
            dialogue=relevant_dialogue_excerpt,
        )
        try:
            plan_call_count = 1
            plan_parsed = _call_agent(
                "recommendation_adjudication",
                _PLAN_AGENT_SYSTEM_PROMPT,
                plan_user_prompt,
                max_tokens=9000,
            )
            plan_adjudication = _extract_plan_adjudication(plan_parsed)
            fact_graph = _apply_plan_adjudication(fact_graph, plan_adjudication)
            fact_graph = _agent_repair_fact_graph(fact_graph, evidence_graph)
        except Exception as exc:
            logger.warning("agent recommendation adjudication failed, using judgment fact_graph: %s", exc)
            plan_adjudication = {"error": str(exc), "recommendations": [], "seed_recommendations": []}
    else:
        plan_adjudication = {"skipped": True, "reason": "no recommendation evidence or recommendation facts"}

    indication_user_prompt = _INDICATION_ADJUDICATION_USER_TEMPLATE.format(
        fact_graph=json.dumps(_compact_fact_graph_for_indications(fact_graph), ensure_ascii=False, indent=2),
        candidate_indications=json.dumps(candidate_indications, ensure_ascii=False, indent=2),
    )
    try:
        indication_parsed = _call_agent(
            "indication_adjudication",
            _INDICATION_ADJUDICATION_SYSTEM_PROMPT,
            indication_user_prompt,
            max_tokens=8000,
        )
        indication_adjudication = _extract_indication_adjudication(indication_parsed)
        fact_graph = _apply_indication_adjudication(fact_graph, indication_adjudication, candidate_indications)
    except Exception as exc:
        logger.warning("agent indication adjudication failed, using preliminary indications: %s", exc)
        indication_adjudication = {
            "final_indications": [],
            "rejected_indications": [],
            "error": str(exc),
        }

    audit_call_count = 0
    indication_after_audit_count = 0
    audit_required, audit_reasons = _audit_needed(
        fact_graph,
        evidence_graph,
        correction_metadata,
        indication_adjudication,
    )
    if audit_required:
        audit_user_prompt = _AUDIT_AGENT_USER_TEMPLATE.format(
            fact_graph=_compact_for_prompt(fact_graph),
            evidence_graph=_compact_for_prompt(evidence_graph),
            candidate_indications=json.dumps(candidate_indications, ensure_ascii=False, indent=2),
            dialogue=relevant_dialogue_excerpt,
        )
        try:
            audit_call_count = 1
            audit_parsed = _call_agent(
                "audit",
                _AUDIT_AGENT_SYSTEM_PROMPT,
                audit_user_prompt,
                max_tokens=9000,
            )
            audit, corrected_fact_graph = _extract_audit(audit_parsed)
            audit["trigger_reasons"] = audit_reasons
            if audit.get("revision_required") and corrected_fact_graph:
                fact_graph = _apply_audit_repair(fact_graph, corrected_fact_graph)
                fact_graph = _agent_repair_fact_graph(fact_graph, evidence_graph)
                # Re-run indication adjudication after fact repair so SAP indications
                # match the final fact graph.
                repaired_indication_user_prompt = _INDICATION_ADJUDICATION_USER_TEMPLATE.format(
                    fact_graph=json.dumps(_compact_fact_graph_for_indications(fact_graph), ensure_ascii=False, indent=2),
                    candidate_indications=json.dumps(candidate_indications, ensure_ascii=False, indent=2),
                )
                indication_after_audit_count = 1
                repaired_indication = _call_agent(
                    "indication_adjudication_after_audit",
                    _INDICATION_ADJUDICATION_SYSTEM_PROMPT,
                    repaired_indication_user_prompt,
                    max_tokens=8000,
                )
                indication_adjudication = _extract_indication_adjudication(repaired_indication)
                fact_graph = _apply_indication_adjudication(fact_graph, indication_adjudication, candidate_indications)
        except Exception as exc:
            logger.warning("agent audit failed, using pre-audit fact_graph: %s", exc)
            audit = {"error": str(exc), "revision_required": False, "issues": [], "trigger_reasons": audit_reasons}
    else:
        audit = {"skipped": True, "revision_required": False, "issues": [], "trigger_reasons": []}

    fact_graph = _merge_profile_facts_from_evidence_graph(fact_graph, evidence_graph)
    fact_graph = _agent_repair_fact_graph(fact_graph, evidence_graph)
    analysis_result = _build_analysis_result_from_fact_graph(fact_graph, raw, allow_raw_augmentation=False)
    debug = analysis_result.setdefault("staged_pipeline_debug", {})
    if isinstance(debug, dict):
        total_logical_calls = 3 + evidence_call_count + plan_call_count + audit_call_count + indication_after_audit_count
        debug["production_chain"] = PIPELINE_NAME
        debug["llm_call_plan"] = {
            "model": STAGED_LLM_MODEL,
            "correction_agent": 1,
            "evidence_agent": evidence_call_count,
            "judgment_agent": 1,
            "recommendation_adjudication_agent": plan_call_count,
            "indication_adjudication_agent": 1,
            "audit_agent": audit_call_count,
            "indication_adjudication_after_audit": indication_after_audit_count,
            "fact_graph_to_analysis_result": 0,
            "total_logical_calls": total_logical_calls,
        }
        debug["agent_audit"] = audit
        debug["agent_evidence_chunking"] = {
            "chunk_count": evidence_call_count,
            "target_chars": EVIDENCE_CHUNK_TARGET_CHARS,
            "overlap_lines": EVIDENCE_CHUNK_OVERLAP_LINES,
        }

    total_logical_calls = 3 + evidence_call_count + plan_call_count + audit_call_count + indication_after_audit_count
    return {
        "pipeline": PIPELINE_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "llm_call_plan": {
            "model": STAGED_LLM_MODEL,
            "correction_agent": 1,
            "evidence_agent": evidence_call_count,
            "judgment_agent": 1,
            "recommendation_adjudication_agent": plan_call_count,
            "indication_adjudication_agent": 1,
            "audit_agent": audit_call_count,
            "indication_adjudication_after_audit": indication_after_audit_count,
            "fact_graph_to_analysis_result": 0,
            "total_logical_calls": total_logical_calls,
        },
        "input_stats": {
            "dialogue_chars": len(dialogue),
            "corrected_dialogue_chars": len(corrected_dialogue),
            "raw_payload_chars": _estimate_payload_chars(raw),
            "numbered_dialogue_lines": len(numbered_line_map),
            "applied_speaker_correction_count": len(correction_metadata.get("applied_speaker_corrections", [])),
            "applied_term_correction_count": len(correction_metadata.get("applied_term_corrections", [])),
            "evidence_chunk_count": evidence_call_count,
        },
        "preprocess_context": preprocess_context,
        "correction_patch": correction_patch,
        "correction_metadata": correction_metadata,
        "corrected_dialogue": corrected_dialogue,
        "evidence_graph": evidence_graph,
        "evidence_chunk_debug": evidence_chunk_debug,
        "relevant_dialogue_excerpt": relevant_dialogue_excerpt,
        "candidate_indications": candidate_indications,
        "plan_adjudication": plan_adjudication,
        "indication_adjudication": indication_adjudication,
        "audit": audit,
        "fact_graph": fact_graph,
        "analysis_result": analysis_result,
    }
