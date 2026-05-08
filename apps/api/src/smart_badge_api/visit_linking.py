from __future__ import annotations

from typing import Iterable

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.analysis.customer_profile_score_sync import (
    refresh_customer_profile_scores,
    refresh_recording_profile_scores_for_current_context,
)
from smart_badge_api.db.models import Recording, RecordingVisitLink, Visit


def _unique_visit_ids(visit_ids: Iterable[str | None]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for visit_id in visit_ids:
        normalized = str(visit_id or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def ordered_recording_visit_links(recording: Recording) -> list[RecordingVisitLink]:
    return sorted(
        recording.visit_links or [],
        key=lambda item: (
            0 if item.is_primary else 1,
            item.created_at.isoformat() if item.created_at else "",
            item.visit_id,
        ),
    )


def ordered_visit_recording_links(visit: Visit) -> list[RecordingVisitLink]:
    return sorted(
        visit.recording_links or [],
        key=lambda item: (
            item.recording.created_at.isoformat() if item.recording and item.recording.created_at else "",
            item.recording_id,
        ),
        reverse=True,
    )


async def _customer_ids_for_visits(db: AsyncSession, visit_ids: Iterable[str | None]) -> list[str]:
    normalized_visit_ids = _unique_visit_ids(visit_ids)
    if not normalized_visit_ids:
        return []
    customer_ids = (
        await db.execute(select(Visit.customer_id).where(Visit.id.in_(normalized_visit_ids)))
    ).scalars().all()
    return _unique_visit_ids(customer_ids)


async def sync_recording_visit_links(
    db: AsyncSession,
    recording: Recording,
    linked_visit_ids: Iterable[str | None],
    *,
    primary_visit_id: str | None = None,
    source: str | None = None,
    sync_segments: bool = True,
) -> list[str]:
    visit_ids = _unique_visit_ids(linked_visit_ids)
    primary_id = str(primary_visit_id or "").strip() or None
    existing_links = (
        await db.execute(select(RecordingVisitLink).where(RecordingVisitLink.recording_id == recording.id))
    ).scalars().all()
    before_visit_ids = [
        recording.visit_id,
        *[link.visit_id for link in existing_links],
    ]
    affected_customer_ids = await _customer_ids_for_visits(db, before_visit_ids)

    if primary_id and primary_id not in visit_ids:
        visit_ids = [primary_id, *visit_ids]
    if not primary_id and visit_ids:
        primary_id = recording.visit_id if recording.visit_id in visit_ids else visit_ids[0]

    if visit_ids:
        existing_visit_ids = set(
            (
                await db.execute(select(Visit.id).where(Visit.id.in_(visit_ids)))
            ).scalars().all()
        )
        missing = [visit_id for visit_id in visit_ids if visit_id not in existing_visit_ids]
        if missing:
            raise ValueError(f"Visit not found: {', '.join(missing)}")

    link_by_visit_id = {link.visit_id: link for link in existing_links}

    for visit_id, link in list(link_by_visit_id.items()):
        if visit_id in visit_ids:
            continue
        await db.delete(link)

    for visit_id in visit_ids:
        link = link_by_visit_id.get(visit_id)
        if link is None:
            link = RecordingVisitLink(recording_id=recording.id, visit_id=visit_id)
            db.add(link)
            if source:
                link.source = source
        link.is_primary = visit_id == primary_id
        if source == "manual" or (source and not link.source):
            link.source = source

    recording.visit_id = primary_id
    if sync_segments:
        state = inspect(recording)
        segments = [] if "segments" in state.unloaded else recording.segments
        for segment in segments:
            segment.visit_id = primary_id

    await db.flush()

    affected_customer_ids = _unique_visit_ids(
        [
            *affected_customer_ids,
            *(
                await _customer_ids_for_visits(
                    db,
                    [primary_id, *visit_ids],
                )
            ),
        ]
    )
    for customer_id in affected_customer_ids:
        await refresh_customer_profile_scores(db, customer_id)

    if recording.visit_id is None:
        await refresh_recording_profile_scores_for_current_context(db, recording.id)

    return visit_ids
