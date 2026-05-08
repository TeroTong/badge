from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import AnalysisTask, Transcript
from smart_badge_api.task_queue import dispatch_analysis_task

_RAW_SPEAKER_PATTERN = re.compile(r"^speaker[_-]?\d+$", re.IGNORECASE)

_ANALYSIS_ROLE_ALIASES = {
    "consultant": "consultant",
    "advisor": "consultant",
    "客服": "frontdesk",
    "前台": "frontdesk",
    "frontdesk": "frontdesk",
    "reception": "frontdesk",
    "doctor": "doctor",
    "医生": "doctor",
    "customer": "customer",
    "client": "customer",
    "客户": "customer",
    "患者": "customer",
    "badge_owner": "badge_owner",
    "工牌本人": "badge_owner",
    "staff_peer": "staff_peer",
    "员工同事": "staff_peer",
    "primary_customer": "primary_customer",
    "主客户": "primary_customer",
    "visitor_companion": "visitor_companion",
    "同行人": "visitor_companion",
    "visitor": "visitor_companion",
    "访客": "visitor_companion",
}

_ANALYSIS_ROLE_LABELS = {
    "consultant": "咨询师",
    "frontdesk": "前台",
    "doctor": "医生",
    "customer": "客户",
    "badge_owner": "工牌本人",
    "staff_peer": "员工同事",
    "primary_customer": "主客户",
    "visitor_companion": "同行人",
    "unknown": "其他在场人员",
}


def ensure_analysis_input_dir() -> Path:
    path = get_settings().upload_path / "analysis_input"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _normalize_analysis_role(value: object) -> str:
    text = _clean_text(value)
    if not text:
        return "unknown"
    normalized = _ANALYSIS_ROLE_ALIASES.get(text)
    if normalized:
        return normalized
    return _ANALYSIS_ROLE_ALIASES.get(text.lower(), "unknown")


def _pick_analysis_role(utterance: dict) -> str:
    for field in ("speaker_business_role", "speaker_role", "speaker"):
        role = _normalize_analysis_role(utterance.get(field))
        if role != "unknown":
            return role
    return "unknown"


def _is_raw_speaker(value: object) -> bool:
    return bool(_RAW_SPEAKER_PATTERN.match(_clean_text(value)))


def _build_analysis_speaker_label(utterance: dict, role: str) -> str:
    display_label = _clean_text(utterance.get("speaker_display_label"))
    if display_label:
        return display_label

    speaker_id = _clean_text(utterance.get("speaker_id"))
    raw_speaker = _clean_text(utterance.get("speaker"))
    fallback_label = _ANALYSIS_ROLE_LABELS.get(role, _ANALYSIS_ROLE_LABELS["unknown"])

    if speaker_id and _is_raw_speaker(speaker_id):
        return f"{fallback_label}（{speaker_id}）"
    if raw_speaker and _is_raw_speaker(raw_speaker):
        return f"{fallback_label}（{raw_speaker}）"
    if raw_speaker and not _normalize_analysis_role(raw_speaker) == "unknown":
        return _ANALYSIS_ROLE_LABELS.get(_normalize_analysis_role(raw_speaker), fallback_label)
    return fallback_label


def _infer_owner_staff_name(utterances: list[dict]) -> str | None:
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        if _normalize_analysis_role(utterance.get("speaker_business_role")) != "badge_owner":
            continue
        display_label = _clean_text(utterance.get("speaker_display_label"))
        if not display_label:
            continue
        name = display_label.split("（", 1)[0].strip()
        if name:
            return name
    return None


