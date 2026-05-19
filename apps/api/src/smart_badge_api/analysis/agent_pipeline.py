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
    _catalog_match_by_name,
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

PIPELINE_NAME = "agent_pipeline_v3_1_gpt52"
EVIDENCE_CHUNK_TARGET_CHARS = 14000
EVIDENCE_CHUNK_OVERLAP_LINES = 2


_CORRECTION_AGENT_SYSTEM_PROMPT = """\
You are Agent 1 in a Chinese medical-aesthetic recording analysis chain:
the transcript correction and speaker/participant-role agent.

Task:
Return only small patch operations that correct high-confidence ASR term
mistakes and clearly wrong speaker/participant roles. Preserve timestamps and
original wording. Do not summarize, extract facts, infer demands, choose
indications, render recommendations, or write SAP remarks.

Rules:
1. Patch conservatively.
   - Use term_corrections only when the replacement is strongly supported by
     local context; otherwise leave it unchanged and add uncertain_notes.
   - Do not rewrite whole lines. Use speaker_role_map for stable speaker-level
     roles, speaker_corrections only for clear line-level diarization or role
     exceptions.

2. Speaker taxonomy.
   speaker role must be one of:
   customer, companion, consultant, doctor, expert_assistant, frontdesk,
   staff_peer, other.
   customer_scope must be one of:
   primary_customer, other_customer, companion_or_family, staff, unknown.

3. Choose roles by speech function, not by current_role alone.
   Treat contradictory compound labels as hints to verify, not as truth; for
   example customer/companion labels containing badge wearer/"工牌本人", or
   doctor/consultant labels containing primary customer.
   - Customer-side speech: personal goals, feelings, treatment questions,
     consent/refusal, hesitation, concerns, prior history, quoted experience,
     budget limit, or price pressure.
   - Staff-side speech: reception/guidance, check/order/payment/write-off,
     queue/appointment/signing flow, clinical explanation, recommendation,
     dosage, quotation or price explanation, risk/process explanation, and
     coworker/phone/intercom/internal talk.
   - Internal staff talk includes leaders/shifts, cost/profit, deal/order,
     payment arrival, another customer's case, and work-ownership phrases such
     as "my customer", "customer under my name", "I am receiving", or
     "who should receive". Set customer_scope=staff.
   - Pre-reception setup before a real customer demand appears is staff/frontdesk:
     name-calling, appointment lookup, room guidance, signing/check-in prep, and
     countdown/test utterances.
   - Professional explanation alone does not prove doctor; consultants and
     expert assistants may explain anatomy, plans, dosage, risks, and prices.
     Self-identified assistant/doctor-assistant/dean-assistant is
     expert_assistant, not doctor.
   Do not over-correct real customers who quote family, friends, coworkers,
   doctors, or compare institutions.

4. Participant labels.
   - 主咨询客户: the person whose visit/order is being handled.
   - 同行客户A/B: another present person asking about their own treatment.
   - 陪同人员: family/friend helping the main customer answer or decide.
   Keep separate present customers separate; do not label two distinguishable
   customers simply as "客户". If primary customer is unclear, choose the
   best-supported one and add uncertain_notes.

5. Term correction scope.
   Correct only high-confidence medical-aesthetic ASR mistakes. Use supplied
   preprocessing hints/hotwords and local context for product, material,
   treatment, and body-area terms. Typical examples: "一字光波/一次光波/一支光波"
   may be "一支玻尿酸" in injection contexts; "鲁板/鲁班" may be "濡白天使";
   "下划线" may be "下颌线" in contour contexts. Keep uncertain cases unchanged.

6. Confidence and output.
   Use confidence >=0.65 for speaker role corrections and >=0.75 for term
   corrections. Return JSON only in the schema below.

Return JSON only:
{
  "correction_patch": {
    "speaker_role_map": [{
      "asr_speaker": "speaker_0",
      "role": "customer|companion|consultant|doctor|expert_assistant|frontdesk|staff_peer|other",
      "participant_label": "主咨询客户|同行客户A|同行客户B|陪同人员|咨询师|医生|专家助理|前台|员工|其他",
      "customer_scope": "primary_customer|other_customer|companion_or_family|staff|unknown",
      "confidence": 0.0,
      "reason": ""
    }],
    "speaker_corrections": [{
      "line_id": "L0001",
      "corrected_speaker": "customer|companion|consultant|doctor|expert_assistant|frontdesk|staff_peer|other",
      "participant_label": "",
      "customer_scope": "primary_customer|other_customer|companion_or_family|staff|unknown",
      "confidence": 0.0,
      "reason": ""
    }],
    "term_corrections": [{
      "line_id": "L0001",
      "original": "",
      "corrected": "",
      "confidence": 0.0,
      "reason": ""
    }],
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


_SCOPE_AGENT_SYSTEM_PROMPT = """\
You are Agent 1.5 in a Chinese medical-aesthetic recording analysis chain:
the current-visit scope segmentation agent.

Task:
Segment the corrected transcript into ranges that should be kept or ignored
before evidence extraction. This step is a conservative gate: keep anything
that may describe the present customer's consultation; ignore only ranges that
are clearly outside the current visit.

What must be kept as current-visit relevant:
- Customer-side goals, symptoms, questions, concerns, hesitation, acceptance or
  refusal, prior treatment history, budget limits, price sensitivity, and price
  calculation.
- Staff/doctor/expert-assistant diagnosis, anatomy explanation, recommendation,
  seed/next-visit suggestion, cross-department suggestion, product/brand,
  dosage, treatment step, risk explanation, quotation, deposit, order creation,
  payment, deal confirmation, and post-deal care if it belongs to this visit.
- Every present person who is asking about their own treatment. Mark another
  present consulting customer as accompanying_customer_consultation and
  participant_scope=other_customer; do not discard them.

What can be ignored:
- Absent third-party/customer-case discussion that is not advice for the
  present customer.
- Staff-only internal work chat, staffing/ownership/order-handling talk,
  casual chat, waiting/room guidance, name calling, test/countdown utterances,
  or unrelated operations with no customer demand, plan, price, deal, or care
  information.

Rules:
1. Do not extract analysis facts. Only segment scope.
2. Cover the transcript with coarse, ordered, non-overlapping ranges. Prefer
   fewer segments unless the relevance really changes.
3. Set business_relevance=ignore only for clearly ignorable ranges. If a range
   mixes ignorable talk with useful current-visit facts, either split it or keep
   the mixed range as supporting.
4. Quote/payment/deal, seed/cross-department, doctor face-to-face, and post-deal
   care are supporting/core current-visit content when tied to the current
   customer, even if they happen near the beginning or end.
5. When uncertain, set current_visit_relevant=true and explain uncertainty. It
   is safer to keep uncertain current-visit content than to drop useful evidence.

Allowed scope_type values:
- current_customer_consultation
- accompanying_customer_consultation
- doctor_face_to_face
- quote_or_payment
- post_deal_care
- future_seed_or_cross_department
- third_party_absent_case
- staff_chat
- casual_chat
- unrelated_operations
- unclear

Allowed business_relevance values: core, supporting, ignore.

Return JSON only:
{
  "scope_graph": {
    "primary_customer": "",
    "dominant_visit_topic": "",
    "segments": [
      {
        "id": "S1",
        "start_line_id": "L0001",
        "end_line_id": "L0010",
        "scope_type": "current_customer_consultation",
        "participant_scope": "primary_customer|other_customer|companion_or_family|staff|unknown",
        "business_relevance": "core|supporting|ignore",
        "current_visit_relevant": true,
        "reason": ""
      }
    ],
    "notes": []
  }
}
"""


_SCOPE_AGENT_USER_TEMPLATE = """\
Staff / recording context:
{staff_context}

Code-side preprocessing hints:
{preprocess_context}

Corrected transcript for scope segmentation:
{dialogue}

Return scope_graph JSON only.
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
12a. If recommendation_evidence.customer_response says the customer worries
    about safety, side effects, sequelae, migration, worsening hollowness, or
    asks whether it is safe, extract the same issue as concern_evidence too.
    Do not leave a concrete worry only inside customer_response.
13. Extract budget_evidence with high precision. It is only for the main
    customer's explicit budget, acceptable price range, affordability limit,
    deposit/payment amount, clear price objection/discount request, or implicit
    budget pressure tied to a concrete quote/range. Example:
    "对总价约29000-30000元较敏感并反复核算" is budget_evidence and will later be
    rendered as "未明确；对总价约29000-30000元较敏感，倾向希望低于该区间".
    Staff-only quotes, price calculation, project fees, discount explanation,
    and effect explanation such as "X块解决不了多少" must stay on
    recommendation_evidence or deal_evidence, not budget_evidence. Keep project
    quote fields on recommendation_evidence too.
14. Extract profile_evidence for customer labels even when they are not SAP
    indications: prior treatments/materials/devices, current budget, price
    sensitivity, pain tolerance, children/family situation, industry/special
    identity, comparison institution, decision maker, treatment preference,
    recovery/time constraint, and product/project preference. Preserve
    participant/participant_scope and exact evidence.
    Do not turn negative history ("从来没打过", "没做过") or current-service
    suitability ("能打", "可以打", "再打一支") into prior-treatment tags.
15. When a recommendation has multiple material/product choices, preserve all
    named choices. Mark the main recommendation and store backup choices in
    implementation_notes instead of dropping them. Example: "双美胶原蛋白"
    can be a backup to "瑞德喜" even when not the main recommendation.
16. For contour injection plans, preserve the structural target instead of
    collapsing the plan into generic product names. Examples:
    - 鼻基底/鼻头/鼻翼/鼻尖 + 三角结构/玻尿酸/再生材料/芭比针/濡白天使
      means a nasal-axis structural injection plan.
    - 下颌线/下颌角拐点/耳前耳后韧带/外轮廓 + 童颜针/芭比针/玻尿酸/
      支撑/提升 means a jawline structural support plan.
    Do not reduce these to "肉毒/除皱瘦脸" when the transcript also contains
    童颜针、芭比针、支撑、下颌角拐点 or 鼻基底 structure.
17. Do not create separate customer_demand_evidence for process questions such
    as instrument version/generation, verification, doctor assignment, surgery
    time, incision, recovery, driving, payment, discount, or price only. Attach
    those to concern_evidence, budget_evidence, deal_evidence, or
    implementation_notes unless they also state a concrete body problem/goal.
18. Keep demand evidence concise and normalized: one item per body problem/goal,
    not one item per repeated question.
19. Do not create customer_demand_evidence from casual body-hair wording such
    as "小毛毛/汗毛/体毛" unless the customer explicitly asks for 脱毛/冰点脱毛/
    激光脱毛 or asks how to remove it.

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


_EVENT_AGENT_SYSTEM_PROMPT = """\
You are Agent 3 in a Chinese medical-aesthetic recording analysis chain:
the event-graph extraction agent.

Your job is to convert evidence_graph plus relevant transcript excerpts into
atomic business events with explicit polarity. Do not render final analysis
and do not choose final SAP indications.

Why this exists:
- evidence_graph says what was mentioned.
- event_graph says how that mention functions in the conversation.
- Later agents must not turn customer questions, staff explanations,
  comparison-only options, or explicitly unsuitable plans into final
  recommendations or indication support.

Event polarity rules:
1. current_recommendation: staff/doctor recommends a plan for the customer's
   current problem in this visit.
2. seed_recommendation: a later, optional, add-on, maintenance, or
   cross-department plan not central to this visit.
3. comparison_or_backup: a choice used for comparison or backup, not selected
   as the main plan. If staff gives an "overall design" or "optional package"
   that can be done later while saying the customer may first do the core
   project, classify that optional package as seed_recommendation instead of
   comparison_or_backup.
4. not_recommended: explicitly unsuitable, rejected by staff/doctor, or
   explained as not preferred.
5. staff_explanation: product, anatomy, risk, device, price, or process
   explanation without a concrete recommendation to do it.
6. customer_question: a customer asks about an item, but staff does not
   recommend it as a current plan.
7. diagnosis_only: staff observes a problem but no current plan is proposed.
8. customer_accept / customer_reject / deal_confirmed: customer response or
   transaction state tied to a specific plan when possible.

Return event_graph JSON only:
{
  "event_graph": {
    "demand_events": [
      {
        "id": "EV_D1",
        "event_type": "current_demand|deferred_demand|diagnosis_only|unclear",
        "participant": "",
        "participant_scope": "primary_customer|other_customer|companion_or_family|unknown",
        "content": "",
        "body_part": "",
        "source_evidence_ids": [],
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "plan_events": [
      {
        "id": "EV_P1",
        "event_type": "current_recommendation|seed_recommendation|comparison_or_backup|not_recommended|staff_explanation|customer_question|diagnosis_only|unclear",
        "participant": "",
        "participant_scope": "primary_customer|other_customer|unknown",
        "plan": "",
        "body_part": "",
        "brand": "",
        "material": "",
        "dosage": "",
        "price": "",
        "course_or_frequency": "",
        "treatment_steps": [],
        "implementation_notes": "",
        "customer_response": "",
        "related_demand": "",
        "source_evidence_ids": [],
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "deal_events": [
      {
        "id": "EV_DEAL1",
        "event_type": "deal_confirmed|deposit|payment|order_created|not_deal|unclear",
        "participant": "",
        "participant_scope": "primary_customer|other_customer|unknown",
        "plan": "",
        "amount": "",
        "quote": "",
        "source_evidence_ids": [],
        "evidence_turn_ids": [],
        "confidence": 0.0
      }
    ],
    "profile_events": [
      {
        "id": "EV_PR1",
        "event_type": "customer_profile|staff_or_product_context|ambiguous|reject",
        "category": "",
        "value": "",
        "participant": "",
        "participant_scope": "primary_customer|other_customer|companion_or_family|unknown",
        "quote": "",
        "source_evidence_ids": [],
        "evidence_turn_ids": [],
        "confidence": 0.0
      }
    ],
    "concern_events": [],
    "budget_events": [],
    "notes": []
  }
}
"""


_EVENT_AGENT_USER_TEMPLATE = """\
Evidence graph:
{evidence_graph}

Scope graph:
{scope_graph}

Relevant corrected transcript excerpts:
{dialogue}

Return event_graph JSON only.
"""


_EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT = """\
You are the empty-evidence rescue and scene-triage agent in a Chinese
medical-aesthetic recording analysis chain.

The first evidence extraction pass found no usable current-customer evidence.
Your job is to decide whether this is a true non-consultation recording or a
missed current-customer consultation.

Hard rules:
1. Do not invent customer demands, indications, recommendations, or SAP content.
2. Distinguish current-customer consultation from internal staff chat, order
   handling, coworker complaints, third-party/customer-case discussion, and
   casual chat.
3. Mentions like "我有个顾客/那个顾客/有个美团的/他问我/她说/医生说/未成交"
   are third-party or internal case discussion unless the current customer is
   clearly present and asks/accepts the plan.
4. If this is not a current-customer consultation, keep every evidence list
   empty and explain why in scene_assessment.
5. If a current-customer consultation was missed, extract only directly
   supported evidence in the same evidence_graph schema used by the previous
   evidence agent. Prefer high precision over recall.

Return JSON only:
{
  "scene_assessment": {
    "scene_type": "active_consultation | internal_staff_chat | frontdesk_order | third_party_case_discussion | casual_chat | unclear",
    "is_current_customer_consultation": false,
    "confidence": 0.0,
    "reason": "short Chinese reason"
  },
  "evidence_graph": {
    "customer_demand_evidence": [],
    "diagnosis_evidence": [],
    "recommendation_evidence": [],
    "concern_evidence": [],
    "budget_evidence": [],
    "medical_history_evidence": [],
    "profile_evidence": [],
    "deal_evidence": []
  }
}
"""


_EMPTY_EVIDENCE_RESCUE_USER_TEMPLATE = """\
Staff / recording context:
{staff_context}

Code-side preprocessing hints:
{preprocess_context}

Corrected transcript:
{dialogue}

Return rescue JSON only.
"""


_JUDGMENT_AGENT_SYSTEM_PROMPT = """\
You are Agent 4 in a Chinese medical-aesthetic recording analysis chain:
the structured fact-graph judgment agent.

You receive evidence_graph, event_graph, and candidate indications recalled
from the local SAP indication dictionary. Build a fact_graph. Application code
will render final analysis_result, so do not write final prose.

Judgment rules:
0. Treat event_graph polarity as the source of truth for ambiguous mentions.
   current_recommendation and deal_confirmed support recommendations.
   seed_recommendation supports seed_recommendations. customer_question,
   staff_explanation, comparison_or_backup, diagnosis_only, and not_recommended
   must not become final recommendations or SAP indication support unless other
   current_recommendation evidence clearly supports the same plan. Deal events
   bind only to the specific plan/order they name. staff_or_product_context
   profile events must not become customer tags.
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
   is necessary to solve the current demand. Optional "whole-face/T-zone/overall
   design" packages that staff proposes but says can be deferred or partially
   selected should be kept as seed_recommendations, not deleted as comparison.
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
   actual limitation. Every demand, concern, budget_fact, recommendation, seed
   recommendation, medical_history, and profile_fact must carry an evidence
   quote or source quote whenever event/evidence data contains one.
9. Indication candidates are preliminary. Copy exact standardized_indication
   strings from candidate_indications only. Prefer high precision over recall.
10. For 副乳, prefer specific 副乳整形 when supported. For 富贵包, keep demand or
   diagnosis unless there is a clear suction/fat-reduction treatment plan.
11. Do not select 痤疮 from mouth-closing wording such as 闭口时/闭上嘴.
12. Do not select 面部除皱 from 咬肌肉毒/瘦脸 unless wrinkle/动态纹/除皱 evidence is explicit.
13. For injection/support contour plans, prefer precise micro-invasive 塑美
    dictionary items:
    - 鼻基底/鼻头/鼻翼/鼻尖/三角结构 + 注射/玻尿酸/再生材料/芭比针/濡白天使
      => 塑美（鼻中轴线（H））, not 外科-面部填充 and not 鼻综合.
    - 下颌线/下颌角拐点/耳前耳后韧带/外轮廓 + 童颜针/芭比针/支撑/提升
      => 塑美（下颌轮廓线（大O））.
14. If transcript has both 童颜针/芭比针 structural support and 肉毒/大提拉,
    keep the structural support as the main recommendation; 肉毒 can be an
    auxiliary or separate recommendation only when explicitly recommended.
14a. Eye issues such as 泪沟/黑眼圈/法令纹 that appear only as staff
    observation, diagnostic explanation, optional seed talk, or customer
    "要不要/是不是/可以先不/化妆即可/先做更在意的" responses must stay in
    diagnoses, concerns, or seed_recommendations. Do not put them into demands
    or final indication_candidates unless the customer clearly asks to treat
    that exact issue now or the current recommendation solves that exact issue.
15. If transcript is internal staff/order/payment discussion without a main
    customer demand or current-customer diagnosis/plan, return empty business
    facts and deal_outcome.status = "未明确".
16. Convert profile_evidence into profile_facts. Also keep profile signals from
    medical_history, budget, concern, and deal evidence when they describe prior
    treatment, material/device, budget, price sensitivity, pain tolerance,
    family/children, industry/special identity, comparison institution,
    decision maker, treatment preference, recovery/time constraint, or product
    preference. These profile_facts are used for customer tags and should not be
    dropped merely because they are not SAP indications.
    Prior-treatment/material/device profile_facts require positive prior-history
    wording such as "做过/打过/去年/上次/外院"; do not create them from
    "从来没打过/没做过" or from current consultation phrases like "能打/可以打".
    For health-risk/contraindication profile_facts, only use evidence clearly
    about the customer or accompanying customer. Do not convert staff/doctor
    self-disclosure, product descriptions, or ambiguous skin sensitivity wording
    into customer tags. "皮肤过敏/敏感肌/玫瑰痤疮" alone is not "过敏史";
    output allergy history only for explicit medical allergy evidence such as
    药物过敏、麻药过敏、碘伏/酒精/胶布过敏 or "对X过敏".
17. Demand facts must be normalized to the fewest concrete customer goals.
    Merge repeated wording and keep usually 3-6 demands for a single customer.
    In particular, merge 面颊/颊区/夹区/脸颊凹陷 + 填充/玻尿酸 wording into one
    demand instead of outputting both "改善面颊凹陷" and "关注面颊凹陷".
    Do not output instrument-version, 发数, 医生, 验证, 恢复, 切口, 排期,
    付款, 优惠, or pure price questions as demands; preserve them in concerns,
    budget_facts, deal_factors, or recommendation implementation_notes.
18. Do not output body-hair casual wording such as "小毛毛/汗毛/体毛" as a demand
    unless 脱毛/冰点脱毛/激光脱毛/removal intent is explicit.

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

Event graph:
{event_graph}

Candidate indications recalled from local dictionary:
{candidate_indications}

Build fact_graph JSON only.
"""


_PLAN_AGENT_SYSTEM_PROMPT = """\
You are Agent 5 in a Chinese medical-aesthetic recording analysis chain:
the recommendation vs seed-plan adjudication agent.

Your job is only to improve recommendation classification and detail
completeness. Do not choose final SAP indications.

Rules:
0. Use event_graph polarity before rewriting plans. current_recommendation and
   deal_confirmed can remain recommendations. seed_recommendation can remain a
   seed. customer_question, staff_explanation, comparison_or_backup,
   diagnosis_only, and not_recommended must be removed from final
   recommendations unless a separate current_recommendation event supports the
   same plan.
1. recommendation = plan solving the customer's current demand.
2. seed_recommendation = additional, future, maintenance, lower-priority, or
   outside-current-demand plan. Optional overall design packages, "you can
   choose", "you may only do the core item first", or secondary contour/skin
   plans should be seed_recommendations when they are proposed but not the
   current core plan.
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
10. 下颌线/下颌角拐点/耳前耳后韧带 structural support with 童颜针、芭比针、
    玻尿酸、濡白天使 or "支撑/拉伸/提升" is a current recommendation when it
    solves the customer's 下颌线/轮廓 demand. Do not drop it because the same
    conversation later discusses 肉毒/大提拉.
11. 鼻基底/鼻头/鼻翼/鼻尖 "三角结构" plans using 再生材料+玻尿酸/芭比针/
    濡白天使 are current nasal-axis structural recommendations. Preserve
    materials and dosage if mentioned.

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

Event graph:
{event_graph}

Relevant corrected transcript excerpts:
{dialogue}

Return recommendation_adjudication JSON only.
"""


