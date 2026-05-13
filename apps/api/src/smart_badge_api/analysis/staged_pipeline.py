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
from smart_badge_api.analysis.transcript import prepare_transcript
from smart_badge_api.api.analysis_normalization import normalize_analysis_result

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
    玻尿酸, 胶原蛋白, 肉毒, 除皱针, 溶解酶.

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
8. Do not turn skin problems on the nose area into nose surgery. If the transcript
   says pore/acne/oil/blackhead/skin texture on nose tip or nose wing, it is a skin
   concern, not rhinoplasty or nose comprehensive surgery unless explicit nose
   surgery/injection contouring is discussed.
9. Do not turn concerns or expectations into demands. "wants natural result",
   "worries about unevenness", "afraid of risks", and "needs to consider" are
   concerns/decision factors unless they are tied to a concrete body-area goal.
10. Preserve specialty terms exactly when supported by evidence, especially:
    眶外C线, 眉弓线, 颞区, 额颞, 外轮廓线, 内轮廓线, 鼻基底, 泪沟,
    瑞德喜, 艾维岚, 艾拉斯提, 贝丽菲尔, 双美胶原蛋白.
11. If a plan contains dosage, brand, material, price, course, or sequence, put
    those details into the structured fields instead of leaving the recommendation
    as a short generic phrase.
12. For recommendations vs seed_recommendations, judge by relation to the
    customer's current demand. A staged sequence that completes the current goal
    remains a recommendation even if it says "later/afterwards"; a plan belongs
    to seed_recommendations only when it is outside the current goal, clearly
    lower priority, maintenance, next-visit, or explicitly "not recommended now".
13. Indication candidates are preliminary and must be high precision. Do not add
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
5. A professional explanation about anatomy, treatment steps, dosage, risks,
   case photos, or recommended plans is usually consultant/doctor/staff, not
   customer, unless surrounding turns clearly show the customer is speaking.
6. A person self-identifying as expert assistant / doctor assistant / dean
   assistant should not be labeled doctor.
7. Customer speech usually contains personal goals, feelings, hesitation,
   budget/price questions, consent/refusal, or follow-up questions.
8. Term corrections must be local string replacements within one line. Correct
   only when the replacement is strongly supported by nearby context.

Common high-value terms:
眶外C线, 眉弓线, 颞区, 额颞, 外轮廓线, 内轮廓线, 鼻基底, 泪沟,
瑞德喜, 艾维岚, 艾拉斯提, 贝丽菲尔, 双美胶原蛋白, 玻尿酸, 胶原蛋白,
肉毒, 除皱针, 溶解酶, 热玛吉, 超声炮.