def refine_utterances_for_analysis(
    utterances: list[dict],
    *,
    staff_id: str | None = None,
    staff_name: str | None = None,
    staff_role: str | None = None,
) -> list[dict]:
    """Re-run local role heuristics before building LLM analysis input.

    Staging overrides and imported ASR payloads may already contain business
    roles, but mixed turns such as "customer answer + consultant explanation"
    can remain inside one staff utterance.  Running the resolver on a copy keeps
    the stored transcript safe while giving the analysis pipeline cleaner turns.
    """
    copied = [dict(item) if isinstance(item, dict) else item for item in utterances]
    try:
        from smart_badge_api.asr.speaker_role_resolver import resolve_speaker_roles

        return resolve_speaker_roles(
            copied,
            staff_id=staff_id,
            staff_name=staff_name or _infer_owner_staff_name(copied),
            staff_role=staff_role,
            respect_speaker_diarization=True,
            split_mixed_turns=True,
        )
    except Exception:
        return copied


def build_analysis_segment(utterance: dict) -> dict | None:
    if not isinstance(utterance, dict):
        return None

    text = _clean_text(utterance.get("text"))
    if not text:
        return None

    begin_ms = int(utterance.get("begin_ms") or 0)
    end_ms = int(utterance.get("end_ms") or begin_ms)
    if end_ms < begin_ms:
        end_ms = begin_ms

    role = _pick_analysis_role(utterance)
    speaker_label = _build_analysis_speaker_label(utterance, role)
    speaker_id = _clean_text(utterance.get("speaker_id")) or None

    return {
        "role": role,
        "speaker_label": speaker_label,
        "speaker_id": speaker_id,
        "text": text,
        "begin": begin_ms,
        "end": end_ms,
    }


def build_analysis_payload_from_utterances(
    utterances: list[dict],
    *,
    staff_id: str | None = None,
    staff_name: str | None = None,
    staff_role: str | None = None,
) -> tuple[dict, int, int]:
    segments: list[dict] = []
    for item in refine_utterances_for_analysis(
        utterances,
        staff_id=staff_id,
        staff_name=staff_name,
        staff_role=staff_role,
    ):
        segment = build_analysis_segment(item)
        if segment is None:
            continue
        segments.append(segment)

    segments.sort(key=lambda value: (value["begin"], value["end"]))
    duration_ms = segments[-1]["end"] if segments else 0
    payload = {"payload": {"transcribeResult": segments}}
    return payload, len(segments), duration_ms


def build_analysis_transcript_payload(transcript: Transcript) -> tuple[dict, int, int]:
    utterances = transcript.utterances or []
    return build_analysis_payload_from_utterances(utterances)


async def create_or_dispatch_recording_analysis(
    db: AsyncSession,
    recording_id: str,
    *,
    transcript: Transcript | None = None,
) -> AnalysisTask:
    if transcript is None:
        transcript = (
            await db.execute(select(Transcript).where(Transcript.recording_id == recording_id))
        ).scalar_one_or_none()

    if transcript is None or transcript.status != "completed":
        raise ValueError("Transcript is not ready")

    payload, segment_count, duration_ms = build_analysis_transcript_payload(transcript)
    if segment_count == 0:
        raise ValueError("Transcript has no valid utterances")

    analysis_file_name = f"recording_{recording_id}.json"
    existing = (
        await db.execute(
            select(AnalysisTask)
            .where(
                AnalysisTask.file_name == analysis_file_name,
                AnalysisTask.status.in_(["pending", "running"]),
            )
            .order_by(AnalysisTask.created_at.desc())
        )
    ).scalars().first()
    if existing:
        return existing

    input_path = ensure_analysis_input_dir() / analysis_file_name
    await asyncio.to_thread(
        input_path.write_text,
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    task = AnalysisTask(
        file_name=analysis_file_name,
        file_path=get_settings().make_relative_path(input_path.resolve()),
        segment_count=segment_count,
        duration_ms=duration_ms,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    try:
        ran_inline = await dispatch_analysis_task(task.id)
    except Exception:
        task.status = "failed"
        task.error_message = "Failed to dispatch analysis task"
        await db.commit()
        raise

    if ran_inline:
        await db.refresh(task)
    return task