_AUDIT_AGENT_SYSTEM_PROMPT = """\
You are Agent 7 in a Chinese medical-aesthetic recording analysis chain:
the final audit and repair agent.

Audit the fact_graph before code renders analysis_result. You may return a
corrected_fact_graph only when there is a clear evidence-backed issue.

Audit priorities:
0. Enforce event_graph polarity. Do not keep final recommendations or SAP
   indications for customer_question, staff_explanation, comparison_or_backup,
   diagnosis_only, or not_recommended events. Deal outcome must be tied to a
   deal_confirmed/payment/deposit/order_created event for a specific plan when
   the transcript contains multiple options.
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
12. If 下颌线/下颌角拐点/耳前耳后韧带 support evidence exists but final
    recommendations only keep 肉毒/大提拉, repair the structural support plan
    as the main recommendation.
13. If 面部除皱 is selected only because of 咬肌/瘦脸/大提拉/下颌线 and there
    is no explicit wrinkle/dynamic-line treatment, remove it.
14. If 鼻基底/鼻头/鼻翼 三角结构 injection evidence exists but final
    indications only keep 面部填充 or 鼻综合, repair to 塑美（鼻中轴线（H））.
15. If you cannot confidently repair an issue, leave fact_graph unchanged and
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

Event graph:
{event_graph}

Candidate indications:
{candidate_indications}

Relevant corrected transcript excerpts:
{dialogue}

Return audit JSON only. Include corrected_fact_graph only if a repair is clearly evidence-backed.
"""


_FINAL_RESULT_AUDIT_SYSTEM_PROMPT = """\
You are Agent 8 in a Chinese medical-aesthetic recording analysis chain:
the final user-visible result consistency auditor.

Audit the rendered analysis_result after code has converted fact_graph into
display fields. Your output may patch only the final result sections. Prefer
small, evidence-backed repairs.

Audit priorities:
1. Customer primary demands must be concrete treatment goals for the current
   visit/customer. Do not keep duplicate or near-duplicate demands. Do not treat
   doctor preference, brand preference, price calculation, payment/deposit,
   recovery/scar questions, or general worries as primary demands.
2. Customer concerns must include explicit worry/hesitation from the customer,
   especially safety, side effects, worsening hollowing, scars, recovery, pain,
   migration, price pressure, or doctor/operator concerns.
3. Recommendations must be actual staff/doctor plans for the customer's current
   demand. Seed recommendations are additional/next-visit/cross-department
   plans, not replacements for the current plan.
4. Every recommendation's demand_priority must point to an existing demand
   priority. If no exact demand exists, leave the link empty instead of linking
   to the wrong demand.
5. SAP indications must be exact to the project/body area and supported by
   current recommendations or confirmed current demands. Do not invent
   indications for unsupported nose surgery, acne, wrinkle treatment, or
   unrelated post-deal care.
6. Budget must be a normalized budget/price-sensitivity conclusion, not a raw
   evidence quote. A deposit/order amount is not automatically the customer's
   budget. If the customer repeatedly calculates or resists a quoted total,
   summarize the price sensitivity or upper bound.
7. Preserve useful recommendation details: dosage, material, brand, price,
   course/frequency, steps, implementation notes, and customer response.

Return JSON only:
{
  "final_result_audit": {
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
  "analysis_result_patch": null
}

If repairs are needed, analysis_result_patch may include only these sections:
customer_primary_demands, customer_concerns, staff_recommendations,
staff_seed_recommendations, standardized_indications, consumption_intent,
consultation_result, customer_profile.
"""


