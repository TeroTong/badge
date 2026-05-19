"""Experimental staged LLM analysis pipeline.

This module is deliberately separate from the production analysis queue. It is
used to compare a staged pipeline against the current one-pass result without
updating AnalysisTask or triggering SAP push.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from smart_badge_api.analysis.consultation_evaluation import (
    rebuild_consultation_evaluation,
    rebuild_consultation_process_evaluation,
)
from smart_badge_api.analysis.extraction_prompts import SYSTEM_PROMPT as FALLBACK_SYSTEM_PROMPT
from smart_badge_api.analysis.llm_client import chat_completion, parse_json_response
from smart_badge_api.analysis.pipeline import sanitize_analysis_result_with_raw
from smart_badge_api.analysis.reference_data import load_analysis_reference_data
from smart_badge_api.analysis.transcript import extract_transcript_segments, prepare_transcript
from smart_badge_api.api.analysis_normalization import normalize_analysis_result
from smart_badge_api.tag_catalog_reference import (
    canonicalize_profile_tag_category,
    canonicalize_profile_tag_value,
    is_valid_profile_tag_value,
    load_tag_catalog_definitions,
)

logger = logging.getLogger(__name__)

PIPELINE_NAME = "staged_evidence_judgment_v1_gpt52"
STAGED_LLM_MODEL = "gpt-5.2-chat-latest"
_POSTPROCESS_CHUNK_TARGET_CHARS = 3500


_POSTPROCESS_SYSTEM_PROMPT = """\
You are an ASR post-processing assistant for Chinese medical-aesthetic
consultation transcripts.

Task scope:
1. Correct obvious ASR term errors only when the context strongly supports it.
2. Infer the speaker role for each input line.
3. Preserve the original evidence and timestamps.

Hard rules:
1. Do not summarize the consultation.
2. Do not extract demands, indications, recommendations, SAP remarks, or sales conclusions.
3. Do not invent information. If uncertain, keep original_text unchanged and add a "suspected" note.
4. Output roughly one turn per input line. Do not collapse many lines into a summary.
5. Speaker must be one of:
   customer, companion, consultant, doctor, expert_assistant, frontdesk, staff_peer, other.
6. If the speaker self-identifies as expert assistant / doctor assistant / dean assistant,
   do not label that turn as doctor.
7. Professional explanation alone is not enough to label a speaker as doctor; consultants
   and expert assistants can also explain procedures and prices.
8. "price / quote / how many syringes / how much" can be asked by a customer or said by
   staff. Judge from surrounding turns.
9. Do not label a long professional explanation as customer merely because it contains
   assessment language. If the turn explains anatomy, diagnoses, indications, treatment
   steps, dosage, risks, or case photos, it is usually staff/doctor unless surrounding
   turns clearly show the customer is speaking.
10. Keep the speaker stable across adjacent turns when the conversation is continuous.
    A speaker should not flip between customer and doctor/consultant without evidence
    such as a question/answer boundary, address term, or self-identification.
11. Correct medical-aesthetic brand/product ASR errors only when strongly supported by
    context. Common terms include 瑞德喜, 艾维岚, 艾拉斯提, 贝丽菲尔, 双美胶原蛋白,
    玻尿酸, 胶原蛋白, 肉毒, 除皱针, 溶解酶, 妈生鼻, 黑曜双波, 黄金微针.
12. In a nose/rhinoplasty context, correct obvious ASR confusions such as
    "妈生皮" to "妈生鼻". Do not make this correction in an unrelated skin-only
    context.

Return JSON only:
{
  "turns": [
    {
      "turn_id": "t001",
      "start": "00:00",
      "end": "00:03",
      "speaker": "consultant",
      "speaker_detail": "expert_assistant",
      "original_text": "",
      "corrected_text": "",
      "corrections": [],
      "confidence": 0.0
    }
  ],
  "role_summary": "",
  "quality_notes": []
}
"""


_POSTPROCESS_USER_TEMPLATE = """\
Staff / recording context:
{staff_context}

This is chunk {part_num}/{total_parts}. Preserve every input line as much as possible.

Transcript chunk:
{dialogue}
"""


_FACT_GRAPH_SYSTEM_PROMPT = """\
You are the core fact extractor for a staged Chinese medical-aesthetic
consultation analysis pipeline.

You will receive:
1. The current production analysis rules and dictionaries.
2. A transcript that may have ASR post-processing and speaker-role correction.

Your output is ONLY fact_graph. Do not output the final production analysis_result.
The application code will deterministically map fact_graph to analysis_result.

Fact graph rules:
1. Preserve links between customer demands, diagnoses, indication candidates,
   recommendations, seed recommendations, concerns, budget facts, and deal outcome.
2. A demand is a current problem or goal expressed directly by the main customer,
   explicitly confirmed by the main customer, or restated by staff and then
   confirmed by the customer. Staff/doctor observations without customer
   confirmation belong in doctor_diagnoses or seed_recommendations, not demands.
   If the main customer explicitly raises a later, cross-department, or deferred
   project/demand such as 美白、毛孔、痘印、暗沉、水光、光电, keep it as a demand
   because it is useful for SAP remarks and future follow-up. Mark it as
   referral/deferred in evidence; do not create a current recommendation or final
   SAP indication from that demand unless a real current plan is discussed.
3. An indication candidate must be supported by at least one demand, recommendation,
   or doctor diagnosis. Copy exact indication codes from the dictionary whenever
   you choose a standardized indication.
4. recommendations are plans that solve the current customer demands. Every
   recommendation should contain related_demand_ids when a demand exists.
5. seed_recommendations are additional upsell/maintenance/next-visit plans outside
   the current main demands. Plans described as "later", "next time", "not urgent",
   "can consider", "afterwards", or "maintenance" belong here unless the customer
   clearly makes them today's main demand.
6. Extract concrete details when present: brand, material, dosage, price, course,
   treatment steps, implementation notes, and customer response.
7. concerns and deal_factors must be concrete. Avoid vague labels like
   "treatment condition limitation" without the actual limitation.
8. budget_facts are only the main customer's explicit budget, acceptable amount,
   payment/deposit amount, clear affordability limit, or implicit budget pressure
   tied to a concrete quoted amount/range. Example: "对总价约29000-30000元较敏感并反复核算"
   is valid budget pressure and the final "本次预算" should be rendered as
   "未明确；对总价约29000-30000元较敏感，倾向希望低于该区间". Staff-only quotes,
   treatment prices, effect explanations such as "X块解决不了多少", and generic
   price calculation must stay in recommendation details or deal_factors; do not
   promote them to the final "本次预算" field unless there is a customer
   affordability reaction.
9. Do not turn skin problems on the nose area into nose surgery. If the transcript
   says pore/acne/oil/blackhead/skin texture on nose tip or nose wing, it is a skin
   concern, not rhinoplasty or nose comprehensive surgery unless explicit nose
   surgery/injection contouring is discussed.
10. Do not turn concerns or expectations into demands. "wants natural result",
   "worries about unevenness", "afraid of risks", and "needs to consider" are
   concerns/decision factors unless they are tied to a concrete body-area goal.
11. Preserve specialty terms exactly when supported by evidence, especially:
    眶外C线, 眉弓线, 颞区, 额颞, 外轮廓线, 内轮廓线, 鼻基底, 泪沟,
    瑞德喜, 艾维岚, 艾拉斯提, 贝丽菲尔, 双美胶原蛋白.
12. If a plan contains dosage, brand, material, price, course, or sequence, put
    those details into the structured fields instead of leaving the recommendation
    as a short generic phrase.
13. For recommendations vs seed_recommendations, judge by relation to the
    customer's current demand. A staged sequence that completes the current goal
    remains a recommendation even if it says "later/afterwards"; a plan belongs
    to seed_recommendations only when it is outside the current goal, clearly
    lower priority, maintenance, next-visit, or explicitly "not recommended now".
14. Indication candidates are preliminary and must be high precision. Do not add
    痤疮 from "闭口时/闭上嘴" mouth-closing context. Do not add 面部除皱 from
    咬肌肉毒/瘦脸 unless wrinkle/动态纹/核桃纹/除皱针 is explicit.

Return JSON only:
{
  "fact_graph": {
    "demands": [
      {
        "id": "D1",
        "content": "",
        "body_part": "",
        "source": "customer_direct|customer_confirmed|staff_restated_confirmed",
        "evidence_turn_ids": [],
        "evidence": [],
        "confidence": 0.0
      }
    ],
    "doctor_diagnoses": [
      {"content": "", "body_part": "", "evidence_turn_ids": [], "evidence": [], "confidence": 0.0}
    ],
    "indication_candidates": [
      {
        "standardized_indication": "Y2|微创|SYZ2001|塑美|BW2019|眶外C线（小O）",
        "indication_name": "",
        "body_part_name": "",
        "linked_demand_ids": [],
        "linked_recommendation_ids": [],
        "evidence_turn_ids": [],
        "evidence": [],
        "confidence": 0.0
      }
    ],
    "recommendations": [
      {
        "id": "R1",
        "content": "",
        "related_demand_ids": [],
        "body_part": "",
        "brand": "",
        "material": "",
        "dosage": "",
        "price": "",
        "course_or_frequency": "",
        "treatment_steps": [],
        "implementation_notes": "",
        "customer_response": "接受|犹豫|拒绝|未明确回应",
        "evidence_turn_ids": [],
        "evidence": [],
        "confidence": 0.0
      }
    ],
    "seed_recommendations": [
      {
        "id": "S1",
        "content": "",
        "reason_not_current_main_plan": "",
        "body_part": "",
        "brand": "",
        "material": "",
        "dosage": "",
        "price": "",
        "course_or_frequency": "",
        "treatment_steps": [],
        "implementation_notes": "",
        "customer_response": "接受|犹豫|拒绝|未明确回应",
        "evidence_turn_ids": [],
        "evidence": [],
        "confidence": 0.0
      }
    ],
    "concerns": [
      {"type": "", "content": "", "evidence_turn_ids": [], "evidence": [], "confidence": 0.0}
    ],
    "budget_facts": [],
    "medical_history": [],
    "deal_factors": [],
    "deal_outcome": {},
    "uncertainties": []
  }
}
"""


_FACT_GRAPH_USER_TEMPLATE = """\
Current production rules and dictionaries:
{analysis_rules}

Transcript for fact extraction:
{postprocessed_dialogue}

Output fact_graph JSON only.
"""


_CORRECTION_SYSTEM_PROMPT = """\
You are the lightweight transcript correction stage for a Chinese
medical-aesthetic consultation analysis pipeline.

Your job is NOT to rewrite the transcript. Output only a patch for lines that
need correction.

Correction scope:
1. Speaker role corrections when the current speaker label is clearly wrong.
2. High-value medical-aesthetic term corrections when confidence is high:
   body areas, contour lines, projects, brands, materials, dosage, prices, and
   treatment sequence.

Hard rules:
1. Do not summarize, delete, merge, or rephrase whole lines.
2. Do not add facts that are not in the transcript.
3. Do not correct normal filler words or everyday chat.
4. If unsure, put the item in uncertain_notes instead of correcting it.
5. First infer a stable role for each ASR speaker id in speaker_role_map.
   The same asr_speaker should normally keep one business role across the
   transcript. Use line-level speaker_corrections only for true exceptions
   such as diarization errors, mixed speakers, or a different person taking
   over the same ASR speaker id.
6. Also infer a stable participant label and customer_scope for each ASR
   speaker id. This is critical when two or more customers/companions are
   present. Use:
   - participant_label="主咨询客户" for the person whose visit/order is being
     handled in this recording;
   - "同行客户A"/"同行客户B" for other people who ask about their own treatment;
   - "陪同人员" for family/friends who speak for or ask about the main customer;
   - staff labels such as "医生", "咨询师", "专家助理", "前台".
   customer_scope must be one of primary_customer, other_customer,
   companion_or_family, staff, unknown.
7. Do not label two different customer speakers simply as "客户" when the
   transcript gives enough evidence to distinguish main customer vs同行客户.
   If unsure which customer is primary, choose the best-supported primary and
   add an uncertain_notes item.
8. A professional explanation about anatomy, treatment steps, dosage, risks,
   case photos, or recommended plans is usually consultant/doctor/staff, not
   customer, unless surrounding turns clearly show the customer is speaking.
9. A person self-identifying as expert assistant / doctor assistant / dean
   assistant should not be labeled doctor.
10. Customer speech usually contains personal goals, feelings, hesitation,
   budget/price questions, consent/refusal, or follow-up questions.
11. Term corrections must be local string replacements within one line. Correct
   only when the replacement is strongly supported by nearby context.

Common high-value terms:
眶外C线, 眉弓线, 颞区, 额颞, 外轮廓线, 内轮廓线, 鼻基底, 泪沟,
瑞德喜, 艾维岚, 艾拉斯提, 贝丽菲尔, 双美胶原蛋白, 玻尿酸, 胶原蛋白,
肉毒, 除皱针, 溶解酶, 热玛吉, 超声炮, 妈生鼻, 黑曜双波, 黄金微针.

Contextual ASR correction examples:
- If surrounding turns are about 鼻子/鼻综合/鼻背/鼻尖/鼻翼 and the line says
  "妈生皮", correct it to "妈生鼻".
- Correct "黑耀双波/黑药双波/黑曜双播" to "黑曜双波" when discussing 光电/射频/
  黄金微针类皮肤项目.

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


_CORRECTION_USER_TEMPLATE = """\
Staff / recording context:
{staff_context}

Code-side preprocessing hints:
{preprocess_context}

Numbered transcript:
{numbered_dialogue}

Output correction_patch JSON only.
"""


_EVIDENCE_SYSTEM_PROMPT = """\
You are the evidence extraction stage for a Chinese medical-aesthetic
consultation analysis pipeline.

Your only job is to extract evidence from the transcript. Do not generate the
final analysis, do not choose final SAP indications, and do not summarize beyond
the evidence itself.

Evidence extraction rules:
1. Keep customer-side evidence separate from staff/doctor-side evidence.
2. A customer demand evidence must be spoken by the customer, clearly confirmed
   by the customer, or restated by staff and accepted by the customer.
3. Staff/doctor observations are evidence, but they are not customer demands
   unless customer confirmation is present.
Participant rules:
- When the corrected transcript distinguishes participants such as 主咨询客户,
  同行客户A/同行客户B, or 陪同人员, preserve that participant label in every evidence
  item. Add participant_scope: primary_customer, other_customer,
  companion_or_family, staff, or unknown.
- Extract evidence for every consulting customer. Independent demands from
  同行客户A/同行客户B must be marked other_customer and kept separate from 主咨询客户,
  because the same recording may later be linked to multiple SAP visit orders.
- Do not merge one customer's demand, concern, budget, recommendation, medical
  history, deal status, or indication support into another customer's facts.
- 陪同人员 can provide supporting information for 主咨询客户, but if the wording is
  about the companion's own treatment need, mark it other_customer instead of
  primary_customer.
4. Preserve exact medical-aesthetic terms, body areas, brand names, material,
   dosage, price, course, sequence, risks, and customer response.
5. For every evidence item, include turn ids/timestamps if available and quote
   the shortest useful original text. If evidence is uncertain, mark confidence
   lower instead of inventing missing information.
6. Distinguish:
   - current customer demand evidence;
   - doctor/staff diagnosis evidence;
   - current-plan recommendation evidence;
   - planting/next-visit/maintenance recommendation evidence;
   - concern/decision-factor evidence;
   - budget/price/deal evidence;
   - medical history evidence;
   - speaker-role correction evidence.
7. Do not classify final recommendation vs planting only from words such as
   "later" or "can consider". Preserve the evidence and the reason; the next
   stage will make the final judgment.
8. When evidence contains a treatment sequence, keep it as one current-plan
   recommendation if the sequence is needed to solve the current demand.
   Example: "先做侧面，以后再做前面" can still be one current contour plan.
9. Mark a recommendation as planting/next-visit only when it is outside the
   current customer demand, clearly lower priority, maintenance, or explicitly
   not recommended for now.
10. For acne/pores evidence, distinguish skin "闭口/粉刺/痘痘/毛孔" from
    mouth-closing wording such as "闭口时/闭上嘴".
11. Do not promote ASR fragments into demands, diagnoses, recommendations, or
    indication evidence. A low-confidence fragment is text with broken body
    terms but no clear predicate/goal, for example "嘴巴鼻如还在的一个问题",
    "这个地方的问题", or isolated corrupted terms such as "全突/列区/面水".
    Put them in quality_notes or keep confidence <= 0.45.
12. A demand should be actionable: it should contain a body area plus a problem,
    goal, or customer-confirmed treatment intent. If the customer says only
    "这里/这个地方", combine it with the nearest staff-restated body area only
    when the customer clearly confirms it.
13. For contour/filling consultations that involve multiple regions, preserve
    each concrete region separately in evidence: e.g. 眶外C线/眉尾, 颞区/额颞,
    外颊/颧弓, 下颌轮廓线/下颌角, 下巴/颏部, 鼻基底/面中.
14. For body-contouring consultations, preserve explicit body concerns such as
    副乳/腋前胸外侧鼓出, 富贵包/颈后上背凸起, 手臂粗, 后背厚, 腰腹赘肉.
    When the customer says "给你看一下/帮我看一下" about their own body, treat
    it as current customer-demand evidence, not as a third-party case.
15. If the customer explicitly raises a project/demand but the transcript says
    it is not handled in this consultation, belongs to another department, or
    will be discussed later (for example "美白去皮肤科，不在我这里", "回来再说",
    "下次再说"), still keep it in customer_demand_evidence because it is a real
    customer demand. Mark handling_status as "referral_or_deferred" and quote
    the referral/deferred wording. Do not turn it into a current recommendation.
16. Concern evidence must come from customer/companion wording or explicit
    customer confirmation. Staff statements such as "效果自然", "别人看不出来",
    "很安全", or "恢复快" are selling points unless the customer expresses worry.
17. Recommendation evidence should represent a treatment/project plan. Do not
    extract standalone pre-op checks, postoperative medication, wound cleaning,
    scar gel, dressing change, stitches removal, or consumables as treatment
    recommendation items. Put them in implementation_notes of the related main
    plan or quality_notes.
18. If staff describes an option only for comparison, says it is not suitable,
    not recommended, not the priority, or only answers the customer's price
    question, mark relation_to_current_demand as "alternative_not_recommended".
19. For skin anti-aging consultations, preserve skin laxity/tightening evidence
    separately from pores/acne/dullness. If the main discussion is 热玛吉/超声炮/
    黄金微针/黑曜双波/射频/光电 for tightening, capture the tightening/laxity
    plan even when 毛孔、痘印、暗沉 are also mentioned.
20. Extract profile_evidence whenever the transcript contains customer label
    signals, even if they are not SAP indications: prior treatments/materials
    or devices, current budget, price sensitivity, pain tolerance, children or
    family situation, industry/special identity, comparison institution,
    decision maker, treatment preference, recovery/time constraint, and product
    or project preference. Keep participant/participant_scope and quote the
    exact evidence. Do not drop these facts just because they are not part of
    the current treatment plan.

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
    "diagnosis_evidence": [
      {"id": "E_X1", "content": "", "body_part": "", "speaker": "", "evidence_turn_ids": [], "quote": "", "confidence": 0.0}
    ],
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


_EVIDENCE_USER_TEMPLATE = """\
Staff / recording context:
{staff_context}

Code-side preprocessing hints:
{preprocess_context}

Transcript:
{dialogue}

Extract evidence_graph JSON only.
"""


_JUDGMENT_SYSTEM_PROMPT = """\
You are the structured judgment stage for a Chinese medical-aesthetic
consultation analysis pipeline.

You receive an evidence_graph and a small candidate indication list recalled by
code from the local SAP indication dictionary. Your job is to turn evidence into
a fact_graph. The application will render the final analysis_result
deterministically from fact_graph.

Judgment rules:
1. Demands: include only current problems/goals with customer-side evidence or
   customer confirmation. Do not use staff/doctor observations alone as demands.
   When participant_scope is present, build facts for every consulting customer
   and keep participant/participant_scope on each item. Do not convert
   同行客户A/同行客户B independent needs into 主咨询客户 facts. Companion/family speech
   may support the main customer's demand only when it clearly describes the
   main customer rather than the companion's own treatment need.
   If the customer's explicit wording is sparse but the evidence_graph contains
   high-confidence diagnosis of the current customer, a current_main_plan
   recommendation, and customer confirmation/acceptance/price inquiry, create a
   conservative staff_restated_confirmed demand instead of returning empty.
   Also include explicit secondary/future/cross-department demands raised by the
   main customer, even when they are not solved in this consultation. Examples:
   美白、毛孔、痘印、暗沉、水光、光电、皮肤科项目. Keep them in demands for SAP
   remarks/follow-up, but do not generate current recommendations or final SAP
   indications from them without a current plan.
2. Diagnoses: keep staff/doctor observations in doctor_diagnoses when they help
   explain the plan or indication, but do not rewrite them as customer demands.
3. Recommendations: include plans that solve current demands. Every current
   recommendation should link to related_demand_ids when demands exist. Do not
   drop a high-confidence current_main_plan merely because the customer did not
   state the full complaint in one sentence.
