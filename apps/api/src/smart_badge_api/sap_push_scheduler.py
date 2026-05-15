from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, and_, cast, exists, func, literal, or_, select, text

from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import (
    AnalysisTask,
    Recording,
    RecordingVisitAnalysis,
    RecordingVisitLink,
    SapConsultationReview,
    SapPushLog,
)
from smart_badge_api.db.session import _session_factory
from smart_badge_api.sap_push_service import (
    SapPushPreparationError,
    create_sap_push_log,
    execute_sap_push_log,
    summarize_sap_push_log_result,
)
from smart_badge_api.task_queue import dispatch_sap_push_log

logger = logging.getLogger("smart_badge.sap_push_scheduler")

_AUTO_PUSH_LOCK = asyncio.Lock()
_AUTO_PUSH_ADVISORY_LOCK_ID = 0x5342505341500001
_AUTO_PUSH_FINAL_BLOCKING_STATUSES = ("succeeded",)
_AUTO_PUSH_IN_PROGRESS_STATUSES = ("queued", "sending", "prepared")
_AUTO_PUSH_RETRYABLE_STATUSES = ("failed", "skipped")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc_aware(value: datetime | None, *, default: datetime | None = None) -> datetime:
    if value is None:
        return default or datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _push_log_activity_at():
    return func.coalesce(SapPushLog.updated_at, SapPushLog.sent_at, SapPushLog.created_at)


def _stale_before(settings) -> datetime:
    stale_seconds = max(int(settings.sap_rfc_auto_push_stale_seconds or 0), 0)
    return _utcnow() - timedelta(seconds=stale_seconds)


