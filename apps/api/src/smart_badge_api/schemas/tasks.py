from datetime import datetime

from pydantic import BaseModel


class TaskOut(BaseModel):
    id: str
    file_name: str
    status: str
    progress: int
    error_message: str | None = None
    duration_ms: int | None = None
    segment_count: int | None = None
    overall_score: float | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


class TaskDetailOut(TaskOut):
    result: dict | None = None