4. Seed recommendations: include additional, next-visit, maintenance, or lower
   priority plans that do not directly solve the current demand. If a later-step
   plan is necessary to complete the current demand, keep it in recommendations
   and explain the sequence/course.
5. Concerns: naturalness, risk, side effects, price hesitation, recovery time,
   and "need to think" are concerns or decision factors, not demands.
6. Preliminary indications: propose candidates conservatively. The next stage
    will adjudicate the final SAP indications. Prefer the most specific candidate
    supported by evidence, but do not include a candidate unless you can point to
    a current demand, current-customer diagnosis, current recommendation, or
    concrete seed recommendation that supports the exact body area and project.
    If no candidate is supported, leave indications empty.
    When a real plan spans multiple distinct treated regions, include each
    supported specific candidate instead of collapsing everything to the first
    recalled item.
    For 副乳/腋前胸外侧鼓出, prefer the specific candidate "副乳整形" over generic
    "身体吸脂" when the current issue or plan is about 副乳. For 富贵包, keep it
    as a demand/diagnosis unless there is a clear吸脂/抽脂/超脂/减脂 treatment
    plan; do not force it into an SAP indication from "看一下/评估" alone.
7. Do not invent brands, dosage, prices, body parts, or deal outcomes. If the
   evidence is ambiguous, keep customer_response as "未明确回应" and confidence
   moderate/low.
8. Before returning JSON, silently check for contradictions:
   - concern duplicated as demand;
   - recommendation without any relation to a current demand;
   - planting plan mixed into current recommendations;
   - recommendation has brand/dosage/material evidence but fields are empty;
   - indication unsupported by demand, diagnosis, or recommendation evidence;
   - 面部除皱 selected from 咬肌肉毒/瘦脸 without wrinkle evidence;
   - 痤疮 selected from 闭口时/闭上嘴 mouth-closing wording;
   - 唇部 selected when the transcript says lip adjustment should wait.
   - demand/indication/recommendation built from a low-confidence ASR fragment
     rather than a complete business fact.
9. If the evidence looks like internal staff/order/payment discussion and there
   is no main-customer demand evidence, no doctor/staff diagnosis of the current
   customer, and no current_main_plan recommendation, return empty demands,
   indications, recommendations, seed_recommendations, and
   deal_outcome.status="未明确". Do not infer a SAP consultation from internal
   discussion alone.
10. Deal outcome must require direct customer payment/order/deposit evidence or
   an explicit staff statement that this customer completed payment/order. Staff
   discussing historical/internal orders is not enough.
11. A customer-explicit project sent to another department or deferred to later
   is still a demand and should stay in demands/SAP consultation remarks. Mark
   it clearly as referral/deferred when the evidence supports that. However, do
   not create recommendations or indication candidates solely from referral or
   deferred demand evidence.
12. Recommendations must be treatment/project plans solving current demands.
   Exclude standalone pre-op checks, postoperative medication, wound cleaning,
   scar gel, dressing changes, and other nursing/consumable instructions from
   recommendations and seed_recommendations. Keep them as implementation notes
   only when they are attached to a main treatment plan.
13. If an option is explicitly "not suitable", "not recommended", "only for
   price comparison", or "backup/alternative", do not put it in current
   recommendations. It may appear as a decision factor, but not as the final
   recommended plan.
14. For skin anti-aging, when the evidence shows the customer's core issue is
    skin laxity, sagging, tightening, or anti-aging and the plan is 热玛吉/超声炮/
    黄金微针/黑曜双波/射频/光电, prioritize 松弛下垂/紧致淡纹 indication candidates
    over incidental 毛孔、痘印、暗黄. Do not select 痤疮 from 痘印/痘坑 alone unless
    active acne/粉刺/炎症痘 is clearly present.
    This priority rule is only for SAP indication selection. Do not delete the
    customer's explicit 毛孔、痘印、暗黄、美白、水光/光电需求 from demands.
15. Every recommendation, seed_recommendation, concern, budget fact, and
   indication candidate must carry evidence text or quote. Do not return only
   evidence ids without the evidence text.
16. Convert evidence_graph.profile_evidence into profile_facts. Also preserve
   customer profile signals from medical_history_evidence, budget_evidence,
   concern_evidence, and deal_evidence when they describe labels such as prior
   treatment, material/device, budget, price sensitivity, pain tolerance,
   children/family situation, industry/special identity, comparison institution,
   decision maker, treatment preference, recovery/time constraint, or product
   preference. These facts are used for customer tags and must not be dropped
   just because they are not SAP indications.
   For health-risk/contraindication profile_facts, only use evidence clearly
   about the customer or accompanying customer. Do not convert staff/doctor
   self-disclosure, product descriptions, or ambiguous skin sensitivity wording
   into customer tags. "皮肤过敏/敏感肌/玫瑰痤疮" alone is not "过敏史";
   output allergy history only for explicit medical allergy evidence such as
   药物过敏、麻药过敏、碘伏/酒精/胶布过敏 or "对X过敏".