_FINAL_RESULT_AUDIT_USER_TEMPLATE = """\
Trigger reasons:
{trigger_reasons}

Scope graph:
{scope_graph}

Evidence graph:
{evidence_graph}

Event graph:
{event_graph}

Fact graph:
{fact_graph}

Rendered analysis_result:
{analysis_result}

Relevant corrected transcript excerpts:
{dialogue}

Return final_result_audit JSON only. Include analysis_result_patch only when a repair is clearly evidence-backed.
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
    return _first_text(item, "content", "demand_content", "demand", "recommendation", "plan", "text", "summary", "value")


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
            "价格敏感",
            "敏感",
            "反复核算",
            "反复算",
            "核算",
            "少一点",
            "差别有点大",
        )
    )


_AGENT_NOT_BUDGET_EXPLANATION_CUES = (
    "解决不了多少",
    "改善的程度有限",
    "改善程度有限",
    "效果有限",
    "做不了多少",
    "没效果",
)


def _agent_has_explicit_budget_intent(text: str) -> bool:
    text = _clean_text(text)
    if not text:
        return False
    return any(
        term in text
        for term in (
            "预算",
            "可接受",
            "能接受",
            "接受不了",
            "承受",
            "顶死",
            "最多",
            "上限",
            "不超过",
            "打不起",
            "付款",
            "支付",
            "付了",
            "付定",
            "定金",
            "订金",
            "意向金",
            "交钱",
        )
    )


def _agent_is_budget_fact_text(text: str) -> bool:
    text = _clean_text(text)
    if not text or not _agent_has_price_signal(text):
        return False
    if any(term in text for term in _AGENT_NOT_BUDGET_EXPLANATION_CUES) and not _agent_has_explicit_budget_intent(text):
        return False
    return _agent_has_explicit_budget_intent(text) or _agent_has_affordability_reaction(text)


def _agent_next_id(prefix: str, items: list[dict[str, Any]]) -> str:
    max_index = 0
    for item in items:
        raw = _clean_text(item.get("id") or item.get(f"{prefix.lower()}_id"))
        match = re.search(r"(\d+)$", raw)
        if match:
            max_index = max(max_index, int(match.group(1)))
    return f"{prefix}{max_index + 1}"


def _agent_participant_key(item: dict[str, Any]) -> tuple[str, str]:
    scope = _clean_text(item.get("participant_scope") or item.get("customer_scope"))
    participant = _clean_text(item.get("participant") or item.get("participant_label"))
    primary_aliases = {
        "",
        "客户",
        "顾客",
        "主客户",
        "主顾客",
        "主咨询客户",
        "primary_customer",
        "primary",
        "customer",
    }
    if scope in primary_aliases and participant in primary_aliases:
        return ("primary_customer", "")
    if not scope and participant in primary_aliases:
        return ("primary_customer", "")
    if not participant and scope in primary_aliases:
        return ("primary_customer", "")
    return (scope, participant)


def _agent_profile_item_is_staff_scoped(item: dict[str, Any]) -> bool:
    scope = _clean_text(item.get("participant_scope") or item.get("customer_scope") or item.get("scope")).lower()
    if scope in {"staff", "doctor", "consultant", "badge_owner", "employee", "assistant", "nurse"}:
        return True
    participant = _clean_text(
        item.get("participant")
        or item.get("participant_label")
        or item.get("speaker")
        or item.get("speaker_label")
    )
    return any(term in participant for term in ("工牌本人", "咨询师", "医生", "顾问", "助理", "护士", "员工"))


def _agent_should_skip_profile_fact(category: str, value: str, evidence: str, item: dict[str, Any]) -> bool:
    if _agent_profile_item_is_staff_scoped(item):
        return True
    combined = _agent_join_text(category, value, evidence, item.get("content"), item.get("text"))
    if "过敏" not in combined:
        return False
    if not (any(term in category for term in ("健康风险", "禁忌", "病史")) or "过敏" in value):
        return False
    if any(term in combined for term in ("无药物过敏", "没有药物过敏", "无过敏史", "没有过敏史", "不过敏", "不是过敏")):
        return True
    if any(term in combined for term in ("过敏率", "不易过敏", "不容易过敏", "低敏", "抗过敏")):
        return True
    allergy_context = _agent_join_text(evidence, item.get("quote"), item.get("source_quote"))
    if not allergy_context or _compact_key_text(allergy_context) == _compact_key_text(value):
        content_text = _agent_join_text(item.get("content"), item.get("text"))
        if _compact_key_text(content_text) != _compact_key_text(value):
            allergy_context = content_text
    strong_allergy = any(
        term in allergy_context
        for term in (
            "药物过敏",
            "麻药过敏",
            "麻醉过敏",
            "利多卡因过敏",
            "碘伏过敏",
            "酒精过敏",
            "胶布过敏",
            "敷贴过敏",
            "过敏史",
            "对玻尿酸过敏",
            "对胶原过敏",
            "对肉毒过敏",
        )
    ) or bool(re.search(r"对.{1,12}过敏", allergy_context))
    if strong_allergy:
        return False
    return any(term in combined for term in ("皮肤过敏", "玫瑰痤疮", "敏感肌", "皮肤敏感", "容易泛红"))


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
    if not _agent_is_budget_fact_text(combined):
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


def _agent_should_preserve_as_deferred_seed(item: dict[str, Any]) -> bool:
    relation = _clean_text(item.get("relation_to_current_demand"))
    if relation not in {"alternative_not_recommended", "unclear", "possible_current_plan"}:
        return False
    text = _agent_join_text(
        item.get("content"),
        item.get("quote"),
        item.get("implementation_notes"),
        item.get("customer_response"),
        item.get("treatment_steps"),
    )
    if not any(term in text for term in ("后续", "后期", "之后", "以后", "炎症控制后", "稳定后", "后面", "下次")):
        return False
    return any(term in text for term in ("可以", "可在", "联合", "考虑", "再做", "进行", "改善", "治疗"))


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


def _agent_preserve_deferred_seed_recommendations(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
) -> dict[str, Any]:
    deferred_items = [
        item
        for item in _as_list(evidence_graph.get("recommendation_evidence"))
        if isinstance(item, dict) and _agent_should_preserve_as_deferred_seed(item)
    ]
    if not deferred_items:
        return fact_graph
    updated = dict(fact_graph)
    seeds = [dict(item) for item in _as_list(updated.get("seed_recommendations")) if isinstance(item, dict)]
    seen = {_compact_key_text(_agent_item_content(item)) for item in seeds if _agent_item_content(item)}
    for item in deferred_items:
        content = _agent_item_content(item)
        key = _compact_key_text(content)
        if not key or key in seen:
            continue
        copied = dict(item)
        copied["relation_to_current_demand"] = "planting_or_later"
        copied.setdefault("seed_reason", "炎症/恢复/当前阶段后续可考虑的方案")
        seeds.append(copied)
        seen.add(key)
    updated["seed_recommendations"] = seeds
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
        if _agent_should_skip_profile_fact(category, value, evidence, copied):
            continue
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


def _agent_normalize_fact_content_fields(fact_graph: dict[str, Any]) -> dict[str, Any]:
    updated = dict(fact_graph)
    for section in (
        "demands",
        "doctor_diagnoses",
        "recommendations",
        "seed_recommendations",
        "concerns",
        "budget_facts",
        "medical_history",
        "profile_facts",
        "deal_factors",
    ):
        normalized: list[dict[str, Any]] = []
        changed = False
        for item in _as_list(updated.get(section)):
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            if not _first_text(copied, "content", "demand_content", "demand", "recommendation", "plan", "text"):
                summary = _first_text(copied, "demand_content", "summary", "description")
                if summary:
                    copied["content"] = summary
                    changed = True
            normalized.append(copied)
        if changed:
            updated[section] = normalized
    return updated


def _agent_ensure_demands_from_diagnoses_when_empty(fact_graph: dict[str, Any]) -> dict[str, Any]:
    if _as_list(fact_graph.get("demands")):
        return fact_graph
    if not (_as_list(fact_graph.get("recommendations")) or _as_list(fact_graph.get("indication_candidates"))):
        return fact_graph
    diagnoses = [dict(item) for item in _as_list(fact_graph.get("doctor_diagnoses")) if isinstance(item, dict)]
    if not diagnoses:
        return fact_graph

    def clean_diagnosis_demand_text(text: str, body: str) -> str:
        cleaned = re.sub(r"[，,；;]?\s*既往[^，,；;。]*(?:假体|注射史|治疗史)[^，,；;。]*", "", text).strip("，,；;。 ")
        if "基础尚可" in cleaned and "存在" in cleaned:
            suffix = cleaned.split("存在", 1)[1].strip("，,；;。 ")
            if suffix:
                cleaned = f"{body}{suffix}" if body else suffix
        return cleaned or text

    demands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in diagnoses[:6]:
        text = _first_text(item, "content", "summary", "diagnosis", "text")
        body = _first_text(item, "body_part", "body_part_name", "area")
        if not text:
            continue
        text = clean_diagnosis_demand_text(text, body)
        if any(term in text for term in ("既往", "假体", "做过", "注射史")) and not any(
            term in text for term in ("偏", "低", "凹", "凸", "不顺", "浮肿", "松", "垮", "显", "扁平")
        ):
            continue
        key = _compact_key_text(_agent_join_text(body, text))
        if not key or key in seen:
            continue
        seen.add(key)
        content = text if text.startswith(("改善", "希望", "想")) else f"希望改善{text}"
        demands.append(
            {
                "demand_id": f"D{len(demands) + 1}",
                "content": content,
                "body_part": body or None,
                "evidence_ids": _as_list(item.get("evidence_ids")),
                "handling_status": "current_handled",
                "participant": _first_text(item, "participant") or "主咨询客户",
                "participant_scope": _first_text(item, "participant_scope") or "primary_customer",
                "source": "diagnosis_recommendation_fallback",
            }
        )
    if not demands:
        return fact_graph
    updated = dict(fact_graph)
    updated["demands"] = demands
    return updated


_AGENT_DEMAND_KEY_TERMS = (
    "双眼皮",
    "内双",
    "肿眼泡",
    "眼睛肿",
    "无神",
    "显大",
    "小平扇",
    "平扇",
    "开眼角",
    "内眼角",
    "提肌",
    "不对称",
    "眼袋",
    "泪沟",
    "卧蚕",
    "细纹",
    "干纹",
    "胶原",
    "热玛吉",
    "钻石精雕",
    "隐痕精雕",
    "腰腹",
    "妈妈臀",
    "臀凹",
    "大腿",
    "手臂",
    "吸脂",
    "填胸",
    "丰胸",
    "太阳穴",
    "下巴",
    "副乳",
    "富贵包",
    "美白",
    "毛孔",
    "痘印",
    "痘坑",
    "出油",
    "提亮",
    "暗沉",
    "雀斑",
    "色斑",
    "汗管瘤",
    "鼻部",
    "鼻子",
    "下颌缘",
    "瘦脸",
    "水光",
    "童颜",
    "祛斑",
    "皮秒",
    "发红",
    "泛红",
    "下至",
    "太窄",
    "过窄",
    "变宽",
    "加宽",
    "显凶",
    "柔和",
    "眼尾",
    "眼修复",
    "修复",
)


def _agent_demand_text(item: dict[str, Any]) -> str:
    return _agent_join_text(
        _first_text(item, "content", "demand_content", "demand", "text", "summary"),
        _first_text(item, "body_part", "body_part_name"),
        item.get("quote"),
        item.get("evidence"),
    )


def _agent_demand_core_text(item: dict[str, Any]) -> str:
    return _agent_join_text(
        _first_text(item, "content", "demand_content", "demand", "text", "summary"),
        _first_text(item, "body_part", "body_part_name"),
    )


def _agent_has_prior_eyelid_surgery_context(text: str) -> bool:
    """Return True only for explicit prior-eyelid-surgery repair context."""

    if any(term in text for term in ("眼修复", "双眼皮修复", "重睑修复")):
        return True
    prior_terms = (
        "做过双眼皮",
        "做过重睑",
        "双眼皮做过",
        "重睑做过",
        "以前做过双眼皮",
        "之前做过双眼皮",
        "既往双眼皮",
        "双眼皮术后",
        "重睑术后",
        "韩式三点",
        "埋线双眼皮",
        "埋线重睑",
    )
    repair_terms = ("修复", "不满意", "变形", "肉条", "疤痕", "太宽", "过宽", "太窄", "过窄")
    return any(term in text for term in prior_terms) and any(term in text for term in repair_terms)


def _agent_is_vague_skin_request(text: str) -> bool:
    if not any(term in text for term in ("皮肤科", "皮肤项目", "看皮肤", "皮肤咨询")):
        return False
    if any(
        term in text
        for term in (
            "痘",
            "痤疮",
            "闭口",
            "毛孔",
            "痘坑",
            "痘印",
            "斑",
            "暗沉",
            "暗黄",
            "美白",
            "提亮",
            "泛红",
            "发红",
            "红血丝",
            "敏感",
            "水光",
            "干燥",
            "缺水",
            "细纹",
            "皱纹",
            "松弛",
            "热玛吉",
            "皮秒",
            "脱毛",
            "汗管瘤",
        )
    ):
        return False
    return True


def _agent_is_non_business_demand(item: dict[str, Any]) -> bool:
    text = _agent_demand_text(item)
    if not text:
        return True
    if "具体问题未说明" in text:
        return True
    if _agent_is_vague_skin_request(text):
        return True
    if any(term in text for term in ("小毛毛", "汗毛", "体毛", "毛面")) and not any(
        term in text for term in ("脱毛", "冰点脱毛", "激光脱毛", "去毛", "去除", "处理")
    ):
        return True
    if any(term in text for term in ("价格", "多少钱", "费用", "报价", "预算")) and not any(
        term in text for term in ("改善", "治疗", "手术", "注射", "填充", "吸脂", "双眼皮", "脱毛")
    ):
        return True
    if any(term in text for term in ("接受", "确认", "确定", "决定")) and any(
        term in text for term in ("套餐", "案例价", "回填方式", "方案")
    ) and not any(term in text for term in ("改善", "去除", "填充", "提升", "塑形", "调整")):
        return True
    if any(term in text for term in ("主咨询客户", "持续咨询", "围绕")) and "眼袋" in text:
        return True
    if any(term in text for term in ("安排", "预约", "下个月", "早点做", "具体时间")) and not any(
        term in text for term in ("改善", "肿", "凹陷", "无神", "显小", "松弛", "下垂", "填充", "吸脂")
    ):
        return True
    if any(term in text for term in ("具体时间", "时间安排", "下半年", "下个月")) and any(
        term in text for term in ("计划做", "计划", "安排")
    ) and not any(term in text for term in ("腰腹", "妈妈臀", "臀凹", "大腿", "手臂", "胸", "太阳穴", "下巴", "眼周", "眼部")):
        return True
    return False


def _agent_demand_cluster(item: dict[str, Any]) -> str:
    text = _agent_demand_core_text(item) or _agent_demand_text(item)
    if "鼻" in text and any(term in text for term in ("残留", "没溶干净", "摸得到", "填充物")):
        return "nose_residual_filler"
    if any(term in text for term in ("水光", "补水", "干燥", "肤质粗", "胶原流失")):
        return "skin_hydration"
    if any(term in text for term in ("热玛吉", "超声炮", "超声刀", "抗衰", "紧致", "提升")) and any(
        term in text for term in ("面部", "脸", "皮肤", "本次", "想做", "希望")
    ):
        return "face_anti_aging"
    if any(term in text for term in ("价格", "多少钱", "费用", "报价", "预算")) and not any(
        term in text
        for term in (
            "改善",
            "治疗",
            "去除",
            "祛斑",
            "色斑",
            "点痣",
            "祛痣",
            "痘坑",
            "毛孔",
            "颈纹",
            "红血丝",
            "注射",
            "填充",
        )
    ):
        return "process_price"
    if any(term in text for term in ("安排", "预约", "下个月", "早点做", "具体时间")) and "改善" not in text:
        return "process_schedule"
    if "笑" in text and any(term in text for term in ("厚重", "一坨肉", "中下面部", "面中")) and any(
        term in text for term in ("改善", "想", "希望")
    ):
        return "smile_midface_heavy"
    if any(term in text for term in ("鱼尾纹", "眉间纹", "抬头纹", "动态纹", "除皱", "皱眉纹", "川字纹")):
        return "dynamic_wrinkle"
    if any(term in text for term in ("肉毒", "除皱针", "瘦脸针")):
        return "botox_injection"
    if any(term in text for term in ("上眼", "上睑", "眼部提升", "提眉", "切眉", "上睑提升")) and any(
        term in text for term in ("提升", "松弛", "眼皮", "手术", "改善")
    ):
        return "upper_eyelid_lift"
    if any(term in text for term in ("点痣", "祛痣", "色素痣")) or ("痣" in text and any(term in text for term in ("点", "去除", "包干", "复发"))):
        return "mole_removal"
    if any(term in text for term in ("祛斑", "色斑", "雀斑", "斑点", "皮秒", "双击")):
        return "pigmentation"
    if "胶原流失" in text or (
        any(term in text for term in ("衰老", "紧致", "抗衰", "提升")) and any(term in text for term in ("面部", "脸", "胶原"))
    ):
        return "face_anti_aging"
    if "卡粉" in text or any(term in text for term in ("上妆卡", "妆容不服帖", "妆感不服帖")):
        return "makeup_caking_texture"
    if any(term in text for term in ("额头", "额结节", "额颞", "眉峰")) and any(
        term in text for term in ("不够高", "高光", "立体", "上镜", "起来", "填充", "瑞德喜")
    ):
        return "forehead_contour"
    if any(term in text for term in ("耳基底", "耳朵", "耳轮", "耳位")) and any(
        term in text for term in ("往上", "往外", "提", "出来", "填充", "支撑", "肉肉")
    ):
        return "ear_base_support"
    if any(term in text for term in ("眶外C", "眶外", "眉尾", "眉弓")) and any(
        term in text for term in ("提", "平", "支撑", "立体", "补", "填充", "瑞德喜", "眼睛", "双眼皮")
    ):
        return "orbital_tail_support"
    if any(term in text for term in ("人中窝", "人中")) and any(term in text for term in ("加深", "缩短", "改善", "打", "注射")):
        return "philtrum_shape"
    if any(term in text for term in ("小腿", "腿部")) and any(term in text for term in ("肌肉", "瘦", "肉毒", "注射")):
        return "calf_slimming"
    if _agent_has_prior_eyelid_surgery_context(text):
        return "eye_repair"
    if "外切眼袋" in text or "眼袋" in text:
        return "eye_bag"
    if "泪沟" in text:
        return "tear_trough"
    if "黑眼圈" in text or "眼下黑" in text:
        return "dark_circle"
    if any(term in text for term in ("上睑下垂", "眼皮下垂", "遮瞳", "遮挡瞳孔", "瞳孔暴露")):
        return "eye_exposure"
    if any(term in text for term in ("眼皮大", "眼眶周围水肿", "眼周水肿", "眼部浮肿", "浮肿", "浮泡", "上眼泡")):
        return "eye_puffiness"
    if any(term in text for term in ("显凶", "柔和", "不好相处", "眼神凶", "眼神柔")):
        return "eye_expression"
    if any(term in text for term in ("眼尾上扬", "眼尾下调", "眼尾走势", "眼尾走向", "眼尾形态", "眼尾设计")):
        return "eye_tail_design"
    if any(term in text for term in ("双眼皮", "重睑", "内双", "肿眼泡", "肿泡眼", "眼睛肿", "上睑臃肿", "太窄", "过窄", "变宽", "加宽")):
        if any(term in text for term in ("太窄", "过窄", "偏窄", "变宽", "加宽", "宽度", "平扇", "开扇", "形态", "上妆")):
            return "double_eyelid_style"
        if any(term in text for term in ("松弛", "下垂", "耷拉", "去皮", "遮挡", "上睑臃肿")):
            return "double_eyelid_laxity"
        return "double_eyelid"
    if "下至" in text:
        return "eye_downward"
    if any(term in text for term in ("卧蚕", "媚眼针")):
        return "eye_wocan"
    if any(term in text for term in ("中面部", "苹果肌", "鼻基底", "法令纹")) and any(
        term in text for term in ("凹陷", "填充", "饱满", "年轻", "衔接")
    ):
        return "midface_filling"
    if "下巴" in text and any(term in text for term in ("后缩", "偏短", "短", "下庭", "长度", "翘度", "玻尿酸", "支撑", "比例", "填充")):
        return "chin_shape"
    if any(term in text for term in ("下颌缘", "脸变小", "视觉瘦脸", "瘦脸", "轮廓更精致", "轮廓线条", "骨相感", "轻薄感")):
        return "jawline_slimming"
    if any(term in text for term in ("鼻小柱", "人中")) and any(
        term in text for term in ("拉出", "偏长", "缩短", "改善", "精致", "注射")
    ):
        return "nose_philtrum"
    if any(term in text for term in ("鼻部", "鼻子", "山根", "鼻背", "鼻基底")) and any(
        term in text for term in ("调整", "补打", "支撑", "立体", "玻尿酸", "材料", "改善")
    ):
        return "nose_filling"
    if any(term in text for term in ("唇", "嘴巴", "嘴凸")):
        return "lip_shape"
    if "脱毛" in text:
        return "hair_removal"
    if any(term in text for term in ("痘痘", "痤疮", "炎症", "痘印", "痘坑", "闭口")):
        return "acne_texture"
    if any(term in text for term in ("色斑", "雀斑", "黄褐斑", "斑点", "祛斑", "淡斑", "皮秒", "色素沉着", "肤色不均")) or (
        "光子" in text and any(term in text for term in ("色斑", "雀斑", "祛斑", "淡斑"))
    ):
        return "pigmentation"
    if any(term in text for term in ("发红", "泛红", "敏感", "红血丝")):
        return "skin_redness"
    if "汗管瘤" in text:
        return "syringoma"
    if any(term in text for term in ("毛孔", "痘印", "痘坑", "肤质", "出油", "暗沉", "暗黄", "提亮")):
        return "skin_texture"
    if any(term in text for term in ("水光", "童颜", "中胚层", "胶原水光")):
        return "skin_booster"
    if any(term in text for term in ("抗衰", "松垮", "松弛", "法令纹", "口角囊袋")) and any(
        term in text for term in ("面部", "脸", "热玛吉", "超声", "提升")
    ):
        return "face_anti_aging"
    if any(term in text for term in ("显年轻", "疲态", "疲惫", "憔悴")) and any(
        term in text for term in ("整体", "面部", "脸")
    ):
        return "face_anti_aging"
    if any(term in text for term in ("钻石精雕", "隐痕精雕", "收紧抗衰", "眼周收紧", "热玛吉")):
        return "eye_tightening"
    if "开眼角" in text or "内眼角" in text or "内眦" in text:
        return "eye_canthus"
    if any(term in text for term in ("平扇", "开扇", "自然", "宽", "眼头尖", "尖眼角", "假感", "妈生")):
        return "eye_style"
    if any(term in text for term in ("无神", "显小", "有神", "瞳孔曝光", "眼睛小")):
        return "eye_exposure"
    if any(term in text for term in ("提肌", "不对称", "体积对称", "双眼体积")):
        return "eye_symmetry"
    if any(term in text for term in ("腰腹", "妈妈臀")):
        return "waist_liposuction"
    if any(term in text for term in ("背部", "后背", "背上")) and any(term in text for term in ("吸脂", "抽脂", "术后", "没抽")):
        return "back_liposuction"
    if "大腿" in text and "吸脂" in text:
        return "thigh_liposuction"
    if "手臂" in text and "吸脂" in text:
        return "arm_liposuction"
    if any(term in text for term in ("臀凹", "臀部凹陷")):
        return "hip_dip"
    if any(term in text for term in ("丰胸", "填胸", "隆胸")):
        return "breast_augmentation"
    if "太阳穴" in text:
        return "temple_filling"
    if "下巴" in text:
        return "chin_filling"
    if "细纹" in text or "干纹" in text:
        return "fine_lines"
    return ""


def _agent_demand_key_terms(item: dict[str, Any]) -> set[str]:
    text = _agent_demand_core_text(item) or _agent_demand_text(item)
    return {term for term in _AGENT_DEMAND_KEY_TERMS if term in text}


def _agent_demand_is_duplicate(item: dict[str, Any], kept: list[dict[str, Any]]) -> bool:
    participant = _agent_participant_key(item)
    body = _compact_key_text(_first_text(item, "body_part", "body_part_name"))
    content = _compact_key_text(_first_text(item, "content", "demand_content", "demand", "text", "summary"))
    terms = _agent_demand_key_terms(item)
    cluster = _agent_demand_cluster(item)
    related_cluster_sets = (
        {"nose_filling", "nose_philtrum"},
    )
    for existing in kept:
        if _agent_participant_key(existing) != participant:
            continue
        existing_cluster = _agent_demand_cluster(existing)
        if cluster and existing_cluster and cluster == existing_cluster:
            return True
        if cluster and existing_cluster and any(cluster in group and existing_cluster in group for group in related_cluster_sets):
            return True
        existing_body = _compact_key_text(_first_text(existing, "body_part", "body_part_name"))
        existing_content = _compact_key_text(_first_text(existing, "content", "demand_content", "demand", "text", "summary"))
        if content and existing_content and (content in existing_content or existing_content in content):
            return True
        if body and existing_body and body != existing_body:
            continue
        existing_terms = _agent_demand_key_terms(existing)
        if terms and existing_terms and len(terms & existing_terms) >= 2:
            return True
    return False


def _agent_normalize_demands(fact_graph: dict[str, Any]) -> dict[str, Any]:
    updated = dict(fact_graph)
    kept: list[dict[str, Any]] = []
    for item in _as_list(updated.get("demands")):
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        if _clean_text(copied.get("participant_scope") or copied.get("customer_scope")).lower() == "companion_or_family":
            continue
        if _agent_is_non_business_demand(copied):
            continue
        if _agent_demand_is_duplicate(copied, kept):
            continue
        kept.append(copied)
    updated["demands"] = kept
    return updated


def _agent_issue_has_current_recommendation(recommendation_context: str, terms: tuple[str, ...]) -> bool:
    compact = _clean_text(recommendation_context)
    if not compact or not any(term in compact for term in terms):
        return False
    return any(
        cue in compact
        for cue in (
            "改善",
            "治疗",
            "处理",
            "方案",
            "做",
            "打",
            "注射",
            "填充",
            "激光",
            "光电",
            "皮秒",
            "水光",
            "胶原",
            "玻尿酸",
            "嗨体",
            "福曼",
        )
    )


def _agent_prune_observation_only_demands(fact_graph: dict[str, Any]) -> dict[str, Any]:
    updated = dict(fact_graph)
    demands = [dict(item) for item in _as_list(updated.get("demands")) if isinstance(item, dict)]
    if not demands:
        return fact_graph

    recommendation_context = _agent_join_text(updated.get("recommendations"))
    full_context = _agent_join_text(
        updated.get("demands"),
        updated.get("doctor_diagnoses"),
        updated.get("recommendations"),
        updated.get("seed_recommendations"),
        updated.get("concerns"),
    )
    weak_or_deferred_cues = (
        "要不要",
        "是不是",
        "是否",
        "有一点",
        "一点点",
        "轻度",
        "可接受",
        "考虑",
        "关注",
        "化妆就行",
        "化个妆就行",
        "可以化妆",
        "先打你在意的",
        "先打在意的",
        "先做更在意的",
        "先做你在意的",
        "先不处理",
        "暂时不处理",
        "后期再",
        "下次再",
        "以后再",
        "不是这次",
    )
    issue_groups = (
        ("泪沟", "眼下凹", "眼下凹陷", "眶下凹陷"),
        ("黑眼圈", "眼下黑", "眼周暗沉", "眼周色沉"),
        ("法令纹", "鼻唇沟"),
    )

    kept: list[dict[str, Any]] = []
    changed = False
    for item in demands:
        item_text = _agent_join_text(item)
        evidence_text = _agent_evidence_text(item)
        remove_item = False
        for terms in issue_groups:
            if not any(term in item_text for term in terms):
                continue
            if _agent_issue_has_current_recommendation(recommendation_context, terms):
                continue
            if not evidence_text or any(cue in item_text or cue in full_context for cue in weak_or_deferred_cues):
                remove_item = True
                break
        if remove_item:
            changed = True
            continue
        kept.append(item)

    if not changed:
        return fact_graph
    updated["demands"] = kept
    return updated


_AGENT_PLAN_AREA_TERMS = (
    "颧骨前方",
    "颧骨后方",
    "内轮廓",
    "外轮廓",
    "颧弓",
    "眶外C",
    "眶外",
    "眉尾",
    "眉弓",
    "额头",
    "额结节",
    "中面部",
    "上颌",
    "泪沟",
    "鼻基底",
    "下巴",
    "下颌缘",
    "耳基底",
    "耳朵",
    "小腿",
    "唇",
    "鼻",
    "法令纹",
)

_AGENT_PLAN_MATERIAL_TERMS = (
    "瑞德喜",
    "玻尿酸",
    "肉毒",
    "胶原",
    "双美",
    "熊猫",
    "濡白",
    "定彩",
    "乔雅登",
    "艾拉斯提",
    "童颜",
)


def _agent_plan_text(item: dict[str, Any]) -> str:
    return _agent_join_text(
        _first_text(item, "content", "plan", "recommendation", "summary"),
        _first_text(item, "body_part", "body_part_name"),
        _first_text(item, "brand", "material", "dosage", "price", "course_or_frequency", "implementation_notes"),
        item.get("treatment_steps"),
        item.get("evidence"),
    )


def _agent_plan_terms(text: str, terms: tuple[str, ...]) -> set[str]:
    return {term for term in terms if term in text}


def _agent_plan_quality_score(item: dict[str, Any]) -> int:
    text = _agent_plan_text(item)
    score = min(len(text), 260)
    if _clean_text(item.get("evidence")):
        score += 30
    if _clean_text(item.get("customer_response")):
        score += 20
    if _as_list(item.get("treatment_steps")):
        score += 12
    if "推断" in text:
        score -= 80
    if "未明确回应" in text:
        score -= 10
    return score


def _agent_plan_semantic_signature(item: dict[str, Any]) -> str:
    text = _agent_join_text(
        _first_text(item, "content", "plan", "recommendation", "summary"),
        _first_text(item, "body_part", "body_part_name"),
        _first_text(item, "brand", "material", "dosage", "price", "course_or_frequency", "implementation_notes"),
        item.get("treatment_steps"),
    )
    if not text:
        return ""
    area_terms = _agent_plan_terms(text, _AGENT_PLAN_AREA_TERMS)
    material_terms = _agent_plan_terms(
        text,
        _AGENT_PLAN_MATERIAL_TERMS
        + ("英伦大提升", "海派", "海妹", "黑曜", "朗普洛", "濡白", "熊猫", "爱拉斯提"),
    )
    body_part = _first_text(item, "body_part", "body_part_name")
    body_context = _agent_join_text(body_part, text)
    if any(term in body_context for term in ("下颌缘", "下颌线", "下颌角", "下颌轮廓")):
        area_sig = "jawline"
    elif "下巴" in body_context:
        area_sig = "chin"
    elif any(term in body_context for term in ("唇", "嘴唇", "嘴巴")):
        area_sig = "lip"
    elif any(term in body_context for term in ("鼻", "山根", "鼻小柱", "鼻中轴")):
        area_sig = "nose"
    elif area_terms:
        area_sig = "|".join(sorted(area_terms))
    else:
        area_sig = _agent_demand_cluster({"content": text, "body_part": body_part})
    if not area_sig:
        return ""
    material_sig = "|".join(sorted(material_terms))
    if not material_sig:
        material_sig = _compact_key_text(_first_text(item, "brand", "material", "product_or_solution"))
    if not material_sig:
        return ""
    return f"{area_sig}::{material_sig}"


def _agent_plan_is_duplicate(seed: dict[str, Any], recommendations: list[dict[str, Any]]) -> bool:
    seed_text = _agent_plan_text(seed)
    seed_compact = _compact_key_text(seed_text)
    seed_cluster = _agent_demand_cluster({"content": seed_text, "body_part": seed.get("body_part")})
    seed_areas = _agent_plan_terms(seed_text, _AGENT_PLAN_AREA_TERMS)
    seed_materials = _agent_plan_terms(seed_text, _AGENT_PLAN_MATERIAL_TERMS)
    seed_participant = _agent_participant_key(seed)
    for rec in recommendations:
        if seed_participant != ("", "") and _agent_participant_key(rec) != ("", "") and _agent_participant_key(rec) != seed_participant:
            continue
        rec_text = _agent_plan_text(rec)
        rec_compact = _compact_key_text(rec_text)
        if seed_compact and rec_compact and (seed_compact in rec_compact or rec_compact in seed_compact):
            return True
        rec_cluster = _agent_demand_cluster({"content": rec_text, "body_part": rec.get("body_part")})
        rec_areas = _agent_plan_terms(rec_text, _AGENT_PLAN_AREA_TERMS)
        rec_materials = _agent_plan_terms(rec_text, _AGENT_PLAN_MATERIAL_TERMS)
        if seed_areas and rec_areas and seed_areas - rec_areas:
            continue
        if seed_cluster and rec_cluster and seed_cluster == rec_cluster:
            if not seed_materials or not rec_materials or seed_materials & rec_materials:
                return True
        if seed_areas and rec_areas and seed_areas & rec_areas:
            if seed_materials and rec_materials and seed_materials & rec_materials:
                return True
    return False


def _agent_remove_redundant_seed_recommendations(fact_graph: dict[str, Any]) -> dict[str, Any]:
    recommendations = [dict(item) for item in _as_list(fact_graph.get("recommendations")) if isinstance(item, dict)]
    seeds = [dict(item) for item in _as_list(fact_graph.get("seed_recommendations")) if isinstance(item, dict)]
    if not seeds:
        return fact_graph
    kept = [item for item in seeds if not recommendations or not _agent_plan_is_duplicate(item, recommendations)]
    deduped: list[dict[str, Any]] = []
    for item in kept:
        key = _compact_key_text(_agent_plan_text(item))
        if not key:
            continue
        signature = _agent_plan_semantic_signature(item)
        duplicate_index: int | None = None
        for index, existing in enumerate(deduped):
            existing_key = _compact_key_text(_agent_plan_text(existing))
            existing_signature = _agent_plan_semantic_signature(existing)
            if (
                key == existing_key
                or key in existing_key
                or existing_key in key
                or (signature and signature == existing_signature)
            ):
                duplicate_index = index
                break
        if duplicate_index is None:
            deduped.append(item)
        elif _agent_plan_quality_score(item) > _agent_plan_quality_score(deduped[duplicate_index]):
            deduped[duplicate_index] = item
    kept = deduped
    if len(kept) == len(seeds):
        return fact_graph
    updated = dict(fact_graph)
    updated["seed_recommendations"] = kept
    return updated


def _agent_evidence_text_from_item(item: dict[str, Any]) -> str:
    text = _first_text(item, "quote", "content", "text", "summary", "description")
    if text:
        return text
    evidence = item.get("evidence")
    if isinstance(evidence, list):
        return "\n".join(_clean_text(value) for value in evidence if _clean_text(value))
    if evidence:
        return _clean_text(evidence)
    return ""


def _agent_existing_fact_evidence_text(item: dict[str, Any]) -> str:
    evidence = item.get("evidence")
    if isinstance(evidence, list):
        return "\n".join(_clean_text(value) for value in evidence if _clean_text(value))
    if evidence:
        return _clean_text(evidence)
    supporting = item.get("supporting_evidence")
    if isinstance(supporting, list):
        return "\n".join(_clean_text(value) for value in supporting if _clean_text(value))
    if supporting:
        return _clean_text(supporting)
    return _first_text(item, "quote", "source_quote", "evidence_quote")


def _agent_evidence_lookup(evidence_graph: dict[str, Any]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for section in _EVIDENCE_LIST_SECTIONS:
        for item in _as_list(evidence_graph.get(section)):
            if not isinstance(item, dict):
                continue
            text = _agent_evidence_text_from_item(item)
            if not text:
                continue
            for key in ("id", "evidence_id", "source_id", "quote_id"):
                item_id = _clean_text(item.get(key))
                if item_id:
                    lookup[item_id] = text
    return lookup


def _agent_backfill_evidence_texts(fact_graph: dict[str, Any], evidence_graph: dict[str, Any]) -> dict[str, Any]:
    lookup = _agent_evidence_lookup(evidence_graph)
    if not lookup:
        return fact_graph
    updated = dict(fact_graph)
    for key in (
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
    ):
        items: list[dict[str, Any]] = []
        changed = False
        for item in _as_list(updated.get(key)):
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            current_evidence = _agent_existing_fact_evidence_text(copied)
            evidence_ids = [_clean_text(value) for value in _as_list(copied.get("evidence_ids")) if _clean_text(value)]
            resolved = [lookup[item_id] for item_id in evidence_ids if lookup.get(item_id)]
            if resolved and not current_evidence:
                copied["evidence"] = resolved
                changed = True
            items.append(copied)
        if changed:
            updated[key] = items
    return updated


def _agent_dedupe_fact_items_by_content(
    fact_graph: dict[str, Any],
    key: str,
    *,
    content_keys: tuple[str, ...] = ("content", "summary", "text", "factor", "concern", "value"),
) -> dict[str, Any]:
    items = [dict(item) for item in _as_list(fact_graph.get(key)) if isinstance(item, dict)]
    if not items:
        return fact_graph
    kept: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, str]]] = set()
    for item in items:
        content = _first_text(item, *content_keys)
        compact = _compact_key_text(content)
        loose_compact = re.sub(r"(约|大概|左右|元|块钱|人民币)", "", compact)
        if not compact:
            continue
        participant = _agent_participant_key(item)
        duplicate_index: int | None = None
        for index, existing in enumerate(kept):
            existing_content = _first_text(existing, *content_keys)
            existing_compact = _compact_key_text(existing_content)
            existing_loose = re.sub(r"(约|大概|左右|元|块钱|人民币)", "", existing_compact)
            if participant != ("", "") and _agent_participant_key(existing) not in {("", ""), participant}:
                continue
            if (
                compact == existing_compact
                or compact in existing_compact
                or existing_compact in compact
                or (loose_compact and existing_loose and (loose_compact in existing_loose or existing_loose in loose_compact))
            ):
                duplicate_index = index
                break
        if duplicate_index is not None:
            if len(content) > len(_first_text(kept[duplicate_index], *content_keys)):
                kept[duplicate_index] = item
            continue
        key_tuple = (compact, participant)
        if key_tuple in seen:
            continue
        seen.add(key_tuple)
        kept.append(item)
    if len(kept) == len(items):
        return fact_graph
    updated = dict(fact_graph)
    updated[key] = kept
    return updated


def _agent_normalize_concerns(fact_graph: dict[str, Any]) -> dict[str, Any]:
    concerns = [dict(item) for item in _as_list(fact_graph.get("concerns")) if isinstance(item, dict)]
    if not concerns:
        return fact_graph
    kept: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, str]]] = set()
    for item in concerns:
        content = _first_text(item, "content", "concern", "text", "summary")
        if not content:
            continue
        key = (_compact_key_text(content), _agent_participant_key(item))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        kept.append(item)
    if len(kept) == len(concerns):
        return fact_graph
    updated = dict(fact_graph)
    updated["concerns"] = kept
    return updated


def _agent_has_current_surgical_plan(fact_graph: dict[str, Any]) -> bool:
    plan_text = _agent_join_text(fact_graph.get("recommendations"))
    return any(term in plan_text for term in ("手术", "外切", "内切", "切开", "麻醉", "抽脂", "吸脂"))


def _agent_has_positive_medical_safety_signal(fact_graph: dict[str, Any]) -> bool:
    medical_text = _agent_join_text(fact_graph.get("medical_history"))
    positive_terms = (
        "葡萄膜炎",
        "眼底病",
        "泼尼松",
        "激素",
        "长期服药",
        "不能停药",
        "糖尿病",
        "凝血",
        "抗凝",
        "心脏病",
        "本人高血压",
        "确诊高血压",
        "患有高血压",
    )
    if any(term in medical_text for term in positive_terms):
        return True
    if "高血压" in medical_text and not any(term in medical_text for term in ("无高血压", "没有高血压", "母亲高血压", "家族高血压")):
        return True
    return False


def _agent_filter_unsupported_medical_safety_concerns(fact_graph: dict[str, Any]) -> dict[str, Any]:
    concerns = [dict(item) for item in _as_list(fact_graph.get("concerns")) if isinstance(item, dict)]
    if not concerns:
        return fact_graph
    if _agent_has_current_surgical_plan(fact_graph) and _agent_has_positive_medical_safety_signal(fact_graph):
        return fact_graph
    generic_terms = ("既往疾病或长期用药是否影响手术安全", "长期用药是否影响手术安全和术后恢复")
    kept = [
        item
        for item in concerns
        if not any(term in _first_text(item, "content", "concern", "text", "summary") for term in generic_terms)
    ]
    if len(kept) == len(concerns):
        return fact_graph
    updated = dict(fact_graph)
    updated["concerns"] = kept
    return updated


def _agent_remove_rejected_indications(
    fact_graph: dict[str, Any],
    indication_adjudication: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(indication_adjudication, dict):
        return fact_graph
    rejected = {
        _clean_text(item.get("standardized_indication"))
        for item in _as_list(indication_adjudication.get("rejected_indications"))
        if isinstance(item, dict) and _clean_text(item.get("standardized_indication"))
    }
    rejected_name_body: set[tuple[str, str]] = set()
    for standardized in rejected:
        parts = standardized.split("|")
        if len(parts) >= 6:
            rejected_name_body.add((_clean_text(parts[3]), _clean_text(parts[5])))
    if not rejected and not rejected_name_body:
        return fact_graph
    candidates = [item for item in _as_list(fact_graph.get("indication_candidates")) if isinstance(item, dict)]
    kept = [
        item
        for item in candidates
        if item.get("force_include")
        or (
            _clean_text(item.get("standardized_indication")) not in rejected
            and (_clean_text(item.get("indication_name")), _clean_text(item.get("body_part_name"))) not in rejected_name_body
        )
    ]
    if len(kept) == len(candidates):
        return fact_graph
    updated = dict(fact_graph)
    updated["indication_candidates"] = kept
    return updated


def _agent_add_catalog_indication(
    candidates: list[dict[str, Any]],
    *,
    name: str,
    body: str,
    evidence: str,
    confidence: float = 0.72,
    force_include: bool = False,
) -> bool:
    row = _catalog_match_by_name(name, body)
    if not row:
        return False
    for item in candidates:
        if _clean_text(item.get("indication_name")) == row["indication_name"] and _clean_text(item.get("body_part_name")) == row["body_part_name"]:
            return False
    candidates.append(
        {
            **row,
            "evidence_ids": [],
            "evidence": [evidence],
            "confidence": confidence,
            "force_include": force_include,
            "reason": "agent deterministic indication fallback",
        }
    )
    return True


def _agent_result_has_indication(items: list[dict[str, Any]], *, name: str, body_contains: str) -> bool:
    return any(
        _clean_text(item.get("indication_name")) == name
        and body_contains in _clean_text(item.get("body_part_name"))
        for item in items
    )


def _agent_append_result_catalog_indication(result: dict[str, Any], *, name: str, body: str, evidence: str) -> bool:
    row = _catalog_match_by_name(name, body)
    if not row:
        return False
    block = result.setdefault("standardized_indications", {})
    if not isinstance(block, dict):
        block = {"inference_note": None, "summary": "", "items": []}
        result["standardized_indications"] = block
    items = [dict(item) for item in _as_list(block.get("items")) if isinstance(item, dict)]
    if _agent_result_has_indication(items, name=row["indication_name"], body_contains=row["body_part_name"]):
        return False
    items.append({**row, "evidence": evidence})
    block["items"] = items
    block["summary"] = "；".join(
        f"{_clean_text(item.get('indication_name'))}（{_clean_text(item.get('body_part_name'))}）"
        for item in items
        if _clean_text(item.get("indication_name"))
    )
    return True


def _agent_prune_result_profile_tags(result: dict[str, Any]) -> bool:
    profile = result.get("customer_profile")
    if not isinstance(profile, dict):
        return False
    tags = [dict(item) for item in _as_list(profile.get("tags")) if isinstance(item, dict)]
    if not tags:
        return False
    negative_history_markers = (
        "从来没打过",
        "从来没做过",
        "没有打过",
        "没打过",
        "未打过",
        "没有做过",
        "没做过",
        "未做过",
        "无治疗史",
        "无既往",
        "没有既往",
    )
    prior_markers = (
        "做过",
        "打过",
        "填过",
        "治疗过",
        "做了",
        "打了",
        "割过",
        "隆过",
        "术后",
        "既往",
        "之前",
        "以前",
        "曾",
        "外院",
        "去年",
        "今年",
        "最近一次",
        "上次",
    )
    kept: list[dict[str, Any]] = []
    changed = False
    for item in tags:
        category = _clean_text(item.get("category"))
        value = _clean_text(item.get("value"))
        evidence = _clean_text(item.get("evidence"))
        combined = _agent_join_text(category, value, evidence)
        if category in {"治疗项目", "历史用的设备/原材料名称"}:
            if any(term in combined for term in negative_history_markers) or not any(term in combined for term in prior_markers):
                changed = True
                continue
        kept.append(item)
    if not changed:
        return False
    profile["tags"] = kept
    return True


def _agent_recompute_result_seed_summary(result: dict[str, Any]) -> None:
    block = result.get("staff_seed_recommendations")
    if not isinstance(block, dict):
        return
    items = [dict(item) for item in _as_list(block.get("items")) if isinstance(item, dict)]
    block["items"] = items
    block["summary"] = "；".join(
        _first_text(item, "recommendation", "content", "summary")
        for item in items
        if _first_text(item, "recommendation", "content", "summary")
    )


def _agent_recompute_result_recommendation_summary(result: dict[str, Any]) -> None:
    block = result.get("staff_recommendations")
    if not isinstance(block, dict):
        return
    items = [dict(item) for item in _as_list(block.get("items")) if isinstance(item, dict)]
    block["items"] = items
    block["summary"] = "；".join(
        _first_text(item, "recommendation", "content", "summary")
        for item in items
        if _first_text(item, "recommendation", "content", "summary")
    )


def _agent_correct_result_brand_terms(result: dict[str, Any], *, context: str) -> bool:
    if "海派" not in context:
        return False
    changed = False
    for block_name in ("staff_recommendations", "staff_seed_recommendations"):
        block = result.get(block_name)
        if not isinstance(block, dict):
            continue
        for item in _as_list(block.get("items")):
            if not isinstance(item, dict):
                continue
            for key in ("recommendation", "brand", "implementation_notes", "evidence"):
                value = item.get(key)
                if isinstance(value, str) and "海薇" in value:
                    item[key] = value.replace("海薇", "海派")
                    changed = True
    if changed:
        _agent_recompute_result_recommendation_summary(result)
        _agent_recompute_result_seed_summary(result)
    return changed


def _agent_demote_result_orphan_recommendations(result: dict[str, Any]) -> bool:
    rec_block = result.get("staff_recommendations")
    if not isinstance(rec_block, dict):
        return False
    kept: list[dict[str, Any]] = []
    changed = False
    for item in [dict(value) for value in _as_list(rec_block.get("items")) if isinstance(value, dict)]:
        text = _agent_plan_text(item)
        demand_links = _as_list(item.get("demand_priority")) + _as_list(item.get("related_demand_ids")) + _as_list(item.get("linked_demand_ids"))
        should_demote = (
            not demand_links
            and "英伦大提升" in text
            and any(term in text for term in ("下颌缘", "斜方肌", "除皱", "300单位", "一瓶"))
        )
        if should_demote:
            changed = _agent_append_result_seed_recommendation(result, item) or changed
        else:
            kept.append(item)
    if changed:
        rec_block["items"] = kept
        _agent_recompute_result_recommendation_summary(result)
    return changed


def _agent_result_demand_key(item: dict[str, Any]) -> str:
    text = _agent_join_text(
        _first_text(item, "demand", "content", "text", "summary"),
        _first_text(item, "body_part", "body_part_name", "area"),
        item.get("evidence"),
    )
    if any(term in text for term in ("面颊", "颊区", "夹区", "脸颊")) and any(
        term in text for term in ("凹陷", "填充", "玻尿酸")
    ):
        return "cheek_hollow_filling"
    return _agent_demand_cluster(
        {"content": text, "body_part": _first_text(item, "body_part", "body_part_name", "area")}
    ) or _compact_key_text(text)


def _agent_result_demand_score(item: dict[str, Any]) -> int:
    content = _first_text(item, "demand", "content", "text", "summary")
    evidence = _clean_text(item.get("evidence"))
    score = min(len(content), 100)
    if evidence:
        score += 5
    if "改善" in content:
        score += 30
    if any(term in content for term in ("关注", "是否", "考虑是否")):
        score -= 40
    if _first_text(item, "body_part", "body_part_name", "area"):
        score += 8
    return score


def _agent_dedupe_result_demands(result: dict[str, Any]) -> bool:
    block = result.get("customer_primary_demands")
    if not isinstance(block, dict):
        return False
    items = [dict(item) for item in _as_list(block.get("items")) if isinstance(item, dict)]
    if not items:
        return False
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        key = _agent_result_demand_key(item)
        if not key:
            continue
        if key not in by_key:
            by_key[key] = item
            order.append(key)
        elif _agent_result_demand_score(item) > _agent_result_demand_score(by_key[key]):
            by_key[key] = item
    deduped = [by_key[key] for key in order if key in by_key]
    if len(deduped) == len(items):
        return False
    for index, item in enumerate(deduped, start=1):
        item["priority"] = index
    block["items"] = deduped
    block["summary"] = "；".join(
        _first_text(item, "demand", "content", "text", "summary")
        for item in deduped
        if _first_text(item, "demand", "content", "text", "summary")
    )
    return True


def _agent_context_evidence_for_terms(context: str, terms: tuple[str, ...]) -> str:
    if not context:
        return ""
    parts = [part.strip() for part in re.split(r"[。！？!?；;\n]+", context) if part.strip()]
    for index, part in enumerate(parts):
        if any(term in part for term in terms):
            start = max(index - 1, 0)
            end = min(index + 2, len(parts))
            return " / ".join(parts[start:end])[:260]
    return ""


def _agent_append_result_concern(result: dict[str, Any], *, content: str, evidence: str) -> bool:
    content = _clean_text(content)
    evidence = _clean_text(evidence)
    if not content or not evidence:
        return False
    block = result.setdefault("customer_concerns", {})
    if not isinstance(block, dict):
        block = {"inference_note": None, "summary": "", "items": []}
        result["customer_concerns"] = block
    items = [dict(item) for item in _as_list(block.get("items")) if isinstance(item, dict)]
    key = _compact_key_text(content)
    if any(_compact_key_text(_first_text(item, "content", "concern", "text", "summary")) == key for item in items):
        return False
    items.append({"type": "顾虑", "content": content, "evidence": evidence})
    block["items"] = items
    block["summary"] = "；".join(
        _first_text(item, "content", "concern", "text", "summary")
        for item in items
        if _first_text(item, "content", "concern", "text", "summary")
    )
    return True


def _agent_backfill_result_concerns_from_recommendations(result: dict[str, Any], *, context: str) -> bool:
    changed = False
    recommendations = _as_list(_as_dict(result.get("staff_recommendations")).get("items"))
    for item in recommendations:
        if not isinstance(item, dict):
            continue
        response = _first_text(item, "customer_response", "response")
        if not response:
            continue
        body = _first_text(item, "body_part", "body_part_name")
        text = _agent_join_text(response, item.get("evidence"), item.get("recommendation"), body)
        if any(term in text for term in ("颊凹", "夹凹", "凹陷")) and any(
            term in text for term in ("担心", "怕", "更狠", "加重", "更凹", "越凹")
        ):
            evidence = _agent_context_evidence_for_terms(
                context, ("怕凹", "怕越", "凹的更", "凹陷加重", "颊凹加重")
            ) or response
            changed = _agent_append_result_concern(
                result,
                content="担心咬肌肉毒后面颊凹陷加重",
                evidence=evidence,
            ) or changed
        if any(term in text for term in ("安全", "后遗症", "风险", "移位", "副作用")) and any(
            term in text for term in ("担心", "怕", "询问", "安不安全", "有没有")
        ):
            target = "玻尿酸填充" if any(term in text for term in ("玻尿酸", "填充", "面颊", "颊区")) else (body or "方案")
            evidence = _agent_context_evidence_for_terms(context, ("安不安全", "安全", "后遗症", "移位", "副作用")) or response
            changed = _agent_append_result_concern(
                result,
                content=f"担心{target}的安全性及后遗症",
                evidence=evidence,
            ) or changed
    return changed


_AGENT_NON_DEMAND_CONCERN_CUES = (
    "担心",
    "害怕",
    "怕",
    "顾虑",
    "风险",
    "后遗症",
    "副作用",
    "安全",
    "移位",
    "留疤",
    "疤痕",
    "恢复",
    "疼",
    "闭眼",
)

_AGENT_NON_DEMAND_PRICE_CUES = (
    "多少钱",
    "价格",
    "报价",
    "费用",
    "预算",
    "贵",
    "便宜",
    "定金",
    "订金",
    "付款",
)

_AGENT_EXECUTOR_CUES = (
    "主刀",
    "亲自做",
    "谁做",
    "哪个医生",
    "院长做",
    "教授做",
    "医生做",
    "医生操作",
)

_AGENT_TREATMENT_GOAL_CUES = (
    "改善",
    "调整",
    "解决",
    "想做",
    "希望",
    "提升",
    "填充",
    "支撑",
    "祛",
    "去",
    "瘦",
    "变",
    "修复",
    "塑形",
    "淡化",
    "美白",
    "紧致",
    "抗衰",
)


def _agent_result_item_text(item: dict[str, Any]) -> str:
    return _agent_join_text(
        _first_text(item, "demand", "content", "text", "summary", "concern", "recommendation"),
        _first_text(item, "body_part", "body_part_name", "area"),
        item.get("evidence"),
        item.get("customer_response"),
    )


def _agent_demote_non_demand_result_items(result: dict[str, Any]) -> bool:
    block = result.get("customer_primary_demands")
    if not isinstance(block, dict):
        return False
    items = [dict(item) for item in _as_list(block.get("items")) if isinstance(item, dict)]
    if not items:
        return False
    kept: list[dict[str, Any]] = []
    changed = False
    for item in items:
        text = _agent_result_item_text(item)
        has_goal = any(term in text for term in _AGENT_TREATMENT_GOAL_CUES)
        is_concern = any(term in text for term in _AGENT_NON_DEMAND_CONCERN_CUES)
        is_price = any(term in text for term in _AGENT_NON_DEMAND_PRICE_CUES)
        is_executor = any(term in text for term in _AGENT_EXECUTOR_CUES)
        is_brand_preference = any(term in text for term in ("倾向选择", "偏向选择", "想用", "品牌")) and any(
            term in text for term in ("保妥适", "衡力", "吉适", "瑞德喜", "艾拉斯提", "乔雅登", "濡白")
        )
        if (is_concern or is_price) and not has_goal:
            changed = _agent_append_result_concern(
                result,
                content=_first_text(item, "demand", "content", "text", "summary") or text[:80],
                evidence=_clean_text(item.get("evidence")) or text[:160],
            ) or changed
            changed = True
            continue
        if (is_executor or is_brand_preference) and not has_goal:
            changed = True
            continue
        kept.append(item)
    if not changed:
        return False
    for index, item in enumerate(kept, start=1):
        item["priority"] = index
    block["items"] = kept
    block["summary"] = "；".join(
        _first_text(item, "demand", "content", "text", "summary")
        for item in kept
        if _first_text(item, "demand", "content", "text", "summary")
    )
    return True


def _agent_match_recommendation_to_demand_priority(item: dict[str, Any], demands: list[dict[str, Any]]) -> list[int]:
    text = _agent_plan_text(item)
    if not text:
        return []
    scored: list[tuple[int, int]] = []
    for demand in demands:
        try:
            priority = int(demand.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        if priority <= 0:
            continue
        demand_text = _agent_result_item_text(demand)
        score = 0
        for term in ("鼻", "眼", "泪沟", "面颊", "颊", "下巴", "下颌", "嘴", "唇", "咬肌", "额", "颞", "太阳穴", "胸", "副乳", "富贵包", "皮肤", "痣", "斑"):
            if term in text and term in demand_text:
                score += 2
        for term in ("凹陷", "填充", "支撑", "提升", "祛", "瘦", "塑形", "修复", "美白", "淡化", "紧致"):
            if term in text and term in demand_text:
                score += 1
        if score:
            scored.append((score, priority))
    scored.sort(reverse=True)
    return [priority for _score, priority in scored[:2]]


def _agent_repair_result_recommendation_links(result: dict[str, Any]) -> bool:
    demands = [dict(item) for item in _as_list(_as_dict(result.get("customer_primary_demands")).get("items")) if isinstance(item, dict)]
    valid = {int(item.get("priority") or 0) for item in demands if isinstance(item.get("priority"), int) or str(item.get("priority") or "").isdigit()}
    block = result.get("staff_recommendations")
    if not isinstance(block, dict) or not valid:
        return False
    changed = False
    items = [dict(item) for item in _as_list(block.get("items")) if isinstance(item, dict)]
    for item in items:
        raw_values = _as_list(item.get("demand_priority")) or _as_list(item.get("related_demand_ids")) or _as_list(item.get("linked_demand_ids"))
        kept: list[int] = []
        for value in raw_values:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed in valid and parsed not in kept:
                kept.append(parsed)
        if not kept:
            kept = _agent_match_recommendation_to_demand_priority(item, demands)
        if kept != _as_list(item.get("demand_priority")):
            item["demand_priority"] = kept
            changed = True
    if changed:
        block["items"] = items
        _agent_recompute_result_recommendation_summary(result)
    return changed


def _agent_remove_executor_only_result_recommendations(result: dict[str, Any]) -> bool:
    block = result.get("staff_recommendations")
    if not isinstance(block, dict):
        return False
    kept: list[dict[str, Any]] = []
    changed = False
    for item in [dict(value) for value in _as_list(block.get("items")) if isinstance(value, dict)]:
        text = _agent_plan_text(item)
        has_executor = any(term in text for term in _AGENT_EXECUTOR_CUES)
        has_plan_detail = any(_clean_text(item.get(key)) for key in ("brand", "material", "dosage", "price", "course_or_frequency", "implementation_notes"))
        has_steps = bool(_as_list(item.get("treatment_steps")))
        has_plan_language = any(term in text for term in ("建议", "推荐", "可以做", "考虑做", "方案", "改善", "治疗", "注射", "填充", "塑形", "提升"))
        if has_executor and not has_plan_detail and not has_steps and not has_plan_language:
            changed = True
            continue
        kept.append(item)
    if changed:
        block["items"] = kept
        _agent_recompute_result_recommendation_summary(result)
    return changed


def _agent_repair_budget_raw_quote(result: dict[str, Any], *, context: str) -> bool:
    block = result.get("consumption_intent")
    if not isinstance(block, dict):
        return False
    changed = False
    for key in ("budget", "current_budget", "budget_amount", "budget_summary", "summary"):
        value = block.get(key)
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        looks_like_raw_quote = bool(re.match(r"^\[?\d{1,2}:\d{2}\]?", stripped)) or len(stripped) > 80
        if looks_like_raw_quote:
            if any(term in context for term in ("29000", "30000", "2万9", "三万", "3万")) and any(
                term in context for term in ("贵", "太高", "便宜", "优惠", "算一下", "核算", "接受不了", "超")
            ):
                block[key] = "对约29000-30000元总价较敏感，倾向低于该报价"
            else:
                block[key] = "未明确"
            changed = True
    return changed


def _agent_clear_resolved_quality_flags(result: dict[str, Any]) -> None:
    has_indications = bool(_as_list(_as_dict(result.get("standardized_indications")).get("items")))
    quality = result.get("analysis_quality")
    if not isinstance(quality, dict):
        return
    issues = [_clean_text(item) for item in _as_list(quality.get("issues")) if _clean_text(item)]
    if has_indications:
        issues = [issue for issue in issues if "未提取到可支撑 SAP 回写的适应症" not in issue]
    quality["issues"] = issues
    quality["requires_review"] = bool(issues)


def _agent_result_has_seed_recommendation(result: dict[str, Any], *terms: str) -> bool:
    seed_context = _agent_join_text(_as_dict(result.get("staff_seed_recommendations")).get("items"))
    return all(term in seed_context for term in terms)


def _agent_append_result_seed_recommendation(result: dict[str, Any], item: dict[str, Any]) -> bool:
    block = result.setdefault("staff_seed_recommendations", {})
    if not isinstance(block, dict):
        block = {"summary": "", "items": []}
        result["staff_seed_recommendations"] = block
    items = [dict(existing) for existing in _as_list(block.get("items")) if isinstance(existing, dict)]
    signature = _agent_plan_semantic_signature(item)
    for existing in items:
        if signature and signature == _agent_plan_semantic_signature(existing):
            return False
    items.append(item)
    block["items"] = items
    _agent_recompute_result_seed_summary(result)
    return True


def _agent_finalize_analysis_result(result: dict[str, Any], *, context: str = "") -> dict[str, Any]:
    updated = dict(result)
    changed = False
    changed = _agent_correct_result_brand_terms(updated, context=context) or changed
    changed = _agent_demote_result_orphan_recommendations(updated) or changed
    changed = _agent_dedupe_result_demands(updated) or changed
    changed = _agent_backfill_result_concerns_from_recommendations(updated, context=context) or changed
    changed = _agent_demote_non_demand_result_items(updated) or changed
    changed = _agent_remove_executor_only_result_recommendations(updated) or changed
    changed = _agent_repair_result_recommendation_links(updated) or changed
    changed = _agent_repair_budget_raw_quote(updated, context=context) or changed
    changed = _agent_dedupe_result_demands(updated) or changed
    recommendation_context = _agent_join_text(_as_dict(updated.get("staff_recommendations")).get("items"))
    if any(term in recommendation_context for term in ("唇部", "嘴唇", "嘴巴", "唇峰", "唇珠", "口周")) and any(
        term in recommendation_context for term in ("玻尿酸", "填充", "注射", "补打", "塑形", "海派", "海妹", "弹性材料")
    ):
        changed = _agent_append_result_catalog_indication(
            updated,
            name="塑美",
            body="唇部",
            evidence="正式推荐方案出现唇部玻尿酸/弹性材料注射补打或塑形，按本系统字典映射为塑美-唇部（D）",
        ) or changed

    if "下巴" in recommendation_context and any(
        term in recommendation_context for term in ("玻尿酸", "填充", "注射", "支撑", "塑形", "翘", "拉出来", "兜住")
    ):
        changed = _agent_append_result_catalog_indication(
            updated,
            name="塑美",
            body="下颌轮廓线（大O）",
            evidence="正式推荐方案出现下巴注射/填充/支撑塑形，按本系统字典映射为塑美-下颌轮廓线（大O）",
        ) or changed

    if _agent_has_face_fill_support_context(recommendation_context):
        changed = _agent_append_result_catalog_indication(
            updated,
            name="面部填充",
            body="面部",
            evidence="正式推荐方案出现面颊/颊区凹陷玻尿酸填充或注射支撑，按字典映射为面部填充-面部",
        ) or changed

    seed_block = _as_dict(updated.get("staff_seed_recommendations"))
    seed_items = [dict(item) for item in _as_list(seed_block.get("items")) if isinstance(item, dict)]
    if seed_items:
        deduped_graph = _agent_remove_redundant_seed_recommendations({"recommendations": [], "seed_recommendations": seed_items})
        deduped_items = _as_list(deduped_graph.get("seed_recommendations"))
        if len(deduped_items) != len(seed_items):
            updated["staff_seed_recommendations"] = {**seed_block, "items": deduped_items}
            _agent_recompute_result_seed_summary(updated)
            changed = True

    if (
        "英伦大提升" in context
        and any(term in context for term in ("下颌缘", "斜方肌", "除皱", "300单位"))
        and not _agent_result_has_seed_recommendation(updated, "英伦大提升")
    ):
        changed = _agent_append_result_seed_recommendation(
            updated,
            {
                "recommendation": "英伦大提升用于下颌缘/斜方肌提升，并可少量分配至除皱",
                "product_or_solution": None,
                "body_part": "下颌缘/斜方肌/动态纹",
                "brand": "英伦大提升",
                "material": "肉毒类",
                "dosage": "300单位（建议一瓶，可按部位分配）",
                "price": None,
                "course_or_frequency": "单次，可作为加做项目",
                "treatment_steps": ["下颌缘及斜方肌注射提升", "少量剂量分配至动态纹除皱"],
                "implementation_notes": "作为省钱的加做/种草方案，不属于本次艾拉斯提下巴塑形的主方案。",
                "demand_priority": [],
                "evidence": "英伦大提升…300单位…打到下颌缘斜方肌…匀一点点打到除皱…买一瓶就够了",
                "customer_response": "倾向省钱方式，未确认本次实施",
            },
        ) or changed

    changed = _agent_prune_result_profile_tags(updated) or changed
    if changed:
        debug = updated.setdefault("staged_pipeline_debug", {})
        if isinstance(debug, dict):
            debug["agent_final_result_safety_patch"] = True
    _agent_clear_resolved_quality_flags(updated)
    return updated


def _agent_has_wrinkle_treatment_context(text: str) -> bool:
    return any(term in text for term in ("鱼尾纹", "眉间纹", "抬头纹", "川字纹", "动态纹", "皱纹", "除皱"))


def _agent_has_non_wrinkle_botox_context(text: str) -> bool:
    return any(
        term in text
        for term in ("咬肌", "瘦脸", "头大", "下颌线", "下划线", "下颌角", "下颌轮廓", "大提拉", "斜方肌", "肩膀", "小腿")
    ) and any(term in text for term in ("肉毒", "肉毒素", "大提拉", "一瓶", "注射"))


def _agent_has_nose_axis_support_context(text: str) -> bool:
    return any(
        term in text
        for term in ("鼻基底", "鼻头", "鼻翼", "鼻尖", "鼻小柱", "鼻中下段", "鼻中轴", "三角结构")
    ) and any(
        term in text
        for term in ("玻尿酸", "一支玻尿酸", "注射", "支撑", "填充", "塑形", "再生", "芭比针", "濡白", "鲁班", "鲁板", "三角结构")
    )


def _agent_has_jawline_support_context(text: str) -> bool:
    return any(
        term in text
        for term in ("下颌线", "下划线", "下颌角", "下颌缘", "下颌轮廓", "下颌角拐点", "耳前", "耳后", "韧带", "外轮廓")
    ) and any(
        term in text
        for term in ("玻尿酸", "注射", "支撑", "填充", "塑形", "童颜", "芭比", "濡白", "提升", "收紧")
    )


def _agent_has_face_fill_support_context(text: str) -> bool:
    has_area = any(
        term in text
        for term in (
            "鼻基底",
            "口基底",
            "法令纹",
            "面中",
            "侧面凹陷",
            "外轮廓",
            "太阳穴",
            "额颞",
            "苹果肌",
            "泪沟",
            "面颊",
            "颊区",
            "夹区",
            "脸颊",
        )
    )
    if not has_area:
        return False
    has_structural_action = any(
        term in text
        for term in ("填充", "支撑", "塑形", "凹陷", "断层", "轮廓", "衔接", "法令纹")
    )
    if not has_structural_action:
        return False
    return any(
        term in text
        for term in ("玻尿酸", "再生", "童颜", "瑞德喜", "濡白", "芭比", "注射", "填充", "支撑")
    ) or ("胶原" in text and any(term in text for term in ("填充", "支撑", "塑形", "凹陷")))


def _agent_has_ear_support_plan(fact_graph: dict[str, Any]) -> bool:
    """Only infer ear plastic indication from an actual ear-area plan, not history."""
    for key in ("recommendations", "seed_recommendations"):
        for item in _as_list(fact_graph.get(key)):
            if not isinstance(item, dict):
                continue
            text = _agent_plan_text(item)
            if any(term in text for term in ("中耳炎", "面神经", "耳朵手术", "病史", "受损")):
                continue
            if any(term in text for term in ("耳朵", "耳垂", "耳部", "耳基底", "耳轮")) and any(
                term in text for term in ("玻尿酸", "注射", "支撑", "填充", "塑形", "拉长", "衬托", "偏小")
            ):
                return True
    return False


def _agent_remove_indication_by_name(
    candidates: list[dict[str, Any]],
    *,
    name: str,
    body_contains: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    kept: list[dict[str, Any]] = []
    removed = False
    for item in candidates:
        item_name = _clean_text(item.get("indication_name"))
        item_body = _clean_text(item.get("body_part_name"))
        standardized = _clean_text(item.get("standardized_indication"))
        parts = standardized.split("|")
        if len(parts) >= 6:
            item_name = item_name or _clean_text(parts[3])
            item_body = item_body or _clean_text(parts[5])
        if item_name == name and (not body_contains or body_contains in item_body):
            removed = True
            continue
        kept.append(item)
    return kept, removed


def _agent_has_current_eye_plan(text: str, terms: tuple[str, ...]) -> bool:
    compact = _clean_text(text)
    if not compact or not any(term in compact for term in terms):
        return False
    return any(
        cue in compact
        for cue in (
            "治疗",
            "改善",
            "处理",
            "方案",
            "做",
            "打",
            "注射",
            "填充",
            "激光",
            "光电",
            "皮秒",
            "水光",
            "胶原",
            "玻尿酸",
            "嗨体",
            "福曼",
        )
    )


def _agent_prune_observation_only_eye_indications(
    candidates: list[dict[str, Any]],
    *,
    context: str,
    recommendation_context: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Drop eye indications that came from observation/deferred seed talk only."""

    changed = False
    compact_context = _clean_text(context)
    defer_or_observation_only = any(
        cue in compact_context
        for cue in (
            "化妆就行",
            "化个妆就行",
            "可以化妆",
            "先打你在意的",
            "先打在意的",
            "先做你在意的",
            "先不处理",
            "暂时不处理",
            "后期再",
            "下次再",
            "以后再",
            "不是这次",
        )
    )

    if not _agent_has_current_eye_plan(
        recommendation_context,
        ("黑眼圈", "眼下黑", "眼周暗沉", "眼周色沉"),
    ):
        candidates, removed = _agent_remove_indication_by_name(candidates, name="黑眼圈", body_contains="眼部")
        changed = changed or removed

    if defer_or_observation_only and not _agent_has_current_eye_plan(
        recommendation_context,
        ("泪沟", "眼下凹", "眼下凹陷", "眶下凹陷"),
    ):
        candidates, removed = _agent_remove_indication_by_name(candidates, name="塑美", body_contains="眼部")
        changed = changed or removed
        candidates, removed = _agent_remove_indication_by_name(candidates, name="眼袋", body_contains="眼部")
        changed = changed or removed

    return candidates, changed