Return JSON only:
{
  "correction_patch": {
    "speaker_corrections": [
      {
        "line_id": "L0001",
        "corrected_speaker": "customer|companion|consultant|doctor|expert_assistant|frontdesk|staff_peer|other",
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

Return JSON only:
{
  "evidence_graph": {
    "customer_demand_evidence": [
      {
        "id": "E_D1",
        "content": "",
        "body_part": "",
        "speaker": "customer|companion|staff_restated_confirmed",
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
        "brand": "",
        "material": "",
        "dosage": "",
        "price": "",
        "course_or_frequency": "",
        "treatment_steps": [],
        "implementation_notes": "",
        "customer_response": "",
        "relation_to_current_demand": "current_main_plan|possible_current_plan|planting_or_later|unclear",
        "evidence_turn_ids": [],
        "quote": "",
        "confidence": 0.0
      }
    ],
    "concern_evidence": [],
    "budget_evidence": [],
    "medical_history_evidence": [],
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
   If the customer's explicit wording is sparse but the evidence_graph contains
   high-confidence diagnosis of the current customer, a current_main_plan
   recommendation, and customer confirmation/acceptance/price inquiry, create a
   conservative staff_restated_confirmed demand instead of returning empty.
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

Return JSON only:
{
  "fact_graph": {
    "demands": [
      {
        "id": "D1",
        "content": "",
        "body_part": "",
        "source": "customer_direct|customer_confirmed|staff_restated_confirmed",
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
12. If the transcript is internal chat/order/payment discussion and has no valid
   main-customer demand/diagnosis/recommendation, return an empty final list.

Return JSON only:
{
  "final_indications": [
    {
      "standardized_indication": "Y2|微创|SYZ2001|塑美|BW2019|眶外C线（小O）",
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
    if _has_any_text(compact, _LOW_CONFIDENCE_FRAGMENT_TERMS):
        return True
    body_count = _body_fragment_count(compact)
    has_action = _has_any_text(compact, _BUSINESS_ACTION_TERMS)
    has_problem = _has_any_text(compact, _BUSINESS_PROBLEM_TERMS)
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
    if _has_any_text(text, ("痤疮", "痘痘", "粉刺", "炎症痘", "痘印", "痘坑")):
        return True
    if "闭口" not in text:
        return False
    if _has_any_text(text, ("闭口时", "闭上", "闭嘴", "闭合", "闭口的状态", "完全闭")):
        return False
    return _has_any_text(text, ("皮肤", "毛孔", "粉刺", "痘", "刷酸", "水光", "黑头", "出油"))


def _has_pore_context(text: str) -> bool:
    return _has_any_text(text, ("毛孔", "黑头", "出油", "皮肤粗糙", "肤质", "肤感", "点阵", "光子", "水光"))


def _has_wrinkle_context(text: str) -> bool:
    return _has_any_text(text, ("除皱针", "皱纹", "动态纹", "鱼尾纹", "抬头纹", "川字纹", "法令纹", "核桃纹", "颈纹"))


def _has_non_wrinkle_botox_context(text: str) -> bool:
    return _has_any_text(text, ("咬肌", "瘦脸", "轮廓线", "下巴肌肉", "颏肌", "肌肉放松", "放松下拉肌", "斜方肌"))


def _has_fill_context(text: str) -> bool:
    return _has_any_text(text, ("填充", "玻尿酸", "胶原", "瑞德喜", "艾维岚", "艾拉斯提", "贝丽菲尔", "双美", "支撑", "打", "支"))


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
        "贵",
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
    if "眼袋" in text:
        add("眼袋", "眼部")
    if _has_wrinkle_context(text):
        add("面部除皱", "面部")
    if any(term in text for term in ("鼻基底", "苹果肌", "面部填充", "玻尿酸填充", "胶原填充", "瑞德喜")):
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
    if any(term in text for term in ("下颌轮廓", "下颌缘", "下颌角", "颈阔肌", "轮廓线")):
        add("塑美", "下颌轮廓线")
    if any(term in text for term in ("额区", "上庭窄", "上庭偏窄")):
        add("塑美", "额区")
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
    if indication_name == "面部除皱" and not _has_wrinkle_context(context):
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
    if name == "面部除皱":
        return _has_wrinkle_context(context)
    if name == "面部填充":
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
                "下颌轮廓线": ("下颌轮廓", "下颌缘", "下颌角", "颈阔肌", "轮廓线"),
                "眉弓线": ("眉弓", "眉弓线", "眉尾"),
                "鼻额衔接线": ("鼻额", "山根", "鼻额衔接"),
                "鼻中轴线": ("鼻中轴", "鼻梁", "鼻背", "山根"),
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

    def append(row: dict[str, str], evidence: str = "", support_context: str = "") -> None:
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
        append(row, item_evidence, item_context)

    if not adjudicated:
        for fallback in _map_common_indication_from_text(current_context):
            if not _should_drop_indication(fallback, current_context):
                append(fallback, support_context=current_context)
    return selected


def _dedupe_demands(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for item in items:
        content = _first_text(item, "content", "demand", "text")
        if not content:
            continue
        key = _normalize_key(content)
        if not key:
            continue
        replaced = False
        for existing_key, existing in list(by_key.items()):
            existing_content = _first_text(existing, "content", "demand", "text")
            if key in existing_key or existing_key in key:
                if len(content) > len(existing_content):
                    by_key.pop(existing_key)
                    by_key[key] = item
                replaced = True
                break
        if not replaced:
            by_key[key] = item
    return list(by_key.values())


def _demand_priority_map(demands: list[dict[str, Any]]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, item in enumerate(demands, start=1):
        item_id = _clean_text(item.get("id")) or f"D{index}"
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
        content = _first_text(item, "content", "demand", "text")
        if not content:
            continue
        if _item_has_low_confidence_fragment(item) or _looks_like_low_confidence_fragment(content):
            continue
        if _is_staff_only_demand(item):
            continue
        if _is_concern_like_text(content):
            continue
        raw_demands.append(item)
    demands = _dedupe_demands(raw_demands)
    priority_map = _demand_priority_map(demands)
    result_items: list[dict[str, Any]] = []
    for index, item in enumerate(demands, start=1):
        result_items.append(
            {
                "priority": index,
                "demand": _first_text(item, "content", "demand", "text"),
                "body_part": _first_text(item, "body_part", "body_part_name") or None,
                "evidence": _evidence_text(item.get("evidence")),
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


def _build_recommendation_item(item: dict[str, Any], demand_map: dict[str, int]) -> dict[str, Any] | None:
    recommendation = _first_text(item, "content", "recommendation", "plan", "text")
    if not recommendation:
        return None
    if _item_has_low_confidence_fragment(item) or _looks_like_low_confidence_fragment(recommendation):
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
    return {
        "recommendation": recommendation,
        "product_or_solution": _first_text(item, "product_or_solution", "product", "solution", "brand_or_product") or None,
        "body_part": _first_text(item, "body_part", "body_part_name") or None,
        "brand": _first_text(item, "brand", "brand_or_product") or None,
        "material": _first_text(item, "material", "brand_or_material") or None,
        "dosage": _first_text(item, "dosage", "dosage_or_quantity", "dosage_or_course") or None,
        "price": _first_text(item, "price") or None,
        "course_or_frequency": _first_text(item, "course_or_frequency", "course", "frequency", "dosage_or_course") or None,
        "treatment_steps": [_clean_text(value) for value in steps if _clean_text(value)],
        "implementation_notes": notes or None,
        "demand_priority": priorities,
        "evidence": _evidence_text(item.get("evidence")),
        "customer_response": _first_text(item, "customer_response", "response", "acceptance") or "未明确回应",
    }


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
        item_text = json.dumps(item, ensure_ascii=False)
        if not seed and _is_seed_like_text(item_text) and not _linked_priorities(item, demand_map):
            continue
        mapped = _build_recommendation_item(item, demand_map)
        if mapped:
            if seed:
                mapped["demand_priority"] = []
            key = _normalize_key(mapped.get("recommendation"))
            if key and all(_normalize_key(existing.get("recommendation")) != key for existing in result_items):
                result_items.append(mapped)
    return {
        "summary": "；".join(item["recommendation"] for item in result_items),
        "items": result_items,
    }


def _build_concerns(fact_graph: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for item in _as_list(fact_graph.get("concerns")):
        if not isinstance(item, dict):
            continue
        content = _first_text(item, "content", "concern", "text")
        if not content:
            continue
        items.append(
            {
                "type": _first_text(item, "type", "category") or "顾虑",
                "content": content,
                "evidence": _evidence_text(item.get("evidence")),
            }
        )
    return {"inference_note": None, "summary": "；".join(item["content"] for item in items), "items": items}


def _build_customer_profile(fact_graph: dict[str, Any]) -> dict[str, Any]:
    tags: list[dict[str, Any]] = []
    for item in _as_list(fact_graph.get("medical_history")):
        if not isinstance(item, dict):
            continue
        content = _first_text(item, "content", "project", "material", "text")
        if not content:
            continue
        tags.append(
            {
                "category": _first_text(item, "category") or "历史治疗项目",
                "value": content,
                "weight_level": None,
                "evidence": _evidence_text(item.get("evidence")),
            }
        )
    return {"inference_note": None, "age": None, "age_evidence": None, "tags": tags}


def _build_consumption_intent(fact_graph: dict[str, Any]) -> dict[str, Any]:
    decision_factors: list[str] = []
    evidence: list[str] = []
    for key in ("deal_factors", "budget_facts"):
        for item in _as_list(fact_graph.get(key)):
            if not isinstance(item, dict):
                continue
            content = _first_text(item, "content", "factor", "text")
            if content:
                decision_factors.append(content)
            ev = _evidence_text(item.get("evidence"))
            if ev:
                evidence.append(ev)
    return {"budget": None, "decision_factors": decision_factors, "evidence": evidence}


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


def _build_analysis_result_from_fact_graph(fact_graph: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    fact_graph = _stabilize_fact_graph(fact_graph)
    primary_demands, demand_source_items, demand_map = _build_demands(fact_graph)
    indications = _build_standardized_indications(fact_graph)
    recommendations = _build_recommendations(fact_graph, demand_map, seed=False)
    seed_recommendations = _build_recommendations(fact_graph, demand_map, seed=True)
    concerns = _build_concerns(fact_graph)
    profile = _build_customer_profile(fact_graph)
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


def _evidence_item_to_fact_item(item: dict[str, Any], *, item_id: str, source_id: str | None = None) -> dict[str, Any]:
    fact_item = {
        "id": item_id,
        "content": _evidence_item_text(item),
        "body_part": _first_text(item, "body_part", "body_part_name"),
        "evidence_ids": [source_id or _clean_text(item.get("id"))],
        "evidence": [_first_text(item, "quote", "content", "text")],
        "confidence": item.get("confidence"),
    }
    return {key: value for key, value in fact_item.items() if value not in (None, "", [], {})}


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
        if isinstance(item, dict) and not _item_has_low_confidence_fragment(item)
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
        if isinstance(item, dict) and not _item_has_low_confidence_fragment(item)
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


def _number_dialogue_lines(dialogue: str) -> tuple[str, dict[str, str]]:
    numbered: list[str] = []
    line_map: dict[str, str] = {}
    index = 1
    for raw_line in dialogue.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line_id = f"L{index:04d}"
        line_map[line_id] = line
        numbered.append(f"{line_id}: {line}")
        index += 1
    return "\n".join(numbered), line_map


def _apply_correction_patch(numbered_dialogue: str, line_map: dict[str, str], patch: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    lines = dict(line_map)
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
        speaker_notes[line_id] = f" <speaker_correction={speaker}; reason={reason}>"
        applied_speaker.append(item)

    merged_lines = []
    for line_id in line_map:
        merged_lines.append(f"{line_id}: {lines[line_id]}{speaker_notes.get(line_id, '')}")

    metadata = {
        "applied_speaker_corrections": applied_speaker,
        "applied_term_corrections": applied_terms,
        "skipped_term_corrections": skipped_terms,
        "uncertain_notes": _as_list(patch.get("uncertain_notes")),
        "input_line_count": len(line_map),
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
    numbered_dialogue, numbered_line_map = _number_dialogue_lines(dialogue)
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

