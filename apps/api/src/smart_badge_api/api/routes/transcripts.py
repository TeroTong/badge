from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.api.data_scope import build_permission_scope, recording_scope_condition
from smart_badge_api.api.deps import get_current_user, require_system_admin_or_above
from smart_badge_api.asr.service import dispatch_transcription, execute_segmentation
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import AnalysisTask, Recording, Segment, Transcript, User, _new_id
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.pagination import PaginatedResponse, make_page_response
from smart_badge_api.schemas.transcripts import TranscriptOut

router = APIRouter(prefix="/transcripts", tags=["transcripts"])

ALLOWED_TRANSCRIPT_EXTENSIONS = {".txt", ".json", ".jsonl"}
EXTERNAL_ANALYSIS_KEYS = (
    "requirementAnalyzeResult",
    "consultAnalyzeResult",
    "tagsAnalyzeResult",
    "strategyAnalyzeResult",
    "faceAnalyzeResult",
)
ROLE_ALIASES = {
    "consultant": "consultant",
    "advisor": "consultant",
    "sales": "consultant",
    "staff": "consultant",
    "service": "consultant",
    "agent": "consultant",
    "assistant": "consultant",
    "beauty_consultant": "consultant",
    "doctor": "doctor",
    "customer": "customer",
    "client": "customer",
    "patient": "customer",
    "unknown": "unknown",
    "客服": "consultant",
    "美容顾问": "consultant",
    "顾问": "consultant",
    "咨询师": "consultant",
    "销售": "consultant",
    "助理": "consultant",
    "医生": "doctor",
    "客户": "customer",
    "患者": "customer",
    "未知": "unknown",
}


def _load_opts():
    return [selectinload(Transcript.recording)]


def _to_out(transcript: Transcript) -> TranscriptOut:
    return TranscriptOut(
        id=transcript.id,
        recording_id=transcript.recording_id,
        recording_file_name=transcript.recording.file_name if transcript.recording else None,
        asr_provider=transcript.asr_provider,
        asr_task_id=transcript.asr_task_id,
        status=transcript.status,
        full_text=transcript.full_text,
        utterances=transcript.utterances,
        duration_ms=transcript.duration_ms,
        error_message=transcript.error_message,
        created_at=transcript.created_at.isoformat() if transcript.created_at else "",
        completed_at=transcript.completed_at.isoformat() if transcript.completed_at else None,
    )


def _to_out_slim(transcript: Transcript) -> TranscriptOut:
    """List-view variant that drops the heavy full_text/utterances fields."""
    return TranscriptOut(
        id=transcript.id,
        recording_id=transcript.recording_id,
        recording_file_name=transcript.recording.file_name if transcript.recording else None,
        asr_provider=transcript.asr_provider,
        asr_task_id=transcript.asr_task_id,
        status=transcript.status,
        full_text=None,
        utterances=None,
        duration_ms=transcript.duration_ms,
        error_message=transcript.error_message,
        created_at=transcript.created_at.isoformat() if transcript.created_at else "",
        completed_at=transcript.completed_at.isoformat() if transcript.completed_at else None,
    )


