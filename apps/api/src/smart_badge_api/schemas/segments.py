from pydantic import BaseModel, field_validator

VALID_SPEAKER_LABELS = {"consultant", "doctor", "customer", "unknown"}


class SegmentUpdate(BaseModel):
    visit_id: str | None = None
    speaker_label: str | None = None

    @field_validator("speaker_label")
    @classmethod
    def _validate_speaker(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_SPEAKER_LABELS:
            raise ValueError(f"speaker_label 必须是 {VALID_SPEAKER_LABELS} 之一")
        return v


class SegmentOut(BaseModel):
    id: str
    recording_id: str
    visit_id: str | None
    segment_index: int
    begin_ms: int
    end_ms: int
    speaker_label: str | None
    text: str | None
    status: str
    has_analysis: bool
    created_at: str

    model_config = {"from_attributes": True}
