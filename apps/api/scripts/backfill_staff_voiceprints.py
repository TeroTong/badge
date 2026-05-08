from __future__ import annotations

import argparse
import asyncio
import copy
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from smart_badge_api.asr.speaker_role_resolver import resolve_speaker_roles
from smart_badge_api.asr.speaker_voiceprint import auto_enroll_staff_voiceprint
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import Recording, Transcript
from smart_badge_api.db.session import _session_factory


async def _run(limit_per_staff: int, max_records: int | None) -> None:
    settings = get_settings()
    enrolled_per_staff: dict[str, int] = defaultdict(int)
    processed = 0
    enrolled = 0

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
        if max_records is not None and processed >= max_records:
            break
        processed += 1

        if not recording.staff_id or recording.staff is None or recording.transcript is None:
            continue
        if enrolled_per_staff[recording.staff_id] >= limit_per_staff:
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
        success = auto_enroll_staff_voiceprint(
            audio_path,
            prepared,
            staff_id=recording.staff_id,
            staff_name=recording.staff.name,
            staff_role=recording.staff.role,
            source_id=recording.id,
        )
        if not success:
            continue

        enrolled_per_staff[recording.staff_id] += 1
        enrolled += 1
        print(
            f"enrolled recording={recording.id} staff_id={recording.staff_id} "
            f"staff_name={recording.staff.name} samples={enrolled_per_staff[recording.staff_id]}"
        )

    print(f"processed={processed}")
    print(f"enrolled={enrolled}")
    print(f"staff_count={len(enrolled_per_staff)}")
    print(f"registry={settings.resolved_speaker_voiceprint_registry_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill staff voiceprints from completed recordings")
    parser.add_argument("--limit-per-staff", type=int, default=3, help="max enrolled recordings per staff")
    parser.add_argument("--max-records", type=int, default=None, help="optional maximum number of recordings to scan")
    args = parser.parse_args()
    asyncio.run(_run(limit_per_staff=max(args.limit_per_staff, 1), max_records=args.max_records))


if __name__ == "__main__":
    main()