def _is_under_allowed_root(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


async def _get_scoped_recording(recording_id: str, db: AsyncSession, current_user: User) -> Recording | None:
    scope = await build_permission_scope(current_user)
    return (
        await db.execute(
            select(Recording).where(
                Recording.id == recording_id,
                recording_scope_condition(scope),
            )
        )
    ).scalar_one_or_none()


async def _get_scoped_transcript(transcript_id: str, db: AsyncSession, current_user: User) -> Transcript | None:
    scope = await build_permission_scope(current_user)
    return (
        await db.execute(
            select(Transcript)
            .join(Recording, Transcript.recording_id == Recording.id)
            .where(
                Transcript.id == transcript_id,
                recording_scope_condition(scope),
            )
            .options(*_load_opts())
        )
    ).scalar_one_or_none()


def _safe_int(value: object, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _normalize_speaker(value: object) -> str:
    raw = str(value or "unknown").strip()
    if not raw:
        return "unknown"
    return ROLE_ALIASES.get(raw.lower(), ROLE_ALIASES.get(raw, "unknown"))


def _estimate_duration_ms(text: str) -> int:
    return max(1500, min(len(text) * 180, 15000))


def _normalize_utterance(item: dict, cursor_ms: int) -> tuple[dict | None, int]:
    text = str(item.get("text") or item.get("content") or "").strip()
    if not text:
        return None, cursor_ms

    begin_ms = _safe_int(item.get("begin_ms"), None)
    if begin_ms is None:
        begin_ms = _safe_int(item.get("begin"), cursor_ms)
    if begin_ms is None:
        begin_ms = cursor_ms

    end_ms = _safe_int(item.get("end_ms"), None)
    if end_ms is None:
        end_ms = _safe_int(item.get("end"), begin_ms + _estimate_duration_ms(text))
    if end_ms is None or end_ms <= begin_ms:
        end_ms = begin_ms + _estimate_duration_ms(text)

    utterance = {
        "speaker": _normalize_speaker(item.get("speaker") or item.get("role") or item.get("speaker_label") or item.get("speakerRole")),
        "text": text,
        "begin_ms": begin_ms,
        "end_ms": end_ms,
    }
    return utterance, end_ms


def _build_plain_text_utterances(text: str) -> tuple[list[dict], str, int]:
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
    if not lines:
        raise ValueError("Transcript text is empty")

    utterances: list[dict] = []
    cursor_ms = 0
    for line in lines:
        duration_ms = _estimate_duration_ms(line)
        utterances.append(
            {
                "speaker": "unknown",
                "text": line,
                "begin_ms": cursor_ms,
                "end_ms": cursor_ms + duration_ms,
            }
        )
        cursor_ms += duration_ms

    return utterances, "\n".join(lines), utterances[-1]["end_ms"]


def _decode_json_document(name: str, text: str) -> object:
    suffix = Path(name).suffix.lower()
    if suffix == ".jsonl":
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            raise ValueError("Transcript file is empty")
        return json.loads(lines[0])
    return json.loads(text)


def _parse_embedded_json(value: object) -> object | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.startswith("{") or stripped.startswith("["):
            return json.loads(stripped)
        return stripped
    return value


def _unique_text(parts: list[str], limit: int = 4) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in parts:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
        if len(output) >= limit:
            break
    return output


def _collect_text_fields(value: object, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if field_name in {"summary", "content"} and value.strip() else []
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.extend(_collect_text_fields(item, field_name))
        return parts
    if isinstance(value, dict):
        parts: list[str] = []
        direct = value.get(field_name)
        if isinstance(direct, str) and direct.strip():
            parts.append(direct.strip())
        elif isinstance(direct, list):
            for item in direct:
                if isinstance(item, (str, int, float)) and str(item).strip():
                    parts.append(str(item).strip())
        for nested_value in value.values():
            if nested_value is direct:
                continue
            parts.extend(_collect_text_fields(nested_value, field_name))
        return parts
    return []


def _summarize_text(value: object, field_name: str) -> str:
    parts = _unique_text(_collect_text_fields(value, field_name))
    if not parts:
        return ""
    return "；".join(parts)


def _build_external_focus_areas(external_analysis: dict) -> list[dict]:
    requirement = external_analysis.get("requirementAnalyzeResult")
    if not isinstance(requirement, dict):
        return []

    summary = requirement.get("summary")
    if isinstance(summary, str) and summary.strip():
        return [
            {
                "area": "需求摘要",
                "surface_need": summary.strip(),
                "deep_need": "",
                "discovery_process": "",
            }
        ]

    if not isinstance(summary, dict):
        return []

    areas: list[dict] = []
    for section_name, section_value in summary.items():
        content = _summarize_text(section_value, "content") or _summarize_text(section_value, "summary")
        evidence = _summarize_text(section_value, "evidence")
        areas.append(
            {
                "area": str(section_name),
                "surface_need": content or "-",
                "deep_need": "",
                "discovery_process": evidence or "-",
            }
        )
    return areas


def _build_external_concerns(external_analysis: dict) -> dict | None:
    strategy = external_analysis.get("strategyAnalyzeResult")
    consult = external_analysis.get("consultAnalyzeResult")

    summary = ""
    if isinstance(strategy, dict):
        raw = strategy.get("strategy", {}).get("key_concerns")
        if isinstance(raw, list):
            summary = "；".join(str(item).strip() for item in raw if str(item).strip())
        elif isinstance(raw, str):
            summary = raw.strip()

    if not summary and isinstance(consult, dict):
        raw = consult.get("summary", {}).get("决策顾虑")
        if isinstance(raw, str):
            summary = raw.strip()
        elif raw is not None:
            summary = _summarize_text(raw, "content") or _summarize_text(raw, "summary")

    if not summary:
        return None

    items = [
        {"type": "顾虑点", "content": part.strip(), "evidence": ""}
        for part in re.split(r"[；;]\s*", summary)
        if part.strip()
    ]
    return {"summary": summary, "items": items[:6]}


def _build_external_profile(external_analysis: dict) -> dict | None:
    tags = external_analysis.get("tagsAnalyzeResult")
    if not isinstance(tags, dict):
        return None

    extracted = tags.get("extracted_data")
    if not isinstance(extracted, list):
        return None

    normalized_tags: list[dict] = []
    for item in extracted:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "标签").strip()
        value = str(
            item.get("sub_tag")
            or item.get("tag")
            or item.get("content")
            or item.get("summary")
            or ""
        ).strip()
        if not category or not value:
            continue
        normalized_tags.append({"category": category, "value": value})

    if not normalized_tags:
        return None
    return {"tags": normalized_tags[:12]}


def _build_external_evaluation(external_analysis: dict) -> dict | None:
    face = external_analysis.get("faceAnalyzeResult")
    if not isinstance(face, dict):
        return None

    overall_summary = face.get("overall_summary")
    analysis_details = face.get("analysis_details")
    dimensions: list[dict] = []

    if isinstance(analysis_details, dict):
        for key in sorted(analysis_details):
            detail = analysis_details.get(key)
            if not isinstance(detail, dict):
                continue
            try:
                score = float(detail.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            dimensions.append(
                {
                    "name": str(detail.get("name") or key),
                    "score": score,
                    "comment": str(detail.get("reasoning") or detail.get("suggestion") or detail.get("evidence") or ""),
                }
            )

    overall_score: float | None = None
    if isinstance(overall_summary, dict):
        try:
            raw_score = overall_summary.get("total_score")
            overall_score = float(raw_score) if raw_score is not None else None
        except (TypeError, ValueError):
            overall_score = None

    if overall_score is None and not dimensions:
        return None

    return {"overall_score": overall_score or 0.0, "dimensions": dimensions}


def _normalize_external_analysis_result(external_analysis: dict) -> dict | None:
    if not external_analysis:
        return None

    result: dict[str, object] = {
        "source": "external_upload",
        "external_analysis": external_analysis,
    }

    focus_areas = _build_external_focus_areas(external_analysis)
    if focus_areas:
        result["customer_demands"] = {"focus_areas": focus_areas}

    concerns = _build_external_concerns(external_analysis)
    if concerns:
        result["customer_concerns"] = concerns

    profile = _build_external_profile(external_analysis)
    if profile:
        result["customer_profile"] = profile

    evaluation = _build_external_evaluation(external_analysis)
    if evaluation:
        result["consultation_evaluation"] = evaluation

    return result


def _extract_external_analysis(data: object) -> dict | None:
    if not isinstance(data, dict):
        return None

    analysis: dict[str, object] = {}
    for key in EXTERNAL_ANALYSIS_KEYS:
        parsed = _parse_embedded_json(data.get(key))
        if parsed is not None:
            analysis[key] = parsed
    return analysis or None


def _build_analysis_input_payload(utterances: list[dict]) -> dict:
    segments: list[dict] = []
    for item in utterances:
        if not isinstance(item, dict):
            continue
        segments.append(
            {
                "role": str(item.get("speaker") or "unknown"),
                "text": str(item.get("text") or ""),
                "begin": int(item.get("begin_ms") or 0),
                "end": int(item.get("end_ms") or 0),
            }
        )
    return {"payload": {"transcribeResult": segments}}


def _build_import_source_key(content: bytes) -> str:
    return f"batch-import:{hashlib.sha256(content).hexdigest()}"


def _infer_recording_file_name(data: object, source_path: Path) -> str:
    if isinstance(data, dict):
        audio_url = str(data.get("audioUrl") or "").strip()
        if audio_url:
            candidate = Path(audio_url.split("?")[0]).name.strip()
            stem = Path(candidate).stem
            if candidate and not re.fullmatch(r"[0-9a-f]{24,}", stem, re.IGNORECASE):
                return candidate

        audio_id = str(data.get("audioId") or "").strip()
        if audio_id:
            return f"audio_{audio_id}.mp3"

    folder_name = source_path.parent.name.strip()
    if folder_name:
        return f"{folder_name}.mp3"
    return f"{source_path.stem}.mp3"


def _store_import_source_copy(source_path: Path, content: bytes) -> Path:
    settings = get_settings()
    target_dir = settings.upload_path / "imported_transcripts"
    target_dir.mkdir(parents=True, exist_ok=True)
    source_hash = hashlib.sha256(content).hexdigest()[:12]
    target_path = target_dir / f"{source_path.parent.name}_{source_hash}{source_path.suffix.lower()}"
    target_path.write_bytes(content)
    return target_path


def _parse_transcript_payload(name: str, content: bytes) -> tuple[list[dict], str, int, dict | None]:
    suffix = Path(name).suffix.lower()
    text = content.decode("utf-8-sig").strip()
    if not text:
        raise ValueError("Transcript file is empty")

    if suffix == ".txt":
        utterances, full_text, duration_ms = _build_plain_text_utterances(text)
        return utterances, full_text, duration_ms, None

    data = _decode_json_document(name, text)
    external_analysis = _extract_external_analysis(data)

    if isinstance(data, dict):
        if isinstance(data.get("payload"), dict) and isinstance(data["payload"].get("transcribeResult"), list):
            source_items = data["payload"]["transcribeResult"]
        elif isinstance(data.get("transcribeResult"), list):
            source_items = data["transcribeResult"]
        elif isinstance(data.get("utterances"), list):
            source_items = data["utterances"]
        elif isinstance(data.get("segments"), list):
            source_items = data["segments"]
        elif isinstance(data.get("full_text"), str):
            utterances, full_text, duration_ms = _build_plain_text_utterances(data["full_text"])
            return utterances, full_text, duration_ms, external_analysis
        else:
            raise ValueError("Unsupported JSON transcript format")
    elif isinstance(data, list):
        source_items = data
    else:
        raise ValueError("Unsupported JSON transcript format")

    utterances: list[dict] = []
    cursor_ms = 0
    for item in source_items:
        if not isinstance(item, dict):
            continue
        normalized, cursor_ms = _normalize_utterance(item, cursor_ms)
        if normalized:
            utterances.append(normalized)

    if not utterances:
        raise ValueError("No valid utterances found in transcript file")

    full_text = "\n".join(item["text"] for item in utterances)
    duration_ms = max(item["end_ms"] for item in utterances)
    return utterances, full_text, duration_ms, external_analysis


async def _reset_segments_and_analysis(recording_id: str, db: AsyncSession) -> None:
    segments = (await db.execute(select(Segment).where(Segment.recording_id == recording_id))).scalars().all()
    for segment in segments:
        await db.delete(segment)

    analysis_file_name = f"recording_{recording_id}.json"
    tasks = (
        await db.execute(select(AnalysisTask).where(AnalysisTask.file_name == analysis_file_name))
    ).scalars().all()
    for task in tasks:
        await db.delete(task)

    settings = get_settings()
    input_path = settings.upload_path / "analysis_input" / analysis_file_name
    result_path = settings.results_path / f"recording_{recording_id}.result.json"
    input_path.unlink(missing_ok=True)
    result_path.unlink(missing_ok=True)


async def _apply_transcript_import(
    recording: Recording,
    *,
    provider: str,
    file_name: str,
    content: bytes,
    db: AsyncSession,
    asr_task_id: str | None = None,
) -> Transcript:
    utterances, full_text, duration_ms, external_analysis = _parse_transcript_payload(file_name, content)

    transcript = (
        await db.execute(select(Transcript).where(Transcript.recording_id == recording.id))
    ).scalar_one_or_none()
    if transcript is None:
        transcript = Transcript(recording_id=recording.id)
        db.add(transcript)

    completed_at = datetime.now(timezone.utc)
    transcript.asr_provider = provider or "manual"
    transcript.asr_task_id = asr_task_id
    transcript.status = "completed"
    transcript.full_text = full_text
    transcript.utterances = utterances
    transcript.duration_ms = duration_ms
    transcript.error_message = None
    transcript.completed_at = completed_at

    recording.transcript_text = full_text
    recording.transcript_segments = utterances
    recording.duration_seconds = max(duration_ms // 1000, 1) if duration_ms else recording.duration_seconds
    recording.status = "transcribed"

    await _reset_segments_and_analysis(recording.id, db)

    await db.commit()
    await execute_segmentation(recording.id)

    transcript = (
        await db.execute(select(Transcript).where(Transcript.recording_id == recording.id).options(*_load_opts()))
    ).scalar_one()
    return transcript


@router.get("", response_model=PaginatedResponse[TranscriptOut])
async def list_transcripts(
    recording_id: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    include_text: bool = Query(False, description="Include full_text/utterances payload (large)"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    scope = await build_permission_scope(current_user)
    stmt = (
        select(Transcript)
        .join(Recording, Transcript.recording_id == Recording.id)
        .where(recording_scope_condition(scope))
        .order_by(Transcript.created_at.desc())
    )
    if recording_id:
        stmt = stmt.where(Transcript.recording_id == recording_id)
    if status:
        stmt = stmt.where(Transcript.status == status)

    total: int = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(stmt.options(*_load_opts()).offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    # Slim payload: per-recording detail pages pass recording_id and need
    # full_text/utterances; the global list view does not. Stripping the heavy
    # JSON payload reduces /transcripts response size from ~MBs to ~KBs.
    if recording_id is None and not include_text:
        items = [_to_out_slim(item) for item in rows]
    else:
        items = [_to_out(item) for item in rows]
    return make_page_response(items, total, page, page_size)


@router.get("/{transcript_id}", response_model=TranscriptOut)
async def get_transcript(
    transcript_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    transcript = await _get_scoped_transcript(transcript_id, db, current_user)
    if not transcript:
        raise HTTPException(404, "Transcript not found")
    return _to_out(transcript)


@router.post("/trigger/{recording_id}", response_model=TranscriptOut, status_code=201)
async def trigger_transcription(
    recording_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")
    if recording.status not in ("uploaded", "failed"):
        raise HTTPException(400, f"Recording status {recording.status} cannot trigger transcription")

    existing = (await db.execute(select(Transcript).where(Transcript.recording_id == recording_id))).scalar_one_or_none()
    if existing:
        if existing.status == "failed":
            await db.delete(existing)
            await db.flush()
        else:
            raise HTTPException(400, f"Transcript already exists with status {existing.status}")

    recording.status = "uploaded"
    await db.commit()

    await dispatch_transcription(recording_id)

    transcript = (await db.execute(select(Transcript).where(Transcript.recording_id == recording_id))).scalar_one_or_none()
    if not transcript:
        return TranscriptOut(
            id="pending",
            recording_id=recording_id,
            recording_file_name=recording.file_name,
            asr_provider="pending",
            asr_task_id=None,
            status="pending",
            full_text=None,
            utterances=None,
            duration_ms=None,
            error_message=None,
            created_at="",
            completed_at=None,
        )

    transcript = await db.get(Transcript, transcript.id, options=_load_opts())
    if transcript is None:
        raise HTTPException(500, "Transcript could not be loaded")
    return _to_out(transcript)


@router.post("/upload", response_model=TranscriptOut, status_code=201)
async def upload_manual_transcript(
    recording_id: str = Form(...),
    provider: str = Form("manual"),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recording = await _get_scoped_recording(recording_id, db, current_user)
    if not recording:
        raise HTTPException(404, "Recording not found")

    if not file.filename:
        raise HTTPException(400, "Transcript file is required")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_TRANSCRIPT_EXTENSIONS:
        raise HTTPException(400, f"Unsupported transcript file format: {suffix}")

    try:
        content = await file.read()
        _parse_transcript_payload(file.filename, content)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"Transcript JSON is invalid: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    transcript = await _apply_transcript_import(
        recording,
        provider=provider,
        file_name=file.filename,
        content=content,
        db=db,
    )
    return _to_out(transcript)


class TranscriptBatchImportRequest(BaseModel):
    directory: str
    provider: str = "validated-batch"


class TranscriptBatchImportItem(BaseModel):
    source_path: str
    recording_id: str | None = None
    recording_file_name: str | None = None
    status: str
    message: str
    created_recording: bool = False


class TranscriptBatchImportResult(BaseModel):
    imported: int
    skipped: int
    conflicts: int
    errors: int
    items: list[TranscriptBatchImportItem]


@router.post("/batch-import", response_model=TranscriptBatchImportResult)
async def batch_import_transcripts(
    payload: TranscriptBatchImportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_system_admin_or_above),
):
    del current_user
    source_dir = Path(payload.directory).expanduser().resolve()
    if not source_dir.is_dir():
        raise HTTPException(400, f"Directory does not exist: {payload.directory}")

    allowed_roots = get_settings().resolved_batch_import_allowed_paths
    if not allowed_roots:
        raise HTTPException(403, "批量导入未配置允许目录，请设置 BATCH_IMPORT_ALLOWED_DIRS")
    if not _is_under_allowed_root(source_dir, allowed_roots):
        raise HTTPException(403, "Import from this directory is not allowed")

    source_files = sorted(source_dir.rglob("payload.jsonl"))
    if not source_files:
        source_files = sorted(
            path for path in source_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in ALLOWED_TRANSCRIPT_EXTENSIONS
        )
    if not source_files:
        raise HTTPException(400, f"No transcript files found under: {payload.directory}")

    imported = 0
    skipped = 0
    conflicts = 0
    errors = 0
    items: list[TranscriptBatchImportItem] = []

    for source_path in source_files:
        try:
            content = source_path.read_bytes()
            source_key = _build_import_source_key(content)

            existing_transcript = (
                await db.execute(
                    select(Transcript)
                    .where(Transcript.asr_task_id == source_key)
                    .options(selectinload(Transcript.recording))
                )
            ).scalar_one_or_none()
            if existing_transcript is not None:
                items.append(
                    TranscriptBatchImportItem(
                        source_path=str(source_path),
                        recording_id=existing_transcript.recording_id,
                        recording_file_name=existing_transcript.recording.file_name if existing_transcript.recording else None,
                        status="skipped",
                        message="Duplicate source file already imported",
                    )
                )
                skipped += 1
                continue

            data = _decode_json_document(source_path.name, content.decode("utf-8-sig").strip())
            recording_file_name = _infer_recording_file_name(data, source_path)

            recording = (
                await db.execute(
                    select(Recording)
                    .where(Recording.file_name == recording_file_name)
                    .options(selectinload(Recording.transcript))
                    .order_by(Recording.created_at.desc())
                )
            ).scalars().first()

            created_recording = False
            if recording is not None and recording.transcript is not None:
                items.append(
                    TranscriptBatchImportItem(
                        source_path=str(source_path),
                        recording_id=recording.id,
                        recording_file_name=recording.file_name,
                        status="conflict",
                        message="Recording already has a different transcript; use single-file upload to replace it",
                    )
                )
                conflicts += 1
                continue

            if recording is None:
                managed_copy = _store_import_source_copy(source_path, content)
                recording = Recording(
                    id=_new_id(),
                    file_name=recording_file_name,
                    file_path=get_settings().make_relative_path(managed_copy),
                    file_size=len(content),
                    status="uploaded",
                    device_id="external-transcript-import",
                )
                db.add(recording)
                await db.commit()
                await db.refresh(recording)
                created_recording = True

            transcript = await _apply_transcript_import(
                recording,
                provider=payload.provider,
                file_name=source_path.name,
                content=content,
                db=db,
                asr_task_id=source_key,
            )
            items.append(
                TranscriptBatchImportItem(
                    source_path=str(source_path),
                    recording_id=transcript.recording_id,
                    recording_file_name=transcript.recording.file_name if transcript.recording else recording_file_name,
                    status="imported",
                    message="Imported successfully",
                    created_recording=created_recording,
                )
            )
            imported += 1
        except json.JSONDecodeError as exc:
            await db.rollback()
            items.append(
                TranscriptBatchImportItem(
                    source_path=str(source_path),
                    status="error",
                    message=f"Invalid JSON: {exc}",
                )
            )
            errors += 1
        except ValueError as exc:
            await db.rollback()
            items.append(
                TranscriptBatchImportItem(
                    source_path=str(source_path),
                    status="error",
                    message=str(exc),
                )
            )
            errors += 1
        except Exception as exc:
            await db.rollback()
            items.append(
                TranscriptBatchImportItem(
                    source_path=str(source_path),
                    status="error",
                    message=str(exc),
                )
            )
            errors += 1

    return TranscriptBatchImportResult(
        imported=imported,
        skipped=skipped,
        conflicts=conflicts,
        errors=errors,
        items=items,
    )