def _auto_push_ignore_before(settings) -> datetime | None:
    raw_value = str(getattr(settings, "sap_rfc_auto_push_ignore_before", "") or "").strip()
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("invalid SAP_RFC_AUTO_PUSH_IGNORE_BEFORE=%s; ignoring cutoff", raw_value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _blocking_push_log_condition(*, changed_at, retry_after: datetime, stale_before: datetime):
    status = func.coalesce(SapPushLog.status, "")
    activity_at = _push_log_activity_at()
    return and_(
        SapPushLog.created_at >= changed_at,
        or_(
            status.in_(_AUTO_PUSH_FINAL_BLOCKING_STATUSES),
            and_(
                status.in_(_AUTO_PUSH_IN_PROGRESS_STATUSES),
                activity_at >= stale_before,
            ),
            and_(
                status.in_(_AUTO_PUSH_RETRYABLE_STATUSES),
                activity_at >= retry_after,
            ),
        ),
    )


def _is_push_log_blocking(log: SapPushLog | None, *, retry_after: datetime, stale_before: datetime) -> bool:
    if log is None:
        return False

    status = log.status or ""
    activity_at = _as_utc_aware(log.updated_at or log.sent_at or log.created_at)
    if status in _AUTO_PUSH_FINAL_BLOCKING_STATUSES:
        return True
    if status in _AUTO_PUSH_IN_PROGRESS_STATUSES:
        return activity_at >= stale_before
    if status in _AUTO_PUSH_RETRYABLE_STATUSES:
        return activity_at >= retry_after
    return False


def _extract_request_text_from_push_log(log: SapPushLog | None) -> str:
    if log is None:
        return ""
    for payload in log.request_payloads or []:
        if not isinstance(payload, dict):
            continue
        text_value = str(payload.get("text") or "").strip()
        if text_value:
            return text_value
    return ""


def _review_effective_text_matches_push_log(review: SapConsultationReview, log: SapPushLog | None) -> bool:
    review_text = str(review.effective_text or "").strip()
    log_text = _extract_request_text_from_push_log(log)
    return bool(review_text and log_text and review_text == log_text)


def _is_effective_success_push_log(log: SapPushLog | None) -> bool:
    if log is None:
        return False
    summary = summarize_sap_push_log_result(log)
    return str(summary.get("effective_status") or log.status or "").strip() == "succeeded"


async def _has_newer_push_log_for_same_scope(db, push_log: SapPushLog) -> bool:
    conditions = [SapPushLog.id != push_log.id]
    if push_log.recording_id:
        conditions.append(SapPushLog.recording_id == push_log.recording_id)
        if push_log.visit_id:
            conditions.append(SapPushLog.visit_id == push_log.visit_id)
        else:
            conditions.append(SapPushLog.visit_id.is_(None))
    elif push_log.visit_order_no:
        conditions.append(SapPushLog.visit_order_no == push_log.visit_order_no)
        if push_log.visit_order_seg:
            conditions.append(SapPushLog.visit_order_seg == push_log.visit_order_seg)
        else:
            conditions.append(or_(SapPushLog.visit_order_seg.is_(None), SapPushLog.visit_order_seg == ""))
    else:
        return False

    newer_id = (
        await db.execute(
            select(SapPushLog.id)
            .where(
                *conditions,
                SapPushLog.created_at > (push_log.created_at or datetime.min.replace(tzinfo=timezone.utc)),
            )
            .order_by(SapPushLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return newer_id is not None


async def _redispatch_stale_in_progress_push_logs(*, stale_before: datetime, limit: int) -> int:
    if limit <= 0:
        return 0

    async with _session_factory() as db:
        result = await db.execute(
            select(SapPushLog)
            .where(
                SapPushLog.send_enabled.is_(True),
                func.coalesce(SapPushLog.status, "").in_(_AUTO_PUSH_IN_PROGRESS_STATUSES),
                _push_log_activity_at() < stale_before,
            )
            .order_by(_push_log_activity_at().asc(), SapPushLog.created_at.asc(), SapPushLog.id.asc())
            .limit(limit)
        )
        push_logs = list(result.scalars().all())
        if not push_logs:
            return 0

        now = _utcnow()
        retry_logs = []
        for push_log in push_logs:
            if await _has_newer_push_log_for_same_scope(db, push_log):
                continue
            previous_status = push_log.status or "unknown"
            push_log.status = "queued"
            push_log.error_message = (
                f"Stale SAP push stayed in {previous_status}; requeued for automatic retry"
            )
            push_log.updated_at = now
            retry_logs.append(push_log)
        if not retry_logs:
            return 0
        await db.commit()

        requeued = 0
        for push_log in retry_logs:
            try:
                await dispatch_sap_push_log(push_log.id)
                requeued += 1
            except Exception:
                logger.exception("failed to redispatch stale sap push log_id=%s", push_log.id)
        return requeued


def _session_dialect_name(db) -> str:
    try:
        bind = db.get_bind()
    except Exception:
        return ""
    return str(getattr(getattr(bind, "dialect", None), "name", "") or "").lower()


async def _try_acquire_auto_push_advisory_lock(db) -> bool:
    dialect_name = _session_dialect_name(db)
    if dialect_name not in {"postgresql", "postgres"}:
        return True
    acquired = (
        await db.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": _AUTO_PUSH_ADVISORY_LOCK_ID},
        )
    ).scalar_one()
    return bool(acquired)


async def _release_auto_push_advisory_lock(db) -> None:
    dialect_name = _session_dialect_name(db)
    if dialect_name not in {"postgresql", "postgres"}:
        return
    try:
        await db.execute(
            text("SELECT pg_advisory_unlock(:lock_id)"),
            {"lock_id": _AUTO_PUSH_ADVISORY_LOCK_ID},
        )
    except Exception:
        logger.warning("failed to release sap auto push advisory lock", exc_info=True)


async def _has_current_auto_push_log(
    db,
    recording_id: str,
    target_visit_id: str | None,
    retry_after: datetime,
    stale_before: datetime,
) -> bool:
    recording = await db.get(Recording, recording_id)
    if recording is None:
        return True

    effective_visit_id = target_visit_id or recording.visit_id
    link_change_stmt = select(func.max(func.coalesce(RecordingVisitLink.updated_at, RecordingVisitLink.created_at))).where(
        RecordingVisitLink.recording_id == recording_id
    )
    if effective_visit_id:
        link_change_stmt = link_change_stmt.where(RecordingVisitLink.visit_id == effective_visit_id)
    link_changed_at = (await db.execute(link_change_stmt)).scalar_one_or_none()
    changed_at = _as_utc_aware(link_changed_at or recording.created_at)
    if effective_visit_id:
        review_changed_at = (
            await db.execute(
                select(SapConsultationReview.updated_at).where(
                    SapConsultationReview.visit_id == effective_visit_id,
                    SapConsultationReview.status == "pending",
                )
            )
        ).scalar_one_or_none()
        if review_changed_at:
            review_changed_at = _as_utc_aware(review_changed_at)
            if review_changed_at > changed_at:
                changed_at = review_changed_at

    conditions = [
        SapPushLog.recording_id == recording_id,
        SapPushLog.created_at >= changed_at,
    ]
    if effective_visit_id:
        conditions.append(SapPushLog.visit_id == effective_visit_id)
    else:
        conditions.append(SapPushLog.visit_id.is_(None))

    latest_log = (
        await db.execute(
            select(SapPushLog)
            .where(*conditions)
            .order_by(SapPushLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return _is_push_log_blocking(latest_log, retry_after=retry_after, stale_before=stale_before)


async def _record_auto_push_preparation_error(
    db,
    recording_id: str,
    target_visit_id: str | None,
    exc: SapPushPreparationError,
) -> None:
    settings = get_settings()
    recording = await db.get(Recording, recording_id)
    visit_id = target_visit_id or (recording.visit_id if recording else None)
    db.add(
        SapPushLog(
            recording_id=recording_id,
            visit_id=visit_id,
            trigger_mode="auto_bind",
            status="skipped",
            send_enabled=bool(settings.sap_rfc_send_enabled),
            initiated_by="system:auto_stable_bind",
            request_url=settings.sap_rfc_gateway_url,
            request_payloads=[],
            gateway_requests=[],
            response_items=[],
            error_message=f"{exc.error_code}: {exc.message}",
            updated_at=_utcnow(),
        )
    )
    await db.commit()


def _first_review_recording_id(review: SapConsultationReview) -> str | None:
    recording_ids = review.recording_ids if isinstance(review.recording_ids, list) else []
    for value in recording_ids:
        recording_id = str(value or "").strip()
        if recording_id:
            return recording_id

    blocks = review.blocks if isinstance(review.blocks, list) else []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        recording_id = str(block.get("recording_id") or "").strip()
        if recording_id:
            return recording_id
    return None


async def _find_pending_review_candidate_refs(
    db,
    *,
    limit: int,
    stable_before: datetime,
    retry_after: datetime,
    stale_before: datetime,
    ignore_before: datetime | None,
    excluded_visit_ids: set[str],
) -> list[tuple[str, str | None]]:
    if limit <= 0:
        return []

    reviews = (
        await db.execute(
            select(SapConsultationReview)
            .where(
                SapConsultationReview.status == "pending",
                SapConsultationReview.updated_at <= stable_before,
            )
            .order_by(SapConsultationReview.updated_at.asc(), SapConsultationReview.created_at.asc())
            .limit(max(limit * 4, limit))
        )
    ).scalars().all()

    refs: list[tuple[str, str | None]] = []
    for review in reviews:
        visit_id = str(review.visit_id or "").strip()
        if not visit_id or visit_id in excluded_visit_ids:
            continue
        recording_id = _first_review_recording_id(review)
        if not recording_id:
            continue

        recording = (
            await db.execute(
                select(Recording)
                .join(RecordingVisitLink, RecordingVisitLink.recording_id == Recording.id)
                .where(
                    Recording.id == recording_id,
                    Recording.status == "analyzed",
                    RecordingVisitLink.visit_id == visit_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if recording is None:
            continue
        if ignore_before and recording.created_at and _as_utc_aware(recording.created_at) < ignore_before:
            continue

        latest_any_review_push = (
            await db.execute(
                select(SapPushLog)
                .where(SapPushLog.visit_id == visit_id)
                .order_by(SapPushLog.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if (
            _is_effective_success_push_log(latest_any_review_push)
            and _review_effective_text_matches_push_log(review, latest_any_review_push)
        ):
            review.last_push_log_id = latest_any_review_push.id
            review.status = latest_any_review_push.status or "succeeded"
            await db.commit()
            continue

        latest_review_push = (
            await db.execute(
                select(SapPushLog)
                .where(
                    SapPushLog.visit_id == visit_id,
                    SapPushLog.created_at >= review.updated_at,
                )
                .order_by(SapPushLog.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if _is_push_log_blocking(latest_review_push, retry_after=retry_after, stale_before=stale_before):
            continue

        refs.append((recording_id, visit_id))
        excluded_visit_ids.add(visit_id)
        if len(refs) >= limit:
            break

    return refs


async def _find_auto_push_candidate_refs(limit: int) -> list[tuple[str, str | None]]:
    settings = get_settings()
    stable_before = _utcnow() - timedelta(seconds=max(settings.sap_rfc_auto_push_stable_seconds, 0))
    retry_after = _utcnow() - timedelta(seconds=max(settings.sap_rfc_auto_push_retry_delay_seconds, 0))
    stale_before = _stale_before(settings)
    ignore_before = _auto_push_ignore_before(settings)

    link_count_subquery = (
        select(func.count(RecordingVisitLink.id))
        .where(RecordingVisitLink.recording_id == Recording.id)
        .correlate(Recording)
        .scalar_subquery()
    )
    latest_link_change_subquery = (
        select(func.max(func.coalesce(RecordingVisitLink.updated_at, RecordingVisitLink.created_at)))
        .where(RecordingVisitLink.recording_id == Recording.id)
        .correlate(Recording)
        .scalar_subquery()
    )
    link_changed_at = func.coalesce(latest_link_change_subquery, Recording.created_at)
    latest_push_created_at = (
        select(func.max(SapPushLog.created_at))
        .where(
            SapPushLog.recording_id == Recording.id,
            SapPushLog.created_at >= link_changed_at,
        )
        .correlate(Recording)
        .scalar_subquery()
    )
    has_blocking_push_log = exists(
        select(SapPushLog.id).where(
            SapPushLog.recording_id == Recording.id,
            SapPushLog.created_at == latest_push_created_at,
            _blocking_push_log_condition(
                changed_at=link_changed_at,
                retry_after=retry_after,
                stale_before=stale_before,
            ),
        )
    )
    has_completed_analysis = exists(
        select(AnalysisTask.id).where(
            AnalysisTask.file_name == (literal("recording_") + Recording.id + literal(".json")),
            AnalysisTask.status == "done",
            AnalysisTask.result.is_not(None),
            cast(AnalysisTask.result, String) != "null",
        )
    )

    async with _session_factory() as db:
        visit_ready_cache: dict[str, bool] = {}

        async def _all_visit_recordings_ready(visit_id: str) -> bool:
            cached = visit_ready_cache.get(visit_id)
            if cached is not None:
                return cached

            linked_rows = (
                await db.execute(
                    select(Recording.id, Recording.status)
                    .join(RecordingVisitLink, RecordingVisitLink.recording_id == Recording.id)
                    .where(
                        RecordingVisitLink.visit_id == visit_id,
                        Recording.status != "filtered",
                    )
                )
            ).all()
            if not linked_rows:
                visit_ready_cache[visit_id] = False
                return False

            analysis_file_names = [f"recording_{recording_id}.json" for recording_id, _status in linked_rows]
            completed_file_names = set(
                (
                    await db.execute(
                        select(AnalysisTask.file_name).where(
                            AnalysisTask.file_name.in_(analysis_file_names),
                            AnalysisTask.status == "done",
                            AnalysisTask.result.is_not(None),
                            cast(AnalysisTask.result, String) != "null",
                        )
                    )
                ).scalars().all()
            )
            ready = all(
                status == "analyzed" and f"recording_{recording_id}.json" in completed_file_names
                for recording_id, status in linked_rows
            )
            visit_ready_cache[visit_id] = ready
            return ready

        stmt = (
            select(Recording.id, Recording.visit_id)
            .where(
                Recording.status == "analyzed",
                Recording.visit_id.is_not(None),
                *([Recording.created_at >= ignore_before] if ignore_before else []),
                link_count_subquery <= 1,
                has_completed_analysis,
                ~has_blocking_push_log,
                or_(
                    latest_link_change_subquery.is_(None),
                    latest_link_change_subquery <= stable_before,
                ),
            )
            .order_by(Recording.created_at.asc(), Recording.id.asc())
            .limit(max(limit, 1))
        )
        raw_single_rows = [(str(recording_id), str(visit_id)) for recording_id, visit_id in (await db.execute(stmt)).all()]
        single_refs: list[tuple[str, str | None]] = []
        seen_multi_recording_visit_ids: set[str] = set()
        visit_ids = [visit_id for _recording_id, visit_id in raw_single_rows]
        visit_recording_counts: dict[str, int] = {}
        if visit_ids:
            count_rows = (
                await db.execute(
                    select(RecordingVisitLink.visit_id, func.count(RecordingVisitLink.recording_id))
                    .where(RecordingVisitLink.visit_id.in_(visit_ids))
                    .group_by(RecordingVisitLink.visit_id)
                )
            ).all()
            visit_recording_counts = {str(visit_id): int(count) for visit_id, count in count_rows}

        for recording_id, visit_id in raw_single_rows:
            if visit_recording_counts.get(visit_id, 0) <= 1:
                single_refs.append((recording_id, None))
                continue
            if visit_id in seen_multi_recording_visit_ids:
                continue
            if not await _all_visit_recordings_ready(visit_id):
                continue
            latest_visit_link_change = (
                await db.execute(
                    select(func.max(func.coalesce(RecordingVisitLink.updated_at, RecordingVisitLink.created_at)))
                    .where(RecordingVisitLink.visit_id == visit_id)
                )
            ).scalar_one_or_none()
            visit_push_threshold = latest_visit_link_change or stable_before
            latest_visit_push = (
                await db.execute(
                    select(SapPushLog)
                    .where(
                        SapPushLog.visit_id == visit_id,
                        SapPushLog.created_at >= visit_push_threshold,
                    )
                    .order_by(SapPushLog.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if _is_push_log_blocking(latest_visit_push, retry_after=retry_after, stale_before=stale_before):
                continue
            single_refs.append((recording_id, visit_id))
            seen_multi_recording_visit_ids.add(visit_id)

        remaining_limit = max(limit, 1) - len(single_refs)
        if remaining_limit <= 0:
            return single_refs

        multi_fallback_stmt = (
            select(Recording.id, RecordingVisitLink.visit_id)
            .join(RecordingVisitLink, RecordingVisitLink.recording_id == Recording.id)
            .where(
                Recording.status == "analyzed",
                RecordingVisitLink.visit_id.is_not(None),
                *([Recording.created_at >= ignore_before] if ignore_before else []),
                link_count_subquery > 1,
                has_completed_analysis,
                or_(
                    latest_link_change_subquery.is_(None),
                    latest_link_change_subquery <= stable_before,
                ),
            )
            .order_by(Recording.created_at.asc(), Recording.id.asc(), RecordingVisitLink.visit_id.asc())
            .limit(max(remaining_limit * 4, remaining_limit))
        )
        multi_fallback_refs: list[tuple[str, str | None]] = []
        seen_multi_fallback_keys: set[tuple[str, str]] = set()
        for recording_id, visit_id in (await db.execute(multi_fallback_stmt)).all():
            recording_id = str(recording_id)
            visit_id = str(visit_id)
            key = (recording_id, visit_id)
            if key in seen_multi_fallback_keys:
                continue
            scoped_mapping_status = (
                await db.execute(
                    select(RecordingVisitAnalysis.mapping_status).where(
                        RecordingVisitAnalysis.recording_id == recording_id,
                        RecordingVisitAnalysis.visit_id == visit_id,
                    )
                )
            ).scalar_one_or_none()
            if scoped_mapping_status == "confirmed":
                continue
            if not await _all_visit_recordings_ready(visit_id):
                continue
            if await _has_current_auto_push_log(db, recording_id, visit_id, retry_after, stale_before):
                continue
            multi_fallback_refs.append((recording_id, visit_id))
            seen_multi_fallback_keys.add(key)
            if len(multi_fallback_refs) >= remaining_limit:
                break

        refs = [*single_refs, *multi_fallback_refs]
        remaining_limit = max(limit, 1) - len(refs)
        if remaining_limit <= 0:
            return refs

        multi_latest_link_change = (
            select(func.max(func.coalesce(RecordingVisitLink.updated_at, RecordingVisitLink.created_at)))
            .where(RecordingVisitLink.recording_id == RecordingVisitAnalysis.recording_id)
            .correlate(RecordingVisitAnalysis)
            .scalar_subquery()
        )
        multi_analysis_ready_at = func.coalesce(RecordingVisitAnalysis.sap_ready_at, RecordingVisitAnalysis.updated_at)
        latest_visit_push_created_at = (
            select(func.max(SapPushLog.created_at))
            .where(
                SapPushLog.recording_id == RecordingVisitAnalysis.recording_id,
                SapPushLog.visit_id == RecordingVisitAnalysis.visit_id,
                SapPushLog.created_at >= multi_analysis_ready_at,
            )
            .correlate(RecordingVisitAnalysis)
            .scalar_subquery()
        )
        has_blocking_visit_push_log = exists(
            select(SapPushLog.id).where(
                SapPushLog.recording_id == RecordingVisitAnalysis.recording_id,
                SapPushLog.visit_id == RecordingVisitAnalysis.visit_id,
                SapPushLog.created_at == latest_visit_push_created_at,
                _blocking_push_log_condition(
                    changed_at=multi_analysis_ready_at,
                    retry_after=retry_after,
                    stale_before=stale_before,
                ),
            )
        )
        multi_stmt = (
            select(RecordingVisitAnalysis.recording_id, RecordingVisitAnalysis.visit_id)
            .join(Recording, Recording.id == RecordingVisitAnalysis.recording_id)
            .where(
                Recording.status == "analyzed",
                *([Recording.created_at >= ignore_before] if ignore_before else []),
                link_count_subquery > 1,
                RecordingVisitAnalysis.mapping_status == "confirmed",
                RecordingVisitAnalysis.analysis_status == "done",
                RecordingVisitAnalysis.analysis_result.is_not(None),
                RecordingVisitAnalysis.sap_ready_at.is_not(None),
                RecordingVisitAnalysis.sap_ready_at <= stable_before,
                multi_latest_link_change <= stable_before,
                ~has_blocking_visit_push_log,
            )
            .order_by(Recording.created_at.asc(), RecordingVisitAnalysis.visit_id.asc())
            .limit(remaining_limit)
        )
        multi_refs = [(str(recording_id), str(visit_id)) for recording_id, visit_id in (await db.execute(multi_stmt)).all()]
        refs = [*refs, *multi_refs]
        remaining_review_limit = max(limit, 1) - len(refs)
        if remaining_review_limit <= 0:
            return refs

        excluded_visit_ids = {str(visit_id) for _recording_id, visit_id in refs if visit_id}
        review_refs = await _find_pending_review_candidate_refs(
            db,
            limit=remaining_review_limit,
            stable_before=stable_before,
            retry_after=retry_after,
            stale_before=stale_before,
            ignore_before=ignore_before,
            excluded_visit_ids=excluded_visit_ids,
        )
        return [*refs, *review_refs]


async def _find_auto_push_candidate_ids(limit: int) -> list[str]:
    """Backward-compatible helper used by existing tests and diagnostics."""
    refs = await _find_auto_push_candidate_refs(limit)
    return [recording_id for recording_id, target_visit_id in refs if target_visit_id is None]


async def run_sap_auto_push_scan(limit: int | None = None) -> dict[str, int]:
    settings = get_settings()
    if not settings.sap_rfc_auto_push_on_bind:
        return {"checked": 0, "queued": 0, "executed": 0, "skipped": 0, "failed": 0, "stale_requeued": 0}

    scan_limit = int(limit or settings.sap_rfc_auto_push_limit_per_run or 20)
    async with _AUTO_PUSH_LOCK:
        async with _session_factory() as lock_db:
            if not await _try_acquire_auto_push_advisory_lock(lock_db):
                logger.info("sap auto push scan skipped because another process holds the advisory lock")
                return {"checked": 0, "queued": 0, "executed": 0, "skipped": 0, "failed": 0, "stale_requeued": 0}

            try:
                stale_requeued = await _redispatch_stale_in_progress_push_logs(
                    stale_before=_stale_before(settings),
                    limit=scan_limit,
                )
                candidate_refs = await _find_auto_push_candidate_refs(scan_limit)
                checked = len(candidate_refs)
                queued = 0
                executed = 0
                skipped = 0
                failed = 0
                retry_after = _utcnow() - timedelta(seconds=max(settings.sap_rfc_auto_push_retry_delay_seconds, 0))
                stale_before = _stale_before(settings)

                for recording_id, target_visit_id in candidate_refs:
                    try:
                        async with _session_factory() as db:
                            if await _has_current_auto_push_log(
                                db,
                                recording_id,
                                target_visit_id,
                                retry_after,
                                stale_before,
                            ):
                                skipped += 1
                                continue
                            push_log = await create_sap_push_log(
                                db,
                                recording_id,
                                target_visit_id=target_visit_id,
                                trigger_mode="auto_bind",
                                initiated_by="system:auto_stable_bind",
                                prefer_async=True,
                            )
                        if push_log.status == "queued":
                            await dispatch_sap_push_log(push_log.id)
                            queued += 1
                        elif push_log.status == "prepared":
                            await execute_sap_push_log(push_log.id)
                            executed += 1
                        else:
                            skipped += 1
                    except SapPushPreparationError as exc:
                        logger.warning(
                            "skip sap auto push after stable bind recording_id=%s error=%s",
                            recording_id,
                            exc.message,
                        )
                        try:
                            async with _session_factory() as db:
                                if not await _has_current_auto_push_log(
                                    db,
                                    recording_id,
                                    target_visit_id,
                                    retry_after,
                                    stale_before,
                                ):
                                    await _record_auto_push_preparation_error(
                                        db,
                                        recording_id,
                                        target_visit_id,
                                        exc,
                                    )
                        except Exception:
                            logger.warning(
                                "failed to record sap auto push preparation error recording_id=%s",
                                recording_id,
                                exc_info=True,
                            )
                        skipped += 1
                    except Exception:
                        logger.exception("sap auto push scan failed recording_id=%s", recording_id)
                        failed += 1

                return {
                    "checked": checked,
                    "queued": queued,
                    "executed": executed,
                    "skipped": skipped,
                    "failed": failed,
                    "stale_requeued": stale_requeued,
                }
            finally:
                await _release_auto_push_advisory_lock(lock_db)


async def periodic_sap_auto_push_scan(
    stop_event: asyncio.Event,
    *,
    interval_seconds: int,
    limit: int,
) -> None:
    while not stop_event.is_set():
        try:
            summary = await run_sap_auto_push_scan(limit)
            logger.info(
                "sap auto push scan checked=%d queued=%d executed=%d skipped=%d failed=%d stale_requeued=%d",
                summary["checked"],
                summary["queued"],
                summary["executed"],
                summary["skipped"],
                summary["failed"],
                summary["stale_requeued"],
            )
        except Exception:
            logger.exception("sap auto push periodic scan failed")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue
