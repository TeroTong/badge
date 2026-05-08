import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.analysis.schemas import AnalysisResult
from smart_badge_api.api.analysis_access import (
    build_analysis_artifact_access,
    ensure_task_visible,
    task_is_visible,
)
from smart_badge_api.api.analysis_normalization import normalize_task_detail
from smart_badge_api.api.deps import get_current_user
from smart_badge_api.core.config import get_settings
from smart_badge_api.core.permissions import permission_role_level
from smart_badge_api.db.models import AnalysisTask
from smart_badge_api.db.models import User
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.tasks import TaskDetailOut, TaskOut
from smart_badge_api.schemas.pagination import PaginatedResponse, make_page_response
from smart_badge_api.task_queue import dispatch_analysis_task

router = APIRouter(prefix="/tasks", tags=["分析任务"])
logger = logging.getLogger(__name__)


def _require_min_role(user: User, min_role: str, *, detail: str) -> None:
    if permission_role_level(user.role) < permission_role_level(min_role):
        raise HTTPException(403, detail)


def _upload_dir() -> Path:
    p = get_settings().upload_path
    p.mkdir(parents=True, exist_ok=True)
    return p


def _results_dir() -> Path:
    p = get_settings().results_path
    p.mkdir(parents=True, exist_ok=True)
    return p


def _storage_path(filename: str) -> Path:
    safe_name = Path(filename).name
    upload_dir = _upload_dir()
    candidate = upload_dir / safe_name
    if not candidate.exists():
        return candidate

    stem = Path(safe_name).stem or "upload"
    suffix = Path(safe_name).suffix
    return upload_dir / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"


def _task_result_path(task: AnalysisTask) -> Path:
    stored = get_settings().resolve_file_path(task.file_path)
    return _results_dir() / f"{stored.stem}.result.json"


def _delete_managed_file(path: Path, managed_dir: Path) -> None:
    try:
        resolved = path.resolve()
        root = managed_dir.resolve()
        if resolved.exists() and resolved.is_file() and resolved.is_relative_to(root):
            resolved.unlink()
    except Exception as exc:
        logger.warning("Failed to delete managed file %s: %s", path, exc)


@router.get("", response_model=PaginatedResponse[TaskOut])
async def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 默认只取最近 90 天的任务，避免一次性加载全部历史任务记录拖慢接口。
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    stmt = (
        select(AnalysisTask)
        .where(AnalysisTask.created_at >= cutoff)
        .order_by(AnalysisTask.created_at.desc())
    )
    tasks = list((await db.execute(stmt)).scalars().all())
    if not tasks:
        return make_page_response([], 0, page, page_size)
    access = await build_analysis_artifact_access(db, current_user)
    items = [task for task in tasks if task_is_visible(task, access)]
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    return make_page_response(items[start:end], total, page, page_size)