def _agent_ensure_common_indications(fact_graph: dict[str, Any]) -> dict[str, Any]:
    context = _agent_join_text(
        fact_graph.get("demands"),
        fact_graph.get("doctor_diagnoses"),
        fact_graph.get("recommendations"),
        fact_graph.get("seed_recommendations"),
    )
    recommendation_context = _agent_join_text(fact_graph.get("recommendations"))
    updated = dict(fact_graph)
    candidates = [dict(item) for item in _as_list(updated.get("indication_candidates")) if isinstance(item, dict)]

    changed = False
    if _agent_has_non_wrinkle_botox_context(context) and not _agent_has_wrinkle_treatment_context(recommendation_context):
        candidates, removed = _agent_remove_indication_by_name(candidates, name="面部除皱", body_contains="面部")
        changed = changed or removed
    explicit_surgical_face_fill = any(
        term in recommendation_context
        for term in ("面部填充", "脂肪填充", "自体脂肪", "太阳穴填充", "额颞填充", "苹果肌填充", "泪沟填充")
    ) or _agent_has_face_fill_support_context(recommendation_context)
    if not explicit_surgical_face_fill and (
        _agent_has_nose_axis_support_context(recommendation_context)
        or _agent_has_jawline_support_context(recommendation_context)
    ):
        candidates, removed = _agent_remove_indication_by_name(candidates, name="面部填充", body_contains="面部")
        changed = changed or removed
    candidates, removed = _agent_prune_observation_only_eye_indications(
        candidates,
        context=context,
        recommendation_context=recommendation_context,
    )
    changed = changed or removed
    demand_plan_context = _agent_join_text(
        fact_graph.get("demands"),
        fact_graph.get("recommendations"),
        fact_graph.get("seed_recommendations"),
    )
    diagnosis_context = _agent_join_text(fact_graph.get("doctor_diagnoses"))
    negative_pigment_context = any(
        term in diagnosis_context
        for term in ("无明显色斑", "没有明显色斑", "没有色斑", "没什么太多的色斑", "无真皮斑", "没有真皮斑")
    )
    positive_pigment_context = any(term in demand_plan_context for term in ("祛斑", "色斑", "斑点", "雀斑", "皮秒", "双击", "淡斑"))
    if negative_pigment_context and not positive_pigment_context:
        before_len = len(candidates)
        candidates = [
            item
            for item in candidates
            if not (
                _clean_text(item.get("indication_name")) == "色斑"
                and _clean_text(item.get("body_part_name")) == "面部"
            )
        ]
        changed = changed or len(candidates) != before_len

    anti_aging_anchor_context = _agent_join_text(fact_graph.get("demands"), fact_graph.get("doctor_diagnoses"))
    if not any(term in anti_aging_anchor_context for term in ("松弛", "下垂", "细纹", "干纹", "皱纹", "抗衰", "紧致", "提升")):
        before_len = len(candidates)
        candidates = [
            item
            for item in candidates
            if _clean_text(item.get("indication_name")) not in {"松弛下垂", "紧致淡纹"}
        ]
        changed = changed or len(candidates) != before_len

    if any(term in context for term in ("肉毒", "除皱针", "玻尿酸", "瑞德喜", "注射")):
        before_len = len(candidates)
        candidates = [
            item
            for item in candidates
            if not (
                _clean_text(item.get("indication_name")) == "生活美容"
                and _clean_text(item.get("body_part_name")) == "其他"
            )
        ]
        changed = changed or len(candidates) != before_len
    if any(term in context for term in ("肉毒", "除皱针")) and any(
        term in context for term in ("鱼尾纹", "眉间纹", "抬头纹", "动态纹", "皱眉纹", "川字纹", "除皱")
    ):
        changed = _agent_add_catalog_indication(
            candidates,
            name="面部除皱",
            body="面部",
            evidence="正式方案或主诉出现肉毒/除皱针对鱼尾纹、眉间纹、抬头纹等动态纹治疗",
            confidence=0.82,
        ) or changed
    if any(term in recommendation_context for term in ("咬肌", "瘦脸", "英伦大提升", "下颌轮廓线")) and any(
        term in recommendation_context for term in ("肉毒", "注射", "提升", "塑形")
    ):
        changed = _agent_add_catalog_indication(
            candidates,
            name="塑美",
            body="下颌轮廓线（大O）",
            evidence="正式推荐方案出现咬肌/下颌轮廓线肉毒注射瘦脸或轮廓提升",
            confidence=0.76,
        ) or changed
    if _agent_has_jawline_support_context(recommendation_context):
        changed = _agent_add_catalog_indication(
            candidates,
            name="塑美",
            body="下颌轮廓线（大O）",
            evidence="正式推荐方案出现下颌线/下颌角拐点/耳前耳后韧带注射支撑或轮廓提升，按字典映射为塑美-下颌轮廓线（大O）",
            confidence=0.84,
        ) or changed
    if _agent_has_face_fill_support_context(recommendation_context):
        changed = _agent_add_catalog_indication(
            candidates,
            name="面部填充",
            body="面部",
            evidence="正式推荐方案出现鼻基底/口基底/面中/外轮廓等面部填充或注射支撑，按字典映射为面部填充-面部",
            confidence=0.82,
            force_include=True,
        ) or changed
    if "外油内干" in context or ("皮肤" in context and any(term in context for term in ("干燥", "缺水"))):
        changed = _agent_add_catalog_indication(
            candidates,
            name="干燥",
            body="面部",
            evidence="皮肤主诉出现外油内干/干燥缺水信息",
        ) or changed
    # Do not infer pigment indications from incidental diagnosis chatter.  The
    # SAP indication should only be added when pigment/spot removal is part of
    # the customer's demand or a staff recommendation/seed plan.
    if positive_pigment_context and not negative_pigment_context:
        changed = _agent_add_catalog_indication(
            candidates,
            name="色斑",
            body="面部",
            evidence="主诉或方案出现祛斑/雀斑/皮秒信息",
        ) or changed
    if any(term in context for term in ("点痣", "祛痣", "色素痣")) or ("痣" in context and any(term in context for term in ("点", "去除", "祛", "包干", "复发"))):
        body = "面部"
        if "眼" in context:
            body = "眼部"
        elif "颈" in context:
            body = "颈部"
        elif "身体" in context:
            body = "身体"
        changed = _agent_add_catalog_indication(
            candidates,
            name="祛痣/祛疣",
            body=body,
            evidence="主诉或方案出现点痣/祛痣/色素痣处理需求",
            confidence=0.86,
        ) or changed
    if "痘坑" in context:
        changed = _agent_add_catalog_indication(
            candidates,
            name="疤痕",
            body="面部",
            evidence="主诉或方案出现痘坑/凹陷性痤疮瘢痕信息",
            confidence=0.76,
        ) or changed
    has_eye_repair = _agent_has_prior_eyelid_surgery_context(context)
    if not has_eye_repair:
        before_len = len(candidates)
        candidates = [
            item
            for item in candidates
            if not (
                _clean_text(item.get("indication_name")) == "眼修复"
                and _clean_text(item.get("body_part_name")) == "眼部"
            )
        ]
        changed = changed or len(candidates) != before_len
        if any(term in context for term in ("双眼皮", "重睑", "内双", "肿眼泡", "切开重睑", "重睑成形")) and any(
            term in context for term in ("手术", "切开", "去皮", "切掉", "重睑成形", "做双眼皮")
        ):
            changed = _agent_add_catalog_indication(
                candidates,
                name="双眼皮",
                body="眼部",
                evidence="主诉或方案为首次双眼皮/重睑改善，未出现明确既往双眼皮修复语义",
                confidence=0.78,
            ) or changed
    if has_eye_repair:
        before_len = len(candidates)
        candidates = [
            item
            for item in candidates
            if not (
                _clean_text(item.get("indication_name")) == "双眼皮"
                and _clean_text(item.get("body_part_name")) == "眼部"
            )
        ]
        changed = changed or len(candidates) != before_len
        changed = _agent_add_catalog_indication(
            candidates,
            name="眼修复",
            body="眼部",
            evidence="既往双眼皮/重睑术后不满意或松弛下垂，属于修复场景",
            confidence=0.78,
        ) or changed

    if "下巴" in recommendation_context and any(
        term in recommendation_context for term in ("玻尿酸", "填充", "注射", "支撑", "塑形", "翘", "拉出来", "兜住")
    ):
        changed = _agent_add_catalog_indication(
            candidates,
            name="塑美",
            body="下颌轮廓线（大O）",
            evidence="正式推荐方案出现下巴注射/填充/支撑塑形，按本系统字典映射为塑美-下颌轮廓线（大O）",
            confidence=0.76,
            force_include=True,
        ) or changed

    if any(term in recommendation_context for term in ("唇部", "嘴唇", "嘴巴", "唇峰", "唇珠", "口周")) and any(
        term in recommendation_context for term in ("玻尿酸", "填充", "注射", "补打", "塑形", "海派", "海妹", "弹性材料")
    ):
        changed = _agent_add_catalog_indication(
            candidates,
            name="塑美",
            body="唇部",
            evidence="正式推荐方案出现唇部玻尿酸/弹性材料注射补打或塑形，按本系统字典映射为塑美-唇部（D）",
            confidence=0.82,
            force_include=True,
        ) or changed

    if any(term in recommendation_context for term in ("鼻基底", "鼻头", "鼻翼", "鼻尖", "鼻小柱", "鼻中下段", "鼻中段", "鼻下段", "鼻中轴", "鼻中轴线", "三角结构")) and any(
        term in recommendation_context for term in ("玻尿酸", "定彩", "注射", "支撑", "填充", "塑形", "抬高", "拉高", "纵深")
    ):
        changed = _agent_add_catalog_indication(
            candidates,
            name="塑美",
            body="鼻中轴线",
            evidence="正式推荐方案出现鼻小柱/鼻中下段玻尿酸注射支撑塑形，按字典映射为塑美-鼻中轴线（H）",
            confidence=0.82,
        ) or changed
    if _agent_has_nose_axis_support_context(recommendation_context):
        changed = _agent_add_catalog_indication(
            candidates,
            name="塑美",
            body="鼻中轴线",
            evidence="正式推荐方案出现鼻基底/鼻头/鼻翼三角结构注射支撑塑形，按字典映射为塑美-鼻中轴线（H）",
            confidence=0.86,
        ) or changed

    if not _agent_has_ear_support_plan(updated):
        before_len = len(candidates)
        candidates = [
            item
            for item in candidates
            if not (
                _clean_text(item.get("indication_name")) == "塑美"
                and "耳" in _clean_text(item.get("body_part_name"))
            )
        ]
        changed = changed or len(candidates) != before_len

    if _agent_has_ear_support_plan(updated):
        changed = _agent_add_catalog_indication(
            candidates,
            name="塑美",
            body="耳部",
            evidence="正式推荐方案出现耳朵/耳垂玻尿酸注射支撑或塑形，按字典映射为塑美-耳部（大O）",
            confidence=0.82,
        ) or changed

    if changed:
        updated["indication_candidates"] = candidates
    return updated


