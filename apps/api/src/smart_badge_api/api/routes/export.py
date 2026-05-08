"""导出相关的 API 路由。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.api.analysis_access import (
    build_analysis_artifact_access,
    ensure_task_visible,
    task_is_visible,
)
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.db.models import AnalysisTask
from smart_badge_api.db.models import User
from smart_badge_api.db.session import get_db
from smart_badge_api.export.excel import export_task_excel, export_tasks_batch_excel

router = APIRouter(prefix="/export", tags=["导出"])

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _content_disposition(filename: str) -> str:
    """生成兼容中文的 Content-Disposition 头 (RFC 5987)。"""
    encoded = quote(filename, safe="")
    return f"attachment; filename*=UTF-8''{encoded}"


@router.get("/tasks/{task_id}")
async def export_single_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """导出单个任务分析报告为 Excel。"""
    task = await db.get(AnalysisTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    await ensure_task_visible(task, db, current_user, not_found_detail="任务不存在")
    if task.status != "done":
        raise HTTPException(400, "只能导出已完成的任务")

    data = export_task_excel(task)
    filename = f"{task.file_name.rsplit('.', 1)[0]}_分析报告.xlsx"
    return Response(
        content=data,
        media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@router.get("/tasks")
async def export_batch_tasks(
    status: str | None = Query(None, description="按状态筛选: done/failed/pending/running"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量导出任务汇总为 Excel。"""
    stmt = select(AnalysisTask).order_by(AnalysisTask.created_at.desc())
    if status:
        stmt = stmt.where(AnalysisTask.status == status)
    result = await db.execute(stmt)
    tasks = list(result.scalars().all())
    if not tasks:
        raise HTTPException(404, "没有符合条件的任务")
    access = await build_analysis_artifact_access(db, current_user)
    tasks = [task for task in tasks if task_is_visible(task, access)]
    if not tasks:
        raise HTTPException(404, "没有符合条件的任务")

    data = export_tasks_batch_excel(tasks)
    return Response(
        content=data,
        media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": _content_disposition("任务汇总报告.xlsx")},
    )
