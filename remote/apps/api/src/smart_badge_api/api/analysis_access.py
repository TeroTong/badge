from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.data_scope import build_permission_scope, recording_scope_condition
from smart_badge_api.db.models import AnalysisTask, Recording, User


@dataclass(slots=True)
class AnalysisArtifactAccess:
    allowed_recording_ids: set[str]
    allow_unscoped_tasks: bool


def extract_recording_id_from_analysis_file_name(file_name: str) -> str | None:
    if not file_name.startswith("recording_") or not file_name.endswith(".json"):
        return None
    recording_id = file_name.removeprefix("recording_").removesuffix(".json").strip()
    return recording_id or None


async def build_analysis_artifact_access(
    db: AsyncSession,
    current_user: User,
) -> AnalysisArtifactAccess:
    scope = await build_permission_scope(current_user)
    rows = await db.execute(
        select(Recording.id).where(
            recording_scope_condition(scope),
            Recording.status != "filtered",
        )
    )
    return AnalysisArtifactAccess(
        allowed_recording_ids=set(rows.scalars().all()),
        allow_unscoped_tasks=False,
    )


def task_is_visible(task: AnalysisTask | str, access: AnalysisArtifactAccess) -> bool:
    file_name = task.file_name if isinstance(task, AnalysisTask) else task
    recording_id = extract_recording_id_from_analysis_file_name(file_name)
    if recording_id is None:
        return access.allow_unscoped_tasks
    return recording_id in access.allowed_recording_ids


async def ensure_task_visible(
    task: AnalysisTask,
    db: AsyncSession,
    current_user: User,
    *,
    not_found_detail: str,
) -> AnalysisArtifactAccess:
    recording_id = extract_recording_id_from_analysis_file_name(task.file_name)
    if recording_id is None:
        raise HTTPException(status_code=404, detail=not_found_detail)

    scope = await build_permission_scope(current_user)
    visible_recording_id = (
        await db.execute(
            select(Recording.id).where(
                Recording.id == recording_id,
                recording_scope_condition(scope),
                Recording.status != "filtered",
            )
        )
    ).scalar_one_or_none()
    if visible_recording_id is None:
        raise HTTPException(status_code=404, detail=not_found_detail)
    return AnalysisArtifactAccess(
        allowed_recording_ids={recording_id},
        allow_unscoped_tasks=False,
    )