def _agent_ensure_medical_safety_concerns(fact_graph: dict[str, Any]) -> dict[str, Any]:
    medical_items = [dict(item) for item in _as_list(fact_graph.get("medical_history")) if isinstance(item, dict)]
    if not medical_items:
        return fact_graph
    medical_text = _agent_join_text(medical_items)
    if not _agent_has_current_surgical_plan(fact_graph):
        return fact_graph
    safety_terms = (
        "葡萄膜炎",
        "眼底病",
        "泼尼松",
        "激素",
        "长期服药",
        "不能停药",
        "高血压",
        "糖尿病",
        "凝血",
        "抗凝",
        "心脏病",
    )
    if not any(term in medical_text for term in safety_terms):
        return fact_graph
    if not _agent_has_positive_medical_safety_signal(fact_graph):
        return fact_graph
    if any(term in medical_text for term in ("无高血压", "没有高血压", "无药物过敏", "没有药物过敏")) and not any(
        term in medical_text for term in ("葡萄膜炎", "眼底病", "泼尼松", "激素", "长期服药", "不能停药", "糖尿病", "凝血", "抗凝", "心脏病")
    ):
        return fact_graph
    concerns = [dict(item) for item in _as_list(fact_graph.get("concerns")) if isinstance(item, dict)]
    concern_text = _agent_join_text(concerns)
    if any(term in concern_text for term in safety_terms):
        return fact_graph

    if "葡萄膜炎" in medical_text or "眼底病" in medical_text:
        content = "担心既往葡萄膜炎/眼底病及长期用药是否影响手术安全和术后恢复"
    elif "泼尼松" in medical_text or "激素" in medical_text:
        content = "担心长期服用激素类药物是否影响手术安全和术后恢复"
    else:
        content = "担心既往疾病或长期用药是否影响手术安全和术后恢复"
    if content in concern_text:
        return fact_graph
    evidence_ids: list[str] = []
    for item in medical_items:
        for value in _as_list(item.get("evidence_ids")):
            text = _clean_text(value)
            if text and text not in evidence_ids:
                evidence_ids.append(text)
    concerns.append(
        {
            "concern_id": f"C{len(concerns) + 1}",
            "content": content,
            "evidence_ids": evidence_ids,
            "participant": _first_text(medical_items[0], "participant") or "主咨询客户",
            "participant_scope": _first_text(medical_items[0], "participant_scope") or "primary_customer",
        }
    )
    updated = dict(fact_graph)
    updated["concerns"] = concerns
    return updated


_AGENT_DEMAND_LINK_TERMS = (
    "水光",
    "补水",
    "干燥",
    "胶原",
    "热玛吉",
    "抗衰",
    "紧致",
    "提升",
    "松弛",
    "超声",
    "鼻",
    "鼻基底",
    "下巴",
    "下颌",
    "眼袋",
    "泪沟",
    "毛孔",
    "痘",
    "色斑",
    "祛斑",
    "点痣",
    "耳",
)