Return JSON only:
{
  "fact_graph": {
    "demands": [
      {
        "id": "D1",
        "content": "",
        "body_part": "",
        "source": "customer_direct|customer_confirmed|staff_restated_confirmed",
        "participant": "主咨询客户|同行客户A|同行客户B|unknown",
        "participant_scope": "primary_customer|other_customer|unknown",
        "evidence_ids": [],
        "evidence": [],
        "confidence": 0.0
      }
    ],
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


_JUDGMENT_USER_TEMPLATE = """\
Evidence graph:
{evidence_graph}

Candidate indications recalled from local dictionary:
{candidate_indications}

Build fact_graph JSON only.
"""


_INDICATION_ADJUDICATION_SYSTEM_PROMPT = """\
You are the final SAP indication adjudicator for a Chinese medical-aesthetic
consultation analysis pipeline.

The SAP indication field is business-critical. Optimize for precision over
recall. It is better to return fewer indications than to write an unsupported
or similar-but-wrong indication.

You will receive:
1. A preliminary fact_graph with demands, diagnoses, current recommendations,
   seed recommendations, and preliminary indication candidates.
2. A candidate list recalled from the local SAP indication dictionary.

Choose final_indications only from candidate_indications. Do not invent codes.

Hard selection rules:
1. A final indication must be supported by concrete evidence from at least one
   of these:
   - current customer demand;
   - staff/doctor diagnosis of the current customer;
   - current recommendation that solves the current demand;
   - concrete seed recommendation only when it is a real suggested project, not
     merely a deferred or explicitly not-recommended item.
   If fact/evidence carries participant_scope, adjudicate indications separately
   for each participant. Preserve participant and participant_scope on every
   selected indication. Do not use 同行客户A/同行客户B evidence to support 主咨询客户
   indications, and do not use 主咨询客户 evidence to support 同行客户 indications.
2. Body area must match the evidence. Do not select a body-area indication
   because that body part was only mentioned in passing.
3. A brand/material/product does not by itself determine an indication. Connect
   it to the treated body area and actual plan.
4. Do not select 面部除皱 only because "肉毒" appears. Select it only when the
   evidence is about wrinkles/dynamic lines/核桃纹/除皱针. 咬肌肉毒/瘦脸 and
   下巴肌肉放松 without wrinkle evidence are not enough.
5. Do not select 痤疮/毛孔 for "闭口时", "闭上嘴", "闭嘴", or other mouth-closing
   contexts. 痤疮 requires acne/痘痘/粉刺/炎症痘 evidence. 毛孔 requires pore or
   skin-texture evidence.
6. Do not select 唇部/嘴唇-related indications when the evidence says the lip
   plan is not recommended now, should wait until later, or is only discussed as
   a comparison.
7. Do not turn 鼻头/鼻翼 pore/acne/oil/blackhead/skin texture into 鼻综合,
   隆鼻, or nose contouring unless surgery/injection nose contouring is explicit.
8. If a generic indication and a more specific indication both apply, keep the
   specific indication. Keep the generic one only if it represents a different
   real treatment category that is also supported.
9. If a multi-region contour/filling plan is supported by evidence, keep all
   distinct supported regions/categories. Do not collapse 眶外C线, 颞区/额颞,
   外颊/颧弓, 下颌轮廓线, 下巴/颏部, 鼻基底/面中 into one indication merely
   because they appear in the same treatment discussion.
10. Use candidate selection_note as disambiguation guidance. If the note says
    the candidate does not match the described complaint/plan, reject it even if
    the body part name overlaps.
11. Reject a candidate when the only support is a low-confidence ASR fragment,
     broken body-term string, or text without a clear problem/plan predicate.
12. For body-contouring evidence:
    - 副乳/腋前胸外侧鼓出 with a current customer complaint or staff-restated
      current issue should select "副乳整形" when that candidate exists.
    - Do not replace 副乳整形 with generic 身体吸脂 merely because the method may
      include抽脂/吸脂.
    - 富贵包 has no dedicated SAP indication in this dictionary; select 身体吸脂
      only when there is a clear current plan or accepted option involving
      吸脂/抽脂/超脂/局部减脂 for the富贵包/颈后上背 area. Mere "看一下/评估富贵包"
      is a demand but not enough for a final SAP indication.
12. If the transcript is internal chat/order/payment discussion and has no valid
   main-customer demand/diagnosis/recommendation, return an empty final list.
13. Reject not-current/referral projects as final SAP indications, such as
    "美白去皮肤科，不在我这里", unless there is a real current recommendation or
    plan for that same project. They may still remain in chief complaint text.
14. For skin tightening/anti-aging consultations, if the current demand or plan
    is skin laxity/tightening and the candidate list contains 松弛下垂 or 紧致淡纹,
    select the supported anti-aging indication before incidental 毛孔、暗黄.
15. Do not select 痤疮 from 痘印/痘坑 alone. 痤疮 requires active acne, acne
    lesions, 粉刺, 炎症痘, or current acne treatment evidence.
16. Do not select or keep an indication supported only by standalone pre-op
    checks, postoperative care, scar gel, medication, or a rejected/comparison
    option.

Return JSON only:
{
  "final_indications": [
    {
      "standardized_indication": "Y2|微创|SYZ2001|塑美|BW2019|眶外C线（小O）",
      "participant": "主咨询客户|同行客户A|同行客户B|unknown",
      "participant_scope": "primary_customer|other_customer|unknown",
      "reason": "",
      "supporting_evidence": [],
      "confidence": 0.0
    }
  ],
  "rejected_indications": [
    {
      "standardized_indication": "",
      "reason": ""
    }
  ]
}
"""


_INDICATION_ADJUDICATION_USER_TEMPLATE = """\
Preliminary fact graph:
{fact_graph}

Candidate indications recalled from local dictionary:
{candidate_indications}

Return final SAP indications JSON only.
"""


def _call_json(system_prompt: str, user_prompt: str, *, max_tokens: int = 12000, attempts: int = 2) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            text = chat_completion(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.1,
                max_tokens=max_tokens,
                model_override=STAGED_LLM_MODEL,
            )
            parsed = parse_json_response(text)
            if not isinstance(parsed, dict):
                raise ValueError("LLM JSON root is not an object")
            return parsed
        except Exception as exc:  # pragma: no cover - exercised in live LLM calls
            last_error = exc
            if attempt >= max(attempts, 1):
                raise
            logger.warning("staged LLM JSON parse failed attempt=%d/%d: %s", attempt, attempts, exc)
    raise RuntimeError("staged LLM JSON parsing failed") from last_error


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _format_staff_context(staff_context: dict[str, Any] | None) -> str:
    if not staff_context:
        return "- no staff context"
    lines: list[str] = []
    for key in ("staff_name", "staff_role", "position", "hospital_code", "file_name"):
        value = _clean_text(staff_context.get(key))
        if value:
            lines.append(f"- {key}: {value}")
    return "\n".join(lines) if lines else "- no staff context"


def _split_dialogue_chunks(dialogue: str, *, target_chars: int = _POSTPROCESS_CHUNK_TARGET_CHARS) -> list[str]:
    lines = [line for line in dialogue.splitlines() if line.strip()]
    if not lines:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current and current_len + line_len > target_chars:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def _format_timestamp(value: object) -> str:
    text = _clean_text(value)
    if not text:
        return "00:00"
    if re.match(r"^\d{1,2}:\d{2}(?::\d{2})?$", text):
        return text
    try:
        seconds = float(text)
    except ValueError:
        return text
    if seconds > 10000:
        seconds = seconds / 1000.0
    total = max(int(seconds), 0)
    return f"{total // 60:02d}:{total % 60:02d}"


def _raw_transcribe_segments(raw: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _as_dict(raw.get("payload"))
    candidates = (
        _as_list(payload.get("transcribeResult"))
        or _as_list(payload.get("segments"))
        or _as_list(raw.get("transcribeResult"))
        or _as_list(raw.get("segments"))
    )
    return [item for item in candidates if isinstance(item, dict)]


def _segment_evidence(seg: dict[str, Any]) -> str:
    text = _clean_text(seg.get("text") or seg.get("content"))
    if not text:
        return ""
    return f"[{_format_timestamp(seg.get('begin') or seg.get('start') or seg.get('start_ms'))}] {text}"


def _segment_is_customer_side(seg: dict[str, Any]) -> bool:
    role = _clean_text(seg.get("role") or seg.get("speaker") or seg.get("speaker_role")).lower()
    label = _clean_text(seg.get("speaker_label"))
    return role in {"primary_customer", "customer", "client", "companion"} or "客户" in label or "顾客" in label


def _format_turn_line(turn: dict[str, Any], *, part_num: int, index: int) -> str | None:
    turn_id = _clean_text(turn.get("turn_id")) or f"t{index:03d}"
    if not turn_id.startswith(f"p{part_num}_"):
        turn_id = f"p{part_num}_{turn_id}"
    start = _format_timestamp(turn.get("start"))
    end = _format_timestamp(turn.get("end"))
    speaker = _clean_text(turn.get("speaker")) or "other"
    detail = _clean_text(turn.get("speaker_detail"))
    text = _clean_text(turn.get("corrected_text")) or _clean_text(turn.get("original_text"))
    if not text:
        return None
    label = speaker if not detail or detail == speaker else f"{speaker}/{detail}"
    return f"[{turn_id} {start}-{end}] {label}: {text}"


def _format_postprocessed_chunk(
    postprocessed: dict[str, Any],
    fallback_dialogue: str,
    *,
    part_num: int,
) -> tuple[str, bool, list[dict[str, Any]], list[str]]:
    notes: list[str] = []
    turns = _as_list(postprocessed.get("turns"))
    input_line_count = len([line for line in fallback_dialogue.splitlines() if line.strip()])
    if not turns:
        return fallback_dialogue, True, [], ["postprocess_missing_turns_used_original_chunk"]

    lines: list[str] = []
    clean_turns: list[dict[str, Any]] = []
    for index, turn in enumerate(turns, start=1):
        if not isinstance(turn, dict):
            continue
        line = _format_turn_line(turn, part_num=part_num, index=index)
        if line:
            lines.append(line)
            clean_turns.append(turn)

    formatted = "\n".join(lines)
    min_expected_chars = max(300, int(len(fallback_dialogue) * 0.40))
    min_expected_turns = max(1, int(input_line_count * 0.55))
    if len(formatted) < min_expected_chars or len(clean_turns) < min_expected_turns:
        notes.append(
            "postprocess_coverage_low_used_original_chunk: "
            f"chars={len(formatted)}/{len(fallback_dialogue)} turns={len(clean_turns)}/{input_line_count}"
        )
        return fallback_dialogue, True, clean_turns, notes
    return formatted, False, clean_turns, notes


def _postprocess_dialogue(dialogue: str, *, staff_text: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
    chunks = _split_dialogue_chunks(dialogue)
    if not chunks:
        return {"chunks": [], "turns": [], "quality_notes": ["empty_dialogue"]}, dialogue, {
            "postprocess_chunks": 0,
            "used_original_chunk_count": 0,
        }

    all_turns: list[dict[str, Any]] = []
    chunk_results: list[dict[str, Any]] = []
    dialogue_parts: list[str] = []
    quality_notes: list[str] = []
    used_original_count = 0

    for idx, chunk in enumerate(chunks, start=1):
        user_prompt = _POSTPROCESS_USER_TEMPLATE.format(
            staff_context=staff_text,
            part_num=idx,
            total_parts=len(chunks),
            dialogue=chunk,
        )
        try:
            parsed = _call_json(_POSTPROCESS_SYSTEM_PROMPT, user_prompt, max_tokens=8000)
        except Exception as exc:
            parsed = {"turns": [], "quality_notes": [f"postprocess_chunk_failed: {exc}"]}
            logger.warning("staged postprocess chunk failed part=%d/%d: %s", idx, len(chunks), exc)

        formatted, used_original, turns, notes = _format_postprocessed_chunk(parsed, chunk, part_num=idx)
        if used_original:
            used_original_count += 1
        dialogue_parts.append(formatted)
        all_turns.extend(turns)
        chunk_notes = [str(item) for item in _as_list(parsed.get("quality_notes")) if _clean_text(item)]
        chunk_notes.extend(notes)
        quality_notes.extend(f"chunk {idx}/{len(chunks)}: {note}" for note in chunk_notes)
        chunk_results.append(
            {
                "part_num": idx,
                "input_chars": len(chunk),
                "output_chars": len(formatted),
                "input_lines": len([line for line in chunk.splitlines() if line.strip()]),
                "turn_count": len(turns),
                "used_original_chunk": used_original,
                "quality_notes": chunk_notes,
            }
        )

    postprocessed = {
        "chunks": chunk_results,
        "turns": all_turns,
        "turn_count": len(all_turns),
        "role_summary": "",
        "quality_notes": quality_notes,
    }
    stats = {
        "postprocess_chunks": len(chunks),
        "used_original_chunk_count": used_original_count,
    }
    return postprocessed, "\n".join(dialogue_parts), stats


def _normalize_key(value: object) -> str:
    text = _clean_text(value)
    return re.sub(r"[\s,，;；。.!！?？、/\\|（）()]+", "", text).lower()


def _evidence_text(value: object) -> str:
    if isinstance(value, list):
        return "\n".join(_clean_text(item) for item in value if _clean_text(item))
    return _clean_text(value)


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _clean_text(payload.get(key))
        if value:
            return value
    return ""


def _all_fact_text(fact_graph: dict[str, Any]) -> str:
    pieces: list[str] = []
    for value in fact_graph.values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    pieces.append(json.dumps(item, ensure_ascii=False))
                else:
                    pieces.append(_clean_text(item))
        elif isinstance(value, dict):
            pieces.append(json.dumps(value, ensure_ascii=False))
        else:
            pieces.append(_clean_text(value))
    return "\n".join(piece for piece in pieces if piece)


def _fact_text_for_keys(fact_graph: dict[str, Any], keys: tuple[str, ...]) -> str:
    subset = {key: fact_graph.get(key) for key in keys if key in fact_graph}
    return _all_fact_text(subset)


def _text_has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _has_any_text(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


_LOW_CONFIDENCE_FRAGMENT_TERMS = (
    "嘴巴鼻如",
    "嘴巴鼻入",
    "嘴巴鼻乳",
    "鼻如",
    "全突",
    "列区",
    "面水",
    "甲水",
    "定彩海带",
    "瑞的乙",
    "抑制read",
)

_BODY_FRAGMENT_TERMS = (
    "嘴巴",
    "嘴唇",
    "唇",
    "鼻子",
    "鼻",
    "眼睛",
    "眼",
    "脸",
    "面中",
    "下巴",
    "颧",
    "太阳穴",
    "额",
)

_BUSINESS_ACTION_TERMS = (
    "改善",
    "调整",
    "解决",
    "想做",
    "希望",
    "咨询",
    "了解",
    "手术",
    "内切",
    "外切",
    "填充",
    "注射",
    "打",
    "塑形",
    "提升",
    "抬高",
    "收紧",
    "去除",
    "治疗",
)

_BUSINESS_PROBLEM_TERMS = (
    "太多",
    "太少",
    "显",
    "低",
    "塌",
    "凹",
    "凸",
    "宽",
    "大",
    "小",
    "松",
    "垮",
    "皱",
    "纹",
    "痘",
    "斑",
    "毛孔",
    "黑头",
    "出油",
    "不流畅",
    "不自然",
    "不协调",
)


def _body_fragment_count(text: str) -> int:
    return sum(1 for term in _BODY_FRAGMENT_TERMS if term and term in text)


def _looks_like_low_confidence_fragment(text: str) -> bool:
    compact = _normalize_key(text)
    if not compact:
        return False
    body_count = _body_fragment_count(compact)
    has_action = _has_any_text(compact, _BUSINESS_ACTION_TERMS)
    has_problem = _has_any_text(compact, _BUSINESS_PROBLEM_TERMS)
    if _has_any_text(compact, _LOW_CONFIDENCE_FRAGMENT_TERMS):
        if len(compact) <= 40:
            return True
        if body_count >= 1 and not has_action and not has_problem and len(compact) <= 80:
            return True
    if body_count >= 2 and not has_action and not has_problem and len(compact) <= 28:
        return True
    if body_count >= 1 and "问题" in compact and not has_action and not has_problem and len(compact) <= 20:
        return True
    if body_count >= 1 and len(compact) <= 8 and not has_action and not has_problem:
        return True
    return False


def _item_confidence(item: dict[str, Any]) -> float | None:
    for key in ("confidence", "score"):
        try:
            value = item.get(key)
            if value is None or value == "":
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _item_has_low_confidence_fragment(item: dict[str, Any]) -> bool:
    text = "\n".join(
        _clean_text(value)
        for value in (
            item.get("content"),
            item.get("demand"),
            item.get("recommendation"),
            item.get("plan"),
            item.get("evidence"),
            item.get("quote"),
            item.get("supporting_evidence"),
        )
        if _clean_text(value)
    )
    if _looks_like_low_confidence_fragment(text):
        return True
    confidence = _item_confidence(item)
    return confidence is not None and confidence < 0.45


def _has_acne_context(text: str) -> bool:
    if _has_any_text(text, ("痤疮", "痘痘", "粉刺", "炎症痘", "丘疹", "脓包", "爆痘", "长痘", "祛痘")):
        return True
    if "闭口" not in text:
        return False
    if _has_any_text(text, ("闭口时", "闭上", "闭嘴", "闭合", "闭口的状态", "完全闭")):
        return False
    return _has_any_text(text, ("皮肤", "毛孔", "粉刺", "痘", "刷酸", "水光", "黑头", "出油"))


def _has_pore_context(text: str) -> bool:
    return _has_any_text(text, ("毛孔", "黑头", "出油", "皮肤粗糙", "肤质", "肤感", "点阵", "光子", "水光"))


def _has_skin_laxity_context(text: str) -> bool:
    has_problem = _has_any_text(
        text,
        (
            "松弛",
            "下垂",
            "松垮",
            "皮松",
            "脸垮",
            "提升",
            "提拉",
            "紧致",
            "收紧",
            "抗衰",
            "苹果肌下垂",
            "轮廓线不清晰",
        ),
    )
    has_project_or_area = _has_any_text(
        text,
        (
            "热玛吉",
            "超声炮",
            "超声刀",
            "黄金微针",
            "黄金射频",
            "黑曜",
            "黑耀",
            "双波",
            "射频",
            "光电",
            "拉皮",
            "中面部",
            "苹果肌",
            "面部",
            "皮肤",
        ),
    )
    return has_problem and has_project_or_area


def _has_skin_tightening_plan_context(text: str) -> bool:
    has_energy_project = _has_any_text(
        text,
        ("热玛吉", "超声炮", "超声刀", "黄金微针", "黄金射频", "黑曜", "黑耀", "双波", "射频", "光电", "热拉提", "热提拉"),
    )
    has_tightening_goal = _has_any_text(text, ("紧致", "收紧", "淡纹", "抗衰", "松弛", "下垂", "皮松", "提拉", "提升"))
    has_area = _has_any_text(text, ("面部", "皮肤", "苹果肌", "中面部", "轮廓线", "脸"))
    return has_energy_project and (has_tightening_goal or has_area)


def _has_wrinkle_context(text: str) -> bool:
    return _has_any_text(text, ("除皱针", "皱纹", "动态纹", "鱼尾纹", "抬头纹", "川字纹", "法令纹", "核桃纹", "颈纹"))


def _has_injectable_wrinkle_context(text: str) -> bool:
    return (
        _has_any_text(text, ("肉毒", "除皱针", "思奥美", "保妥适", "衡力"))
        and _has_any_text(text, ("动态纹", "鱼尾纹", "抬头纹", "川字纹", "眉间纹", "皱纹", "除皱"))
        and _has_any_text(
        text,
        ("打", "注射", "一瓶", "除皱", "放松肌肉", "肉毒素"),
    )
    )


def _has_non_wrinkle_botox_context(text: str) -> bool:
    return _has_any_text(text, ("咬肌", "瘦脸", "轮廓线", "下巴肌肉", "颏肌", "肌肉放松", "放松下拉肌", "斜方肌"))


def _has_fill_context(text: str) -> bool:
    return _has_any_text(text, ("填充", "玻尿酸", "胶原", "瑞德喜", "艾维岚", "艾拉斯提", "贝丽菲尔", "双美", "支撑", "打", "支"))


def _has_nose_axis_injection_context(text: str) -> bool:
    return _has_any_text(
        text,
        ("鼻基底", "鼻头", "鼻翼", "鼻尖", "鼻小柱", "鼻中下段", "鼻中轴", "鼻中轴线", "三角结构"),
    ) and _has_any_text(
        text,
        ("玻尿酸", "注射", "支撑", "填充", "塑形", "再生", "芭比针", "濡白", "鲁班", "鲁板", "三角结构"),
    )


def _has_jawline_injection_support_context(text: str) -> bool:
    return _has_any_text(
        text,
        ("下颌线", "下划线", "下颌角", "下颌缘", "下颌轮廓", "下颌角拐点", "耳前", "耳后", "韧带", "外轮廓"),
    ) and _has_any_text(
        text,
        ("玻尿酸", "注射", "支撑", "填充", "塑形", "童颜", "芭比", "濡白", "提升", "收紧"),
    )


def _has_lip_current_context(text: str) -> bool:
    has_lip = _has_any_text(text, ("唇", "嘴唇", "嘴巴"))
    has_plan = _has_any_text(text, ("填充", "塑形", "调整", "打", "玻尿酸", "丰唇"))
    has_defer = _has_any_text(text, ("不建议", "先不要", "现在不", "以后", "后续", "过一个多月", "一两个月", "再看", "暂缓"))
    return has_lip and has_plan and not has_defer


def _is_concern_like_text(text: str) -> bool:
    concern_terms = (
        "担心",
        "怕",
        "顾虑",
        "风险",
        "副作用",
        "凹凸不平",
        "不自然",
        "考虑一下",
        "再考虑",
        "纠结",
        "预算",
        "价格",
        "太贵",
        "贵了",
        "有点贵",
        "没时间",
        "恢复期",
    )
    concrete_goal_terms = (
        "改善",
        "调整",
        "解决",
        "想做",
        "希望",
        "提升",
        "填充",
        "去除",
        "变",
        "显得",
    )
    has_concern = _text_has_any(text, concern_terms)
    has_goal = _text_has_any(text, concrete_goal_terms)
    if _text_has_any(text, ("点痣", "祛痣", "色素痣", "祛斑", "色斑", "斑点", "雀斑", "痘坑", "毛孔", "颈纹", "红血丝")) and _text_has_any(
        text,
        ("治疗", "了解", "询问", "咨询", "做"),
    ):
        return False
    if has_concern and _text_has_any(text, ("术后", "吸脂", "抽脂", "手术", "治疗后", "做完")) and _text_has_any(
        text,
        ("不满意", "效果不明显", "没有效果", "没抽", "像没抽", "凹凸不平", "坑坑洼洼", "形态不满意"),
    ):
        return False
    return has_concern and not has_goal


def _is_seed_like_text(text: str) -> bool:
    strong_seed_terms = (
        "后期",
        "后续可以",
        "以后可以",
        "以后再考虑",
        "下次",
        "下回",
        "下一次",
        "可以考虑",
        "再考虑",
        "不急",
        "暂缓",
        "维护",
        "顺带",
        "种草",
        "有机会",
        "先不要",
        "不建议现在",
    )
    if _text_has_any(text, strong_seed_terms):
        return True
    return False


def _is_non_current_or_referral_text(text: str) -> bool:
    compact = _normalize_key(text)
    if not compact:
        return False
    return _has_any_text(
        compact,
        (
            "不在我这里",
            "去皮肤科",
            "不是我这里",
            "不属于我们这里",
            "回来再说",
            "下次再说",
            "以后再说",
            "今天先不",
            "这次先不",
            "不是本次",
            "非本次",
        ),
    )


def _is_auxiliary_or_care_recommendation_text(text: str) -> bool:
    compact = _normalize_key(text)
    if not compact:
        return False
    return _has_any_text(
        compact,
        (
            "术前血液检查",
            "血常规",
            "凝血功能",
            "输血前四项",
            "体检",
            "检查费",
            "术后口服",
            "消炎药",
            "头孢",
            "阿莫西林",
            "生理盐水清洗",
            "清洗伤口",
            "伤口清洗",
            "拆线",
            "换药",
            "祛疤膏",
            "疤痕膏",
            "巴克",
            "抗瘢痕",
            "护理",
        ),
    )


def _is_alternative_or_not_recommended_text(text: str) -> bool:
    compact = _normalize_key(text)
    if not compact:
        return False
    if _has_any_text(
        compact,
        (
            "备选方案对比",
            "作为备选",
            "只作对比",
            "只是对比",
            "价格对比",
            "客户询价",
            "仅供对比",
            "不是优先",
            "非优先",
        ),
    ):
        return True
    return bool(
        re.search(r"(?:这个|该|此|本)?(?:方案|项目|方式).{0,8}(?:不适合|不建议|不推荐)", compact)
        or re.search(r"(?:不适合|不建议|不推荐).{0,8}(?:做|选择|采用|作为方案)", compact)
        or re.search(r"(?:先不要|暂缓|以后再).{0,8}(?:做|选择|考虑)", compact)
    )


def _is_staff_only_demand(item: dict[str, Any]) -> bool:
    source = _clean_text(item.get("source")).lower()
    if source in {"doctor_diagnosis", "doctor_observation", "staff_suggested", "staff_observed"}:
        return True
    return False


@lru_cache(maxsize=1)
def _indication_catalog() -> list[dict[str, str]]:
    reference_data = load_analysis_reference_data()
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in reference_data.indication_catalog_by_code_triplet.values():
        key = (item.department_code, item.indication_code, item.body_part_code)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "department_code": item.department_code,
                "department_name": item.department_name,
                "indication_code": item.indication_code,
                "indication_name": item.indication_name,
                "body_part_code": item.body_part_code,
                "body_part_name": item.body_part_name,
                "selection_note": getattr(item, "indication_note", ""),
            }
        )
    return rows


def _catalog_match_by_code(item: dict[str, Any]) -> dict[str, str] | None:
    standardized = _clean_text(item.get("standardized_indication")) or _clean_text(item.get("standardized"))
    row = _parse_standardized_indication(standardized)
    if row is not None:
        return row
    dept = _clean_text(item.get("department_code"))
    indication = _clean_text(item.get("indication_code"))
    body = _clean_text(item.get("body_part_code"))
    if not indication:
        return None
    for row in _indication_catalog():
        if row["indication_code"] != indication:
            continue
        if dept and row["department_code"] != dept:
            continue
        if body and row["body_part_code"] != body:
            continue
        return dict(row)
    return None


def _parse_standardized_indication(value: str) -> dict[str, str] | None:
    text = _clean_text(value)
    if not text or "|" not in text:
        return None
    parts = [part.strip() for part in text.split("|")]
    if len(parts) != 6:
        return None
    return {
        "department_code": parts[0],
        "department_name": parts[1],
        "indication_code": parts[2],
        "indication_name": parts[3],
        "body_part_code": parts[4],
        "body_part_name": parts[5],
    }


def _catalog_match_by_name(name: str, body_part: str | None = None) -> dict[str, str] | None:
    normalized_name = _normalize_key(name)
    normalized_body = _normalize_key(body_part)
    if not normalized_name:
        return None

    exact_matches = [
        row
        for row in _indication_catalog()
        if _normalize_key(row["indication_name"]) == normalized_name
    ]
    if normalized_body:
        for row in exact_matches:
            if normalized_body in _normalize_key(row["body_part_name"]) or _normalize_key(row["body_part_name"]) in normalized_body:
                return dict(row)
    if exact_matches:
        return dict(exact_matches[0])
    return None


def _map_common_indication_from_text(text: str) -> list[dict[str, str]]:
    mapped: list[dict[str, str]] = []

    def add(name: str, body: str | None = None) -> None:
        row = _catalog_match_by_name(name, body)
        if row and row not in mapped:
            mapped.append(row)

    if _has_pore_context(text):
        add("毛孔", "面部")
    if _has_acne_context(text):
        add("痤疮", "面部")
    if _has_skin_laxity_context(text):
        add("松弛下垂", "面部")
    if _has_skin_tightening_plan_context(text):
        add("紧致淡纹", "面部")
    if "眼袋" in text:
        add("眼袋", "眼部")
    if _has_wrinkle_context(text):
        add("面部除皱", "面部")
    if _has_nose_axis_injection_context(text):
        add("塑美", "鼻中轴线")
    if _has_jawline_injection_support_context(text):
        add("塑美", "下颌轮廓线")
    if any(term in text for term in ("苹果肌", "面部填充", "玻尿酸填充", "胶原填充", "瑞德喜")) or (
        "鼻基底" in text and not _has_nose_axis_injection_context(text)
    ):
        add("面部填充", "面部")
    if any(term in text for term in ("泪沟", "下巴", "颏部", "太阳穴填充", "胶原蛋白", "双美")) and any(
        term in text for term in ("填充", "玻尿酸", "胶原", "瑞德喜", "支")
    ):
        add("面部填充", "面部")
    if any(term in text for term in ("眶外C线", "眶外", "眉尾", "颧突", "内轮廓线")):
        add("塑美", "眶外C线")
    if any(term in text for term in ("颞区", "太阳穴", "额颞", "外轮廓线", "颧弓", "额角")):
        add("塑美", "颞区")
    if any(term in text for term in ("外颊", "颧弓", "颧骨外侧", "外轮廓线")):
        add("塑美", "外颊")
    if any(term in text for term in ("内颊", "面中", "中面部", "苹果肌", "内轮廓线", "颧突")):
        add("塑美", "内颊")
    if any(term in text for term in ("下颌轮廓", "下颌缘", "下颌角", "下颌线", "下划线", "下颌角拐点", "颈阔肌", "轮廓线")):
        add("塑美", "下颌轮廓线")
    if any(term in text for term in ("额区", "上庭窄", "上庭偏窄")):
        add("塑美", "额区")
    if any(term in text for term in ("点痣", "祛痣", "色素痣")) or ("痣" in text and any(term in text for term in ("点", "去除", "祛", "包干", "复发"))):
        if "眼" in text:
            add("祛痣/祛疣", "眼部")
        elif "颈" in text:
            add("祛痣/祛疣", "颈部")
        elif "身体" in text:
            add("祛痣/祛疣", "身体")
        else:
            add("祛痣/祛疣", "面部")
    if any(term in text for term in ("鼻基底", "鼻头", "鼻翼", "鼻尖", "鼻小柱", "鼻中下段", "鼻中段", "鼻下段", "鼻中轴", "鼻中轴线", "三角结构")) and any(
        term in text for term in ("玻尿酸", "定彩", "注射", "支撑", "填充", "塑形", "抬高", "拉高", "纵深")
    ):
        add("塑美", "鼻中轴线")
    if any(term in text for term in ("耳朵", "耳垂", "耳部", "耳基底")) and any(
        term in text for term in ("玻尿酸", "注射", "支撑", "填充", "塑形", "拉长", "衬托", "偏小")
    ):
        add("塑美", "耳部")
    if any(term in text for term in ("副乳", "腋前", "胸外侧", "穿内衣勒出来", "穿内衣夹出来")):
        add("副乳整形", "胸部")
    if "富贵包" in text and any(term in text for term in ("吸脂", "抽脂", "超脂", "减脂", "局部减脂", "做掉", "去掉")):
        add("身体吸脂", "身体")
    return mapped


def _is_skin_context_on_nose(context: str) -> bool:
    has_nose_area = any(term in context for term in ("鼻头", "鼻翼", "鼻尖", "鼻子"))
    has_skin_problem = any(term in context for term in ("毛孔", "痘", "闭口", "粉刺", "出油", "黑头", "水光", "光子", "点阵", "黄金微针", "刷酸"))
    return has_nose_area and has_skin_problem


def _has_explicit_nose_surgery_context(context: str) -> bool:
    return any(term in context for term in ("鼻综合", "隆鼻", "假体", "膨体", "耳软骨", "肋软骨", "鼻翼缩小", "鼻尖塑形", "鼻修复", "鼻手术"))


def _should_drop_indication(row: dict[str, str], context: str) -> bool:
    if row.get("indication_name") == "鼻综合" and _is_skin_context_on_nose(context) and not _has_explicit_nose_surgery_context(context):
        return True
    indication_name = row.get("indication_name", "")
    body_part_name = row.get("body_part_name", "")
    if indication_name == "痤疮" and not _has_acne_context(context):
        return True
    if indication_name == "毛孔" and not _has_pore_context(context):
        return True
    if indication_name == "松弛下垂" and not _has_skin_laxity_context(context):
        return True
    if indication_name == "紧致淡纹" and not _has_skin_tightening_plan_context(context):
        return True
    if indication_name == "面部除皱" and not _has_wrinkle_context(context):
        return True
    if indication_name == "面部除皱" and _has_non_wrinkle_botox_context(context) and not _has_injectable_wrinkle_context(context):
        return True
    if indication_name == "面部除皱" and _has_non_wrinkle_botox_context(context) and not _has_wrinkle_context(context):
        return True
    if "唇" in body_part_name and not _has_lip_current_context(context):
        return True
    return False


def _indication_supported_by_context(row: dict[str, str], context: str) -> bool:
    if _should_drop_indication(row, context):
        return False
    name = row.get("indication_name", "")
    body = row.get("body_part_name", "")
    body_base = _catalog_body_base(row)

    if name == "痤疮":
        return _has_acne_context(context)
    if name == "毛孔":
        return _has_pore_context(context)
    if name == "松弛下垂":
        return _has_skin_laxity_context(context)
    if name == "紧致淡纹":
        return _has_skin_tightening_plan_context(context)
    if name == "面部除皱":
        return _has_wrinkle_context(context) and not (
            _has_non_wrinkle_botox_context(context) and not _has_injectable_wrinkle_context(context)
        )
    if name == "祛痣/祛疣":
        return any(term in context for term in ("点痣", "祛痣", "色素痣", "祛疣")) or (
            "痣" in context and any(term in context for term in ("点", "去除", "祛", "包干", "复发"))
        )
    if name == "面部填充":
        explicit_face_fill = _has_any_text(
            context,
            (
                "面部填充",
                "脂肪填充",
                "自体脂肪",
                "太阳穴填充",
                "额颞填充",
                "苹果肌填充",
                "泪沟填充",
                "鼻基底填充",
                "口基底填充",
                "面中填充",
                "外轮廓填充",
                "侧面凹陷",
            ),
        )
        if (not explicit_face_fill) and (
            _has_nose_axis_injection_context(context) or _has_jawline_injection_support_context(context)
        ):
            return False
        has_body = _has_any_text(
            context,
            (
                "鼻基底",
                "苹果肌",
                "泪沟",
                "下巴",
                "颏部",
                "颞区",
                "太阳穴",
                "额颞",
                "眶外C线",
                "内轮廓线",
                "外轮廓线",
                "面部",
                "中面部",
                "轮廓",
            ),
        )
        return has_body and _has_fill_context(context)
    if name == "塑美":
        if "唇" in body:
            return _has_lip_current_context(context)
        if body_base:
            normalized_body = _normalize_key(body_base)
            normalized_context = _normalize_key(context)
            if normalized_body and normalized_body in normalized_context:
                return True
            synonyms = {
                "颞区": ("颞区", "太阳穴", "额颞"),
                "眶外C线": ("眶外C线", "眶外", "眉尾", "颧突", "内轮廓线"),
                "内颊": ("内颊", "面中", "中面部", "苹果肌", "内轮廓线", "颧突"),
                "外颊": ("外颊", "颧弓", "颧骨外侧", "外轮廓线"),
                "下颌轮廓线": ("下颌轮廓", "下颌缘", "下颌角", "下颌线", "下划线", "下颌角拐点", "耳前", "耳后", "韧带", "外轮廓", "颈阔肌", "轮廓线"),
                "眉弓线": ("眉弓", "眉弓线", "眉尾"),
                "鼻额衔接线": ("鼻额", "山根", "鼻额衔接"),
                "鼻中轴线": ("鼻基底", "鼻头", "鼻翼", "鼻尖", "鼻中轴", "鼻中轴线", "鼻小柱", "鼻中下段", "鼻中段", "鼻下段", "鼻梁", "鼻背", "山根", "三角结构"),
                "耳部": ("耳部", "耳朵", "耳垂", "耳基底"),
            }
            if _has_any_text(context, synonyms.get(body_base, ())):
                return True
        return False
    if body_base and body_base not in {"面部", "眼部", "身体", "私密", "颅区", "耳部"}:
        return _normalize_key(body_base) in _normalize_key(context)
    return _normalize_key(name) in _normalize_key(context)


def _catalog_body_base(row: dict[str, str]) -> str:
    body = _clean_text(row.get("body_part_name"))
    return re.split(r"[（(]", body, maxsplit=1)[0].strip()


def _append_candidate_indication(
    rows: list[dict[str, str]],
    row: dict[str, str],
    *,
    reason: str,
    context: str,
) -> None:
    if _should_drop_indication(row, context):
        return
    key = (row.get("department_code", ""), row.get("indication_code", ""), row.get("body_part_code", ""))
    for existing in rows:
        existing_key = (
            existing.get("department_code", ""),
            existing.get("indication_code", ""),
            existing.get("body_part_code", ""),
        )
        if existing_key == key:
            return
    copy = dict(row)
    copy["recall_reason"] = reason
    rows.append(copy)


def _candidate_indications_from_text(text: str, *, max_items: int = 40) -> list[dict[str, str]]:
    """Recall candidates from the local dictionary without deciding final indications."""
    rows: list[dict[str, str]] = []
    generic_bodies = {"面部", "眼部", "身体", "私密", "颅区", "耳部"}
    normalized_text = _normalize_key(text)

    for row in _indication_catalog():
        name = _clean_text(row.get("indication_name"))
        body_base = _catalog_body_base(row)
        normalized_name = _normalize_key(name)
        normalized_body = _normalize_key(body_base)
        if normalized_body and body_base not in generic_bodies and normalized_body in normalized_text:
            _append_candidate_indication(rows, row, reason=f"body_part:{body_base}", context=text)
            continue
        if normalized_name and normalized_name in normalized_text:
            if body_base in generic_bodies or not normalized_body or normalized_body in normalized_text:
                _append_candidate_indication(rows, row, reason=f"indication_name:{name}", context=text)

    for row in _map_common_indication_from_text(text):
        _append_candidate_indication(rows, row, reason="common_term_recall", context=text)

    return rows[:max_items]


def _format_candidate_indications(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "standardized_indication": "|".join(
                [
                    row["department_code"],
                    row["department_name"],
                    row["indication_code"],
                    row["indication_name"],
                    row["body_part_code"],
                    row["body_part_name"],
                ]
            ),
            "department_code": row["department_code"],
            "department_name": row["department_name"],
            "indication_code": row["indication_code"],
            "indication_name": row["indication_name"],
            "body_part_code": row["body_part_code"],
            "body_part_name": row["body_part_name"],
            "selection_note": row.get("selection_note", ""),
            "recall_reason": row.get("recall_reason", ""),
        }
        for row in rows
    ]


def _resolve_indications(fact_graph: dict[str, Any]) -> list[dict[str, str]]:
    context_all = _all_fact_text(fact_graph)
    current_context = _fact_text_for_keys(
        fact_graph,
        ("demands", "doctor_diagnoses", "indication_candidates", "recommendations", "seed_recommendations"),
    )
    adjudicated = bool(fact_graph.get("_indication_adjudicated"))
    selected: list[dict[str, str]] = []
    rejected_name_body: set[tuple[str, str]] = set()
    rejected_standardized: set[str] = set()
    adjudication = _as_dict(fact_graph.get("_indication_adjudication"))
    for rejected in _as_list(adjudication.get("rejected_indications")):
        if not isinstance(rejected, dict):
            continue
        standardized = _clean_text(rejected.get("standardized_indication"))
        if standardized:
            rejected_standardized.add(standardized)
            parts = standardized.split("|")
            if len(parts) >= 6:
                rejected_name_body.add((_clean_text(parts[3]), _clean_text(parts[5])))
        name = _first_text(rejected, "indication_name", "name")
        body = _first_text(rejected, "body_part_name", "body_part")
        if name:
            rejected_name_body.add((name, body))

    def is_rejected(row: dict[str, str]) -> bool:
        standardized = "|".join(
            _clean_text(row.get(key))
            for key in (
                "department_code",
                "department_name",
                "indication_code",
                "indication_name",
                "body_part_code",
                "body_part_name",
            )
        )
        name_body = (_clean_text(row.get("indication_name")), _clean_text(row.get("body_part_name")))
        return standardized in rejected_standardized or name_body in rejected_name_body

    def append(row: dict[str, str], evidence: str = "", support_context: str = "", force_include: bool = False) -> None:
        if not force_include and is_rejected(row):
            return
        support_context = support_context or current_context or context_all
        if not _indication_supported_by_context(row, support_context):
            return
        key = (row.get("department_code", ""), row.get("indication_code", ""), row.get("body_part_code", ""))
        for existing in selected:
            existing_key = (
                existing.get("department_code", ""),
                existing.get("indication_code", ""),
                existing.get("body_part_code", ""),
            )
            if existing_key == key:
                if evidence and not existing.get("evidence"):
                    existing["evidence"] = evidence
                return
        copy = dict(row)
        copy["evidence"] = evidence
        selected.append(copy)

    for item in _as_list(fact_graph.get("indication_candidates")):
        if not isinstance(item, dict):
            continue
        if _item_has_low_confidence_fragment(item):
            continue
        item_context = json.dumps(item, ensure_ascii=False) + "\n" + context_all
        item_evidence = _evidence_text(item.get("evidence"))
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        if adjudicated and confidence and confidence < 0.55:
            continue
        row = _catalog_match_by_code(item)
        if row is None:
            name = _first_text(item, "indication_name", "name", "content", "text")
            body = _first_text(item, "body_part_name", "body_part")
            row = _catalog_match_by_name(name, body)
        if row is None:
            for fallback in _map_common_indication_from_text(item_context):
                if not _should_drop_indication(fallback, item_context):
                    append(fallback, item_evidence, item_context)
            continue
        if _should_drop_indication(row, item_context):
            continue
        append(row, item_evidence, item_context, bool(item.get("force_include")))

    fallback_rows = _map_common_indication_from_text(current_context)
    if not adjudicated:
        for fallback in fallback_rows:
            if not _should_drop_indication(fallback, current_context):
                append(fallback, support_context=current_context)
    elif _has_skin_laxity_context(current_context) or _has_skin_tightening_plan_context(current_context):
        selected_names = {_clean_text(item.get("indication_name")) for item in selected}
        skin_surface_only = selected and selected_names.issubset({"毛孔", "痤疮", "暗黄", "色斑", "疤痕"})
        if not selected or skin_surface_only:
            for fallback in fallback_rows:
                if _clean_text(fallback.get("indication_name")) not in {"松弛下垂", "紧致淡纹"}:
                    continue
                if not _should_drop_indication(fallback, current_context):
                    append(fallback, support_context=current_context)
    if _has_injectable_wrinkle_context(current_context):
        selected = [
            item
            for item in selected
            if not (
                _clean_text(item.get("indication_name")) == "纹路"
                and _clean_text(item.get("department_code")) == "Y3"
            )
        ]
        injectable_row = _catalog_match_by_name("面部除皱", "面部")
        if injectable_row:
            append(injectable_row, support_context=current_context)
    return selected


def _dedupe_demands(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for item in items:
        content = _first_text(item, "content", "demand_content", "demand", "text")
        if not content:
            continue
        key = _demand_semantic_key(content, item)
        if not key:
            continue
        replaced = False
        for existing_key, existing in list(by_key.items()):
            existing_content = _first_text(existing, "content", "demand_content", "demand", "text")
            if key in existing_key or existing_key in key:
                if _demand_item_score(item) > _demand_item_score(existing):
                    by_key[existing_key] = item
                replaced = True
                break
        if not replaced:
            by_key[key] = item
    return list(by_key.values())


def _demand_semantic_key(text: str, item: dict[str, Any] | None = None) -> str:
    compact = _normalize_key(text)
    if not compact:
        return ""
    body = _normalize_key(_first_text(item or {}, "body_part", "body_part_name", "area"))
    groups = (
        (("眼袋",), "眼袋"),
        (("双眼皮", "小平扇", "平扇", "开扇", "眼尾"), "双眼皮"),
        (("泪沟", "眶下凹陷"), "泪沟凹陷"),
        (("隆胸", "丰胸", "胸部假体"), "隆胸"),
        (("卧蚕",), "卧蚕"),
        (("唇", "嘴唇", "嘴巴形状", "唇形"), "唇形"),
        (("面颊凹陷", "颊区凹陷", "脸颊凹陷", "面颊", "颊区", "夹区"), "面颊凹陷填充"),
        (("下巴", "颏部", "下庭", "下巴后缩", "下巴注射", "下巴塑形"), "下巴塑形"),
        (("清纯甜美", "幼态", "甜美风", "清纯", "面部整体风格"), "面部风格"),
        (("鼻小柱",), "鼻小柱"),
        (("痘坑", "凹陷性痘坑", "痤疮瘢痕"), "痘坑"),
        (("毛孔",), "毛孔"),
        (("痘印", "痘痘", "痤疮", "闭口", "粉刺", "皮肤炎症", "泛红"), "肤质问题"),
        (("美白", "暗沉", "暗黄", "提亮"), "肤色提亮"),
        (("脱毛",), "脱毛"),
        (("瘦肩", "斜方肌"), "瘦肩"),
        (("颈纹",), "颈纹"),
        (("色斑", "斑"), "色斑"),
        (("富贵包",), "富贵包"),
        (("副乳",), "副乳"),
        (("点痣", "祛痣", "色素痣"), "点痣祛痣"),
        (("祛斑", "色斑", "斑点", "雀斑"), "色斑"),
        (("胶原流失", "衰老", "抗衰", "紧致"), "面部抗衰紧致"),
        (("卡粉", "上妆卡", "妆容不服帖", "妆感不服帖"), "卡粉肤质"),
        (("下颌线", "收紧", "紧致", "松弛", "提升", "超声炮"), "面部紧致提升"),
        (("鱼尾纹", "眼部纹", "眼纹", "除皱"), "眼部除皱"),
    )
    for terms, group in groups:
        if body and any(term in body for term in terms):
            return group
    for terms, group in groups:
        if any(term in compact for term in terms):
            return group
    return compact


def _demand_item_score(item: dict[str, Any]) -> int:
    content = _first_text(item, "content", "demand_content", "demand", "text")
    compact = _normalize_key(content)
    score = 0
    if _has_any_text(compact, ("希望", "想", "改善", "调整", "解决", "去除", "收紧", "提升", "脱毛", "隆胸", "丰胸", "填充", "注射")):
        score += 8
    if compact.startswith(("改善", "希望改善", "想改善", "想做")):
        score += 3
    if _first_text(item, "body_part", "body_part_name", "area"):
        score += 4
    if _has_any_text(compact, ("眼袋", "泪沟", "隆胸", "丰胸", "卧蚕", "唇", "鼻小柱", "脱毛", "瘦肩", "皱", "纹", "松弛", "下垂", "富贵包", "副乳", "下巴", "幼态")):
        score += 3
    if _has_any_text(compact, ("小平扇", "平扇", "开扇", "眼尾", "延长", "偏宽", "自然")):
        score += 4
    if compact in {"想做双眼皮", "做双眼皮", "双眼皮手术", "通过手术方式做双眼皮"}:
        score -= 3
    if _has_any_text(compact, ("咨询", "关注", "确认", "询问", "要求确认")):
        score -= 5
    if _is_process_or_meta_demand(content):
        score -= 8
    length = len(compact)
    if 8 <= length <= 40:
        score += 3
    elif length > 80:
        score -= 4
    elif length > 55:
        score -= 2
    return score


def _is_process_or_meta_demand(text: str) -> bool:
    compact = _normalize_key(text)
    if not compact:
        return True
    if _has_any_text(compact, ("点痣", "祛痣", "色素痣", "祛斑", "色斑", "斑点", "雀斑", "痘坑", "毛孔", "颈纹", "红血丝")) and _has_any_text(
        compact,
        ("改善", "治疗", "去除", "了解", "询问", "咨询", "做"),
    ):
        return False
    if _has_any_text(
        compact,
        (
            "仪器版本",
            "确认仪器",
            "代际",
            "几代",
            "发数",
            "可调整分配",
            "白钻",
            "黑钻",
            "验证",
            "医生是谁",
            "是否包含",
            "是否做",
            "恢复时间",
            "出院时间",
            "开车",
            "切口位置",
            "安排最早时间",
            "推迟到",
            "咨询光子项目",
            "咨询玻尿酸注射项目",
        ),
    ):
        return True
    if compact.startswith("咨询") and _has_any_text(compact, ("光子", "玻尿酸")) and not _has_any_text(
        compact,
        ("改善", "解决", "希望", "想", "纹", "斑", "痘", "凹", "凸", "松", "垮", "填充", "塑形"),
    ):
        return True
    preference_only = (
        "一次性到位",
        "不想频繁",
        "频繁注射",
        "效果要明显",
        "不要像没做一样",
        "自然效果",
        "不要过大",
    )
    has_problem_or_project = _has_any_text(
        compact,
        (
            "眼袋",
            "泪沟",
            "隆胸",
            "丰胸",
            "卧蚕",
            "唇",
            "鼻小柱",
            "毛孔",
            "痘",
            "斑",
            "水光",
            "光子",
            "脱毛",
            "瘦肩",
            "瘦脸",
            "皱",
            "纹",
            "松弛",
            "下垂",
            "收紧",
            "提升",
            "富贵包",
            "副乳",
        ),
    )
    if _has_any_text(compact, preference_only) and not has_problem_or_project:
        return True
    return False


def _demand_priority_map(demands: list[dict[str, Any]]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, item in enumerate(demands, start=1):
        item_id = _clean_text(item.get("id")) or _clean_text(item.get("demand_id")) or f"D{index}"
        mapping[item_id] = index
    return mapping


def _linked_priorities(item: dict[str, Any], mapping: dict[str, int]) -> list[int]:
    ids = []
    for key in ("related_demand_ids", "linked_demand_ids", "demand_ids"):
        ids.extend(str(value) for value in _as_list(item.get(key)) if _clean_text(value))
    for key in ("for_demand_ids", "target_demand_ids", "supported_by_demand_ids"):
        ids.extend(str(value) for value in _as_list(item.get(key)) if _clean_text(value))
    priorities = []
    for item_id in ids:
        priority = mapping.get(item_id)
        if priority and priority not in priorities:
            priorities.append(priority)
    return priorities


def _build_demands(fact_graph: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    raw_demands: list[dict[str, Any]] = []
    for item in _as_list(fact_graph.get("demands")):
        if not isinstance(item, dict):
            continue
        content = _first_text(item, "content", "demand_content", "demand", "text")
        if not content:
            continue
        if _item_has_low_confidence_fragment(item) or _looks_like_low_confidence_fragment(content):
            continue
        if _is_staff_only_demand(item):
            continue
        if _is_concern_like_text(content):
            continue
        if _is_process_or_meta_demand(content):
            continue
        raw_demands.append(item)
    demands = _dedupe_demands(raw_demands)
    priority_map = _demand_priority_map(demands)
    result_items: list[dict[str, Any]] = []
    for index, item in enumerate(demands, start=1):
        evidence = _evidence_text(item.get("evidence")) or _first_text(item, "quote", "supporting_quote", "source_quote")
        result_items.append(
            {
                "priority": index,
                "demand": _first_text(item, "content", "demand_content", "demand", "text"),
                "body_part": _first_text(item, "body_part", "body_part_name") or None,
                "evidence": evidence,
            }
        )
    summary = "；".join(item["demand"] for item in result_items if item.get("demand"))
    return {"inference_note": None, "summary": summary, "items": result_items}, demands, priority_map


def _build_standardized_indications(fact_graph: dict[str, Any]) -> dict[str, Any]:
    rows = _resolve_indications(fact_graph)
    items = [
        {
            "department_code": row["department_code"],
            "department_name": row["department_name"],
            "indication_code": row["indication_code"],
            "indication_name": row["indication_name"],
            "body_part_code": row["body_part_code"],
            "body_part_name": row["body_part_name"],
            "evidence": row.get("evidence", ""),
        }
        for row in rows
    ]
    return {
        "inference_note": None,
        "summary": "；".join(f"{item['indication_name']}（{item['body_part_name']}）" for item in items),
        "items": items,
    }


def _result_has_term(result: dict[str, Any], term: str) -> bool:
    for section in ("customer_primary_demands", "standardized_indications", "customer_demands"):
        if term in json.dumps(result.get(section, {}), ensure_ascii=False):
            return True
    return False


def _section_has_term(result: dict[str, Any], section: str, term: str) -> bool:
    return term in json.dumps(result.get(section, {}), ensure_ascii=False)


def _append_primary_demand(
    result: dict[str, Any],
    *,
    demand: str,
    body_part: str,
    evidence: str,
) -> None:
    primary = _as_dict(result.setdefault("customer_primary_demands", {}))
    items = [dict(item) for item in _as_list(primary.get("items")) if isinstance(item, dict)]
    demand_key = _normalize_key(demand)
    if any(demand_key and demand_key in _normalize_key(_clean_text(item.get("demand"))) for item in items):
        return
    items.append(
        {
            "priority": len(items) + 1,
            "demand": demand,
            "body_part": body_part,
            "evidence": evidence,
        }
    )
    for index, item in enumerate(items, start=1):
        item["priority"] = index
    primary["items"] = items
    primary["summary"] = "；".join(_clean_text(item.get("demand")) for item in items if _clean_text(item.get("demand")))
    primary.setdefault("inference_note", None)
    result["customer_primary_demands"] = primary

    demands = _as_dict(result.setdefault("customer_demands", {}))
    focus_areas = [dict(item) for item in _as_list(demands.get("focus_areas")) if isinstance(item, dict)]
    if not any(_normalize_key(body_part) in _normalize_key(json.dumps(item, ensure_ascii=False)) for item in focus_areas):
        focus_areas.append(
            {
                "area": body_part,
                "surface_need": demand,
                "deep_need": None,
                "discovery_process": evidence,
            }
        )
    demands["focus_areas"] = focus_areas
    demands.setdefault("inference_note", None)
    demands.setdefault("expectation", {"entry_state": None, "exit_state": None, "turning_points": [], "specific_standards": None})
    demands.setdefault("product_preference", {"preferred_products": [], "information_sources": [], "comparison_factors": [], "consultant_influence": None})
    result["customer_demands"] = demands


def _append_standardized_indication(
    result: dict[str, Any],
    *,
    name: str,
    body: str,
    evidence: str,
) -> None:
    row = _catalog_match_by_name(name, body)
    if not row:
        return
    standardized = _as_dict(result.setdefault("standardized_indications", {}))
    items = [dict(item) for item in _as_list(standardized.get("items")) if isinstance(item, dict)]
    key = (row["department_code"], row["indication_code"], row["body_part_code"])
    for item in items:
        existing = (
            _clean_text(item.get("department_code")),
            _clean_text(item.get("indication_code")),
            _clean_text(item.get("body_part_code")),
        )
        if existing == key:
            return
    items.append(
        {
            "department_code": row["department_code"],
            "department_name": row["department_name"],
            "indication_code": row["indication_code"],
            "indication_name": row["indication_name"],
            "body_part_code": row["body_part_code"],
            "body_part_name": row["body_part_name"],
            "evidence": evidence,
        }
    )
    standardized["items"] = items
    standardized["summary"] = "；".join(f"{item['indication_name']}（{item['body_part_name']}）" for item in items)
    standardized.setdefault("inference_note", None)
    result["standardized_indications"] = standardized


def _raw_full_text(raw: dict[str, Any]) -> str:
    segments = _raw_transcribe_segments(raw)
    if not segments:
        return ""
    return "\n".join(_clean_text(seg.get("text") or seg.get("content")) for seg in segments if _clean_text(seg.get("text") or seg.get("content")))


def _is_non_current_demand_item(item: dict[str, Any], raw_text: str) -> bool:
    item_text = json.dumps(item, ensure_ascii=False)
    if _is_non_current_or_referral_text(item_text):
        return True
    if "美白" in item_text and re.search(r"美白.{0,40}(?:皮肤科|不在我这里|回来再说|再说)", raw_text):
        return True
    return False


def _remove_non_current_demands_from_result(result: dict[str, Any], raw: dict[str, Any]) -> None:
    raw_text = _raw_full_text(raw)
    if not raw_text:
        return
    primary = _as_dict(result.get("customer_primary_demands"))
    items = [dict(item) for item in _as_list(primary.get("items")) if isinstance(item, dict)]
    removed_demands: set[str] = set()
    kept: list[dict[str, Any]] = []
    for item in items:
        if _is_non_current_demand_item(item, raw_text):
            demand = _clean_text(item.get("demand"))
            if demand:
                removed_demands.add(demand)
            continue
        kept.append(item)
    if len(kept) == len(items):
        return
    for index, item in enumerate(kept, start=1):
        item["priority"] = index
    primary["items"] = kept
    primary["summary"] = "；".join(_clean_text(item.get("demand")) for item in kept if _clean_text(item.get("demand")))
    result["customer_primary_demands"] = primary

    demands = _as_dict(result.get("customer_demands"))
    focus_areas = []
    for item in _as_list(demands.get("focus_areas")):
        if not isinstance(item, dict):
            continue
        if _clean_text(item.get("surface_need")) in removed_demands:
            continue
        focus_areas.append(item)
    demands["focus_areas"] = focus_areas
    result["customer_demands"] = demands


def _augment_body_contouring_demands_from_raw(result: dict[str, Any], raw: dict[str, Any]) -> None:
    segments = _raw_transcribe_segments(raw)
    if not segments:
        return
    full_text = "\n".join(_clean_text(seg.get("text") or seg.get("content")) for seg in segments)

    if "副乳" in full_text:
        breast_evidence_parts = [
            _segment_evidence(seg)
            for seg in segments
            if "副乳" in _clean_text(seg.get("text") or seg.get("content"))
            or any(term in _clean_text(seg.get("text") or seg.get("content")) for term in ("夹出来", "勒出来"))
        ]
        breast_evidence = "\n".join(part for part in breast_evidence_parts[:4] if part)
    else:
        breast_evidence = ""

    if not _section_has_term(result, "customer_primary_demands", "副乳") and breast_evidence:
        _append_primary_demand(
            result,
            demand="存在副乳，穿内衣时会被勒出或夹出，希望评估是否需要处理",
            body_part="副乳/胸部外侧",
            evidence=breast_evidence,
        )
    if not _section_has_term(result, "standardized_indications", "副乳整形") and breast_evidence:
        _append_standardized_indication(result, name="副乳整形", body="胸部", evidence=breast_evidence)

    if not _section_has_term(result, "customer_primary_demands", "富贵包"):
        for seg in segments:
            text = _clean_text(seg.get("text") or seg.get("content"))
            if "富贵包" not in text:
                continue
            if not (_segment_is_customer_side(seg) or any(term in text for term in ("看一下", "看下", "评估", "想给你看", "帮我看"))):
                continue
            if not any(term in text for term in ("看一下", "看下", "评估", "想给你看", "帮我看")):
                continue
            _append_primary_demand(
                result,
                demand="希望查看并评估富贵包情况",
                body_part="富贵包/颈后上背",
                evidence=_segment_evidence(seg),
            )
            break


_SKIN_EXPLICIT_DEMAND_TERMS = (
    "毛孔",
    "痘印",
    "痘坑",
    "暗沉",
    "暗黄",
    "美白",
    "提亮",
    "水光",
    "光电",
    "热玛吉",
    "超声炮",
    "黄金微针",
    "黑曜双波",
    "射频",
)

_SKIN_DEMAND_INTENT_TERMS = (
    "想",
    "希望",
    "改善",
    "做",
    "先做",
    "考虑",
    "了解",
    "问",
    "咨询",
    "收紧",
    "提亮",
)


def _augment_explicit_skin_followup_demands_from_raw(result: dict[str, Any], raw: dict[str, Any]) -> None:
    """Keep explicit skin/follow-up demands even when they are not SAP indications.

    The indication stage intentionally prioritizes the current treatment category,
    but SAP consultation remarks still need customer-raised secondary or deferred
    demands such as pores/acne marks/dullness/whitening/light-device care.
    """
    segments = _raw_transcribe_segments(raw)
    if not segments:
        return

    customer_segments = [
        seg
        for seg in segments
        if _segment_is_customer_side(seg)
        and _has_any_text(_clean_text(seg.get("text") or seg.get("content")), _SKIN_EXPLICIT_DEMAND_TERMS)
    ]
    if not customer_segments:
        return

    has_customer_skin_context = any(
        _has_any_text(
            _clean_text(seg.get("text") or seg.get("content")),
            ("皮肤", "光电", "水光", "热玛吉", "射频", "紧致", "暗沉", "暗黄", "美白", "毛孔", "痘印", "痘坑"),
        )
        for seg in customer_segments
    )
    evidence_segments = list(customer_segments)
    if has_customer_skin_context:
        for seg in segments:
            if _segment_is_customer_side(seg):
                continue
            text = _clean_text(seg.get("text") or seg.get("content"))
            if not _has_any_text(text, ("毛孔", "痘印", "痘坑", "暗沉", "暗黄", "美白", "水光", "光电")):
                continue
            if not _has_any_text(text, _SKIN_DEMAND_INTENT_TERMS + ("收紧", "解决", "针对")):
                continue
            evidence_segments.append(seg)

    def evidence_for(*terms: str) -> str:
        parts: list[str] = []
        for seg in evidence_segments:
            text = _clean_text(seg.get("text") or seg.get("content"))
            if terms and not any(term in text for term in terms):
                continue
            if not _segment_is_customer_side(seg) and not has_customer_skin_context:
                continue
            if not _has_any_text(text, _SKIN_DEMAND_INTENT_TERMS) and not _has_any_text(text, ("有点", "不清晰", "不太好", "收紧", "针对")):
                continue
            ev = _segment_evidence(seg)
            if ev:
                parts.append(ev)
            if len(parts) >= 3:
                break
        return "\n".join(parts)

    if not _section_has_term(result, "customer_primary_demands", "毛孔") and not _section_has_term(result, "customer_primary_demands", "痘印"):
        evidence = evidence_for("毛孔", "痘印", "痘坑")
        if evidence:
            _append_primary_demand(
                result,
                demand="希望改善毛孔、痘印/痘坑等皮肤质地问题",
                body_part="面部皮肤",
                evidence=evidence,
            )

    if not _section_has_term(result, "customer_primary_demands", "暗沉") and not _section_has_term(result, "customer_primary_demands", "美白"):
        evidence = evidence_for("暗沉", "暗黄", "美白", "提亮")
        if evidence:
            _append_primary_demand(
                result,
                demand="希望改善皮肤暗沉/暗黄并提亮肤色",
                body_part="面部皮肤",
                evidence=evidence,
            )

    if not _section_has_term(result, "customer_primary_demands", "光电") and not _section_has_term(result, "customer_primary_demands", "水光"):
        evidence = evidence_for("水光", "光电", "热玛吉", "超声炮", "黄金微针", "黑曜双波", "射频")
        if evidence:
            _append_primary_demand(
                result,
                demand="希望通过光电/水光等皮肤管理项目改善皮肤状态",
                body_part="面部皮肤",
                evidence=evidence,
            )


def _mark_low_business_value_if_empty(result: dict[str, Any], raw: dict[str, Any]) -> None:
    has_demands = bool(_as_list(_as_dict(result.get("customer_primary_demands")).get("items")))
    has_indications = bool(_as_list(_as_dict(result.get("standardized_indications")).get("items")))
    has_recommendations = bool(_as_list(_as_dict(result.get("staff_recommendations")).get("items")))
    if has_demands or has_indications or has_recommendations:
        return
    text = _raw_full_text(raw)
    if not text:
        return
    quality = _as_dict(result.get("analysis_quality"))
    issues = [_clean_text(item) for item in _as_list(quality.get("issues")) if _clean_text(item)]
    issue = "疑似低业务价值录音：未发现主客户有效主诉、适应症或推荐方案，可能为闲聊/内部协作/订单沟通"
    if issue not in issues:
        issues.append(issue)
    quality["issues"] = issues
    quality["requires_review"] = True
    result["analysis_quality"] = quality


def _recommendation_needs_detail_suffix(recommendation: str) -> bool:
    text = _clean_text(recommendation)
    if not text:
        return False
    return len(text) <= 24 or _has_any_text(text, ("综合方案", "联合治疗", "治疗方案", "改善方案"))


def _build_recommendation_display_text(
    recommendation: str,
    *,
    brand: str,
    material: str,
    dosage: str,
    price: str,
    course: str,
    steps: list[str],
) -> str:
    recommendation = _clean_text(recommendation)
    if not _recommendation_needs_detail_suffix(recommendation):
        return recommendation
    details: list[str] = []
    if brand:
        details.append(f"项目/设备：{brand}")
    if material and material not in brand:
        details.append(f"材料：{material}")
    if dosage:
        details.append(f"用量：{dosage}")
    if course:
        details.append(f"疗程：{course}")
    if price:
        details.append(f"价格：{price}")
    clean_steps = [_clean_text(value) for value in steps if _clean_text(value)]
    if clean_steps:
        details.append(f"步骤：{'；'.join(clean_steps[:5])}")
    if not details:
        return recommendation
    suffix = "；".join(details)
    if suffix in recommendation:
        return recommendation
    return f"{recommendation}（{suffix}）"


def _build_recommendation_item(item: dict[str, Any], demand_map: dict[str, int]) -> dict[str, Any] | None:
    recommendation = _first_text(item, "content", "recommendation", "plan", "text")
    if not recommendation:
        return None
    if _item_has_low_confidence_fragment(item) or _looks_like_low_confidence_fragment(recommendation):
        return None
    relation = _clean_text(item.get("relation_to_current_demand"))
    if relation in {"alternative_not_recommended", "auxiliary_or_care", "not_current_or_referral"}:
        return None
    if _is_auxiliary_or_care_recommendation_text(recommendation):
        return None
    if _is_alternative_or_not_recommended_text(recommendation):
        return None
    details = _as_dict(item.get("details"))
    steps = _as_list(item.get("treatment_steps")) or _as_list(details.get("treatment_steps"))
    notes = _first_text(item, "implementation_notes", "notes")
    if not notes:
        detail_notes = []
        for key in ("post_op_notes", "included_items", "key_points"):
            values = [_clean_text(value) for value in _as_list(details.get(key)) if _clean_text(value)]
            if values:
                detail_notes.append(f"{key}: {'; '.join(values)}")
        notes = "；".join(detail_notes)
    priorities = _linked_priorities(item, demand_map)
    if not priorities and len(demand_map) == 1:
        priorities = list(demand_map.values())
    evidence = (
        _evidence_text(item.get("evidence"))
        or _evidence_text(item.get("supporting_evidence"))
        or _first_text(item, "quote", "source_quote", "evidence_quote")
    )
    brand = _first_text(item, "brand", "brand_or_product")
    material = _first_text(item, "material", "brand_or_material")
    dosage = _first_text(item, "dosage", "dosage_or_quantity", "dosage_or_course")
    price = _first_text(item, "price")
    course = _first_text(item, "course_or_frequency", "course", "frequency", "dosage_or_course")
    recommendation = _build_recommendation_display_text(
        recommendation,
        brand=brand,
        material=material,
        dosage=dosage,
        price=price,
        course=course,
        steps=[_clean_text(value) for value in steps if _clean_text(value)],
    )
    return {
        "recommendation": recommendation,
        "product_or_solution": _first_text(item, "product_or_solution", "product", "solution", "brand_or_product") or None,
        "body_part": _first_text(item, "body_part", "body_part_name") or None,
        "brand": brand or None,
        "material": material or None,
        "dosage": dosage or None,
        "price": price or None,
        "course_or_frequency": course or None,
        "treatment_steps": [_clean_text(value) for value in steps if _clean_text(value)],
        "implementation_notes": notes or None,
        "demand_priority": priorities,
        "evidence": evidence,
        "customer_response": _first_text(item, "customer_response", "response", "acceptance") or "未明确回应",
    }


def _recommendation_semantic_key(text: object) -> str:
    compact = _normalize_key(_clean_text(text))
    if not compact:
        return ""
    if "水光" in compact and "提透" in compact and "冻颜" in compact:
        return "水光提透冻颜"
    if "水光" in compact and ("胶原" in compact or "补水" in compact or "肤质" in compact):
        return "水光胶原补水"
    if ("下颌线" in compact or "下颌角" in compact) and (
        "支撑" in compact or "提升" in compact or "轮廓" in compact
    ):
        return "下颌线下颌角轮廓支撑"
    if "口基底" in compact and ("填充" in compact or "支撑" in compact or "衔接" in compact):
        return "口基底填充支撑"
    if ("眉弓" in compact or "双c线" in compact or "双C线" in compact) and (
        "支撑" in compact or "立体" in compact
    ):
        return "眉弓双C线支撑"
    if "黄金微针" in compact:
        return "黄金微针"
    if "点痣" in compact or "祛痣" in compact:
        return "点痣祛痣"
    if "皮秒" in compact and ("祛斑" in compact or "色斑" in compact or "雀斑" in compact):
        return "皮秒祛斑"
    return compact


def _build_recommendations(fact_graph: dict[str, Any], demand_map: dict[str, int], *, seed: bool = False) -> dict[str, Any]:
    source_key = "seed_recommendations" if seed else "recommendations"
    result_items: list[dict[str, Any]] = []
    source_items = [item for item in _as_list(fact_graph.get(source_key)) if isinstance(item, dict)]
    if seed:
        source_items.extend(
            item
            for item in _as_list(fact_graph.get("recommendations"))
            if isinstance(item, dict)
            and _is_seed_like_text(json.dumps(item, ensure_ascii=False))
            and not _linked_priorities(item, demand_map)
        )
    for item in source_items:
        if not isinstance(item, dict):
            continue
        recommendation_text = _first_text(item, "content", "recommendation", "plan", "text")
        relation = _clean_text(item.get("relation_to_current_demand"))
        item_text = json.dumps(item, ensure_ascii=False)
        if relation in {"alternative_not_recommended", "auxiliary_or_care", "not_current_or_referral"}:
            continue
        if not seed and _is_seed_like_text(recommendation_text) and not _linked_priorities(item, demand_map):
            continue
        if _is_auxiliary_or_care_recommendation_text(_first_text(item, "content", "recommendation", "plan", "text")):
            continue
        if _is_alternative_or_not_recommended_text(recommendation_text):
            continue
        mapped = _build_recommendation_item(item, demand_map)
        if mapped:
            if seed:
                mapped["demand_priority"] = []
            key = _recommendation_semantic_key(mapped.get("recommendation"))
            if key and all(_recommendation_semantic_key(existing.get("recommendation")) != key for existing in result_items):
                result_items.append(mapped)
    return {
        "summary": "；".join(item["recommendation"] for item in result_items),
        "items": result_items,
    }


def _ensure_recommendation_coverage(
    recommendations: dict[str, Any],
    fact_graph: dict[str, Any],
    demand_map: dict[str, int],
) -> dict[str, Any]:
    """Keep mapped output consistent with fact_graph recommendations.

    Filtering should remove only truly auxiliary/alternative items. It must not
    drop a current main plan just because implementation_notes mention a backup
    option or comparison product.
    """
    items = [dict(item) for item in _as_list(recommendations.get("items")) if isinstance(item, dict)]
    seen = {_recommendation_semantic_key(item.get("recommendation")) for item in items if _recommendation_semantic_key(item.get("recommendation"))}
    for source in _as_list(fact_graph.get("recommendations")):
        if not isinstance(source, dict):
            continue
        mapped = _build_recommendation_item(source, demand_map)
        if not mapped:
            continue
        key = _recommendation_semantic_key(mapped.get("recommendation"))
        if not key or key in seen:
            continue
        relation = _clean_text(source.get("relation_to_current_demand"))
        if relation in {"planting_or_later", "seed", "later"}:
            continue
        if _is_seed_like_text(_first_text(source, "content", "recommendation", "plan", "text")) and not _linked_priorities(source, demand_map):
            continue
        seen.add(key)
        items.append(mapped)
    recommendations["items"] = items
    recommendations["summary"] = "；".join(_clean_text(item.get("recommendation")) for item in items if _clean_text(item.get("recommendation")))
    return recommendations


def _remove_seed_recommendations_covered_by_main(
    seed_recommendations: dict[str, Any],
    recommendations: dict[str, Any],
) -> dict[str, Any]:
    main_keys = {
        _recommendation_semantic_key(item.get("recommendation"))
        for item in _as_list(recommendations.get("items"))
        if _recommendation_semantic_key(item.get("recommendation"))
    }
    if not main_keys:
        return seed_recommendations
    items: list[dict[str, Any]] = []
    for item in _as_list(seed_recommendations.get("items")):
        if not isinstance(item, dict):
            continue
        key = _recommendation_semantic_key(item.get("recommendation"))
        if key and key in main_keys:
            continue
        items.append(item)
    updated = dict(seed_recommendations)
    updated["items"] = items
    updated["summary"] = "；".join(
        _clean_text(item.get("recommendation")) for item in items if _clean_text(item.get("recommendation"))
    )
    return updated


def _build_concerns(fact_graph: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for item in _as_list(fact_graph.get("concerns")):
        if not isinstance(item, dict):
            continue
        content = _first_text(item, "content", "concern", "text")
        if not content:
            continue
        evidence = _evidence_text(item.get("evidence")) or _first_text(item, "quote", "supporting_quote", "source_quote")
        if not evidence:
            continue
        items.append(
            {
                "type": _first_text(item, "type", "category") or "顾虑",
                "content": content,
                "evidence": evidence,
            }
        )
    return {"inference_note": None, "summary": "；".join(item["content"] for item in items), "items": items}


@lru_cache(maxsize=1)
def _profile_weight_by_category() -> dict[str, int]:
    weights: dict[str, int] = {}
    for item in load_tag_catalog_definitions():
        category = canonicalize_profile_tag_category(item.name) or _clean_text(item.name)
        if category:
            weights[category] = int(item.weight_level)
    return weights


def _append_profile_tag(tags: list[dict[str, Any]], category: str, value: str, evidence: str = "") -> None:
    canonical_category = canonicalize_profile_tag_category(category)
    if not canonical_category:
        return
    canonical_value = canonicalize_profile_tag_value(canonical_category, value)
    if not canonical_value or not is_valid_profile_tag_value(canonical_category, canonical_value):
        return
    single_value_ranks = {
        "疼痛耐受度": {"低": 3, "中": 2, "高": 1},
        "价格敏感度": {"高": 3, "中": 2, "低": 1},
    }
    key = (_normalize_key(canonical_category), _normalize_key(canonical_value))
    for existing in tags:
        if _normalize_key(existing.get("category")) == _normalize_key(canonical_category) and canonical_category in single_value_ranks:
            ranks = single_value_ranks[canonical_category]
            old_rank = ranks.get(_clean_text(existing.get("value")), 0)
            new_rank = ranks.get(canonical_value, 0)
            if new_rank > old_rank or (new_rank == old_rank and evidence and not existing.get("evidence")):
                existing["value"] = canonical_value
                existing["weight_level"] = _profile_weight_by_category().get(canonical_category)
                if evidence:
                    existing["evidence"] = evidence
            return
        if canonical_category in {"本次消费预算"} and _normalize_key(existing.get("category")) == _normalize_key(canonical_category):
            if evidence and "[" in evidence and "[" not in _clean_text(existing.get("evidence")):
                existing["value"] = canonical_value
                existing["evidence"] = evidence
            return
        if (_normalize_key(existing.get("category")), _normalize_key(existing.get("value"))) == key:
            if evidence and not existing.get("evidence"):
                existing["evidence"] = evidence
            return
    tags.append(
        {
            "category": canonical_category,
            "value": canonical_value,
            "weight_level": _profile_weight_by_category().get(canonical_category),
            "evidence": evidence,
        }
    )


def _profile_item_evidence(item: dict[str, Any]) -> str:
    evidence = _evidence_text(item.get("evidence"))
    if evidence:
        return evidence
    values = _as_list(item.get("evidence"))
    if values:
        return "；".join(_clean_text(value) for value in values if _clean_text(value))
    return _first_text(item, "quote", "content", "text", "summary")


def _profile_item_text(item: dict[str, Any]) -> str:
    parts = [
        _first_text(item, "category", "type"),
        _first_text(item, "value", "content", "project", "material", "device", "text", "factor", "concern"),
        _evidence_text(item.get("evidence")),
        _first_text(item, "quote"),
    ]
    return " ".join(part for part in parts if part)


def _profile_item_is_staff_scoped(item: dict[str, Any]) -> bool:
    scope = _participant_scope(item)
    if scope in {"staff", "doctor", "consultant", "badge_owner", "employee", "assistant", "nurse"}:
        return True
    participant = _clean_text(
        item.get("participant")
        or item.get("participant_label")
        or item.get("speaker")
        or item.get("speaker_label")
    )
    return _has_any_text(participant, ("工牌本人", "咨询师", "医生", "顾问", "助理", "护士", "员工"))


def _is_weak_allergy_profile_fact(item: dict[str, Any]) -> bool:
    category = _first_text(item, "category", "tag_category", "type")
    value = _first_text(item, "value", "tag_value", "content", "project", "material", "device", "text", "factor", "concern")
    evidence = _profile_item_evidence(item)
    text = _profile_item_text(item)
    combined = " ".join(part for part in (category, value, evidence, text) if part)
    if not combined or "过敏" not in combined:
        return False
    if not (_has_any_text(category, ("健康风险", "禁忌", "病史")) or "过敏" in value):
        return False
    if _has_any_text(combined, ("无药物过敏", "没有药物过敏", "无过敏史", "没有过敏史", "不过敏", "不是过敏")):
        return True
    if _has_any_text(combined, ("过敏率", "不易过敏", "不容易过敏", "低敏", "抗过敏")):
        return True
    allergy_context = " ".join(
        part
        for part in (
            evidence,
            _first_text(item, "quote", "source_quote", "evidence_quote"),
        )
        if part
    )
    if not allergy_context or _normalize_key(allergy_context) == _normalize_key(value):
        content_text = _first_text(item, "content", "text", "summary")
        if _normalize_key(content_text) != _normalize_key(value):
            allergy_context = content_text
    strong_allergy = _has_any_text(
        allergy_context,
        (
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
        ),
    ) or bool(re.search(r"对.{1,12}过敏", allergy_context))
    if strong_allergy:
        return False
    return _has_any_text(combined, ("皮肤过敏", "玫瑰痤疮", "敏感肌", "皮肤敏感", "容易泛红"))


def _should_skip_profile_item_for_tags(item: dict[str, Any]) -> bool:
    if _profile_item_is_staff_scoped(item):
        return True
    if _is_weak_allergy_profile_fact(item):
        return True
    category = canonicalize_profile_tag_category(_first_text(item, "category", "tag_category", "type"))
    value = _first_text(item, "value", "tag_value", "content", "project", "material", "device", "text", "factor", "concern")
    evidence = _profile_item_evidence(item)
    if category == "治疗项目" and canonicalize_profile_tag_value(category, value) == "注射类":
        if _is_non_aesthetic_injection_context(" ".join(part for part in (value, evidence) if part)):
            return True
    if category in {"治疗项目", "历史用的设备/原材料名称"} and _is_treatment_project_without_prior_history_context(value, evidence):
        return True
    return False


def _append_profile_tag_from_item(tags: list[dict[str, Any]], item: dict[str, Any]) -> None:
    if _should_skip_profile_item_for_tags(item):
        return
    category = _first_text(item, "category", "tag_category", "type")
    value = _first_text(item, "value", "tag_value", "content", "project", "material", "device", "text", "factor", "concern")
    if not category or not value:
        return
    _append_profile_tag(tags, category, value, _profile_item_evidence(item))


def _extract_profile_age_from_fact_graph(fact_graph: dict[str, Any]) -> tuple[str | None, str | None]:
    for item in _as_list(fact_graph.get("profile_facts")):
        if not isinstance(item, dict):
            continue
        category = _first_text(item, "category", "tag_category", "type").lower()
        value = _first_text(item, "value", "tag_value", "content", "text", "summary")
        evidence = _profile_item_evidence(item)
        text = " ".join(part for part in (category, value, evidence) if part)
        if not text:
            continue
        match = re.search(r"(\d{1,3})\s*岁", text)
        if category == "age" or "年龄" in category or match:
            age = f"{match.group(1)}岁" if match else value
            if age:
                return age, evidence or value
    return None, None


def _classify_history_treatment_project(text: str) -> str:
    if _is_non_aesthetic_injection_context(text):
        return ""
    medical_surgery_terms = (
        "双眼皮",
        "眼袋",
        "鼻综合",
        "隆鼻",
        "吸脂",
        "抽脂",
        "拉皮",
        "丰胸",
        "线雕",
        "面部除皱",
        "脂肪填充",
        "假体",
    )
    non_aesthetic_surgery_terms = (
        "中耳炎",
        "阑尾",
        "剖腹产",
        "胆囊",
        "骨折",
        "种植牙",
        "拔牙",
        "甲状腺",
        "肿瘤",
        "囊肿",
    )
    if _has_any_text(text, medical_surgery_terms) or ("手术" in text and not _has_any_text(text, non_aesthetic_surgery_terms)):
        return "手术类"
    if _has_any_text(text, ("水光", "玻尿酸", "胶原", "肉毒", "除皱针", "瘦脸针", "注射", "填充", "童颜", "贝丽菲尔")):
        return "注射类"
    if _has_any_text(text, ("热玛吉", "超声炮", "光电", "黄金微针", "光子", "射频", "激光", "黑曜双波")):
        return "光电类"
    return ""


def _is_non_aesthetic_injection_context(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    non_aesthetic_terms = (
        "减肥针",
        "替泊尔肽",
        "替尔泊肽",
        "司美格鲁肽",
        "利拉鲁肽",
        "提西",
        "胰岛素",
        "疫苗",
        "生长激素",
    )
    if not _has_any_text(normalized, non_aesthetic_terms):
        return False
    aesthetic_terms = (
        "水光",
        "玻尿酸",
        "胶原",
        "肉毒",
        "除皱针",
        "瘦脸针",
        "填充",
        "童颜",
        "芭比针",
        "濡白天使",
        "瑞德喜",
        "艾拉斯提",
        "贝丽菲尔",
    )
    return not _has_any_text(normalized, aesthetic_terms)


def _is_treatment_project_without_prior_history_context(value: str, evidence: str) -> bool:
    text = " ".join(part for part in (_clean_text(value), _clean_text(evidence)) if part)
    if not text:
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
        "没有治疗史",
        "无治疗史",
        "无既往",
        "没有既往",
        "否认既往",
    )
    if _has_any_text(text, negative_history_markers):
        return True
    prior_markers = (
        "做过",
        "打过",
        "填过",
        "治疗过",
        "做了",
        "打了",
        "割过",
        "隆过",
        "吸过",
        "术后",
        "手术史",
        "既往",
        "之前",
        "以前",
        "曾",
        "曾经",
        "外院",
        "去年",
        "今年",
        "最近一次",
        "上次",
        "多次",
    )
    if _has_any_text(text, prior_markers):
        return False
    current_or_hypothetical_markers = (
        "能打",
        "可以打",
        "适合打",
        "这次打",
        "现在打",
        "再打一支",
        "准备打",
        "想打",
        "材料选择",
        "反正我都能打",
    )
    if _has_any_text(text, current_or_hypothetical_markers):
        return True
    # "治疗项目" profile tags describe prior history; current or hypothetical treatment belongs in recommendations.
    return True


def _extract_history_material_name(text: str) -> str:
    known_terms = (
        "水光",
        "水光针",
        "热玛吉",
        "超声炮",
        "黄金微针",
        "玻尿酸",
        "胶原蛋白",
        "双美胶原蛋白",
        "肉毒",
        "除皱针",
        "贝丽菲尔",
        "艾拉斯提",
        "瑞德喜",
        "黑曜双波",
    )
    for term in known_terms:
        if term in text:
            return term
    return ""


_BUDGET_AMOUNT_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?\s*(?:元|块钱|万|千|百)|"
    r"[一二三四五六七八九十两俩]+(?:点[一二三四五六七八九十])?(?:多)?(?:万|千|百|块钱|元))"
)

_BUDGET_AMOUNT_PHRASE_PATTERN = re.compile(
    r"((?:总价|价格|报价|费用)?约?\s*"
    r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十两俩]+(?:点[一二三四五六七八九十])?)"
    r"(?:\s*(?:-|—|~|到|至)\s*"
    r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十两俩]+(?:点[一二三四五六七八九十])?))?"
    r"\s*(?:元|块钱|万|千|百))"
)

