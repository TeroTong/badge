from __future__ import annotations

import argparse
import asyncio
import copy
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from smart_badge_api.asr.speaker_role_resolver import resolve_speaker_roles
from smart_badge_api.asr.speaker_voiceprint import (
    _STAFF_ROLES,
    _best_staff_match,
    _clean_text,
    _cosine_similarity,
    _eligible_speaker_intervals,
    _extract_embeddings_for_speakers,
    _find_staff_entry,
    _load_registry,
    _normalize_vector,
    _speaker_roles_by_id,
)
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import Recording, Transcript
from smart_badge_api.db.session import _session_factory


def _round_score(value: float) -> float | None:
    if value < 0:
        return None
    return round(value, 4)


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return -1.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * ratio))
    index = min(max(index, 0), len(ordered) - 1)
    return ordered[index]


def _score_summary(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "min": round(ordered[0], 4),
        "p50": round(_percentile(ordered, 0.5), 4),
        "p90": round(_percentile(ordered, 0.9), 4),
        "max": round(ordered[-1], 4),
    }


def _default_output_path() -> Path:
    settings = get_settings()
    return settings.resolved_speaker_voiceprint_registry_path.with_name("calibration_report.json")


async def _run(
    max_records: int | None,
    output_path: Path,
    *,
    max_duration_seconds: int | None,
) -> None:
    settings = get_settings()
    registry = _load_registry()
    entries = [item for item in registry.get("staff", []) if isinstance(item, dict)]
    current_threshold = float(settings.speaker_voiceprint_match_threshold)

    staff_scores: list[float] = []
    counterparty_scores: list[float] = []
    per_staff_scores: dict[str, list[float]] = defaultdict(list)
    flagged_recordings: list[dict[str, Any]] = []
    scanned = 0
    eligible = 0
    skipped_too_long = 0
    skipped_embedding_errors = 0

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
                .order_by(Recording.duration_seconds.asc(), Recording.created_at.desc())
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
        intervals_by_speaker = _eligible_speaker_intervals(prepared)
        if not intervals_by_speaker:
            continue

        try:
            embeddings_by_speaker = _extract_embeddings_for_speakers(audio_path, intervals_by_speaker)
        except Exception as exc:
            skipped_embedding_errors += 1
            flagged_recordings.append(
                {
                    "recording_id": recording.id,
                    "staff_id": recording.staff_id,
                    "staff_name": recording.staff.name,
                    "flags": [f"embedding_error:{type(exc).__name__}:{exc}"],
                    "speakers": [],
                }
            )
            continue
        if not embeddings_by_speaker:
            continue
        eligible += 1

        roles_by_speaker = _speaker_roles_by_id(prepared)
        bound_entry = _find_staff_entry(entries, staff_id=recording.staff_id)
        bound_embedding = _normalize_vector(bound_entry.get("embedding")) if bound_entry else None
        recording_flags: list[str] = []
        speaker_rows: list[dict[str, Any]] = []

        for speaker_id, embedding in embeddings_by_speaker.items():
            role_set = roles_by_speaker.get(speaker_id, {"unknown"})
            desired_roles = role_set & _STAFF_ROLES
            best_entry, best_score, second_score = _best_staff_match(
                embedding,
                entries,
                desired_roles=desired_roles or None,
            )
            best_staff_id = _clean_text(best_entry.get("staff_id")) if best_entry else ""
            best_staff_name = _clean_text(best_entry.get("staff_name")) if best_entry else ""
            bound_score = _cosine_similarity(embedding, bound_embedding) if bound_embedding else -1.0

            if role_set & _STAFF_ROLES and bound_score >= 0:
                staff_scores.append(bound_score)
                per_staff_scores[recording.staff_id].append(bound_score)
                if bound_score < current_threshold:
                    recording_flags.append(f"{speaker_id}:staff_below_threshold={bound_score:.4f}")
            elif "customer" in role_set and best_score >= 0:
                counterparty_scores.append(best_score)
                if best_score >= current_threshold:
                    recording_flags.append(
                        f"{speaker_id}:customer_high_similarity={best_score:.4f}:{best_staff_name or best_staff_id}"
                    )

            speaker_rows.append(
                {
                    "speaker_id": speaker_id,
                    "roles": sorted(role_set),
                    "bound_staff_score": _round_score(bound_score),
                    "top_staff_id": best_staff_id or None,
                    "top_staff_name": best_staff_name or None,
                    "top_staff_score": _round_score(best_score),
                    "second_staff_score": _round_score(second_score),
                }
            )

        if recording_flags:
            flagged_recordings.append(
                {
                    "recording_id": recording.id,
                    "staff_id": recording.staff_id,
                    "staff_name": recording.staff.name,
                    "flags": recording_flags,
                    "speakers": speaker_rows,
                }
            )

    customer_max = max(counterparty_scores) if counterparty_scores else -1.0
    staff_min = min(staff_scores) if staff_scores else -1.0
    recommended_threshold = current_threshold
    if customer_max >= 0:
        recommended_threshold = max(recommended_threshold, customer_max + 0.02)
    if staff_min >= 0 and recommended_threshold > staff_min:
        recommended_threshold = current_threshold

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "registry_path": str(settings.resolved_speaker_voiceprint_registry_path),
        "registry_staff_count": len(entries),
        "current_threshold": current_threshold,
        "current_margin": float(settings.speaker_voiceprint_match_margin),
        "recordings_scanned": scanned,
        "recordings_with_embeddings": eligible,
        "skipped_too_long": skipped_too_long,
        "skipped_embedding_errors": skipped_embedding_errors,
        "max_duration_seconds": max_duration_seconds,
        "staff_bound_similarity": _score_summary(staff_scores),
        "counterparty_top_similarity": _score_summary(counterparty_scores),
        "per_staff_bound_similarity": {
            staff_id: _score_summary(scores)
            for staff_id, scores in sorted(per_staff_scores.items())
        },
        "separation_gap": round(staff_min - customer_max, 4)
        if staff_min >= 0 and customer_max >= 0
        else None,
        "recommended_threshold": round(recommended_threshold, 4),
        "flagged_recordings": flagged_recordings,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"report={output_path}")
    print(f"registry_staff_count={len(entries)}")
    print(f"recordings_scanned={scanned}")
    print(f"recordings_with_embeddings={eligible}")
    print(f"skipped_too_long={skipped_too_long}")
    print(f"skipped_embedding_errors={skipped_embedding_errors}")
    print(f"staff_bound_similarity={json.dumps(report['staff_bound_similarity'], ensure_ascii=False)}")
    print(f"counterparty_top_similarity={json.dumps(report['counterparty_top_similarity'], ensure_ascii=False)}")
    print(f"recommended_threshold={report['recommended_threshold']}")
    print(f"flagged_recordings={len(flagged_recordings)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze voiceprint similarity on completed staff-bound recordings")
    parser.add_argument("--max-records", type=int, default=None, help="optional maximum number of recordings to scan")
    parser.add_argument(
        "--max-duration-seconds",
        type=int,
        default=None,
        help="skip recordings longer than this many seconds",
    )
    parser.add_argument("--output", type=Path, default=_default_output_path(), help="output JSON report path")
    args = parser.parse_args()
    asyncio.run(
        _run(
            max_records=args.max_records,
            output_path=args.output,
            max_duration_seconds=args.max_duration_seconds,
        )
    )


if __name__ == "__main__":
    main()