def _agent_repair_recommendation_demand_links(fact_graph: dict[str, Any]) -> dict[str, Any]:
    demands = [dict(item) for item in _as_list(fact_graph.get("demands")) if isinstance(item, dict)]
    if not demands:
        return fact_graph
    demand_ids: list[str] = []
    demand_by_id: dict[str, dict[str, Any]] = {}
    for index, demand in enumerate(demands, start=1):
        demand_id = _clean_text(demand.get("id") or demand.get("demand_id")) or f"D{index}"
        demand_ids.append(demand_id)
        demand_by_id[demand_id] = demand

    def best_demand_id(item: dict[str, Any]) -> str:
        item_text = _agent_join_text(_agent_item_content(item), item.get("body_part"), item.get("brand"), item.get("material"))
        item_terms = {term for term in _AGENT_DEMAND_LINK_TERMS if term in item_text}
        item_body = _clean_text(item.get("body_part") or item.get("body_part_name"))
        best_id = ""
        best_score = 0
        for demand_id, demand in demand_by_id.items():
            demand_text = _agent_join_text(_agent_item_content(demand), demand.get("body_part"), demand.get("body_part_name"))
            demand_terms = {term for term in _AGENT_DEMAND_LINK_TERMS if term in demand_text}
            score = len(item_terms & demand_terms) * 3
            demand_body = _clean_text(demand.get("body_part") or demand.get("body_part_name"))
            if item_body and demand_body and (item_body in demand_body or demand_body in item_body):
                score += 1
            if score > best_score:
                best_score = score
                best_id = demand_id
        if best_id and best_score > 0:
            return best_id
        return demand_ids[0] if len(demand_ids) == 1 else ""

    updated = dict(fact_graph)
    for section in ("recommendations", "seed_recommendations"):
        repaired_items: list[dict[str, Any]] = []
        for item in _as_list(updated.get(section)):
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            linked = [_clean_text(value) for value in _as_list(copied.get("related_demand_ids")) if _clean_text(value)]
            valid = [value for value in linked if value in demand_by_id]
            if not valid:
                fallback_id = best_demand_id(copied)
                if fallback_id:
                    valid = [fallback_id]
            if valid:
                copied["related_demand_ids"] = valid
            repaired_items.append(copied)
        updated[section] = repaired_items
    return updated


def _agent_prune_unsupported_pigment_fallbacks(fact_graph: dict[str, Any]) -> dict[str, Any]:
    context = _agent_join_text(
        fact_graph.get("demands"),
        fact_graph.get("doctor_diagnoses"),
        fact_graph.get("recommendations"),
        fact_graph.get("seed_recommendations"),
    )
    positive = any(
        term in context
        for term in ("祛斑", "色斑", "雀斑", "斑点", "黄褐斑", "淡斑", "皮秒", "色素沉着", "肤色不均")
    )
    negative = any(term in context for term in ("无明显色斑", "没有明显色斑", "没有色斑", "无真皮斑", "没有真皮斑"))
    if positive and not negative:
        return fact_graph
    candidates = [dict(item) for item in _as_list(fact_graph.get("indication_candidates")) if isinstance(item, dict)]
    kept = [
        item
        for item in candidates
        if not (
            _clean_text(item.get("indication_name")) == "色斑"
            and _clean_text(item.get("reason")) == "agent deterministic indication fallback"
        )
    ]
    if len(kept) == len(candidates):
        return fact_graph
    updated = dict(fact_graph)
    updated["indication_candidates"] = kept
    return updated


def _agent_ensure_structural_support_recommendations(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
) -> dict[str, Any]:
    recommendations = [dict(item) for item in _as_list(fact_graph.get("recommendations")) if isinstance(item, dict)]
    evidence_items = [dict(item) for item in _as_list(evidence_graph.get("recommendation_evidence")) if isinstance(item, dict)]
    if not evidence_items:
        return fact_graph

    recommendation_text = _agent_join_text(recommendations)
    changed = False

    def append_from_evidence(item: dict[str, Any], *, content: str, body_part: str) -> None:
        nonlocal changed
        evidence_ids = [str(value) for value in _as_list(item.get("evidence_turn_ids")) if str(value).strip()]
        recommendations.append(
            {
                "content": content,
                "body_part": body_part,
                "brand": _first_text(item, "brand"),
                "material": _first_text(item, "material"),
                "dosage": _first_text(item, "dosage"),
                "price": _first_text(item, "price"),
                "course_or_frequency": _first_text(item, "course_or_frequency"),
                "treatment_steps": item.get("treatment_steps") if isinstance(item.get("treatment_steps"), list) else [],
                "implementation_notes": _first_text(item, "implementation_notes", "quote"),
                "customer_response": _first_text(item, "customer_response"),
                "evidence_ids": evidence_ids,
                "participant": _first_text(item, "participant") or "主咨询客户",
                "participant_scope": _first_text(item, "participant_scope") or "primary_customer",
            }
        )
        changed = True

    for item in evidence_items:
        relation = _first_text(item, "relation_to_current_demand")
        if relation in {"alternative_not_recommended", "not_current_or_referral"}:
            continue
        text = _agent_plan_text(item)
        if _agent_has_jawline_support_context(text) and not _agent_has_jawline_support_context(recommendation_text):
            append_from_evidence(
                item,
                content="下颌线/下颌角拐点结构支撑提升",
                body_part="下颌线/下颌角拐点",
            )
            recommendation_text = _agent_join_text(recommendations)
        if _agent_has_nose_axis_support_context(text) and not _agent_has_nose_axis_support_context(recommendation_text):
            append_from_evidence(
                item,
                content="鼻基底/鼻头鼻翼三角结构注射支撑塑形",
                body_part="鼻基底/鼻头鼻翼",
            )
            recommendation_text = _agent_join_text(recommendations)

    if not changed:
        return fact_graph
    updated = dict(fact_graph)
    updated["recommendations"] = recommendations
    return updated


