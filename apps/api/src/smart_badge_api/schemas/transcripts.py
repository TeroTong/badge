from pydantic import BaseModel


class TranscriptOut(BaseModel):
    id: str
    recording_id: str
    recording_file_name: str | None = None
    asr_provider: str
    asr_task_id: str | None
    status: str
    full_text: str | None
    utterances: list | None
    duration_ms: int | None
    error_message: str | None
    created_at: str
    completed_at: str | None

    model_config = {"from_attributes": True}