@router.get("/{task_id}", response_model=TaskDetailOut)
async def get_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await db.get(AnalysisTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    await ensure_task_visible(task, db, current_user, not_found_detail="任务不存在")
    return normalize_task_detail(task)


@router.post("/upload", response_model=TaskOut, status_code=201)
async def upload_and_analyze(
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """上传转写 JSON 文件并触发后台分析。"""
    _require_min_role(current_user, "system_admin", detail="权限不足，需要至少 system_admin 角色")

    safe_name = Path(file.filename or "").name
    if not safe_name or Path(safe_name).suffix.lower() != ".json":
        raise HTTPException(400, "请上传 .json 格式的转写文件")

    content = await file.read()

    # Validate JSON structure
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(400, "文件不是有效的 JSON")

    if "payload" not in data or "transcribeResult" not in data.get("payload", {}):
        raise HTTPException(400, "JSON 格式不正确，缺少 payload.transcribeResult")

    # Save to upload directory
    upload_path = _storage_path(safe_name)
    upload_path.write_bytes(content)

    # Create task record
    task = AnalysisTask(
        file_name=safe_name,
        file_path=get_settings().make_relative_path(upload_path.resolve()),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    try:
        ran_inline = await dispatch_analysis_task(task.id)
    except Exception as exc:
        task.status = "failed"
        task.error_message = f"任务分发失败：{exc}"
        await db.commit()
        raise HTTPException(500, "任务已创建，但分发失败") from exc

    if ran_inline:
        await db.refresh(task)

    return task


@router.post("/{task_id}/retry", response_model=TaskOut)
async def retry_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """重试失败或已完成的分析任务（重跑时会用最新 prompt）。"""
    _require_min_role(current_user, "system_admin", detail="权限不足，需要至少 system_admin 角色")
    task = await db.get(AnalysisTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    await ensure_task_visible(task, db, current_user, not_found_detail="任务不存在")
    if task.status not in ("failed", "done"):
        raise HTTPException(400, "只能重试失败或已完成的任务")

    old_status = task.status
    result = await db.execute(
        update(AnalysisTask)
        .where(AnalysisTask.id == task_id, AnalysisTask.status == old_status)
        .values(
            status="pending",
            progress=0,
            error_message=None,
            result=None,
            overall_score=None,
            completed_at=None,
        )
    )
    if result.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, "任务状态已变化，请刷新后重试")

    await db.commit()
    task = await db.get(AnalysisTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    await db.refresh(task)

    try:
        ran_inline = await dispatch_analysis_task(task.id)
    except Exception as exc:
        task.status = "failed"
        task.error_message = f"任务分发失败：{exc}"
        await db.commit()
        raise HTTPException(500, "任务已重置，但分发失败") from exc

    if ran_inline:
        await db.refresh(task)

    return task


class BatchRerunResponse(BaseModel):
    total: int
    dispatched: int
    failed: int
    details: list[str]


@router.post("/batch-rerun", response_model=BatchRerunResponse)
async def batch_rerun(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """将所有已完成的分析任务重置为 pending 并重新分发（使用最新 prompt）。"""
    _require_min_role(current_user, "system_admin", detail="权限不足，需要至少 system_admin 角色")

    tasks = (
        await db.execute(
            select(AnalysisTask)
            .where(AnalysisTask.status == "done", AnalysisTask.result.is_not(None))
        )
    ).scalars().all()

    if not tasks:
        return BatchRerunResponse(total=0, dispatched=0, failed=0, details=["没有需要重跑的任务"])

    # Reset all to pending
    task_ids = [t.id for t in tasks]
    await db.execute(
        update(AnalysisTask)
        .where(AnalysisTask.id.in_(task_ids))
        .values(
            status="pending",
            progress=0,
            error_message=None,
            result=None,
            overall_score=None,
            completed_at=None,
        )
    )
    await db.commit()

    dispatched = 0
    failed = 0
    details: list[str] = []

    for tid in task_ids:
        try:
            await dispatch_analysis_task(tid)
            dispatched += 1
            details.append(f"已分发：{tid}")
        except Exception as exc:
            failed += 1
            details.append(f"分发失败：{tid} — {exc}")

    return BatchRerunResponse(
        total=len(task_ids),
        dispatched=dispatched,
        failed=failed,
        details=details,
    )


@router.delete("/{task_id}", status_code=204)
async def delete_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_min_role(current_user, "system_admin", detail="权限不足，需要至少 system_admin 角色")

    task = await db.get(AnalysisTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    await ensure_task_visible(task, db, current_user, not_found_detail="任务不存在")
    file_path = get_settings().resolve_file_path(task.file_path)
    result_path = _task_result_path(task)
    await db.delete(task)
    await db.commit()
    _delete_managed_file(file_path, _upload_dir())
    _delete_managed_file(result_path, _results_dir())


class BatchImportRequest(BaseModel):
    raw_dir: str
    results_dir: str


class BatchImportResponse(BaseModel):
    imported: int
    skipped: int
    details: list[str]


@router.post("/batch-import", response_model=BatchImportResponse)
async def batch_import(
    body: BatchImportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量导入已有的原始文件 + 分析结果，创建为已完成的任务。"""
    _require_min_role(current_user, "system_admin", detail="权限不足，需要至少 system_admin 角色")

    raw_path = Path(body.raw_dir).resolve()
    results_path = Path(body.results_dir).resolve()

    if not raw_path.is_dir():
        raise HTTPException(400, f"原始文件目录不存在：{body.raw_dir}")
    if not results_path.is_dir():
        raise HTTPException(400, f"结果目录不存在：{body.results_dir}")

    # 已有任务的 file_name 集合
    existing = await db.execute(select(AnalysisTask.file_name))
    existing_names = {row[0] for row in existing.fetchall()}

    imported = 0
    skipped = 0
    details: list[str] = []

    for raw_file in sorted(raw_path.glob("*.json")):
        if raw_file.name in existing_names:
            skipped += 1
            details.append(f"跳过（已存在）：{raw_file.name}")
            continue

        # 查找对应结果文件
        result_file = results_path / (raw_file.stem + ".result.json")
        if not result_file.exists():
            skipped += 1
            details.append(f"跳过（无结果）：{raw_file.name}")
            continue

        # 读取原始文件元数据
        try:
            raw_data = json.loads(raw_file.read_text(encoding="utf-8"))
            segments = raw_data.get("payload", {}).get("transcribeResult", [])
            segment_count = len(segments)
            duration_ms = 0
            if segments:
                duration_ms = max(s.get("end", 0) for s in segments) - min(s.get("begin", 0) for s in segments)
        except Exception:
            segment_count = 0
            duration_ms = 0

        # 读取结果
        try:
            result_dict = json.loads(result_file.read_text(encoding="utf-8"))
            validated_result = AnalysisResult.model_validate(result_dict)
            result_dict = validated_result.model_dump()
            overall_score = None
        except Exception:
            skipped += 1
            details.append(f"跳过（结果无法解析或 schema 不合法）：{raw_file.name}")
            continue

        task = AnalysisTask(
            file_name=raw_file.name,
            file_path=str(raw_file.resolve()),
            status="done",
            progress=100,
            result=result_dict,
            duration_ms=duration_ms,
            segment_count=segment_count,
            overall_score=overall_score,
            completed_at=datetime.now(timezone.utc),
        )
        db.add(task)
        imported += 1
        details.append(f"导入：{raw_file.name}（已按新结构导入）")

    await db.commit()
    return BatchImportResponse(imported=imported, skipped=skipped, details=details)
