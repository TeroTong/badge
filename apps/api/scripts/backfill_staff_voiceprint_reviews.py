from __future__ import annotations

import argparse
import asyncio
import copy

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from smart_badge_api.asr.speaker_role_resolver import resolve_speaker_roles
from smart_badge_api.asr.speaker_voiceprint import (
    apply_staff_voiceprints,
    auto_enroll_staff_voiceprint,
    list_staff_voiceprint_reviews,
)
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import Recording, Transcript
from smart_badge_api.db.session import _session_factory


async def _run(max_records: int | None, *, max_duration_seconds: int | None) -> None:
    settings = get_settings()
    scanned = 0
    queued = 0
    skipped_too_long = 0

    async with _session_factory() as db:
        rows = (
            await db.execute(
                select(Recording)
                .options(selectinload(Recording.staff), selectinload(Recording.transcript))
                .join(Transcript, Transcript.recording_id == Recording.id)
                .where(
                    Recording.staff_id.is_not(None),
                    Transcript.status == "completed",
                )
                .order_by(Recording.created_at.desc())
            )
        ).scalars().all()

    for recording in rows:
        if max_records is not None and scanned >= max_records:
            break

        duration_seconds = int(recording.duration_seconds or 0)
        if max_duration_seconds is not None and duration_seconds > max_duration_seconds:
            skipped_too_long += 1
            continue
        scanned += 1

        if not recording.staff or not recording.transcript:
            continue
        utterances = recording.transcript.utterances or []
        if not isinstance(utterances, list) or not utterances:
            continue

        audio_path = settings.resolve_file_path(recording.file_path)
        if not audio_path.is_file():
            continue

        prepared = resolve_speaker_roles(
            copy.deepcopy(utterances),
            staff_name=recording.staff.name,
            staff_role=recording.staff.role,
        )
        prepared = apply_staff_voiceprints(
            audio_path,
            prepared,
            staff_id=recording.staff_id,
        )
        pending_before = {item.get("id") for item in list_staff_voiceprint_reviews(status="pending")}
        auto_enroll_staff_voiceprint(
            audio_path,
            prepared,
            staff_id=recording.staff_id,
            staff_name=recording.staff.name,
            staff_role=recording.staff.role,
            source_id=recording.id,
            queue_only=True,
        )
        pending_after = {item.get("id") for item in list_staff_voiceprint_reviews(status="pending")}
        delta = pending_after - pending_before
        if delta:
            queued += len(delta)
            print(f"queued review recording={recording.id} staff_id={recording.staff_id} count={len(delta)}")

    print(f"scanned={scanned}")
    print(f"queued={queued}")
    print(f"skipped_too_long={skipped_too_long}")
    print(f"review_queue={settings.resolved_speaker_voiceprint_review_queue_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill pending voiceprint reviews from completed recordings")
    parser.add_argument("--max-records", type=int, default=None, help="optional maximum number of recordings to scan")
    parser.add_argument(
        "--max-duration-seconds",
        type=int,
        default=None,
        help="skip recordings longer than this many seconds",
    )
    args = parser.parse_args()
    asyncio.run(_run(max_records=args.max_records, max_duration_seconds=args.max_duration_seconds))


if __name__ == "__main__":
    main()
