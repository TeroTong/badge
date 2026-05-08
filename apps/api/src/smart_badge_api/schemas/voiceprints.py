from pydantic import BaseModel


class VoiceprintReviewOut(BaseModel):
    id: str
    status: str
    source_id: str | None = None
    recording_id: str | None = None
    staff_id: str | None = None
    staff_name: str | None = None
    staff_role: str | None = None
    speaker_id: str | None = None
    speaker_role: str | None = None
    speaker_role_sources: list[str] = []
    speaker_duration_ms: int | None = None
    speaker_voiceprint_similarity: float | None = None
    matched_staff_id: str | None = None
    matched_staff_name: str | None = None
    preview_text: str = ""
    reasons: list[str] = []
    created_at: str | None = None
    updated_at: str | None = None
    resolved_at: str | None = None
    resolved_by: str | None = None
    resolution_note: str | None = None


class VoiceprintReviewResolveIn(BaseModel):
    speaker_id: str | None = None
    note: str | None = None


class VoiceprintReviewResolveOut(BaseModel):
    enrolled: bool
    item: VoiceprintReviewOut