_BUDGET_FIELD_CUES = (
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

_PRICE_REACTION_CUES = (
    "价格偏高",
    "价格高",
    "太贵",
    "贵了",
    "有点贵",
    "价格贵",
    "便宜",
    "优惠",
    "打折",
    "申请",
    "少一点",
    "不够",
    "没那么多",
    "价格敏感",
    "敏感",
    "反复核算",
    "反复算",
    "核算",
    "差别有点大",
)

_NOT_BUDGET_EXPLANATION_CUES = (
    "解决不了多少",
    "改善的程度有限",
    "改善程度有限",
    "效果有限",
    "做不了多少",
    "没效果",
)


def _has_budget_amount(text: str) -> bool:
    return bool(_BUDGET_AMOUNT_PATTERN.search(_clean_text(text)))


def _is_explicit_budget_text(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    has_budget_cue = _has_any_text(normalized, _BUDGET_FIELD_CUES)
    has_amount = _has_budget_amount(normalized)
    if _has_any_text(normalized, _NOT_BUDGET_EXPLANATION_CUES) and not has_budget_cue:
        return False
    return has_budget_cue and (
        has_amount
        or _has_any_text(normalized, ("预算有限", "打不起", "接受不了", "没那么多", "不够", "无预算"))
    )


def _is_price_sensitivity_text(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    if _has_any_text(normalized, _NOT_BUDGET_EXPLANATION_CUES) and not _has_any_text(
        normalized, _PRICE_REACTION_CUES + _BUDGET_FIELD_CUES
    ):
        return False
    return _has_any_text(normalized, _PRICE_REACTION_CUES)


def _extract_budget_amount_phrase(text: str) -> str:
    normalized = _clean_text(text)
    if not normalized:
        return ""
    match = _BUDGET_AMOUNT_PHRASE_PATTERN.search(normalized)
    if match:
        return re.sub(r"\s+", "", match.group(1))
    match = _BUDGET_AMOUNT_PATTERN.search(normalized)
    return re.sub(r"\s+", "", match.group(1)) if match else ""


def _is_implicit_budget_pressure_text(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized or not _has_budget_amount(normalized):
        return False
    if _has_any_text(normalized, _NOT_BUDGET_EXPLANATION_CUES) and not _has_any_text(
        normalized, _PRICE_REACTION_CUES + _BUDGET_FIELD_CUES
    ):
        return False
    return _has_any_text(normalized, _PRICE_REACTION_CUES)


def _budget_value_from_text(text: str) -> str | None:
    normalized = _clean_text(text)
    if not normalized:
        return None
    if _is_explicit_budget_text(normalized):
        return normalized
    if not _is_implicit_budget_pressure_text(normalized):
        return None
    amount = _extract_budget_amount_phrase(normalized)
    if not amount:
        return None
    if not amount.startswith(("总价", "价格", "报价", "费用", "约")):
        amount = f"约{amount}"
    return f"未明确；对{amount}较敏感，倾向希望低于该区间"


def _append_profile_tags_from_fact_graph(tags: list[dict[str, Any]], fact_graph: dict[str, Any]) -> None:
    for key in ("profile_facts",):
        for item in _as_list(fact_graph.get(key)):
            if isinstance(item, dict):
                _append_profile_tag_from_item(tags, item)

    for item in _as_list(fact_graph.get("medical_history")):
        if not isinstance(item, dict):
            continue
        text = _profile_item_text(item)
        if not text:
            continue
        evidence = _profile_item_evidence(item)
        history_context = " ".join(part for part in (text, evidence) if part)
        if _is_treatment_project_without_prior_history_context("", history_context):
            continue
        _append_profile_tag_from_item(tags, item)
        project_type = _classify_history_treatment_project(text)
        if project_type:
            _append_profile_tag(tags, "治疗项目", project_type, evidence)
        material = _extract_history_material_name(text)
        if material:
            _append_profile_tag(tags, "历史用的设备/原材料名称", material, evidence)

    budget_texts: list[tuple[str, str]] = []
    for key in ("budget_facts", "deal_factors"):
        for item in _as_list(fact_graph.get(key)):
            if not isinstance(item, dict):
                continue
            text = _profile_item_text(item)
            evidence = _profile_item_evidence(item)
            if _is_explicit_budget_text(text) or _is_price_sensitivity_text(text):
                budget_texts.append((text, evidence))
    for text, evidence in budget_texts[:1]:
        budget_value = _budget_value_from_text(text)
        if budget_value:
            _append_profile_tag(tags, "本次消费预算", budget_value, evidence)
    all_business_text = json.dumps(fact_graph, ensure_ascii=False)
    all_budget_text = " ".join(text for text, _ in budget_texts) or all_business_text
    if _has_any_text(all_budget_text, ("价格偏高", "价格高", "太贵", "贵了", "有点贵", "预算有限", "顶死", "承受", "便宜吗", "申请", "优惠", "差别有点大")):
        _append_profile_tag(tags, "价格敏感度", "高", budget_texts[0][1] if budget_texts else "")
    elif _has_any_text(all_budget_text, ("价格", "预算", "费用", "报价")):
        _append_profile_tag(tags, "价格敏感度", "中", budget_texts[0][1] if budget_texts else "")

    pain_evidence = ""
    for item in _as_list(fact_graph.get("profile_facts")) + _as_list(fact_graph.get("concerns")):
        if not isinstance(item, dict):
            continue
        text = _profile_item_text(item)
        if _has_any_text(text, ("怕痛", "怕疼", "很怕疼", "不能痛", "受不了痛", "不怕痛", "能忍痛", "痛感低", "痛感高")):
            pain_evidence = _profile_item_evidence(item) or text
            break
    if pain_evidence:
        pain_level = "低" if _has_any_text(pain_evidence, ("怕痛", "怕疼", "很怕疼", "不能痛", "受不了痛", "痛感高")) else "中"
        _append_profile_tag(tags, "疼痛耐受度", pain_level, pain_evidence)

    trauma_evidence = ""
    for item in _as_list(fact_graph.get("profile_facts")) + _as_list(fact_graph.get("concerns")):
        if not isinstance(item, dict):
            continue
        text = _profile_item_text(item)
        if _has_any_text(text, ("不想动刀", "不做手术", "怕恢复期", "恢复期短", "不想注射", "只想做皮肤", "只想做抗衰", "偏向光电", "偏向皮肤管理")):
            trauma_evidence = _profile_item_evidence(item) or text
            break
    if trauma_evidence:
        _append_profile_tag(tags, "创伤倾向", "皮肤", trauma_evidence)


def _first_raw_evidence_matching(raw: dict[str, Any], patterns: tuple[str, ...], *, customer_only: bool = False) -> str:
    regexes = [re.compile(pattern) for pattern in patterns]
    for seg in _raw_transcribe_segments(raw):
        if customer_only and not _segment_is_customer_side(seg):
            continue
        text = _clean_text(seg.get("text") or seg.get("content"))
        if not text:
            continue
        if any(regex.search(text) for regex in regexes):
            return _segment_evidence(seg) or text
    return ""


def _append_profile_tags_from_raw(tags: list[dict[str, Any]], raw: dict[str, Any]) -> None:
    if not raw:
        return
    budget_evidence = _first_raw_evidence_matching(
        raw,
        (
            r"(预算|最多|顶死|承受|可接受|能接受|不超过).{0,12}[七八九一二三四五六十百千万0-9]",
            r"[七八九一二三四五六十百千万0-9]{1,8}.{0,8}(顶死|预算|承受|可接受|能接受|不超过)",
        ),
        customer_only=True,
    )
    if budget_evidence:
        _append_profile_tag(tags, "本次消费预算", budget_evidence, budget_evidence)
        _append_profile_tag(tags, "价格敏感度", "高", budget_evidence)

    price_evidence = _first_raw_evidence_matching(raw, (r"价格.{0,12}(太贵|贵了|有点贵|高|差别|便宜|申请|优惠)", r"(太贵|贵了|有点贵|便宜吗|差别有点大|预算有限|顶死)"))
    if price_evidence:
        _append_profile_tag(tags, "价格敏感度", "高", price_evidence)

    pain_evidence = _first_raw_evidence_matching(raw, (r"(痛感|疼痛|痛不痛|疼不疼|怕痛|怕疼)",))
    if pain_evidence:
        pain_level = "低" if _has_any_text(pain_evidence, ("怕痛", "怕疼", "受不了")) else "中"
        _append_profile_tag(tags, "疼痛耐受度", pain_level, pain_evidence)

    child_evidence = _first_raw_evidence_matching(raw, (r"(两个|2个).{0,6}(宝宝|孩子|小孩|娃)", r"(宝宝|孩子|小孩|娃).{0,6}(两个|2个)"))
    if child_evidence:
        _append_profile_tag(tags, "亲属/子女情况", "2孩及以上", child_evidence)

    industry_evidence = _first_raw_evidence_matching(raw, (r"(部队|军人|军队|军官|士官)",))
    if industry_evidence:
        _append_profile_tag(tags, "行业", "政府/公共事业", industry_evidence)

    comparison_evidence = _first_raw_evidence_matching(raw, (r"(艺星|长沙艺星|米兰柏羽|雅美|华美|美莱)",))
    if comparison_evidence:
        value = "长沙艺星" if "艺星" in comparison_evidence else comparison_evidence
        _append_profile_tag(tags, "对比机构", value, comparison_evidence)

    history_evidence = _first_raw_evidence_matching(raw, (r"(做过|打过|填过|治疗过|维养).{0,20}(水光|玻尿酸|胶原|肉毒|热玛吉|超声炮|黄金微针|光电)", r"(水光|玻尿酸|胶原|肉毒|热玛吉|超声炮|黄金微针|光电).{0,20}(做过|打过|填过|治疗过|维养)"))
    if history_evidence:
        project_type = _classify_history_treatment_project(history_evidence)
        material = _extract_history_material_name(history_evidence)
        if project_type:
            _append_profile_tag(tags, "治疗项目", project_type, history_evidence)
        if material:
            _append_profile_tag(tags, "历史用的设备/原材料名称", material, history_evidence)

    decision_evidence = _first_raw_evidence_matching(raw, (r"(我自己|我就|我可以|我考虑|我回去|给我回复|我再决定)",))
    if decision_evidence and not _has_any_text(decision_evidence, ("老公", "老婆", "妈妈", "爸爸", "家人")):
        _append_profile_tag(tags, "决策主体", "自主", decision_evidence)


def _build_customer_profile(fact_graph: dict[str, Any], raw: dict[str, Any] | None = None) -> dict[str, Any]:
    tags: list[dict[str, Any]] = []
    _append_profile_tags_from_fact_graph(tags, fact_graph)
    if raw:
        _append_profile_tags_from_raw(tags, raw)
    age, age_evidence = _extract_profile_age_from_fact_graph(fact_graph)
    return {"inference_note": None, "age": age, "age_evidence": age_evidence, "tags": tags}


def _build_consumption_intent(fact_graph: dict[str, Any]) -> dict[str, Any]:
    decision_factors: list[str] = []
    evidence: list[str] = []
    budget: str | None = None
    for key in ("deal_factors", "budget_facts"):
        for item in _as_list(fact_graph.get(key)):
            if not isinstance(item, dict):
                continue
            content = _first_text(item, "content", "factor", "text")
            if content:
                if key == "deal_factors" or (key == "budget_facts" and _is_price_sensitivity_text(content)):
                    decision_factors.append(content)
                if key == "budget_facts" and budget is None:
                    budget = _budget_value_from_text(content)
            ev = _evidence_text(item.get("evidence"))
            if ev:
                evidence.append(ev)
    return {"budget": budget, "decision_factors": decision_factors, "evidence": evidence}


def _build_consultation_result(
    primary_demands: dict[str, Any],
    indications: dict[str, Any],
    concerns: dict[str, Any],
    recommendations: dict[str, Any],
    seed_recommendations: dict[str, Any],
    profile: dict[str, Any],
    consumption_intent: dict[str, Any],
    fact_graph: dict[str, Any],
) -> dict[str, Any]:
    indication_texts = [
        f"{item.get('indication_name')}（{item.get('body_part_name')}）"
        for item in _as_list(indications.get("items"))
        if item.get("indication_name")
    ]
    recommendation_items = [
        {
            "plan": item.get("recommendation", ""),
            "acceptance": item.get("customer_response") or "未明确回应",
            "evidence": item.get("evidence") or "",
        }
        for item in _as_list(recommendations.get("items"))
    ]
    seed_items = [
        {
            "plan": item.get("recommendation", ""),
            "acceptance": item.get("customer_response") or "未明确回应",
            "evidence": item.get("evidence") or "",
        }
        for item in _as_list(seed_recommendations.get("items"))
    ]
    deal_outcome = _as_dict(fact_graph.get("deal_outcome"))
    status = _normalize_deal_status(
        _first_text(deal_outcome, "status"),
        _first_text(deal_outcome, "content", "summary"),
        _first_text(deal_outcome, "amount"),
    )
    summary = _first_text(deal_outcome, "content", "summary") or status
    return {
        "chief_complaint_and_indications": {
            "summary": primary_demands.get("summary", ""),
            "primary_demands": [item.get("demand") for item in _as_list(primary_demands.get("items")) if item.get("demand")],
            "standardized_indications": indication_texts,
        },
        "deal_factors": {
            "summary": "；".join(_as_list(consumption_intent.get("decision_factors")) + [concerns.get("summary", "")]).strip("；"),
            "budget": consumption_intent.get("budget"),
            "concerns": [item.get("content") for item in _as_list(concerns.get("items")) if item.get("content")],
            "decision_factors": _as_list(consumption_intent.get("decision_factors")),
        },
        "recommended_plan": {"summary": recommendations.get("summary", ""), "items": recommendation_items},
        "seed_plan": {"summary": seed_recommendations.get("summary", ""), "items": seed_items},
        "deal_outcome": {
            "status": status,
            "summary": summary,
            "deal_items": _as_list(deal_outcome.get("deal_items")),
            "amount": _clean_text(deal_outcome.get("amount")) or None,
            "loss_reasons": _as_list(deal_outcome.get("loss_reasons")),
        },
        "customer_profile_summary": {
            "summary": "；".join(f"{tag.get('category')}:{tag.get('value')}" for tag in _as_list(profile.get("tags"))),
            "extracted_tag_count": len(_as_list(profile.get("tags"))),
            "age": profile.get("age"),
            "age_evidence": profile.get("age_evidence"),
            "tags": _as_list(profile.get("tags")),
        },
    }


def _normalize_deal_status(status: str, summary: str = "", amount: str = "") -> str:
    text = f"{status} {summary} {amount}".lower()
    unknown_tokens = (
        "未明确",
        "无成交",
        "无成交相关信息",
        "没有成交信息",
        "未提及成交",
        "未提及付款",
        "未体现成交",
        "无法判断",
        "unknown",
        "unclear",
    )
    negative_tokens = ("未成交", "no_deal", "not_deal", "refused", "拒绝", "不做", "未付款")
    positive_tokens = (
        "已成交",
        "paid",
        "payment",
        "deposit",
        "partial_deal",
        "down_payment",
        "定金",
        "订金",
        "付款",
        "支付",
        "下单",
        "锁定",
    )
    if any(token in text for token in unknown_tokens):
        return "未明确"
    if any(token in text for token in negative_tokens):
        return "未成交"
    if any(token in text for token in positive_tokens):
        return "已成交"
    if "成交" in text and not any(token in text for token in ("未", "无", "没有", "不")):
        return "已成交"
    return "未明确"


def _build_sap_summary_materials(
    primary_demands: dict[str, Any],
    recommendations: dict[str, Any],
    concerns: dict[str, Any],
    fact_graph: dict[str, Any],
) -> dict[str, Any]:
    parts = [
        primary_demands.get("summary", ""),
        recommendations.get("summary", ""),
        concerns.get("summary", ""),
        _first_text(_as_dict(fact_graph.get("deal_outcome")), "content", "summary"),
    ]
    summary = "；".join(part for part in parts if part)
    return {"summary": summary, "sections": []}


def _has_linked_recommendation(fact_graph: dict[str, Any]) -> bool:
    for key in ("recommendations", "seed_recommendations"):
        for item in _as_list(fact_graph.get(key)):
            if not isinstance(item, dict):
                continue
            if _linked_priorities(item, _demand_priority_map(_as_list(fact_graph.get("demands")))):
                return True
            if _as_list(item.get("related_demand_ids")) or _as_list(item.get("linked_demand_ids")):
                return True
    return False


def _stabilize_fact_graph(fact_graph: dict[str, Any]) -> dict[str, Any]:
    """Apply generic safety checks that are independent of any specific recording."""
    stabilized = dict(fact_graph)
    demands = [item for item in _as_list(stabilized.get("demands")) if isinstance(item, dict)]
    diagnoses = [item for item in _as_list(stabilized.get("doctor_diagnoses")) if isinstance(item, dict)]
    has_anchor = bool(demands or diagnoses or _has_linked_recommendation(stabilized))
    if not has_anchor:
        stabilized["demands"] = []
        stabilized["doctor_diagnoses"] = []
        stabilized["indication_candidates"] = []
        stabilized["recommendations"] = []
        stabilized["seed_recommendations"] = []
        stabilized["deal_outcome"] = {"status": "未明确", "summary": "未发现主客户有效面诊证据"}
        uncertainties = _as_list(stabilized.get("uncertainties"))
        uncertainties.append("未发现主客户需求、诊断或与主诉关联的推荐方案，已抑制方案/适应症/成交推断")
        stabilized["uncertainties"] = uncertainties
    return stabilized


def _build_analysis_result_from_fact_graph(
    fact_graph: dict[str, Any],
    raw: dict[str, Any],
    *,
    include_participant_results: bool = True,
    allow_raw_augmentation: bool = True,
) -> dict[str, Any]:
    source_fact_graph = dict(fact_graph)
    customer_participants = _collect_customer_participants(source_fact_graph)
    fact_graph = _default_fact_graph_for_single_result(source_fact_graph)
    fact_graph = _stabilize_fact_graph(fact_graph)
    primary_demands, demand_source_items, demand_map = _build_demands(fact_graph)
    indications = _build_standardized_indications(fact_graph)
    recommendations = _build_recommendations(fact_graph, demand_map, seed=False)
    recommendations = _ensure_recommendation_coverage(recommendations, fact_graph, demand_map)
    seed_recommendations = _build_recommendations(fact_graph, demand_map, seed=True)
    seed_recommendations = _remove_seed_recommendations_covered_by_main(seed_recommendations, recommendations)
    concerns = _build_concerns(fact_graph)
    profile_raw = raw if allow_raw_augmentation and len(customer_participants) <= 1 else None
    profile = _build_customer_profile(fact_graph, profile_raw)
    consumption_intent = _build_consumption_intent(fact_graph)

    # Safety: if the LLM extracted recommendation facts, the mapped result must
    # never have an empty staff_recommendations block.
    if not _as_list(recommendations.get("items")) and _as_list(fact_graph.get("recommendations")):
        for item in _as_list(fact_graph.get("recommendations")):
            if isinstance(item, dict):
                mapped = _build_recommendation_item(item, demand_map)
                if mapped:
                    recommendations["items"].append(mapped)
        recommendations["summary"] = "；".join(item["recommendation"] for item in _as_list(recommendations.get("items")))

    consultation_result = _build_consultation_result(
        primary_demands,
        indications,
        concerns,
        recommendations,
        seed_recommendations,
        profile,
        consumption_intent,
        fact_graph,
    )
    result = {
        "customer_primary_demands": primary_demands,
        "standardized_indications": indications,
        "consumption_intent": consumption_intent,
        "customer_demands": {
            "inference_note": None,
            "focus_areas": [
                {
                    "area": item.get("body_part"),
                    "surface_need": item.get("demand"),
                    "deep_need": None,
                    "discovery_process": item.get("evidence"),
                }
                for item in _as_list(primary_demands.get("items"))
            ],
            "expectation": {"entry_state": None, "exit_state": None, "turning_points": [], "specific_standards": None},
            "product_preference": {"preferred_products": [], "information_sources": [], "comparison_factors": [], "consultant_influence": None},
        },
        "customer_concerns": concerns,
        "customer_profile": profile,
        "staff_recommendations": recommendations,
        "staff_seed_recommendations": seed_recommendations,
        "consultation_result": consultation_result,
        "sap_summary_materials": _build_sap_summary_materials(primary_demands, recommendations, concerns, fact_graph),
        "consultation_evaluation": {"overall_summary": "", "dimensions": []},
        "consultation_process_evaluation": {"total_score": 0, "max_total_score": 9, "overall_score": 0, "overall_summary": "", "sections": []},
        "staged_pipeline_debug": {
            "demand_source_count": len(demand_source_items),
            "fact_recommendation_count": len(_as_list(fact_graph.get("recommendations"))),
        },
    }
    finalized = _finalize_analysis_result(result, raw)

    # The production sanitizer may add fallback items from raw transcript or
    # remove structured items whose evidence format differs from the one-pass
    # prompt. For this experimental pipeline the fact_graph is the source of
    # truth, so restore these deterministic sections after sanitation.
    finalized["customer_primary_demands"] = primary_demands
    finalized["standardized_indications"] = indications
    finalized["staff_recommendations"] = recommendations
    finalized["staff_seed_recommendations"] = seed_recommendations
    finalized["customer_concerns"] = concerns
    finalized["consumption_intent"] = consumption_intent
    finalized["customer_profile"] = profile
    # Raw-text fallback scans the whole recording. In multi-customer recordings
    # it can accidentally add another customer's demand to the default result,
    # so only use it when there is not more than one consulting customer.
    if allow_raw_augmentation and len(customer_participants) <= 1:
        _augment_body_contouring_demands_from_raw(finalized, raw)
        _augment_explicit_skin_followup_demands_from_raw(finalized, raw)
        _mark_low_business_value_if_empty(finalized, raw)
    primary_demands = _as_dict(finalized.get("customer_primary_demands"))
    indications = _as_dict(finalized.get("standardized_indications"))
    finalized["consultation_result"] = _build_consultation_result(
        primary_demands,
        indications,
        concerns,
        recommendations,
        seed_recommendations,
        profile,
        consumption_intent,
        fact_graph,
    )
    finalized["sap_summary_materials"] = _build_sap_summary_materials(primary_demands, recommendations, concerns, fact_graph)
    finalized["staged_pipeline_debug"] = result["staged_pipeline_debug"]
    _clear_stale_analysis_quality_flags(finalized)
    finalized["consultation_evaluation"] = rebuild_consultation_evaluation(finalized)
    finalized["consultation_process_evaluation"] = rebuild_consultation_process_evaluation(finalized)
    if include_participant_results:
        participant_results = _build_participant_analysis_results(source_fact_graph, raw)
        if participant_results:
            finalized["participant_analysis_results"] = participant_results
            debug = _as_dict(finalized.get("staged_pipeline_debug"))
            debug["participant_analysis_count"] = len(participant_results)
            debug["participant_labels"] = [
                _clean_text(item.get("participant"))
                for item in participant_results
                if _clean_text(item.get("participant"))
            ]
            finalized["staged_pipeline_debug"] = debug
    return finalized


def _clear_stale_analysis_quality_flags(result: dict[str, Any]) -> None:
    quality = _as_dict(result.get("analysis_quality"))
    issues = [_clean_text(item) for item in _as_list(quality.get("issues")) if _clean_text(item)]
    if not issues:
        return
    has_demands = bool(_as_list(_as_dict(result.get("customer_primary_demands")).get("items")))
    has_indications = bool(_as_list(_as_dict(result.get("standardized_indications")).get("items")))
    kept: list[str] = []
    for issue in issues:
        if has_demands and "未提取到可支撑 SAP 回写的顾客主诉" in issue:
            continue
        if has_indications and "未提取到可支撑 SAP 回写的适应症" in issue:
            continue
        kept.append(issue)
    quality["issues"] = kept
    quality["requires_review"] = bool(kept)
    result["analysis_quality"] = quality


def _finalize_analysis_result(result: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_analysis_result(result) or result
    finalized = dict(normalized)
    sanitize_analysis_result_with_raw(finalized, raw=raw)
    _clear_stale_analysis_quality_flags(finalized)
    finalized["consultation_evaluation"] = rebuild_consultation_evaluation(finalized)
    finalized["consultation_process_evaluation"] = rebuild_consultation_process_evaluation(finalized)
    return finalized


def _extract_fact_graph(parsed: dict[str, Any]) -> dict[str, Any]:
    fact_graph = parsed.get("fact_graph")
    if isinstance(fact_graph, dict):
        return fact_graph
    return parsed


def _evidence_item_text(item: dict[str, Any]) -> str:
    return _first_text(item, "content", "quote", "text", "summary")


def _participant_scope(item: dict[str, Any]) -> str:
    scope = _clean_text(item.get("participant_scope") or item.get("customer_scope")).lower()
    if scope:
        return scope
    participant = _clean_text(item.get("participant") or item.get("participant_label") or item.get("speaker"))
    if participant.startswith("同行客户"):
        return "other_customer"
    if participant.startswith("主咨询客户"):
        return "primary_customer"
    if participant.startswith("陪同"):
        return "companion_or_family"
    return ""


_FACT_GRAPH_PARTICIPANT_LIST_KEYS = (
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
)


def _participant_label(item: dict[str, Any]) -> str:
    label = _clean_text(item.get("participant") or item.get("participant_label"))
    if label:
        return _normalize_participant_display(label)
    scope = _participant_scope(item)
    if scope == "primary_customer":
        return "主咨询客户"
    if scope == "other_customer":
        return "同行客户"
    if scope == "companion_or_family":
        return "陪同人员"
    return ""


def _participant_key(scope: str, label: str) -> str:
    return f"{scope or 'unknown'}::{label or 'unknown'}"


def _customer_participant_from_item(item: dict[str, Any]) -> dict[str, str] | None:
    scope = _participant_scope(item)
    label = _participant_label(item)
    if scope not in {"primary_customer", "other_customer"}:
        return None
    if not label:
        label = "主咨询客户" if scope == "primary_customer" else "同行客户"
    return {"key": _participant_key(scope, label), "scope": scope, "label": label}


def _collect_customer_participants(fact_graph: dict[str, Any]) -> list[dict[str, str]]:
    participants: list[dict[str, str]] = []
    seen: set[str] = set()
    for key in _FACT_GRAPH_PARTICIPANT_LIST_KEYS:
        for item in _as_list(fact_graph.get(key)):
            if not isinstance(item, dict):
                continue
            participant = _customer_participant_from_item(item)
            if not participant or participant["key"] in seen:
                continue
            seen.add(participant["key"])
            participants.append(participant)
    deal_outcome = fact_graph.get("deal_outcome")
    if isinstance(deal_outcome, dict):
        participant = _customer_participant_from_item(deal_outcome)
        if participant and participant["key"] not in seen:
            participants.append(participant)
    participants.sort(key=lambda item: (0 if item["scope"] == "primary_customer" else 1, item["label"]))
    return participants


def _item_matches_participant(item: dict[str, Any], participant: dict[str, str], *, include_shared: bool = False) -> bool:
    scope = _participant_scope(item)
    label = _participant_label(item)
    if not scope:
        return include_shared
    if scope in {"unknown", "staff", "companion_or_family"}:
        return include_shared
    if scope != participant["scope"]:
        return False
    if not label:
        return True
    return _participant_key(scope, label) == participant["key"]


def _filter_fact_graph_for_participant(
    fact_graph: dict[str, Any],
    participant: dict[str, str],
    *,
    include_shared: bool = False,
) -> dict[str, Any]:
    filtered = dict(fact_graph)
    for key in _FACT_GRAPH_PARTICIPANT_LIST_KEYS:
        filtered[key] = [
            item
            for item in _as_list(fact_graph.get(key))
            if isinstance(item, dict) and _item_matches_participant(item, participant, include_shared=include_shared)
        ]
    deal_outcome = fact_graph.get("deal_outcome")
    if isinstance(deal_outcome, dict) and _item_matches_participant(deal_outcome, participant, include_shared=include_shared):
        filtered["deal_outcome"] = deal_outcome
    else:
        filtered["deal_outcome"] = {}
    filtered["_participant"] = participant
    return filtered


def _default_fact_graph_for_single_result(fact_graph: dict[str, Any]) -> dict[str, Any]:
    participants = _collect_customer_participants(fact_graph)
    if not participants:
        return fact_graph
    primary = next((item for item in participants if item["scope"] == "primary_customer"), participants[0])
    return _filter_fact_graph_for_participant(fact_graph, primary, include_shared=True)


def _fact_graph_has_business_facts(fact_graph: dict[str, Any]) -> bool:
    return any(_as_list(fact_graph.get(key)) for key in _FACT_GRAPH_PARTICIPANT_LIST_KEYS) or bool(
        _as_dict(fact_graph.get("deal_outcome"))
    )


def _build_participant_analysis_results(fact_graph: dict[str, Any], raw: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for participant in _collect_customer_participants(fact_graph):
        scoped = _filter_fact_graph_for_participant(fact_graph, participant, include_shared=False)
        if not _fact_graph_has_business_facts(scoped):
            continue
        analysis_result = _build_analysis_result_from_fact_graph(
            scoped,
            raw,
            include_participant_results=False,
            allow_raw_augmentation=False,
        )
        results.append(
            {
                "participant_key": participant["key"],
                "participant": participant["label"],
                "participant_scope": participant["scope"],
                "analysis_result": analysis_result,
                "fact_counts": {
                    key: len(_as_list(scoped.get(key)))
                    for key in _FACT_GRAPH_PARTICIPANT_LIST_KEYS
                },
            }
        )
    return results


def _evidence_item_to_fact_item(item: dict[str, Any], *, item_id: str, source_id: str | None = None) -> dict[str, Any]:
    fact_item = {
        "id": item_id,
        "content": _evidence_item_text(item),
        "body_part": _first_text(item, "body_part", "body_part_name"),
        "evidence_ids": [source_id or _clean_text(item.get("id"))],
        "evidence": [_first_text(item, "quote", "content", "text")],
        "confidence": item.get("confidence"),
        "participant": _participant_label(item) or None,
        "participant_scope": _participant_scope(item) or None,
    }
    return {key: value for key, value in fact_item.items() if value not in (None, "", [], {})}


def _merge_profile_facts_from_evidence_graph(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(fact_graph)
    profile_facts = [item for item in _as_list(merged.get("profile_facts")) if isinstance(item, dict)]
    seen = {
        (
            _normalize_key(_first_text(item, "category", "tag_category", "type")),
            _normalize_key(_first_text(item, "value", "tag_value", "content", "text")),
            _participant_key(_participant_scope(item), _participant_label(item)),
        )
        for item in profile_facts
    }

    def add_fact(category: str, value: str, item: dict[str, Any], source_id: str) -> None:
        category = _clean_text(category)
        value = _clean_text(value)
        if not category or not value:
            return
        key = (
            _normalize_key(category),
            _normalize_key(value),
            _participant_key(_participant_scope(item), _participant_label(item)),
        )
        if key in seen:
            return
        seen.add(key)
        profile_facts.append(
            {
                "id": f"P{len(profile_facts) + 1}",
                "category": category,
                "value": value,
                "content": value,
                "evidence_ids": [_clean_text(item.get("id")) or source_id],
                "evidence": [_first_text(item, "quote", "content", "text")],
                "confidence": item.get("confidence"),
                "participant": _participant_label(item) or None,
                "participant_scope": _participant_scope(item) or None,
            }
        )

    for item in _as_list(evidence_graph.get("profile_evidence")):
        if not isinstance(item, dict):
            continue
        add_fact(
            _first_text(item, "category", "tag_category", "type"),
            _first_text(item, "value", "tag_value", "content", "text"),
            item,
            "profile_evidence",
        )

    for item in _as_list(evidence_graph.get("budget_evidence")):
        if isinstance(item, dict):
            text = _evidence_item_text(item)
            if text:
                add_fact("本次消费预算", text, item, "budget_evidence")

    for item in _as_list(evidence_graph.get("concern_evidence")):
        if not isinstance(item, dict):
            continue
        text = _profile_item_text(item)
        if _has_any_text(text, ("价格", "预算", "太贵", "贵了", "有点贵", "便宜", "承受", "优惠")):
            add_fact("价格敏感度", "高", item, "concern_evidence")
        if _has_any_text(text, ("疼痛", "痛感", "怕痛", "怕疼", "痛不痛")):
            add_fact("疼痛耐受度", "低" if _has_any_text(text, ("怕痛", "怕疼", "受不了")) else "中", item, "concern_evidence")

    if profile_facts:
        merged["profile_facts"] = profile_facts
    return merged


def _repair_empty_fact_graph_from_evidence_graph(
    fact_graph: dict[str, Any],
    evidence_graph: dict[str, Any],
) -> dict[str, Any]:
    """Recover strong current-plan facts if judgment became over-conservative.

    This is intentionally generic: it only fires when the evidence stage already
    found high-confidence current-customer diagnosis plus current recommendations.
    """
    if any(_as_list(fact_graph.get(key)) for key in ("demands", "doctor_diagnoses", "recommendations")):
        return fact_graph

    demand_evidence = [
        item
        for item in _as_list(evidence_graph.get("customer_demand_evidence"))
        if isinstance(item, dict)
        and not _item_has_low_confidence_fragment(item)
    ]
    diagnoses = [
        item
        for item in _as_list(evidence_graph.get("diagnosis_evidence"))
        if isinstance(item, dict)
        and not _item_has_low_confidence_fragment(item)
        and (_item_confidence(item) or 0.0) >= 0.70
    ]
    current_recommendations = [
        item
        for item in _as_list(evidence_graph.get("recommendation_evidence"))
        if isinstance(item, dict)
        and not _item_has_low_confidence_fragment(item)
        and _clean_text(item.get("relation_to_current_demand")) in {"current_main_plan", "possible_current_plan"}
        and (_item_confidence(item) or 0.0) >= 0.70
    ]
    if not current_recommendations or not (demand_evidence or diagnoses):
        return fact_graph

    has_customer_confirmation = any(
        _clean_text(item.get("customer_response"))
        or _has_any_text(_first_text(item, "quote", "content"), ("对", "可以", "好", "多少钱", "价格", "做", "先"))
        for item in current_recommendations
    )
    if not demand_evidence and not has_customer_confirmation:
        return fact_graph

    repaired = dict(fact_graph)
    source_for_demand = demand_evidence[0] if demand_evidence else diagnoses[0]
    demand_body_part = _first_text(source_for_demand, "body_part", "body_part_name")
    demand_content = _evidence_item_text(source_for_demand)
    if not demand_evidence and demand_body_part:
        demand_content = f"改善{demand_body_part}相关问题"
    elif not demand_content:
        demand_content = "改善当前面诊评估指出的问题"
    repaired["demands"] = [
        {
            "id": "D1",
            "content": demand_content,
            "body_part": demand_body_part,
            "source": "staff_restated_confirmed",
            "evidence_ids": [_clean_text(source_for_demand.get("id"))],
            "evidence": [_first_text(source_for_demand, "quote", "content")],
            "confidence": min(0.82, float(_item_confidence(source_for_demand) or 0.75)),
            "participant": _participant_label(source_for_demand) or None,
            "participant_scope": _participant_scope(source_for_demand) or None,
        }
    ]
    repaired["doctor_diagnoses"] = [
        _evidence_item_to_fact_item(item, item_id=f"X{index}", source_id=_clean_text(item.get("id")))
        for index, item in enumerate(diagnoses[:5], start=1)
    ]
    rec_items: list[dict[str, Any]] = []
    for index, item in enumerate(current_recommendations[:8], start=1):
        rec = {
            "id": f"R{index}",
            "content": _evidence_item_text(item),
            "related_demand_ids": ["D1"],
            "body_part": _first_text(item, "body_part", "body_part_name"),
            "brand": _first_text(item, "brand"),
            "material": _first_text(item, "material"),
            "dosage": _first_text(item, "dosage"),
            "price": _first_text(item, "price"),
            "course_or_frequency": _first_text(item, "course_or_frequency"),
            "treatment_steps": _as_list(item.get("treatment_steps")),
            "implementation_notes": _first_text(item, "implementation_notes"),
            "customer_response": _first_text(item, "customer_response") or "未明确回应",
            "evidence_ids": [_clean_text(item.get("id"))],
            "evidence": [_first_text(item, "quote", "content")],
            "confidence": item.get("confidence"),
            "participant": _participant_label(item) or None,
            "participant_scope": _participant_scope(item) or None,
        }
        rec_items.append({key: value for key, value in rec.items() if value not in (None, "", [], {})})
    repaired["recommendations"] = rec_items
    repaired["seed_recommendations"] = [
        _evidence_item_to_fact_item(item, item_id=f"S{index}", source_id=_clean_text(item.get("id")))
        for index, item in enumerate(_as_list(evidence_graph.get("recommendation_evidence")), start=1)
        if isinstance(item, dict)
        and _clean_text(item.get("relation_to_current_demand")) == "planting_or_later"
        and not _item_has_low_confidence_fragment(item)
    ]
    repaired["concerns"] = [
        _evidence_item_to_fact_item(item, item_id=f"C{index}", source_id=_clean_text(item.get("id")))
        for index, item in enumerate(_as_list(evidence_graph.get("concern_evidence")), start=1)
        if isinstance(item, dict)
        and not _item_has_low_confidence_fragment(item)
    ]
    uncertainties = _as_list(repaired.get("uncertainties"))
    uncertainties.append("客户完整主诉表达较少，已基于现场诊断、当前方案及客户确认反应生成保守主诉")
    repaired["uncertainties"] = uncertainties
    return repaired


def _compact_fact_graph_for_indications(fact_graph: dict[str, Any]) -> dict[str, Any]:
    return {
        "demands": _as_list(fact_graph.get("demands")),
        "doctor_diagnoses": _as_list(fact_graph.get("doctor_diagnoses")),
        "recommendations": _as_list(fact_graph.get("recommendations")),
        "seed_recommendations": _as_list(fact_graph.get("seed_recommendations")),
        "preliminary_indication_candidates": _as_list(fact_graph.get("indication_candidates")),
        "concerns": _as_list(fact_graph.get("concerns")),
        "uncertainties": _as_list(fact_graph.get("uncertainties")),
    }


def _extract_indication_adjudication(parsed: dict[str, Any]) -> dict[str, Any]:
    if isinstance(parsed.get("final_indications"), list):
        return parsed
    payload = parsed.get("indication_adjudication")
    if isinstance(payload, dict):
        return payload
    return {"final_indications": [], "rejected_indications": []}


def _apply_indication_adjudication(
    fact_graph: dict[str, Any],
    adjudication: dict[str, Any],
    candidate_indications: list[dict[str, str]],
) -> dict[str, Any]:
    candidate_by_standardized = {
        _clean_text(item.get("standardized_indication")): item
        for item in candidate_indications
        if _clean_text(item.get("standardized_indication"))
    }
    final_candidates: list[dict[str, Any]] = []
    support_context = _all_fact_text(_compact_fact_graph_for_indications(fact_graph))

    for item in _as_list(adjudication.get("final_indications")):
        if not isinstance(item, dict):
            continue
        standardized = _clean_text(item.get("standardized_indication"))
        if standardized not in candidate_by_standardized:
            continue
        if _item_has_low_confidence_fragment(item):
            continue
        row = _parse_standardized_indication(standardized)
        if row is None:
            continue
        item_context = (
            json.dumps(item, ensure_ascii=False)
            + "\n"
            + json.dumps(candidate_by_standardized.get(standardized), ensure_ascii=False)
            + "\n"
            + support_context
        )
        if not _indication_supported_by_context(row, item_context):
            continue
        final_candidates.append(
            {
                "standardized_indication": standardized,
                "indication_name": row["indication_name"],
                "body_part_name": row["body_part_name"],
                "evidence": _evidence_text(item.get("supporting_evidence")) or _clean_text(item.get("reason")),
                "confidence": item.get("confidence"),
                "adjudication_reason": _clean_text(item.get("reason")),
                "participant": _participant_label(item) or None,
                "participant_scope": _participant_scope(item) or None,
            }
        )

    updated = dict(fact_graph)
    updated["indication_candidates"] = final_candidates
    updated["_indication_adjudicated"] = True
    updated["_indication_adjudication"] = {
        "selected_count": len(final_candidates),
        "rejected_indications": _as_list(adjudication.get("rejected_indications")),
    }
    return updated


def _extract_evidence_graph(parsed: dict[str, Any]) -> dict[str, Any]:
    evidence_graph = parsed.get("evidence_graph")
    if isinstance(evidence_graph, dict):
        return evidence_graph
    return parsed


def _extract_correction_patch(parsed: dict[str, Any]) -> dict[str, Any]:
    patch = parsed.get("correction_patch")
    if isinstance(patch, dict):
        return patch
    return parsed if isinstance(parsed, dict) else {}


def _raw_speaker_key_from_segment(segment: dict[str, Any]) -> str:
    for key in (
        "asr_original_speaker_id",
        "asr_original_speaker",
        "speaker_id",
        "speaker_label",
        "speaker_display_label",
        "speaker",
        "role",
    ):
        value = _clean_text(segment.get(key))
        if value:
            return value
    return ""


def _build_line_speaker_metadata(dialogue: str, raw: dict[str, Any]) -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}
    raw_segments = [
        item
        for item in extract_transcript_segments(raw)
        if _clean_text(item.get("text") or item.get("content"))
    ]
    dialogue_lines = [line for line in dialogue.splitlines() if line.strip()]
    for index, _line in enumerate(dialogue_lines, start=1):
        line_id = f"L{index:04d}"
        segment = raw_segments[index - 1] if index - 1 < len(raw_segments) else {}
        if not isinstance(segment, dict):
            segment = {}
        speaker_key = _raw_speaker_key_from_segment(segment)
        metadata[line_id] = {
            "asr_speaker": speaker_key,
            "speaker": _clean_text(segment.get("speaker")),
            "speaker_id": _clean_text(segment.get("speaker_id")),
            "speaker_label": _clean_text(segment.get("speaker_label") or segment.get("speaker_display_label")),
            "role": _clean_text(segment.get("role") or segment.get("speaker_role") or segment.get("speaker_business_role")),
        }
    return metadata


def _number_dialogue_lines(dialogue: str, line_metadata: dict[str, dict[str, str]] | None = None) -> tuple[str, dict[str, str]]:
    numbered: list[str] = []
    line_map: dict[str, str] = {}
    index = 1
    for raw_line in dialogue.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line_id = f"L{index:04d}"
        line_map[line_id] = line
        metadata = (line_metadata or {}).get(line_id) or {}
        asr_speaker = _clean_text(metadata.get("asr_speaker"))
        role = _clean_text(metadata.get("role"))
        meta_parts = []
        if asr_speaker:
            meta_parts.append(f"asr_speaker={asr_speaker}")
        if role:
            meta_parts.append(f"current_role={role}")
        meta_text = f" [{'; '.join(meta_parts)}]" if meta_parts else ""
        numbered.append(f"{line_id}{meta_text}: {line}")
        index += 1
    return "\n".join(numbered), line_map


def _normalize_business_role_display(role: str) -> str:
    normalized = _clean_text(role)
    if not normalized:
        return ""
    role_map = {
        "customer": "客户",
        "client": "客户",
        "顾客": "客户",
        "客户": "客户",
        "companion": "陪同人员",
        "family": "陪同人员",
        "陪同": "陪同人员",
        "陪同人员": "陪同人员",
        "consultant": "咨询师",
        "advisor": "咨询师",
        "consultant_staff": "咨询师",
        "咨询": "咨询师",
        "咨询师": "咨询师",
        "doctor": "医生",
        "physician": "医生",
        "医生": "医生",
        "expert_assistant": "专家助理",
        "doctor_assistant": "专家助理",
        "assistant": "专家助理",
        "专家助理": "专家助理",
        "医生助理": "专家助理",
        "frontdesk": "前台",
        "front_desk": "前台",
        "reception": "前台",
        "前台": "前台",
        "staff_peer": "员工",
        "staff": "员工",
        "colleague": "员工",
        "员工": "员工",
        "other": "其他",
        "unknown": "其他",
        "其他": "其他",
    }
    lowered = normalized.lower()
    return role_map.get(lowered) or role_map.get(normalized) or normalized


def _normalize_participant_display(label: str, fallback_role: str = "") -> str:
    normalized = _clean_text(label)
    if not normalized:
        return _normalize_business_role_display(fallback_role)
    label_map = {
        "primary_customer": "主咨询客户",
        "main_customer": "主咨询客户",
        "target_customer": "主咨询客户",
        "customer_primary": "主咨询客户",
        "主客户": "主咨询客户",
        "主顾客": "主咨询客户",
        "主咨询客户": "主咨询客户",
        "other_customer": "同行客户",
        "secondary_customer": "同行客户",
        "同行客户": "同行客户",
        "customer_a": "同行客户A",
        "other_customer_a": "同行客户A",
        "同行客户a": "同行客户A",
        "同行客户A": "同行客户A",
        "客户A": "同行客户A",
        "customer_b": "同行客户B",
        "other_customer_b": "同行客户B",
        "同行客户b": "同行客户B",
        "同行客户B": "同行客户B",
        "客户B": "同行客户B",
        "companion": "陪同人员",
        "family": "陪同人员",
        "companion_or_family": "陪同人员",
        "陪同": "陪同人员",
        "家属": "陪同人员",
        "陪同人员": "陪同人员",
    }
    lowered = normalized.lower()
    return label_map.get(lowered) or label_map.get(normalized) or _normalize_business_role_display(normalized)


def _display_speaker_from_mapping(item: dict[str, Any], *, role_key: str = "role") -> str:
    role = _clean_text(item.get(role_key) or item.get("corrected_speaker") or item.get("speaker_role"))
    for key in ("participant_label", "display_speaker", "speaker_label", "label"):
        label = _clean_text(item.get(key))
        if label:
            return _normalize_participant_display(label, role)
    return _normalize_business_role_display(role)


def _replace_line_speaker_role(line: str, role: str) -> str:
    normalized_role = _normalize_participant_display(role)
    if not normalized_role:
        return line
    match = re.match(r"^(\[[^\]]+\]\s*)([^:：\n]+)([:：]\s*)(.*)$", line)
    if not match:
        return line
    return f"{match.group(1)}{normalized_role}{match.group(3)}{match.group(4)}"


def _apply_correction_patch(
    numbered_dialogue: str,
    line_map: dict[str, str],
    patch: dict[str, Any],
    line_metadata: dict[str, dict[str, str]] | None = None,
) -> tuple[str, dict[str, Any]]:
    lines = dict(line_map)
    applied_speaker_maps: list[dict[str, Any]] = []
    applied_speaker: list[dict[str, Any]] = []
    applied_terms: list[dict[str, Any]] = []
    skipped_terms: list[dict[str, Any]] = []

    for item in _as_list(patch.get("term_corrections")):
        if not isinstance(item, dict):
            continue
        line_id = _clean_text(item.get("line_id"))
        original = _clean_text(item.get("original"))
        corrected = _clean_text(item.get("corrected"))
        confidence = item.get("confidence")
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            confidence_value = 0.0
        if not line_id or not original or not corrected or line_id not in lines or confidence_value < 0.75:
            skipped_terms.append(item)
            continue
        if original not in lines[line_id]:
            skipped_terms.append(item)
            continue
        lines[line_id] = lines[line_id].replace(original, corrected)
        applied_terms.append(item)

    role_map: dict[str, dict[str, Any]] = {}
    for item in _as_list(patch.get("speaker_role_map")):
        if not isinstance(item, dict):
            continue
        asr_speaker = _clean_text(item.get("asr_speaker") or item.get("speaker") or item.get("speaker_id"))
        role = _clean_text(item.get("role") or item.get("corrected_role") or item.get("speaker_role"))
        confidence = item.get("confidence")
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            confidence_value = 0.0
        if not asr_speaker or not role or confidence_value < 0.65:
            continue
        participant_label = _clean_text(item.get("participant_label") or item.get("display_speaker") or item.get("speaker_label"))
        customer_scope = _clean_text(item.get("customer_scope") or item.get("participant_scope"))
        role_map[asr_speaker] = {
            **item,
            "asr_speaker": asr_speaker,
            "role": role,
            "participant_label": participant_label,
            "customer_scope": customer_scope,
            "display_speaker": _display_speaker_from_mapping({**item, "role": role}),
            "confidence": confidence_value,
        }

    if role_map:
        for line_id, metadata in (line_metadata or {}).items():
            asr_speaker = _clean_text(metadata.get("asr_speaker"))
            mapped = role_map.get(asr_speaker)
            if not mapped or line_id not in lines:
                continue
            before = lines[line_id]
            lines[line_id] = _replace_line_speaker_role(lines[line_id], _clean_text(mapped.get("display_speaker") or mapped.get("role")))
            if before != lines[line_id]:
                applied_speaker_maps.append(
                    {
                        "line_id": line_id,
                        "asr_speaker": asr_speaker,
                        "role": mapped.get("role"),
                        "participant_label": mapped.get("participant_label"),
                        "customer_scope": mapped.get("customer_scope"),
                        "display_speaker": mapped.get("display_speaker"),
                        "reason": _clean_text(mapped.get("reason")),
                    }
                )

    speaker_notes: dict[str, str] = {}
    for item in _as_list(patch.get("speaker_corrections")):
        if not isinstance(item, dict):
            continue
        line_id = _clean_text(item.get("line_id"))
        speaker = _clean_text(item.get("corrected_speaker"))
        confidence = item.get("confidence")
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            confidence_value = 0.0
        if not line_id or line_id not in lines or not speaker or confidence_value < 0.65:
            continue
        reason = _clean_text(item.get("reason"))
        display_speaker = _display_speaker_from_mapping({**item, "role": speaker})
        lines[line_id] = _replace_line_speaker_role(lines[line_id], display_speaker)
        scope = _clean_text(item.get("customer_scope") or item.get("participant_scope"))
        note_parts = [f"speaker_correction={display_speaker}"]
        if scope:
            note_parts.append(f"scope={scope}")
        if reason:
            note_parts.append(f"reason={reason}")
        speaker_notes[line_id] = f" <{'; '.join(note_parts)}>"
        applied_speaker.append(item)

    merged_lines = []
    for line_id in line_map:
        merged_lines.append(f"{line_id}: {lines[line_id]}{speaker_notes.get(line_id, '')}")

    metadata = {
        "applied_speaker_role_map": applied_speaker_maps,
        "applied_speaker_corrections": applied_speaker,
        "applied_term_corrections": applied_terms,
        "skipped_term_corrections": skipped_terms,
        "uncertain_notes": _as_list(patch.get("uncertain_notes")),
        "input_line_count": len(line_map),
        "speaker_role_map": list(role_map.values()),
        "speaker_line_metadata": line_metadata or {},
    }
    return "\n".join(merged_lines), metadata


def _build_preprocess_context(dialogue: str, staff_context: dict[str, Any] | None) -> dict[str, Any]:
    candidate_rows = _candidate_indications_from_text(dialogue, max_items=25)
    term_hints: list[str] = []
    for term in (
        "眶外C线",
        "眉弓线",
        "颞区",
        "额颞",
        "外轮廓线",
        "内轮廓线",
        "鼻基底",
        "泪沟",
        "瑞德喜",
        "艾维岚",
        "艾拉斯提",
        "贝丽菲尔",
        "双美",
        "玻尿酸",
        "胶原蛋白",
        "肉毒",
        "除皱针",
        "溶解酶",
        "副乳",
        "富贵包",
        "身体吸脂",
        "超脂",
    ):
        if term in dialogue:
            term_hints.append(term)
    return {
        "staff_context": staff_context or {},
        "dialogue_chars": len(dialogue),
        "term_hints": term_hints,
        "candidate_indication_hints": _format_candidate_indications(candidate_rows[:12]),
    }


def _estimate_payload_chars(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def analyze_transcript_staged(
    path: str | Path,
    *,
    system_prompt: str | None = None,
    staff_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the experimental staged analysis for one transcript file."""
    dialogue, raw = prepare_transcript(path)
    if not dialogue.strip():
        raise ValueError(f"Transcript file {Path(path).name} has no valid dialogue")

    staff_text = _format_staff_context(staff_context)
    preprocess_context = _build_preprocess_context(dialogue, staff_context)
    line_speaker_metadata = _build_line_speaker_metadata(dialogue, raw)
    numbered_dialogue, numbered_line_map = _number_dialogue_lines(dialogue, line_speaker_metadata)
    correction_user_prompt = _CORRECTION_USER_TEMPLATE.format(
        staff_context=staff_text,
        preprocess_context=json.dumps(preprocess_context, ensure_ascii=False, indent=2),
        numbered_dialogue=numbered_dialogue,
    )
    logger.info(
        "staged correction prompt chars system=%d user=%d",
        len(_CORRECTION_SYSTEM_PROMPT),
        len(correction_user_prompt),
    )
    correction_parsed = _call_json(_CORRECTION_SYSTEM_PROMPT, correction_user_prompt, max_tokens=6000)
    correction_patch = _extract_correction_patch(correction_parsed)
    corrected_dialogue, correction_metadata = _apply_correction_patch(
        numbered_dialogue,
        numbered_line_map,
        correction_patch,
        line_speaker_metadata,
    )
    postprocessed = {
        "chunks": [],
        "turns": [],
        "turn_count": 0,
        "role_summary": "",
        "quality_notes": ["full_asr_rewrite_skipped; lightweight_patch_applied"],
    }
    postprocessed_dialogue = corrected_dialogue
    postprocess_stats = {"postprocess_chunks": 0, "used_original_chunk_count": 0}

    evidence_user_prompt = _EVIDENCE_USER_TEMPLATE.format(
        staff_context=staff_text,
        preprocess_context=json.dumps(preprocess_context, ensure_ascii=False, indent=2),
        dialogue=corrected_dialogue,
    )
    logger.info(
        "staged evidence prompt chars system=%d user=%d",
        len(_EVIDENCE_SYSTEM_PROMPT),
        len(evidence_user_prompt),
    )
    evidence_parsed = _call_json(_EVIDENCE_SYSTEM_PROMPT, evidence_user_prompt, max_tokens=12000)
    evidence_graph = _extract_evidence_graph(evidence_parsed)

    evidence_text = json.dumps(evidence_graph, ensure_ascii=False)
    candidate_rows = _candidate_indications_from_text(f"{evidence_text}\n{corrected_dialogue}", max_items=40)
    candidate_indications = _format_candidate_indications(candidate_rows)
    judgment_user_prompt = _JUDGMENT_USER_TEMPLATE.format(
        evidence_graph=json.dumps(evidence_graph, ensure_ascii=False, indent=2),
        candidate_indications=json.dumps(candidate_indications, ensure_ascii=False, indent=2),
    )
    logger.info(
        "staged judgment prompt chars system=%d user=%d candidates=%d",
        len(_JUDGMENT_SYSTEM_PROMPT),
        len(judgment_user_prompt),
        len(candidate_indications),
    )
    judgment_parsed = _call_json(_JUDGMENT_SYSTEM_PROMPT, judgment_user_prompt, max_tokens=12000)
    fact_graph = _extract_fact_graph(judgment_parsed)
    fact_graph = _repair_empty_fact_graph_from_evidence_graph(fact_graph, evidence_graph)
    fact_graph = _merge_profile_facts_from_evidence_graph(fact_graph, evidence_graph)
    indication_user_prompt = _INDICATION_ADJUDICATION_USER_TEMPLATE.format(
        fact_graph=json.dumps(_compact_fact_graph_for_indications(fact_graph), ensure_ascii=False, indent=2),
        candidate_indications=json.dumps(candidate_indications, ensure_ascii=False, indent=2),
    )
    logger.info(
        "staged indication adjudication prompt chars system=%d user=%d candidates=%d",
        len(_INDICATION_ADJUDICATION_SYSTEM_PROMPT),
        len(indication_user_prompt),
        len(candidate_indications),
    )
    try:
        indication_parsed = _call_json(_INDICATION_ADJUDICATION_SYSTEM_PROMPT, indication_user_prompt, max_tokens=6000)
        indication_adjudication = _extract_indication_adjudication(indication_parsed)
        fact_graph = _apply_indication_adjudication(fact_graph, indication_adjudication, candidate_indications)
    except Exception as exc:
        logger.warning("staged indication adjudication failed, using preliminary indications: %s", exc)
        indication_adjudication = {
            "final_indications": [],
            "rejected_indications": [],
            "error": str(exc),
        }
    finalized_result = _build_analysis_result_from_fact_graph(fact_graph, raw)

    postprocess_calls = int(postprocess_stats.get("postprocess_chunks") or 0)
    return {
        "pipeline": PIPELINE_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "llm_call_plan": {
            "model": STAGED_LLM_MODEL,
            "correction_patch": 1,
            "postprocess": postprocess_calls,
            "evidence_extraction": 1,
            "structured_judgment": 1,
            "indication_adjudication": 1,
            "fact_graph_to_analysis_result": 0,
            "total_logical_calls": postprocess_calls + 4,
        },
        "input_stats": {
            "dialogue_chars": len(dialogue),
            "postprocessed_dialogue_chars": len(postprocessed_dialogue),
            "raw_payload_chars": _estimate_payload_chars(raw),
            "postprocess_chunks": postprocess_calls,
            "used_original_chunk_count": int(postprocess_stats.get("used_original_chunk_count") or 0),
            "used_original_dialogue_after_postprocess": bool(postprocess_stats.get("used_original_chunk_count")),
            "numbered_dialogue_lines": len(numbered_line_map),
            "applied_speaker_correction_count": len(correction_metadata.get("applied_speaker_corrections", [])),
            "applied_term_correction_count": len(correction_metadata.get("applied_term_corrections", [])),
        },
        "postprocess": postprocessed,
        "postprocessed_dialogue": postprocessed_dialogue,
        "preprocess_context": preprocess_context,
        "correction_patch": correction_patch,
        "correction_metadata": correction_metadata,
        "evidence_graph": evidence_graph,
        "candidate_indications": candidate_indications,
        "indication_adjudication": indication_adjudication,
        "fact_graph": fact_graph,
        "analysis_result": finalized_result,
    }

