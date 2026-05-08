from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.api.audit import append_audit_log
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.asr.speaker_voiceprint import (
    enroll_staff_voiceprint_for_speaker,
    get_staff_voiceprint_review,
    list_staff_voiceprint_reviews,
    resolve_staff_voiceprint_review,
)
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import Recording, User
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.voiceprints import (
    VoiceprintReviewOut,
    VoiceprintReviewResolveIn,
    VoiceprintReviewResolveOut,
)

router = APIRouter(prefix="/voiceprints", tags=["声纹管理"])


def _to_review_out(item: dict) -> VoiceprintReviewOut:
    return VoiceprintReviewOut(
        id=str(item.get("id") or ""),
        status=str(item.get("status") or ""),
        source_id=item.get("source_id"),
        recording_id=item.get("recording_id"),
        staff_id=item.get("staff_id"),
        staff_name=item.get("staff_name"),
        staff_role=item.get("staff_role"),
        speaker_id=item.get("speaker_id"),
        speaker_role=item.get("speaker_role"),
        speaker_role_sources=list(item.get("speaker_role_sources") or []),
        speaker_duration_ms=item.get("speaker_duration_ms"),
        speaker_voiceprint_similarity=item.get("speaker_voiceprint_similarity"),
        matched_staff_id=item.get("matched_staff_id"),
        matched_staff_name=item.get("matched_staff_name"),
        preview_text=str(item.get("preview_text") or ""),
        reasons=list(item.get("reasons") or []),
        created_at=item.get("created_at"),
        updated_at=item.get("updated_at"),
        resolved_at=item.get("resolved_at"),
        resolved_by=item.get("resolved_by"),
        resolution_note=item.get("resolution_note"),
    )


@router.get("/reviews", response_model=list[VoiceprintReviewOut])
async def list_voiceprint_reviews(
    status: str = Query("pending"),
    staff_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
):
    del current_user
    rows = list_staff_voiceprint_reviews(status=status)
    if staff_id:
        rows = [row for row in rows if str(row.get("staff_id") or "") == staff_id]
    return [_to_review_out(row) for row in rows]


@router.post("/reviews/{review_id}/approve", response_model=VoiceprintReviewResolveOut)
async def approve_voiceprint_review(
    review_id: str,
    payload: VoiceprintReviewResolveIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = get_staff_voiceprint_review(review_id)
    if not item:
        raise HTTPException(404, "Voiceprint review not found")
    if str(item.get("status") or "").lower() != "pending":
        raise HTTPException(400, "Voiceprint review is not pending")

    recording_id = str(item.get("recording_id") or item.get("source_id") or "").strip()
    if not recording_id:
        raise HTTPException(400, "Voiceprint review is missing recording id")

    recording = await db.get(
        Recording,
        recording_id,
        options=[selectinload(Recording.staff), selectinload(Recording.transcript)],
    )
    if not recording or not recording.transcript:
        raise HTTPException(404, "Recording or transcript not found")

    speaker_id = (payload.speaker_id or item.get("speaker_id") or "").strip()
    if not speaker_id:
        raise HTTPException(400, "Voiceprint review is missing speaker id")

    utterances = recording.transcript.utterances or []
    if not isinstance(utterances, list) or not utterances:
        raise HTTPException(400, "Transcript utterances are unavailable")

    audio_path = get_settings().resolve_file_path(recording.file_path)
    enrolled = enroll_staff_voiceprint_for_speaker(
        audio_path,
        utterances,
        speaker_id=speaker_id,
        staff_id=str(item.get("staff_id") or recording.staff_id or ""),
        staff_name=str(item.get("staff_name") or (recording.staff.name if recording.staff else "") or ""),
        staff_role=str(item.get("staff_role") or (recording.staff.role if recording.staff else "") or ""),
        source_id=recording.id,
    )
    if not enrolled:
        raise HTTPException(400, "Voiceprint review approval failed")

    resolved = resolve_staff_voiceprint_review(
        review_id,
        status="approved",
        resolved_by=current_user.display_name or current_user.username,
        note=payload.note,
        extra_updates={"speaker_id": speaker_id},
    )
    if not resolved:
        raise HTTPException(404, "Voiceprint review not found")

    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="声纹管理",
        action_name="批准声纹复核",
        content=f"批准声纹样本 staff={resolved.get('staff_name') or resolved.get('staff_id')} recording={recording.id} speaker={speaker_id}",
    )
    return VoiceprintReviewResolveOut(enrolled=True, item=_to_review_out(resolved))


@router.post("/reviews/{review_id}/reject", response_model=VoiceprintReviewResolveOut)
async def reject_voiceprint_review(
    review_id: str,
    payload: VoiceprintReviewResolveIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = get_staff_voiceprint_review(review_id)
    if not item:
        raise HTTPException(404, "Voiceprint review not found")
    if str(item.get("status") or "").lower() != "pending":
        raise HTTPException(400, "Voiceprint review is not pending")

    resolved = resolve_staff_voiceprint_review(
        review_id,
        status="rejected",
        resolved_by=current_user.display_name or current_user.username,
        note=payload.note,
    )
    if not resolved:
        raise HTTPException(404, "Voiceprint review not found")

    await append_audit_log(
        db,
        operator_name=current_user.display_name or current_user.username,
        ip_address=request.client.host if request.client else "",
        module_name="声纹管理",
        action_name="拒绝声纹复核",
        content=f"拒绝声纹样本 staff={resolved.get('staff_name') or resolved.get('staff_id')} review={review_id}",
    )
    return VoiceprintReviewResolveOut(enrolled=False, item=_to_review_out(resolved))