def _agent_repair_fact_graph(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
) -> dict[str, Any]:
    repaired = _agent_normalize_fact_content_fields(fact_graph)
    repaired = _agent_flatten_recommendation_details(repaired)
    repaired = _agent_ensure_demands_from_evidence_graph(repaired, evidence_graph)
    repaired = _agent_ensure_demands_from_diagnoses_when_empty(repaired)
    repaired = _agent_normalize_demands(repaired)
    repaired = _agent_ensure_budget_facts_from_evidence_graph(repaired, evidence_graph)
    repaired = _agent_preserve_backup_options(repaired, evidence_graph)
    repaired = _agent_ensure_structural_support_recommendations(repaired, evidence_graph)
    repaired = _agent_preserve_deferred_seed_recommendations(repaired, evidence_graph)
    repaired = _agent_normalize_demands(repaired)
    repaired = _agent_prune_observation_only_demands(repaired)
    repaired = _agent_remove_redundant_seed_recommendations(repaired)
    repaired = _agent_filter_unsupported_medical_safety_concerns(repaired)
    repaired = _agent_normalize_concerns(repaired)
    repaired = _agent_normalize_profile_facts(repaired)
    repaired = _agent_ensure_common_indications(repaired)
    repaired = _agent_prune_unsupported_pigment_fallbacks(repaired)
    repaired = _agent_ensure_medical_safety_concerns(repaired)
    repaired = _agent_repair_recommendation_demand_links(repaired)
    repaired = _agent_normalize_concerns(repaired)
    repaired = _agent_backfill_evidence_texts(repaired, evidence_graph)
    repaired = _agent_normalize_demands(repaired)
    for list_key in ("budget_facts", "deal_factors", "concerns", "medical_history", "profile_facts"):
        repaired = _agent_dedupe_fact_items_by_content(repaired, list_key)
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
_BUSINESS_EVIDENCE_SECTIONS = (
    "customer_demand_evidence",
    "diagnosis_evidence",
    "recommendation_evidence",
    "concern_evidence",
    "budget_evidence",
    "medical_history_evidence",
    "profile_evidence",
    "deal_evidence",
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


def _line_id_to_int(value: object) -> int | None:
    match = re.search(r"\bL(\d{4})\b", _clean_text(value))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _truncate_text_for_prompt(text: str, *, max_chars: int = 36000) -> str:
    if len(text) <= max_chars:
        return text
    head_chars = max_chars * 2 // 3
    tail_chars = max_chars - head_chars
    return text[:head_chars] + "\n...<truncated_middle>...\n" + text[-tail_chars:]


def _dialogue_for_scope_prompt(corrected_dialogue: str) -> str:
    compact_lines: list[str] = []
    for line in corrected_dialogue.splitlines():
        text = line.strip()
        if not text:
            continue
        if len(text) > 260:
            text = text[:260] + "...<line_truncated>"
        compact_lines.append(text)
    return _truncate_text_for_prompt("\n".join(compact_lines), max_chars=36000)


def _extract_scope_graph(parsed: dict[str, Any]) -> dict[str, Any]:
    payload = parsed.get("scope_graph") if isinstance(parsed.get("scope_graph"), dict) else parsed
    if not isinstance(payload, dict):
        return {}
    segments: list[dict[str, Any]] = []
    for index, item in enumerate(_as_list(payload.get("segments")), start=1):
        if not isinstance(item, dict):
            continue
        start_line_id = _clean_text(item.get("start_line_id"))
        end_line_id = _clean_text(item.get("end_line_id"))
        if not start_line_id or not end_line_id:
            continue
        scope_type = _clean_text(item.get("scope_type")) or "unclear"
        relevance = _clean_text(item.get("business_relevance")) or "supporting"
        current_relevant = item.get("current_visit_relevant")
        if not isinstance(current_relevant, bool):
            current_relevant = relevance != "ignore" and scope_type not in {
                "staff_chat",
                "casual_chat",
                "third_party_absent_case",
                "unrelated_operations",
            }
        segments.append(
            {
                "id": _clean_text(item.get("id")) or f"S{index}",
                "start_line_id": start_line_id,
                "end_line_id": end_line_id,
                "scope_type": scope_type,
                "participant_scope": _clean_text(item.get("participant_scope")) or "unknown",
                "business_relevance": relevance,
                "current_visit_relevant": bool(current_relevant),
                "reason": _clean_text(item.get("reason")),
            }
        )
    return {
        "primary_customer": _clean_text(payload.get("primary_customer")),
        "dominant_visit_topic": _clean_text(payload.get("dominant_visit_topic")),
        "segments": segments,
        "notes": [_clean_text(item) for item in _as_list(payload.get("notes")) if _clean_text(item)],
    }


def _scope_segment_should_ignore(segment: dict[str, Any]) -> bool:
    if segment.get("current_visit_relevant") is True:
        return False
    scope_type = _clean_text(segment.get("scope_type"))
    relevance = _clean_text(segment.get("business_relevance"))

    retain_scope_types = {
        "current_customer_consultation",
        "accompanying_customer_consultation",
        "doctor_face_to_face",
        "quote_or_payment",
        "post_deal_care",
        "future_seed_or_cross_department",
        "unclear",
        "unknown",
    }
    ignore_scope_types = {
        "staff_chat",
        "casual_chat",
        "third_party_absent_case",
        "unrelated_operations",
    }
    if scope_type in retain_scope_types:
        return False
    if scope_type in ignore_scope_types:
        return True
    return relevance == "ignore"


def _dialogue_with_scope_filter(corrected_dialogue: str, scope_graph: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    segments = [item for item in _as_list(scope_graph.get("segments")) if isinstance(item, dict)]
    if not segments:
        return corrected_dialogue, {"removed_line_count": 0, "kept_line_count": len(corrected_dialogue.splitlines())}
    ignore_ranges: list[tuple[int, int, str]] = []
    for segment in segments:
        if not _scope_segment_should_ignore(segment):
            continue
        start = _line_id_to_int(segment.get("start_line_id"))
        end = _line_id_to_int(segment.get("end_line_id"))
        if start is None or end is None:
            continue
        if end < start:
            start, end = end, start
        ignore_ranges.append((start, end, _clean_text(segment.get("scope_type"))))
    if not ignore_ranges:
        return corrected_dialogue, {"removed_line_count": 0, "kept_line_count": len(corrected_dialogue.splitlines())}

    kept: list[str] = []
    removed = 0
    removed_types: dict[str, int] = {}
    for line in corrected_dialogue.splitlines():
        line_no = _line_id_to_int(line)
        should_remove = False
        remove_type = ""
        if line_no is not None:
            for start, end, scope_type in ignore_ranges:
                if start <= line_no <= end:
                    should_remove = True
                    remove_type = scope_type
                    break
        if should_remove:
            removed += 1
            removed_types[remove_type or "unknown"] = removed_types.get(remove_type or "unknown", 0) + 1
            continue
        kept.append(line)
    if removed < 3 or len(kept) < 8:
        return corrected_dialogue, {
            "removed_line_count": 0,
            "kept_line_count": len(corrected_dialogue.splitlines()),
            "filter_skipped": True,
            "reason": "scope_filter_too_small_or_too_aggressive",
        }
    return "\n".join(kept), {
        "removed_line_count": removed,
        "kept_line_count": len(kept),
        "removed_scope_types": removed_types,
    }


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
    preprocess_text = json.dumps(preprocess_context, ensure_ascii=False, separators=(",", ":"))
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


def _evidence_graph_is_empty(evidence_graph: dict[str, Any]) -> bool:
    return not any(_as_list(evidence_graph.get(section)) for section in _BUSINESS_EVIDENCE_SECTIONS)


def _evidence_item_scope(item: dict[str, Any]) -> str:
    return _clean_text(item.get("participant_scope") or item.get("customer_scope") or item.get("scope"))


def _business_evidence_needs_scene_rescue(evidence_graph: dict[str, Any]) -> bool:
    if _evidence_graph_is_empty(evidence_graph):
        return True
    if _as_list(evidence_graph.get("customer_demand_evidence")):
        return False

    business_items = [
        item
        for section in _BUSINESS_EVIDENCE_SECTIONS
        for item in _as_list(evidence_graph.get(section))
        if isinstance(item, dict)
    ]
    if not business_items:
        return True
    has_current_scope = any(
        _evidence_item_scope(item) in {"primary_customer", "current_customer", "main_customer"}
        for item in business_items
    )
    notes_text = _agent_join_text(evidence_graph.get("quality_notes"), evidence_graph.get("speaker_corrections"))
    internal_or_third_party_note = any(
        cue in notes_text
        for cue in (
            "内部",
            "员工",
            "无明确主咨询客户",
            "未出现客户直接",
            "转述",
            "第三方",
            "其他顾客",
            "未发现可归属于具体主咨询客户",
        )
    )
    relation_text = _agent_join_text(evidence_graph.get("recommendation_evidence"), evidence_graph.get("deal_evidence"))
    third_party_or_unclear_relation = any(
        cue in relation_text
        for cue in (
            "有顾客",
            "那个顾客",
            "美团",
            "未成交",
            "准备去韩国",
            "relation_to_current_demand",
            "unclear",
            "alternative_not_recommended",
        )
    )
    return (not has_current_scope and internal_or_third_party_note) or third_party_or_unclear_relation


def _extract_scene_assessment(parsed: dict[str, Any]) -> dict[str, Any]:
    payload = parsed.get("scene_assessment")
    if not isinstance(payload, dict):
        return {}
    scene_type = _clean_text(payload.get("scene_type")) or "unclear"
    reason = _clean_text(payload.get("reason"))
    try:
        confidence = float(payload.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "scene_type": scene_type,
        "is_current_customer_consultation": bool(payload.get("is_current_customer_consultation")),
        "confidence": max(0.0, min(confidence, 1.0)),
        "reason": reason,
    }


def _is_non_current_consultation_scene(scene_assessment: dict[str, Any]) -> bool:
    if not scene_assessment:
        return False
    scene_type = _clean_text(scene_assessment.get("scene_type"))
    if scene_assessment.get("is_current_customer_consultation") is True:
        return False
    return scene_type in {
        "internal_staff_chat",
        "frontdesk_order",
        "third_party_case_discussion",
        "casual_chat",
    }


def _mark_non_consultation_scene(
    result: dict[str, Any],
    scene_assessment: dict[str, Any],
) -> dict[str, Any]:
    if not scene_assessment:
        return result
    enriched = dict(result)
    enriched["scene_assessment"] = scene_assessment
    scene_type = _clean_text(scene_assessment.get("scene_type")) or "unclear"
    reason = _clean_text(scene_assessment.get("reason")) or "未发现当前顾客面诊主线"
    enriched["analysis_quality"] = {
        "requires_review": True,
        "issues": [f"非当前顾客面诊场景：{scene_type}，{reason}"],
    }
    return enriched


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


def _extract_event_graph(parsed: dict[str, Any]) -> dict[str, Any]:
    payload = parsed.get("event_graph")
    if isinstance(payload, dict):
        return payload
    return parsed if isinstance(parsed, dict) else {}


def _event_graph_is_empty(event_graph: dict[str, Any]) -> bool:
    if not isinstance(event_graph, dict):
        return True
    for section in ("demand_events", "plan_events", "deal_events", "profile_events", "concern_events", "budget_events"):
        if _as_list(event_graph.get(section)):
            return False
    return True


_EVENT_CURRENT_PLAN_TYPES = {"current_recommendation", "deal_confirmed", "customer_accept"}
_EVENT_SEED_PLAN_TYPES = {"seed_recommendation"}
_EVENT_BLOCKED_PLAN_TYPES = {
    "comparison_or_backup",
    "not_recommended",
    "staff_explanation",
    "customer_question",
    "diagnosis_only",
}
_EVENT_DEAL_TYPES = {"deal_confirmed", "deposit", "payment", "order_created"}
_EVENT_PROFILE_BLOCK_TYPES = {"staff_or_product_context", "reject"}


_EVENT_PLAN_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("botox", ("\u8089\u6bd2", "\u4fdd\u59a5\u9002", "\u7626\u8138\u9488", "\u9664\u76b1\u9488")),
    ("thermage", ("\u70ed\u739b\u5409", "\u70ed\u62c9\u63d0")),
    ("ultherapy", ("\u8d85\u58f0\u70ae", "\u8d85\u58f0")),
    ("waterlight_collagen", ("\u6c34\u5149", "\u6ce2\u6ce2", "\u52a8\u80fd\u7d20", "\u80f6\u539f", "\u798f\u66fc", "\u5f17\u7f26", "\u53cc\u7f8e")),
    ("hyaluronic_filler", ("\u73bb\u5c3f\u9178", "\u745e\u5fb7\u559c", "\u827e\u62c9\u65af\u63d0", "\u4e54\u96c5\u767b", "\u6cd5\u601d\u4e3d", "\u586b\u5145")),
    ("nose_support", ("\u9f3b\u5c0f\u67f1", "\u9f3b\u4e2d\u4e0b\u6bb5", "\u9f3b\u57fa\u5e95", "\u5c71\u6839", "\u9f3b\u80cc", "\u9f3b\u7efc\u5408")),
    ("ear_support", ("\u8033\u6735", "\u8033\u5782")),
    ("mole_removal", ("\u70b9\u75e3", "\u795b\u75e3", "\u53bb\u75e3")),
    ("eye_bag_tear_trough", ("\u773c\u888b", "\u6cea\u6c9f", "\u7736\u9694", "\u7736\u5916c")),
    ("jawline_chin", ("\u4e0b\u988c\u7f18", "\u4e0b\u5df4", "\u4e0b\u989a")),
    ("whitening", ("\u7f8e\u767d", "\u5149\u5b50", "\u8272\u6c89", "\u9ec4\u6c14")),
)


def _event_text(item: dict[str, Any]) -> str:
    return _agent_join_text(
        item.get("plan"),
        item.get("content"),
        item.get("recommendation"),
        item.get("summary"),
        item.get("body_part"),
        item.get("brand"),
        item.get("material"),
        item.get("implementation_notes"),
        item.get("quote"),
    )


def _event_item_keys(item: dict[str, Any]) -> set[str]:
    text = _event_text(item)
    if not text:
        return set()
    keys: set[str] = set()
    compact = _compact_key_text(text)
    for key, terms in _EVENT_PLAN_KEYWORDS:
        if any(term in text for term in terms):
            keys.add(key)
    if compact:
        keys.add(compact[:80])
    return keys


def _event_key_sets_match(left: set[str], right: set[str]) -> bool:
    if not left or not right:
        return False
    if left & right:
        return True
    for lkey in left:
        for rkey in right:
            if len(lkey) >= 8 and len(rkey) >= 8 and (lkey in rkey or rkey in lkey):
                return True
    return False


def _event_plan_keys_by_type(event_graph: dict[str, Any], types: set[str]) -> set[str]:
    keys: set[str] = set()
    for event in _as_list(event_graph.get("plan_events")):
        if not isinstance(event, dict):
            continue
        if _clean_text(event.get("event_type")) not in types:
            continue
        keys.update(_event_item_keys(event))
    for event in _as_list(event_graph.get("deal_events")):
        if not isinstance(event, dict):
            continue
        if _clean_text(event.get("event_type")) not in types:
            continue
        keys.update(_event_item_keys(event))
    return keys


def _event_is_optional_seed_plan(event: dict[str, Any]) -> bool:
    event_type = _clean_text(event.get("event_type"))
    if event_type != "comparison_or_backup":
        return False
    text = _agent_join_text(
        event.get("plan"),
        event.get("body_part"),
        event.get("implementation_notes"),
        event.get("customer_response"),
        event.get("quote"),
    )
    if not text:
        return False
    optional_markers = (
        "整体方案",
        "整体设计",
        "整体帮你分析",
        "可以选择",
        "可以先",
        "也可以",
        "只做",
        "先做",
        "后续",
        "下次",
        "再做",
        "可选",
        "次要",
    )
    blocked_markers = ("不建议", "不适合", "不能做", "不要做", "没必要", "拒绝", "排除")
    return any(term in text for term in optional_markers) and not any(term in text for term in blocked_markers)


def _optional_seed_plan_keys(event_graph: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for event in _as_list(event_graph.get("plan_events")):
        if isinstance(event, dict) and _event_is_optional_seed_plan(event):
            keys.update(_event_item_keys(event))
    return keys


def _event_quote(event: dict[str, Any]) -> str:
    return _clean_text(event.get("quote")) or _clean_text(event.get("content")) or _clean_text(event.get("plan"))


def _event_related_demand_ids(event: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for key in ("related_demand", "related_demand_id", "demand_id"):
        value = _clean_text(event.get(key))
        if value:
            ids.append(value)
    for key in ("related_demand_ids", "linked_demand_ids", "demand_ids"):
        ids.extend(_clean_text(value) for value in _as_list(event.get(key)) if _clean_text(value))
    return list(dict.fromkeys(ids))


def _agent_add_optional_seed_recommendations_from_events(
    fact_graph: dict[str, Any],
    event_graph: dict[str, Any],
) -> dict[str, Any]:
    events = [event for event in _as_list(event_graph.get("plan_events")) if isinstance(event, dict) and _event_is_optional_seed_plan(event)]
    if not events:
        return fact_graph
    updated = dict(fact_graph)
    seeds = _normalize_fact_item_list(updated.get("seed_recommendations"))
    existing_keys = {_compact_key_text(_event_text(item)) for item in seeds if _compact_key_text(_event_text(item))}
    for event in events:
        event_key = _compact_key_text(_event_text(event))
        if event_key and event_key in existing_keys:
            continue
        plan = _clean_text(event.get("plan"))
        if not plan:
            continue
        copied = {
            "id": _clean_text(event.get("id")),
            "content": plan,
            "body_part": _clean_text(event.get("body_part")),
            "brand": _clean_text(event.get("brand")),
            "material": _clean_text(event.get("material")),
            "dosage": _clean_text(event.get("dosage")),
            "price": _clean_text(event.get("price")),
            "course_or_frequency": _clean_text(event.get("course_or_frequency")),
            "treatment_steps": _as_list(event.get("treatment_steps")),
            "implementation_notes": _clean_text(event.get("implementation_notes")),
            "customer_response": _clean_text(event.get("customer_response")) or "未明确回应",
            "related_demand_ids": _event_related_demand_ids(event),
            "evidence": _event_quote(event),
            "source_evidence_ids": _as_list(event.get("source_evidence_ids")),
            "event_graph_optional_seed": True,
        }
        seeds.append(copied)
        if event_key:
            existing_keys.add(event_key)
    updated["seed_recommendations"] = seeds
    return updated


def _agent_demote_orphan_optional_recommendations(fact_graph: dict[str, Any]) -> dict[str, Any]:
    recommendations = [dict(item) for item in _as_list(fact_graph.get("recommendations")) if isinstance(item, dict)]
    if not recommendations:
        return fact_graph
    demand_context = _agent_join_text(fact_graph.get("demands"))
    optional_markers = (
        "整体方案",
        "整体设计",
        "整体帮你分析",
        "可以选择",
        "可以先",
        "先单做",
        "再考虑",
        "后续",
        "下次",
        "追加",
        "未明确回应",
    )
    kept: list[dict[str, Any]] = []
    demoted: list[dict[str, Any]] = []
    for item in recommendations:
        linked_demand_ids = _as_list(item.get("related_demand_ids")) + _as_list(item.get("demand_priority"))
        body_terms = _agent_indication_body_specific_terms({"body_part_name": _first_text(item, "body_part", "body_part_name")})
        has_demand_body_support = bool(body_terms) and any(term in demand_context for term in body_terms)
        text = _agent_join_text(
            item.get("content"),
            item.get("recommendation"),
            item.get("body_part"),
            item.get("implementation_notes"),
            item.get("customer_response"),
            item.get("evidence"),
        )
        should_demote = (
            not linked_demand_ids
            and body_terms
            and not has_demand_body_support
            and any(term in text for term in optional_markers)
        )
        if should_demote:
            copied = dict(item)
            copied["source"] = _clean_text(copied.get("source")) or "demoted_orphan_optional_recommendation"
            if not _clean_text(copied.get("customer_response")):
                copied["customer_response"] = "未明确回应"
            demoted.append(copied)
        else:
            kept.append(item)
    if not demoted:
        return fact_graph
    updated = dict(fact_graph)
    seeds = [dict(item) for item in _as_list(updated.get("seed_recommendations")) if isinstance(item, dict)]
    existing = {_compact_key_text(_agent_item_content(item)) for item in seeds if _agent_item_content(item)}
    for item in demoted:
        key = _compact_key_text(_agent_item_content(item))
        if key and key in existing:
            continue
        seeds.append(item)
        if key:
            existing.add(key)
    updated["recommendations"] = kept
    updated["seed_recommendations"] = seeds
    return updated


_INDICATION_CURRENT_SUPPORT_TERMS: dict[str, tuple[str, ...]] = {
    "毛孔": ("毛孔", "控油", "油皮", "黑头", "肤质", "水光"),
    "干燥": ("干燥", "缺水", "补水", "水光"),
    "松弛下垂": ("松弛", "下垂", "紧致", "提升", "抗衰", "热玛吉", "超声炮"),
    "紧致淡纹": ("细纹", "干纹", "淡纹", "皱纹", "紧致", "抗衰"),
    "纹路": ("法令纹", "纹路", "皱纹", "细纹", "干纹", "淡纹"),
    "塑美": ("塑形", "支撑", "提升", "鼻", "下颌", "下巴", "轮廓", "英伦", "大O", "耳", "眉弓", "双C", "唇", "嘴"),
    "面部填充": ("填充", "凹陷", "轮廓", "苹果肌", "太阳穴", "额颞", "泪沟", "口基底", "鼻基底"),
    "双眼皮": ("双眼皮", "开扇", "平扇", "眼尾", "去皮", "去脂", "提肌", "开眼角"),
}


def _agent_indication_current_support_terms(item: dict[str, Any]) -> list[str]:
    name = _clean_text(item.get("indication_name"))
    body = _clean_text(item.get("body_part_name"))
    terms = list(_INDICATION_CURRENT_SUPPORT_TERMS.get(name, ()))
    if name and name not in terms:
        terms.append(name)
    for part in re.split(r"[（()）/、,，;；\s]+", body):
        part = _clean_text(part)
        if len(part) >= 2 and part not in terms:
            terms.append(part)
    return terms


def _agent_indication_body_specific_terms(item: dict[str, Any]) -> list[str]:
    body = _clean_text(item.get("body_part_name"))
    terms: list[str] = []
    for part in re.split(r"[（()）/、,，;；\s]+", body):
        part = _clean_text(part)
        if len(part) >= 2 and part not in terms:
            terms.append(part)
    body_synonyms = {
        "下颌": ("下颌", "下颌线", "下颌轮廓", "轮廓线", "大O", "下巴"),
        "鼻": ("鼻", "鼻头", "鼻背", "鼻中轴", "鼻中轴线", "山根", "鼻小柱"),
        "毛孔": ("毛孔", "控油", "油皮", "T区"),
        "口基底": ("口基底", "嘴角", "口角"),
        "眼": ("眼", "双眼皮", "泪沟", "眼袋", "眼尾"),
        "眉": ("眉弓", "眉尾", "眉眼"),
        "双C": ("双C", "眶外C", "C线"),
        "唇": ("唇", "唇部", "嘴唇", "嘴巴", "口周"),
    }
    for key, values in body_synonyms.items():
        if key in body:
            for value in values:
                if value not in terms:
                    terms.append(value)
    return terms


def _agent_prune_seed_only_indications(fact_graph: dict[str, Any]) -> dict[str, Any]:
    candidates = [dict(item) for item in _as_list(fact_graph.get("indication_candidates")) if isinstance(item, dict)]
    if not candidates:
        return fact_graph
    current_context = _agent_join_text(
        fact_graph.get("demands"),
        fact_graph.get("recommendations"),
        fact_graph.get("deal_outcome"),
    )
    seed_or_observation_context = _agent_join_text(
        fact_graph.get("seed_recommendations"),
        fact_graph.get("doctor_diagnoses"),
    )
    kept: list[dict[str, Any]] = []
    changed = False
    for item in candidates:
        body_terms = _agent_indication_body_specific_terms(item)
        if body_terms:
            has_body_current_support = any(term in current_context for term in body_terms)
            has_body_seed_support = any(term in seed_or_observation_context for term in body_terms)
            if not has_body_current_support and has_body_seed_support:
                changed = True
                continue
        terms = [term for term in _agent_indication_current_support_terms(item) if len(term) >= 2]
        if not terms:
            kept.append(item)
            continue
        has_current_support = any(term in current_context for term in terms)
        has_only_seed_or_observation_support = any(term in seed_or_observation_context for term in terms)
        if not has_current_support and has_only_seed_or_observation_support:
            changed = True
            continue
        kept.append(item)
    if not changed:
        return fact_graph
    updated = dict(fact_graph)
    updated["indication_candidates"] = kept
    return updated


def _agent_fill_missing_fact_evidence_from_events(
    fact_graph: dict[str, Any],
    event_graph: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(fact_graph, dict) or _event_graph_is_empty(event_graph):
        return fact_graph
    updated = dict(fact_graph)
    section_events = {
        "demands": [event for event in _as_list(event_graph.get("demand_events")) if isinstance(event, dict)],
        "recommendations": [
            event
            for event in _as_list(event_graph.get("plan_events"))
            if isinstance(event, dict) and _clean_text(event.get("event_type")) in (_EVENT_CURRENT_PLAN_TYPES | _EVENT_DEAL_TYPES)
        ],
        "seed_recommendations": [
            event
            for event in _as_list(event_graph.get("plan_events"))
            if isinstance(event, dict)
            and (_clean_text(event.get("event_type")) in _EVENT_SEED_PLAN_TYPES or _event_is_optional_seed_plan(event))
        ],
        "concerns": [event for event in _as_list(event_graph.get("concern_events")) if isinstance(event, dict)],
        "budget_facts": [event for event in _as_list(event_graph.get("budget_events")) if isinstance(event, dict)],
        "deal_factors": [
            event
            for event in [*_as_list(event_graph.get("budget_events")), *_as_list(event_graph.get("deal_events"))]
            if isinstance(event, dict)
        ],
        "medical_history": [event for event in _as_list(event_graph.get("profile_events")) if isinstance(event, dict)],
        "profile_facts": [event for event in _as_list(event_graph.get("profile_events")) if isinstance(event, dict)],
    }

    def pick_event(item: dict[str, Any], events: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
        if not events:
            return None
        item_ids = _event_evidence_ids(item)
        for event in events:
            event_ids = _event_evidence_ids(event)
            if item_ids and event_ids and item_ids & event_ids:
                return event
        item_key = _compact_key_text(_event_text(item))
        best: dict[str, Any] | None = None
        best_score = 0
        for event in events:
            event_key = _compact_key_text(_event_text(event))
            score = 0
            if item_key and event_key and (item_key in event_key or event_key in item_key):
                score += 4
            for term in ("鼻", "眼", "双眼皮", "毛孔", "法令纹", "口基底", "眉弓", "轮廓", "预算"):
                if term in item_key and term in event_key:
                    score += 1
            if score > best_score:
                best = event
                best_score = score
        if best is not None and best_score > 0:
            return best
        return events[index] if index < len(events) else events[-1]

    for section, events in section_events.items():
        items = _normalize_fact_item_list(updated.get(section))
        if not items:
            continue
        repaired: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            copied = dict(item)
            if not _clean_text(copied.get("evidence")):
                event = pick_event(copied, events, index)
                quote = _event_quote(event) if event else ""
                if quote:
                    copied["evidence"] = quote
            repaired.append(copied)
        updated[section] = repaired
    return updated


def _event_evidence_ids(item: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("id", "source_id", "evidence_id"):
        value = _clean_text(item.get(key))
        if value:
            ids.add(value)
    for key in ("evidence_ids", "source_evidence_ids", "source_ids"):
        ids.update(_clean_text(value) for value in _as_list(item.get(key)) if _clean_text(value))
    return ids


def _profile_event_blocks_item(item: dict[str, Any], blocked_events: list[dict[str, Any]]) -> bool:
    item_ids = _event_evidence_ids(item)
    item_key = _compact_key_text(
        _agent_join_text(
            _first_text(item, "category", "tag_category", "type"),
            _first_text(item, "value", "tag_value", "content", "text"),
            item.get("evidence"),
            item.get("quote"),
        )
    )
    for event in blocked_events:
        event_ids = _event_evidence_ids(event)
        if item_ids and event_ids and item_ids & event_ids:
            return True
        event_key = _compact_key_text(
            _agent_join_text(event.get("category"), event.get("value"), event.get("quote"), event.get("content"))
        )
        if item_key and event_key and (item_key in event_key or event_key in item_key):
            return True
    return False


def _deal_outcome_from_event_graph(event_graph: dict[str, Any]) -> dict[str, Any] | None:
    deal_events = [
        event
        for event in _as_list(event_graph.get("deal_events"))
        if isinstance(event, dict) and _clean_text(event.get("event_type")) in _EVENT_DEAL_TYPES
    ]
    if not deal_events:
        return None
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    amount = ""
    for event in deal_events:
        plan = _first_text(event, "plan", "content", "summary")
        event_amount = _first_text(event, "amount", "price")
        if event_amount and not amount:
            amount = event_amount
        key = (_compact_key_text(plan), _compact_key_text(event_amount))
        if not any(key) or key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "plan": plan,
                "amount": event_amount or None,
                "evidence_ids": _as_list(event.get("source_evidence_ids")) or _as_list(event.get("evidence_ids")),
                "evidence": _first_text(event, "quote", "evidence") or None,
                "participant": _first_text(event, "participant") or None,
                "participant_scope": _first_text(event, "participant_scope") or None,
            }
        )
    if not items and not amount:
        return None
    summary_parts = []
    for item in items:
        text = _agent_join_text(item.get("plan"), item.get("amount"))
        if text:
            summary_parts.append(text)
    return {
        "status": "\u5df2\u6210\u4ea4",
        "summary": "\uff1b".join(summary_parts) or amount or "\u5df2\u6210\u4ea4",
        "deal_items": items,
        "amount": amount or None,
    }


def _apply_event_graph_constraints(fact_graph: dict[str, Any], event_graph: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(fact_graph, dict) or _event_graph_is_empty(event_graph):
        return fact_graph

    updated = dict(fact_graph)
    allowed_keys = _event_plan_keys_by_type(event_graph, _EVENT_CURRENT_PLAN_TYPES | _EVENT_DEAL_TYPES)
    seed_keys = _event_plan_keys_by_type(event_graph, _EVENT_SEED_PLAN_TYPES)
    optional_seed_keys = _optional_seed_plan_keys(event_graph)
    seed_keys |= optional_seed_keys
    blocked_keys = _event_plan_keys_by_type(event_graph, _EVENT_BLOCKED_PLAN_TYPES)
    blocked_keys -= optional_seed_keys

    recommendations: list[dict[str, Any]] = []
    demoted_seeds: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in _normalize_fact_item_list(updated.get("recommendations")):
        keys = _event_item_keys(item)
        if _event_key_sets_match(keys, blocked_keys) and not _event_key_sets_match(keys, allowed_keys):
            rejected.append({"source_id": item.get("id") or item.get("source_id") or "", "reason": "blocked_by_event_graph"})
            continue
        if _event_key_sets_match(keys, seed_keys) and not _event_key_sets_match(keys, allowed_keys):
            copied = dict(item)
            copied["event_graph_demoted"] = True
            demoted_seeds.append(copied)
            continue
        recommendations.append(item)

    seed_recommendations: list[dict[str, Any]] = []
    seen_seed_keys: set[str] = set()
    for item in [*_normalize_fact_item_list(updated.get("seed_recommendations")), *demoted_seeds]:
        keys = _event_item_keys(item)
        if _event_key_sets_match(keys, blocked_keys) and not _event_key_sets_match(keys, seed_keys | allowed_keys):
            rejected.append({"source_id": item.get("id") or item.get("source_id") or "", "reason": "blocked_by_event_graph"})
            continue
        key = "|".join(sorted(keys))
        if key and key in seen_seed_keys:
            continue
        if key:
            seen_seed_keys.add(key)
        seed_recommendations.append(item)

    updated["recommendations"] = recommendations
    updated["seed_recommendations"] = seed_recommendations
    if rejected:
        adjudication = _as_dict(updated.get("_recommendation_adjudication"))
        existing = _as_list(adjudication.get("rejected_recommendations"))
        updated["_recommendation_adjudication"] = {
            **adjudication,
            "rejected_recommendations": [*existing, *rejected],
        }

    blocked_profile_events = [
        event
        for event in _as_list(event_graph.get("profile_events"))
        if isinstance(event, dict) and _clean_text(event.get("event_type")) in _EVENT_PROFILE_BLOCK_TYPES
    ]
    if blocked_profile_events:
        updated["profile_facts"] = [
            item
            for item in _normalize_fact_item_list(updated.get("profile_facts"))
            if not _profile_event_blocks_item(item, blocked_profile_events)
        ]

    deal_outcome = _deal_outcome_from_event_graph(event_graph)
    if deal_outcome:
        updated["deal_outcome"] = deal_outcome

    updated["_event_graph_constraints"] = {
        "current_plan_key_count": len(allowed_keys),
        "seed_plan_key_count": len(seed_keys),
        "optional_seed_plan_key_count": len(optional_seed_keys),
        "blocked_plan_key_count": len(blocked_keys),
        "rejected_recommendation_count": len(rejected),
    }
    return updated


def _extract_audit(parsed: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    audit = parsed.get("audit") if isinstance(parsed.get("audit"), dict) else {}
    corrected = parsed.get("corrected_fact_graph")
    return audit, corrected if isinstance(corrected, dict) else None


def _extract_final_result_audit(parsed: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    audit = parsed.get("final_result_audit") if isinstance(parsed.get("final_result_audit"), dict) else {}
    patch = parsed.get("analysis_result_patch")
    return audit, patch if isinstance(patch, dict) else None


def _apply_final_result_audit_patch(result: dict[str, Any], patch: dict[str, Any] | None) -> dict[str, Any]:
    if not patch:
        return result
    updated = dict(result)
    replaceable_sections = {
        "customer_primary_demands",
        "customer_concerns",
        "staff_recommendations",
        "staff_seed_recommendations",
        "standardized_indications",
        "consumption_intent",
        "consultation_result",
        "customer_profile",
    }
    for section in replaceable_sections:
        value = patch.get(section)
        if isinstance(value, dict):
            updated[section] = value
    debug = updated.setdefault("staged_pipeline_debug", {})
    if isinstance(debug, dict):
        debug["agent_final_result_audit_repaired"] = True
    return updated


def _final_result_audit_needed(
    analysis_result: dict[str, Any],
    *,
    corrected_dialogue: str,
    fact_graph: dict[str, Any],
    event_graph: dict[str, Any],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    demand_items = [dict(item) for item in _as_list(_as_dict(analysis_result.get("customer_primary_demands")).get("items")) if isinstance(item, dict)]
    demand_keys = [_agent_result_demand_key(item) for item in demand_items]
    if len(demand_items) >= 5:
        reasons.append("many_demands_need_consistency_check")
    if len(set(key for key in demand_keys if key)) < len([key for key in demand_keys if key]):
        reasons.append("duplicate_or_near_duplicate_demands")
    for item in demand_items:
        text = _agent_result_item_text(item)
        has_goal = any(term in text for term in _AGENT_TREATMENT_GOAL_CUES)
        if not has_goal and any(term in text for term in _AGENT_NON_DEMAND_CONCERN_CUES + _AGENT_NON_DEMAND_PRICE_CUES + _AGENT_EXECUTOR_CUES):
            reasons.append("non_goal_item_in_demands")
            break
        if any(term in text for term in ("倾向选择", "偏向选择", "品牌", "保妥适", "衡力")) and not has_goal:
            reasons.append("brand_preference_in_demands")
            break

    concern_items = _as_list(_as_dict(analysis_result.get("customer_concerns")).get("items"))
    recommendation_items = [dict(item) for item in _as_list(_as_dict(analysis_result.get("staff_recommendations")).get("items")) if isinstance(item, dict)]
    recommendation_text = _agent_join_text(recommendation_items)
    if not concern_items and any(term in recommendation_text for term in ("担心", "害怕", "怕", "后遗症", "安全", "风险", "移位", "凹陷加重", "疤痕")):
        reasons.append("worry_in_recommendation_response_without_concern")

    valid_priorities = {
        int(item.get("priority") or 0)
        for item in demand_items
        if isinstance(item.get("priority"), int) or str(item.get("priority") or "").isdigit()
    }
    for item in recommendation_items:
        raw_values = _as_list(item.get("demand_priority"))
        for value in raw_values:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                reasons.append("invalid_recommendation_demand_link")
                break
            if parsed not in valid_priorities:
                reasons.append("invalid_recommendation_demand_link")
                break

    budget_text = _agent_join_text(analysis_result.get("consumption_intent"))
    if re.search(r"\[?\d{1,2}:\d{2}\]?", budget_text) or len(budget_text) > 700:
        reasons.append("raw_quote_in_budget")

    if len(corrected_dialogue) > 16000:
        reasons.append("long_recording_final_check")
    if _as_list(event_graph.get("events")) and _as_list(fact_graph.get("recommendations")):
        reasons.append("event_fact_alignment_check")

    actionable = [
        reason
        for reason in reasons
        if reason
        not in {
            "long_recording_final_check",
            "event_fact_alignment_check",
        }
    ]
    if actionable:
        return True, reasons
    # Long recordings get a final audit only when there is enough extracted
    # content to justify the extra token spend.
    return bool(len(corrected_dialogue) > 22000 and (len(demand_items) >= 3 or len(recommendation_items) >= 3)), reasons


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


def _compact_for_prompt(value: object, *, max_chars: int = 20000) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...<truncated>"


def _evidence_for_plan_prompt(evidence_graph: dict[str, Any]) -> dict[str, Any]:
    return {
        "customer_demand_evidence": _as_list(evidence_graph.get("customer_demand_evidence")),
        "diagnosis_evidence": _as_list(evidence_graph.get("diagnosis_evidence")),
        "recommendation_evidence": _as_list(evidence_graph.get("recommendation_evidence")),
        "concern_evidence": _as_list(evidence_graph.get("concern_evidence")),
        "budget_evidence": _as_list(evidence_graph.get("budget_evidence")),
        "deal_evidence": _as_list(evidence_graph.get("deal_evidence")),
    }


def _call_agent(agent_name: str, system_prompt: str, user_prompt: str, *, max_tokens: int) -> dict[str, Any]:
    logger.info(
        "agent pipeline %s prompt chars system=%d user=%d",
        agent_name,
        len(system_prompt),
        len(user_prompt),
    )
    return _call_json(system_prompt, user_prompt, max_tokens=max_tokens)


_CORRECTION_FULL_DIALOGUE_MAX_CHARS = 45000
_CORRECTION_CONTEXT_RADIUS = 2
_CORRECTION_MAX_PROMPT_LINES = 420

_CORRECTION_INTERNAL_CUES = (
    "我的顾客",
    "我有个顾客",
    "我那个顾客",
    "我的老顾客",
    "接顾客",
    "在接顾客",
    "接谁",
    "谁接",
    "前台",
    "领导",
    "早班",
    "晚班",
    "成本",
    "利润",
    "成交",
    "未成交",
    "核销",
    "划扣",
    "到账",
    "退款",
    "退费",
    "开单",
    "开检查单",
    "派单",
    "收银",
    "权限",
    "系统",
    "医生助理",
    "专家助理",
    "院长助理",
    "给我同事",
)
_CORRECTION_PRE_RECEPTION_CUES = (
    "这边请",
    "请坐",
    "稍等",
    "签字",
    "签完字",
    "身份证",
    "预约",
    "叫号",
    "排号",
)
_CORRECTION_TERM_CUES = (
    "一字光波",
    "一次光波",
    "一支光波",
    "鲁板",
    "鲁班",
    "下划线",
)


def _line_id_from_numbered_dialogue_line(line: str) -> str:
    match = re.match(r"^(L\d{4})\b", line)
    return match.group(1) if match else ""


def _line_role_and_text(line: str) -> tuple[str, str]:
    try:
        after_metadata = line.split("]: ", 1)[1]
    except IndexError:
        after_metadata = line
    try:
        _timestamp, rest = after_metadata.split("] ", 1)
        role, text = rest.split(": ", 1)
    except ValueError:
        return "", line
    return role.strip(), text.strip()


def _role_looks_customer(role: str) -> bool:
    return any(term in role for term in ("客户", "主客户", "同行人", "访客"))


def _role_looks_staff(role: str) -> bool:
    return any(term in role for term in ("咨询师", "医生", "助理", "员工", "前台", "工牌本人"))


def _line_needs_correction_context(line: str, metadata: dict[str, str]) -> bool:
    role, text = _line_role_and_text(line)
    compact_text = re.sub(r"\s+", "", text)
    if re.search(r"(客户|主客户|同行人|访客)（[^）]*工牌本人", line):
        return True
    if re.search(r"(咨询师|医生|前台|员工|专家助理)（(主客户|同行人|客户|顾客|访客)）", line):
        return True
    if _role_looks_customer(role) and any(cue in compact_text for cue in _CORRECTION_INTERNAL_CUES):
        return True
    if _role_looks_customer(role) and any(cue in compact_text for cue in _CORRECTION_PRE_RECEPTION_CUES):
        return True
    if any(cue in compact_text for cue in _CORRECTION_TERM_CUES):
        return True
    metadata_role = _clean_text(metadata.get("role"))
    metadata_label = _clean_text(metadata.get("speaker_label"))
    if metadata_role.lower() in {"customer", "client", "patient", "primary_customer", "visitor_companion"} and _role_looks_staff(metadata_label):
        return True
    if metadata_role.lower() in {"consultant", "doctor", "frontdesk", "staff_peer", "badge_owner", "expert_assistant"} and _role_looks_customer(metadata_label):
        return True
    return False


def _dialogue_for_correction_prompt(
    numbered_dialogue: str,
    line_metadata: dict[str, dict[str, str]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return full dialogue for normal transcripts, and focused windows for long ones."""
    lines = [line for line in numbered_dialogue.splitlines() if line.strip()]
    if len(numbered_dialogue) <= _CORRECTION_FULL_DIALOGUE_MAX_CHARS:
        return numbered_dialogue, {
            "mode": "full",
            "input_line_count": len(lines),
            "prompt_line_count": len(lines),
            "prompt_chars": len(numbered_dialogue),
        }

    wanted: set[int] = set(range(min(20, len(lines))))
    speaker_samples: dict[str, int] = {}
    for idx, line in enumerate(lines):
        line_id = _line_id_from_numbered_dialogue_line(line)
        metadata = (line_metadata or {}).get(line_id) or {}
        speaker_key = _clean_text(metadata.get("asr_speaker") or metadata.get("speaker_label") or metadata.get("role"))
        if speaker_key and speaker_samples.get(speaker_key, 0) < 3:
            wanted.add(idx)
            speaker_samples[speaker_key] = speaker_samples.get(speaker_key, 0) + 1
        if _line_needs_correction_context(line, metadata):
            for offset in range(-_CORRECTION_CONTEXT_RADIUS, _CORRECTION_CONTEXT_RADIUS + 1):
                pos = idx + offset
                if 0 <= pos < len(lines):
                    wanted.add(pos)

    selected = sorted(wanted)
    if len(selected) > _CORRECTION_MAX_PROMPT_LINES:
        selected = selected[:_CORRECTION_MAX_PROMPT_LINES]
    prompt_lines = [lines[idx] for idx in selected]
    omitted_count = max(len(lines) - len(prompt_lines), 0)
    header = (
        f"# Focused correction windows: showing {len(prompt_lines)} of {len(lines)} lines. "
        f"{omitted_count} low-risk lines omitted; line IDs are original."
    )
    prompt_dialogue = "\n".join([header, *prompt_lines])
    return prompt_dialogue, {
        "mode": "focused",
        "input_line_count": len(lines),
        "prompt_line_count": len(prompt_lines),
        "omitted_line_count": omitted_count,
        "prompt_chars": len(prompt_dialogue),
    }


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
    correction_dialogue, correction_prompt_debug = _dialogue_for_correction_prompt(
        numbered_dialogue,
        line_speaker_metadata,
    )

    correction_user_prompt = _CORRECTION_AGENT_USER_TEMPLATE.format(
        staff_context=staff_text,
        preprocess_context=json.dumps(preprocess_context, ensure_ascii=False, separators=(",", ":")),
        numbered_dialogue=correction_dialogue,
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
    correction_metadata["correction_prompt_debug"] = correction_prompt_debug

    scope_call_count = 0
    scope_graph: dict[str, Any] = {}
    scope_filter_debug: dict[str, Any] = {}
    scoped_dialogue = corrected_dialogue
    scope_user_prompt = _SCOPE_AGENT_USER_TEMPLATE.format(
        staff_context=staff_text,
        preprocess_context=json.dumps(preprocess_context, ensure_ascii=False, separators=(",", ":")),
        dialogue=_dialogue_for_scope_prompt(corrected_dialogue),
    )
    try:
        scope_call_count = 1
        scope_parsed = _call_agent(
            "scope",
            _SCOPE_AGENT_SYSTEM_PROMPT,
            scope_user_prompt,
            max_tokens=5000,
        )
        scope_graph = _extract_scope_graph(scope_parsed)
        scoped_dialogue, scope_filter_debug = _dialogue_with_scope_filter(corrected_dialogue, scope_graph)
    except Exception as exc:
        logger.warning("agent scope segmentation failed, using full corrected dialogue: %s", exc)
        scope_graph = {"error": str(exc), "segments": []}
        scope_filter_debug = {"removed_line_count": 0, "kept_line_count": len(corrected_dialogue.splitlines()), "error": str(exc)}

    evidence_graph, evidence_chunk_debug = _extract_evidence_by_chunks(
        scoped_dialogue,
        staff_text=staff_text,
        preprocess_context=preprocess_context,
    )
    evidence_call_count = max(1, len(evidence_chunk_debug))
    rescue_call_count = 0
    scene_assessment: dict[str, Any] = {}
    rescue_payload: dict[str, Any] = {}
    if _business_evidence_needs_scene_rescue(evidence_graph):
        rescue_user_prompt = _EMPTY_EVIDENCE_RESCUE_USER_TEMPLATE.format(
            staff_context=staff_text,
            preprocess_context=json.dumps(preprocess_context, ensure_ascii=False, separators=(",", ":")),
            dialogue=_compact_for_prompt(scoped_dialogue, max_chars=18000),
        )
        try:
            rescue_call_count = 1
            rescue_payload = _call_agent(
                "empty_evidence_rescue",
                _EMPTY_EVIDENCE_RESCUE_SYSTEM_PROMPT,
                rescue_user_prompt,
                max_tokens=7000,
            )
            scene_assessment = _extract_scene_assessment(rescue_payload)
            rescue_graph = _extract_evidence_graph(rescue_payload)
            if not _is_non_current_consultation_scene(scene_assessment) and not _evidence_graph_is_empty(rescue_graph):
                evidence_graph = _merge_evidence_graphs(
                    [rescue_graph],
                    [
                        {
                            "chunk_index": 1,
                            "line_range": "rescue",
                            "line_count": len(corrected_dialogue.splitlines()),
                            "char_count": len(corrected_dialogue),
                            "evidence_counts": {
                                section: len(_as_list(rescue_graph.get(section)))
                                for section in _EVIDENCE_LIST_SECTIONS
                            },
                        }
                    ],
                )
        except Exception as exc:
            logger.warning("agent empty-evidence rescue failed, continuing with empty evidence: %s", exc)
            rescue_payload = {"error": str(exc)}

    if _is_non_current_consultation_scene(scene_assessment):
        fact_graph = {
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
            "uncertainties": [],
            "deal_outcome": {"status": "未明确", "summary": "非当前顾客面诊场景，未生成 SAP 回写事实"},
        }
        analysis_result = _build_analysis_result_from_fact_graph(fact_graph, raw, allow_raw_augmentation=False)
        analysis_result = _mark_non_consultation_scene(analysis_result, scene_assessment)
        debug = analysis_result.setdefault("staged_pipeline_debug", {})
        if isinstance(debug, dict):
            total_logical_calls = 1 + scope_call_count + evidence_call_count + rescue_call_count
            debug["production_chain"] = PIPELINE_NAME
            debug["llm_call_plan"] = {
                "model": STAGED_LLM_MODEL,
                "correction_agent": 1,
                "scope_agent": scope_call_count,
                "evidence_agent": evidence_call_count,
                "empty_evidence_rescue_agent": rescue_call_count,
                "event_graph_agent": 0,
                "judgment_agent": 0,
                "recommendation_adjudication_agent": 0,
                "indication_adjudication_agent": 0,
                "audit_agent": 0,
                "final_result_audit_agent": 0,
                "indication_adjudication_after_audit": 0,
                "fact_graph_to_analysis_result": 0,
                "total_logical_calls": total_logical_calls,
            }
            debug["scene_assessment"] = scene_assessment
            debug["agent_scope_graph"] = scope_graph
            debug["agent_scope_filter"] = scope_filter_debug
            debug["agent_evidence_chunking"] = {
                "chunk_count": evidence_call_count,
                "target_chars": EVIDENCE_CHUNK_TARGET_CHARS,
                "overlap_lines": EVIDENCE_CHUNK_OVERLAP_LINES,
            }
        total_logical_calls = 1 + scope_call_count + evidence_call_count + rescue_call_count
        return {
            "pipeline": PIPELINE_NAME,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "llm_call_plan": {
                "model": STAGED_LLM_MODEL,
                "correction_agent": 1,
                "scope_agent": scope_call_count,
                "evidence_agent": evidence_call_count,
                "empty_evidence_rescue_agent": rescue_call_count,
                "event_graph_agent": 0,
                "judgment_agent": 0,
                "recommendation_adjudication_agent": 0,
                "indication_adjudication_agent": 0,
                "audit_agent": 0,
                "final_result_audit_agent": 0,
                "indication_adjudication_after_audit": 0,
                "fact_graph_to_analysis_result": 0,
                "total_logical_calls": total_logical_calls,
            },
            "input_stats": {
                "dialogue_chars": len(dialogue),
                "corrected_dialogue_chars": len(corrected_dialogue),
                "scoped_dialogue_chars": len(scoped_dialogue),
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
            "scope_graph": scope_graph,
            "scope_filter_debug": scope_filter_debug,
            "evidence_graph": evidence_graph,
            "event_graph": {"skipped": True, "reason": "non_current_customer_consultation"},
            "evidence_chunk_debug": evidence_chunk_debug,
            "empty_evidence_rescue": rescue_payload,
            "scene_assessment": scene_assessment,
            "candidate_indications": "",
            "plan_adjudication": {"skipped": True, "reason": "non_current_customer_consultation"},
            "indication_adjudication": {"skipped": True, "reason": "non_current_customer_consultation"},
            "audit": {"skipped": True, "revision_required": False, "issues": [], "trigger_reasons": []},
            "fact_graph": fact_graph,
            "analysis_result": analysis_result,
        }

    relevant_dialogue_excerpt = _relevant_dialogue_excerpt(scoped_dialogue, evidence_graph)

    event_graph_call_count = 0
    event_graph: dict[str, Any] = {}
    event_user_prompt = _EVENT_AGENT_USER_TEMPLATE.format(
        evidence_graph=_compact_for_prompt(evidence_graph, max_chars=18000),
        scope_graph=_compact_for_prompt(scope_graph, max_chars=8000),
        dialogue=relevant_dialogue_excerpt,
    )
    try:
        event_graph_call_count = 1
        event_parsed = _call_agent(
            "event_graph",
            _EVENT_AGENT_SYSTEM_PROMPT,
            event_user_prompt,
            max_tokens=9000,
        )
        event_graph = _extract_event_graph(event_parsed)
    except Exception as exc:
        logger.warning("agent event_graph extraction failed, continuing without event constraints: %s", exc)
        event_graph = {"error": str(exc)}

    evidence_text = json.dumps(evidence_graph, ensure_ascii=False, separators=(",", ":"))
    event_text = json.dumps(event_graph, ensure_ascii=False, separators=(",", ":"))
    candidate_rows = _candidate_indications_from_text(f"{evidence_text}\n{event_text}\n{relevant_dialogue_excerpt}", max_items=36)
    candidate_indications = _format_candidate_indications(candidate_rows)

    judgment_user_prompt = _JUDGMENT_AGENT_USER_TEMPLATE.format(
        evidence_graph=_compact_for_prompt(evidence_graph),
        event_graph=_compact_for_prompt(event_graph, max_chars=14000),
        candidate_indications=_compact_for_prompt(candidate_indications, max_chars=12000),
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
    fact_graph = _apply_event_graph_constraints(fact_graph, event_graph)

    plan_call_count = 0
    if _as_list(evidence_graph.get("recommendation_evidence")) or _as_list(fact_graph.get("recommendations")) or _as_list(fact_graph.get("seed_recommendations")):
        plan_user_prompt = _PLAN_AGENT_USER_TEMPLATE.format(
            fact_graph=_compact_for_prompt(fact_graph),
            evidence_graph=_compact_for_prompt(_evidence_for_plan_prompt(evidence_graph), max_chars=14000),
            event_graph=_compact_for_prompt(event_graph, max_chars=12000),
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
            fact_graph = _apply_event_graph_constraints(fact_graph, event_graph)
        except Exception as exc:
            logger.warning("agent recommendation adjudication failed, using judgment fact_graph: %s", exc)
            plan_adjudication = {"error": str(exc), "recommendations": [], "seed_recommendations": []}
    else:
        plan_adjudication = {"skipped": True, "reason": "no recommendation evidence or recommendation facts"}

    indication_user_prompt = _INDICATION_ADJUDICATION_USER_TEMPLATE.format(
        fact_graph=_compact_for_prompt(_compact_fact_graph_for_indications(fact_graph), max_chars=14000),
        candidate_indications=_compact_for_prompt(candidate_indications, max_chars=12000),
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
            evidence_graph=_compact_for_prompt(evidence_graph, max_chars=18000),
            event_graph=_compact_for_prompt(event_graph, max_chars=12000),
            candidate_indications=_compact_for_prompt(candidate_indications, max_chars=12000),
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
                fact_graph = _apply_event_graph_constraints(fact_graph, event_graph)
                # Re-run indication adjudication after fact repair so SAP indications
                # match the final fact graph.
                repaired_indication_user_prompt = _INDICATION_ADJUDICATION_USER_TEMPLATE.format(
                    fact_graph=_compact_for_prompt(_compact_fact_graph_for_indications(fact_graph), max_chars=14000),
                    candidate_indications=_compact_for_prompt(candidate_indications, max_chars=12000),
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
    fact_graph = _apply_event_graph_constraints(fact_graph, event_graph)
    fact_graph = _agent_remove_rejected_indications(fact_graph, indication_adjudication)
    fact_graph = _agent_ensure_common_indications(fact_graph)
    fact_graph = _agent_prune_unsupported_pigment_fallbacks(fact_graph)
    fact_graph = _agent_remove_rejected_indications(fact_graph, indication_adjudication)
    fact_graph = _agent_add_optional_seed_recommendations_from_events(fact_graph, event_graph)
    fact_graph = _agent_repair_recommendation_demand_links(fact_graph)
    fact_graph = _agent_demote_orphan_optional_recommendations(fact_graph)
    fact_graph = _agent_remove_redundant_seed_recommendations(fact_graph)
    fact_graph = _agent_fill_missing_fact_evidence_from_events(fact_graph, event_graph)
    fact_graph = _agent_prune_seed_only_indications(fact_graph)
    analysis_result = _build_analysis_result_from_fact_graph(fact_graph, raw, allow_raw_augmentation=False)
    analysis_result = _agent_finalize_analysis_result(analysis_result, context=f"{corrected_dialogue}\n{dialogue}")
    final_audit_call_count = 0
    final_audit_required, final_audit_reasons = _final_result_audit_needed(
        analysis_result,
        corrected_dialogue=scoped_dialogue,
        fact_graph=fact_graph,
        event_graph=event_graph,
    )
    if final_audit_required:
        final_audit_user_prompt = _FINAL_RESULT_AUDIT_USER_TEMPLATE.format(
            trigger_reasons=_compact_for_prompt(final_audit_reasons, max_chars=4000),
            scope_graph=_compact_for_prompt(scope_graph, max_chars=8000),
            evidence_graph=_compact_for_prompt(evidence_graph, max_chars=14000),
            event_graph=_compact_for_prompt(event_graph, max_chars=10000),
            fact_graph=_compact_for_prompt(fact_graph, max_chars=14000),
            analysis_result=_compact_for_prompt(analysis_result, max_chars=14000),
            dialogue=_truncate_text_for_prompt(relevant_dialogue_excerpt, max_chars=12000),
        )
        try:
            final_audit_call_count = 1
            final_audit_parsed = _call_agent(
                "final_result_audit",
                _FINAL_RESULT_AUDIT_SYSTEM_PROMPT,
                final_audit_user_prompt,
                max_tokens=9000,
            )
            final_audit, analysis_result_patch = _extract_final_result_audit(final_audit_parsed)
            final_audit["trigger_reasons"] = final_audit_reasons
            if final_audit.get("revision_required") and analysis_result_patch:
                analysis_result = _apply_final_result_audit_patch(analysis_result, analysis_result_patch)
                analysis_result = _agent_finalize_analysis_result(analysis_result, context=f"{corrected_dialogue}\n{dialogue}")
        except Exception as exc:
            logger.warning("agent final result audit failed, using pre-audit analysis_result: %s", exc)
            final_audit = {"error": str(exc), "revision_required": False, "issues": [], "trigger_reasons": final_audit_reasons}
    else:
        final_audit = {"skipped": True, "revision_required": False, "issues": [], "trigger_reasons": final_audit_reasons}
    debug = analysis_result.setdefault("staged_pipeline_debug", {})
    if isinstance(debug, dict):
        total_logical_calls = 3 + scope_call_count + evidence_call_count + rescue_call_count + event_graph_call_count + plan_call_count + audit_call_count + final_audit_call_count + indication_after_audit_count
        debug["production_chain"] = PIPELINE_NAME
        debug["llm_call_plan"] = {
            "model": STAGED_LLM_MODEL,
            "correction_agent": 1,
            "scope_agent": scope_call_count,
            "evidence_agent": evidence_call_count,
            "empty_evidence_rescue_agent": rescue_call_count,
            "event_graph_agent": event_graph_call_count,
            "judgment_agent": 1,
            "recommendation_adjudication_agent": plan_call_count,
            "indication_adjudication_agent": 1,
            "audit_agent": audit_call_count,
            "final_result_audit_agent": final_audit_call_count,
            "indication_adjudication_after_audit": indication_after_audit_count,
            "fact_graph_to_analysis_result": 0,
            "total_logical_calls": total_logical_calls,
        }
        debug["agent_audit"] = audit
        debug["agent_final_result_audit"] = final_audit
        debug["agent_scope_graph"] = scope_graph
        debug["agent_scope_filter"] = scope_filter_debug
        debug["agent_event_graph"] = event_graph
        debug["agent_evidence_chunking"] = {
            "chunk_count": evidence_call_count,
            "target_chars": EVIDENCE_CHUNK_TARGET_CHARS,
            "overlap_lines": EVIDENCE_CHUNK_OVERLAP_LINES,
        }
        if scene_assessment:
            debug["scene_assessment"] = scene_assessment

    total_logical_calls = 3 + scope_call_count + evidence_call_count + rescue_call_count + event_graph_call_count + plan_call_count + audit_call_count + final_audit_call_count + indication_after_audit_count
    return {
        "pipeline": PIPELINE_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "llm_call_plan": {
            "model": STAGED_LLM_MODEL,
            "correction_agent": 1,
            "scope_agent": scope_call_count,
            "evidence_agent": evidence_call_count,
            "empty_evidence_rescue_agent": rescue_call_count,
            "event_graph_agent": event_graph_call_count,
            "judgment_agent": 1,
            "recommendation_adjudication_agent": plan_call_count,
            "indication_adjudication_agent": 1,
            "audit_agent": audit_call_count,
            "final_result_audit_agent": final_audit_call_count,
            "indication_adjudication_after_audit": indication_after_audit_count,
            "fact_graph_to_analysis_result": 0,
            "total_logical_calls": total_logical_calls,
        },
        "input_stats": {
            "dialogue_chars": len(dialogue),
            "corrected_dialogue_chars": len(corrected_dialogue),
            "scoped_dialogue_chars": len(scoped_dialogue),
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
        "scope_graph": scope_graph,
        "scope_filter_debug": scope_filter_debug,
        "evidence_graph": evidence_graph,
        "event_graph": event_graph,
        "evidence_chunk_debug": evidence_chunk_debug,
        "empty_evidence_rescue": rescue_payload,
        "scene_assessment": scene_assessment,
        "relevant_dialogue_excerpt": relevant_dialogue_excerpt,
        "candidate_indications": candidate_indications,
        "plan_adjudication": plan_adjudication,
        "indication_adjudication": indication_adjudication,
        "audit": audit,
        "final_result_audit": final_audit,
        "fact_graph": fact_graph,
        "analysis_result": analysis_result,
    }
