from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smart_badge_api.core.config import get_settings

logger = logging.getLogger(__name__)

_CLAUSE_PATTERN = re.compile(r"[^，。！？；,.!?;]+[，。！？；,.!?;]?")
_RAW_SPEAKER_PATTERN = re.compile(r"^(speaker[_-]?\d+|SPEAKER_\d+|unknown)$", re.IGNORECASE)


@dataclass(slots=True)
class DiarizationSegment:
    begin_ms: int
    end_ms: int
    speaker_id: str


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _coerce_ms(value: object, fallback: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return fallback
    try:
        return max(int(float(value)), 0)
    except (TypeError, ValueError):
        return fallback


def _speaker_key(value: object) -> str:
    text = _clean_text(value)
    return text or "unknown"


def _speaker_duration_ms(segments: list[DiarizationSegment]) -> dict[str, int]:
    durations: dict[str, int] = {}
    for segment in segments:
        duration = max(segment.end_ms - segment.begin_ms, 0)
        if duration <= 0:
            continue
        durations[segment.speaker_id] = durations.get(segment.speaker_id, 0) + duration
    return durations


def _speaker_ids(segments: list[DiarizationSegment]) -> set[str]:
    return {segment.speaker_id for segment in segments if segment.speaker_id}


def _normalize_segments(raw_segments: list[object]) -> list[DiarizationSegment]:
    normalized: list[DiarizationSegment] = []
    for segment in raw_segments:
        begin_ms = _coerce_ms(getattr(segment, "begin_ms", None))
        end_ms = _coerce_ms(getattr(segment, "end_ms", None), begin_ms)
        speaker_id = _speaker_key(getattr(segment, "speaker_id", None))
        if end_ms <= begin_ms or speaker_id == "unknown":
            continue
        normalized.append(DiarizationSegment(begin_ms=begin_ms, end_ms=end_ms, speaker_id=speaker_id))
    normalized.sort(key=lambda item: (item.begin_ms, item.end_ms, item.speaker_id))
    return normalized


def _overlap_by_speaker(begin_ms: int, end_ms: int, segments: list[DiarizationSegment]) -> dict[str, int]:
    overlaps: dict[str, int] = {}
    for segment in segments:
        overlap = min(end_ms, segment.end_ms) - max(begin_ms, segment.begin_ms)
        if overlap <= 0:
            continue
        overlaps[segment.speaker_id] = overlaps.get(segment.speaker_id, 0) + overlap
    return overlaps


def _nearest_speaker(begin_ms: int, end_ms: int, segments: list[DiarizationSegment]) -> str | None:
    midpoint = (begin_ms + end_ms) / 2
    best_speaker: str | None = None
    best_distance: float | None = None
    for segment in segments:
        if segment.begin_ms <= midpoint <= segment.end_ms:
            return segment.speaker_id
        distance = segment.begin_ms - midpoint if midpoint < segment.begin_ms else midpoint - segment.end_ms
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_speaker = segment.speaker_id
    return best_speaker


def _dominant_speaker(
    begin_ms: int,
    end_ms: int,
    segments: list[DiarizationSegment],
) -> tuple[str | None, float, list[dict[str, Any]]]:
    overlaps = _overlap_by_speaker(begin_ms, end_ms, segments)
    duration_ms = max(end_ms - begin_ms, 1)
    candidates = [
        {
            "speaker_id": speaker_id,
            "overlap_ms": overlap_ms,
            "overlap_ratio": round(overlap_ms / duration_ms, 4),
        }
        for speaker_id, overlap_ms in sorted(overlaps.items(), key=lambda item: (-item[1], item[0]))
    ]
    if candidates:
        top = candidates[0]
        return str(top["speaker_id"]), float(top["overlap_ratio"]), candidates
    return _nearest_speaker(begin_ms, end_ms, segments), 0.0, []


def _split_clauses(text: str) -> list[str]:
    clauses = [match.group(0).strip() for match in _CLAUSE_PATTERN.finditer(text)]
    return [clause for clause in clauses if clause]


def _estimate_clause_ranges(text: str, begin_ms: int, end_ms: int) -> list[tuple[str, int, int]]:
    clauses = _split_clauses(text)
    if len(clauses) <= 1:
        return []

    total_chars = sum(max(len(clause), 1) for clause in clauses)
    duration_ms = max(end_ms - begin_ms, 1)
    ranges: list[tuple[str, int, int]] = []
    cursor = begin_ms
    consumed_chars = 0
    for index, clause in enumerate(clauses):
        consumed_chars += max(len(clause), 1)
        if index == len(clauses) - 1:
            clause_end = end_ms
        else:
            clause_end = begin_ms + int(round(duration_ms * consumed_chars / total_chars))
        clause_end = max(clause_end, cursor)
        ranges.append((clause, cursor, clause_end))
        cursor = clause_end
    return ranges


def _should_split_mixed_utterance(
    *,
    text: str,
    begin_ms: int,
    end_ms: int,
    candidates: list[dict[str, Any]],
) -> bool:
    settings = get_settings()
    if not settings.asr_independent_diarization_split_mixed_utterances:
        return False
    if end_ms - begin_ms < max(settings.asr_independent_diarization_min_split_duration_ms, 0):
        return False
    if len(candidates) < 2:
        return False
    meaningful = [
        item
        for item in candidates
        if int(item.get("overlap_ms") or 0) >= settings.asr_independent_diarization_min_speaker_overlap_ms
    ]
    if len(meaningful) < 2:
        return False
    return len(_split_clauses(text)) >= 2


def _annotate_utterance(
    utterance: dict,
    *,
    speaker_id: str,
    overlap_ratio: float,
    candidates: list[dict[str, Any]],
) -> dict:
    clone = dict(utterance)
    original_speaker = clone.get("speaker")
    original_speaker_id = clone.get("speaker_id")
    if original_speaker is not None:
        clone.setdefault("asr_original_speaker", original_speaker)
    if original_speaker_id is not None:
        clone.setdefault("asr_original_speaker_id", original_speaker_id)
    clone["speaker"] = speaker_id
    clone["speaker_id"] = speaker_id
    clone["speaker_diarization_source"] = "independent_3dspeaker"
    clone["speaker_diarization_overlap_ratio"] = round(overlap_ratio, 4)
    clone["speaker_diarization_candidates"] = candidates[:5]
    return clone


def _merge_adjacent_utterances(utterances: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for utterance in utterances:
        text = _clean_text(utterance.get("text"))
        if not text:
            continue
        if (
            merged
            and merged[-1].get("speaker_id") == utterance.get("speaker_id")
            and int(utterance.get("begin_ms") or 0) <= int(merged[-1].get("end_ms") or 0) + 200
            and _clean_text(merged[-1].get("asr_original_speaker_id")) == _clean_text(utterance.get("asr_original_speaker_id"))
        ):
            merged[-1]["text"] = f"{_clean_text(merged[-1].get('text'))}{text}"
            merged[-1]["end_ms"] = max(int(merged[-1].get("end_ms") or 0), int(utterance.get("end_ms") or 0))
            continue
        merged.append(utterance)
    return merged


def _split_mixed_utterance(utterance: dict, segments: list[DiarizationSegment]) -> list[dict] | None:
    text = _clean_text(utterance.get("text"))
    begin_ms = _coerce_ms(utterance.get("begin_ms"))
    end_ms = _coerce_ms(utterance.get("end_ms"), begin_ms)
    speaker_id, overlap_ratio, candidates = _dominant_speaker(begin_ms, end_ms, segments)
    if not speaker_id:
        return None
    if not _should_split_mixed_utterance(text=text, begin_ms=begin_ms, end_ms=end_ms, candidates=candidates):
        return None

    split_items: list[dict] = []
    for clause, clause_begin, clause_end in _estimate_clause_ranges(text, begin_ms, end_ms):
        clause_speaker, clause_overlap_ratio, clause_candidates = _dominant_speaker(clause_begin, clause_end, segments)
        if not clause_speaker:
            clause_speaker = speaker_id
        split_items.append(
            _annotate_utterance(
                {
                    **utterance,
                    "text": clause,
                    "begin_ms": clause_begin,
                    "end_ms": clause_end,
                    "speaker_diarization_split_from_mixed_utterance": True,
                },
                speaker_id=clause_speaker,
                overlap_ratio=clause_overlap_ratio,
                candidates=clause_candidates,
            )
        )

    if len({item.get("speaker_id") for item in split_items}) < 2:
        return None
    return _merge_adjacent_utterances(split_items)


def apply_diarization_segments_to_utterances(
    utterances: list[dict],
    raw_segments: list[object],
) -> tuple[list[dict], dict[str, Any]]:
    segments = _normalize_segments(raw_segments)
    if not utterances or not segments:
        return [dict(item) for item in utterances], {
            "applied": False,
            "reason": "empty_utterances_or_segments",
            "diarization_segment_count": len(segments),
        }

    settings = get_settings()
    speaker_ids = _speaker_ids(segments)
    if len(speaker_ids) < settings.asr_independent_diarization_min_speakers:
        return [dict(item) for item in utterances], {
            "applied": False,
            "reason": "too_few_speakers",
            "diarization_speaker_count": len(speaker_ids),
            "diarization_segment_count": len(segments),
        }
    if len(speaker_ids) > settings.asr_independent_diarization_max_speakers:
        return [dict(item) for item in utterances], {
            "applied": False,
            "reason": "too_many_speakers",
            "diarization_speaker_count": len(speaker_ids),
            "diarization_segment_count": len(segments),
        }

    result: list[dict] = []
    split_count = 0
    reassigned_count = 0
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        text = _clean_text(utterance.get("text"))
        if not text:
            continue

        split_items = _split_mixed_utterance(utterance, segments)
        if split_items:
            result.extend(split_items)
            split_count += 1
            reassigned_count += len(split_items)
            continue

        begin_ms = _coerce_ms(utterance.get("begin_ms"))
        end_ms = _coerce_ms(utterance.get("end_ms"), begin_ms)
        speaker_id, overlap_ratio, candidates = _dominant_speaker(begin_ms, end_ms, segments)
        if not speaker_id:
            result.append(dict(utterance))
            continue
        original_speaker_id = _clean_text(utterance.get("speaker_id") or utterance.get("speaker"))
        if _RAW_SPEAKER_PATTERN.match(original_speaker_id) and original_speaker_id != speaker_id:
            reassigned_count += 1
        result.append(
            _annotate_utterance(
                utterance,
                speaker_id=speaker_id,
                overlap_ratio=overlap_ratio,
                candidates=candidates,
            )
        )

    result.sort(key=lambda item: (int(item.get("begin_ms") or 0), int(item.get("end_ms") or 0)))
    return result, {
        "applied": True,
        "reason": None,
        "diarization_speaker_count": len(speaker_ids),
        "diarization_segment_count": len(segments),
        "diarization_speaker_durations_ms": _speaker_duration_ms(segments),
        "reassigned_utterance_count": reassigned_count,
        "split_mixed_utterance_count": split_count,
    }


def _provider_enabled(provider: str) -> bool:
    settings = get_settings()
    providers = {
        item.strip()
        for item in settings.asr_independent_diarization_providers.split(",")
        if item.strip()
    }
    return provider in providers


def _run_independent_diarization(audio_path: Path, utterances: list[dict]) -> tuple[list[dict], dict[str, Any]]:
    from smart_badge_api.asr.sensevoice_3dspeaker_provider import _get_diarizer, _prepare_audio_path

    prepared_audio_path, cleanup = _prepare_audio_path(audio_path)
    try:
        diarizer = _get_diarizer()
        segments = diarizer.diarize(prepared_audio_path)
    finally:
        cleanup()
    return apply_diarization_segments_to_utterances(utterances, segments)


async def maybe_apply_independent_diarization(
    audio_path: Path,
    utterances: list[dict],
    *,
    provider: str,
    source_id: str | None = None,
) -> list[dict]:
    settings = get_settings()
    if not settings.asr_independent_diarization_enabled or not utterances or not _provider_enabled(provider):
        return utterances

    loop = asyncio.get_running_loop()
    started_at = time.perf_counter()
    try:
        resolved_utterances, metadata = await loop.run_in_executor(
            None,
            _run_independent_diarization,
            audio_path,
            utterances,
        )
    except Exception as exc:
        logger.warning(
            "independent diarization failed for %s provider=%s source_id=%s: %s",
            audio_path.name,
            provider,
            source_id,
            exc,
        )
        return utterances

    logger.info(
        "independent diarization finished for %s provider=%s source_id=%s applied=%s reason=%s speakers=%s segments=%s reassigned=%s split=%s elapsed=%.1fs",
        audio_path.name,
        provider,
        source_id,
        metadata.get("applied"),
        metadata.get("reason"),
        metadata.get("diarization_speaker_count"),
        metadata.get("diarization_segment_count"),
        metadata.get("reassigned_utterance_count"),
        metadata.get("split_mixed_utterance_count"),
        time.perf_counter() - started_at,
    )
    return resolved_utterances if metadata.get("applied") else utterances
