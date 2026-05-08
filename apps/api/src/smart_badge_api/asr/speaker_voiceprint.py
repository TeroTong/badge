from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smart_badge_api.core.config import get_settings

from .sensevoice_3dspeaker_provider import _get_diarizer, _prepare_audio_path

logger = logging.getLogger(__name__)

_STAFF_ROLES = frozenset({"consultant", "doctor"})
_MAX_SOURCE_HISTORY = 12

# 进程内锐锁 + 原子写，保护 voiceprint 注册表与审核队列的 JSON 读改写。
_REGISTRY_LOCK = threading.RLock()
_REVIEW_QUEUE_LOCK = threading.RLock()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """先写入同目录临时文件再 os.replace，避免崩溃/并发造成的部分写入。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _normalize_role(value: object) -> str:
    text = _clean_text(value).lower()
    if text in {"consultant", "doctor", "customer", "unknown"}:
        return text
    return "unknown"


def _speaker_key(utterance: dict) -> str:
    speaker_id = _clean_text(utterance.get("speaker_id"))
    if speaker_id:
        return speaker_id
    speaker = _clean_text(utterance.get("speaker"))
    return speaker or "unknown"


def _is_raw_speaker_label(value: object) -> bool:
    text = _clean_text(value).lower()
    return text.startswith("speaker_")


def _entry_staff_id(entry: dict[str, Any]) -> str:
    return _clean_text(entry.get("staff_id"))


def _normalize_vector(values: list[float] | tuple[float, ...] | None) -> list[float] | None:
    if not values:
        return None
    try:
        vector = [float(item) for item in values]
    except (TypeError, ValueError):
        return None
    norm = sum(item * item for item in vector) ** 0.5
    if norm <= 0:
        return None
    return [item / norm for item in vector]


def _cosine_similarity(left: list[float] | None, right: list[float] | None) -> float:
    if not left or not right or len(left) != len(right):
        return -1.0
    return float(sum(left_item * right_item for left_item, right_item in zip(left, right, strict=False)))


def _registry_path() -> Path:
    path = get_settings().resolved_speaker_voiceprint_registry_path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_registry() -> dict[str, Any]:
    path = _registry_path()
    with _REGISTRY_LOCK:
        if not path.is_file():
            return {"version": 1, "staff": []}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("failed to load speaker voiceprint registry: %s", path)
            return {"version": 1, "staff": []}
    if not isinstance(payload, dict):
        return {"version": 1, "staff": []}
    staff_items = payload.get("staff")
    if not isinstance(staff_items, list):
        payload["staff"] = []
    return payload


def _save_registry(payload: dict[str, Any]) -> None:
    path = _registry_path()
    with _REGISTRY_LOCK:
        _atomic_write_json(path, payload)


def _review_queue_path() -> Path:
    path = get_settings().resolved_speaker_voiceprint_review_queue_path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_review_queue() -> dict[str, Any]:
    path = _review_queue_path()
    with _REVIEW_QUEUE_LOCK:
        if not path.is_file():
            return {"version": 1, "items": []}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("failed to load speaker voiceprint review queue: %s", path)
            return {"version": 1, "items": []}
    if not isinstance(payload, dict):
        return {"version": 1, "items": []}
    items = payload.get("items")
    if not isinstance(items, list):
        payload["items"] = []
    return payload


def _save_review_queue(payload: dict[str, Any]) -> None:
    path = _review_queue_path()
    with _REVIEW_QUEUE_LOCK:
        _atomic_write_json(path, payload)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _total_duration_ms(intervals: list[tuple[int, int]]) -> int:
    return sum(max(end_ms - begin_ms, 0) for begin_ms, end_ms in intervals)


def _speaker_intervals_by_id(utterances: list[dict]) -> dict[str, list[tuple[int, int]]]:
    grouped: dict[str, list[tuple[int, int]]] = {}
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        text = _clean_text(utterance.get("text"))
        if not text:
            continue
        key = _speaker_key(utterance)
        begin_ms = int(utterance.get("begin_ms") or 0)
        end_ms = int(utterance.get("end_ms") or begin_ms)
        if end_ms <= begin_ms:
            continue
        grouped.setdefault(key, []).append((begin_ms, end_ms))
    return grouped


def _extract_embeddings_for_speakers(
    audio_path: Path,
    intervals_by_speaker: dict[str, list[tuple[int, int]]],
) -> dict[str, list[float]]:
    prepared_audio_path, cleanup = _prepare_audio_path(audio_path)
    try:
        diarizer = _get_diarizer()
        return diarizer.extract_speaker_embeddings(prepared_audio_path, intervals_by_speaker)
    finally:
        cleanup()


def _eligible_speaker_intervals(utterances: list[dict]) -> dict[str, list[tuple[int, int]]]:
    min_duration_ms = max(get_settings().speaker_voiceprint_min_duration_ms, 0)
    grouped = _speaker_intervals_by_id(utterances)
    return {
        speaker_id: intervals
        for speaker_id, intervals in grouped.items()
        if _total_duration_ms(intervals) >= min_duration_ms
    }


def _speaker_roles_by_id(utterances: list[dict]) -> dict[str, set[str]]:
    roles: dict[str, set[str]] = {}
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        speaker_id = _speaker_key(utterance)
        roles.setdefault(speaker_id, set()).add(_normalize_role(utterance.get("speaker")))
    return roles


def _speaker_primary_role(utterances: list[dict], speaker_id: str) -> str:
    roles = _speaker_roles_by_id(utterances).get(speaker_id, set())
    for candidate in ("consultant", "doctor", "customer"):
        if candidate in roles:
            return candidate
    return "unknown"


def _speaker_role_sources(utterances: list[dict], speaker_id: str) -> list[str]:
    values = {
        _clean_text(utterance.get("speaker_role_source"))
        for utterance in utterances
        if isinstance(utterance, dict) and _speaker_key(utterance) == speaker_id
    }
    return sorted(value for value in values if value)


def _speaker_best_similarity(utterances: list[dict], speaker_id: str) -> float:
    best = -1.0
    for utterance in utterances:
        if not isinstance(utterance, dict) or _speaker_key(utterance) != speaker_id:
            continue
        try:
            score = float(utterance.get("speaker_voiceprint_similarity"))
        except (TypeError, ValueError):
            continue
        if score > best:
            best = score
    return best


def _speaker_bound_staff(utterances: list[dict], speaker_id: str) -> tuple[str, str]:
    for utterance in utterances:
        if not isinstance(utterance, dict) or _speaker_key(utterance) != speaker_id:
            continue
        staff_id = _clean_text(utterance.get("speaker_staff_id"))
        staff_name = _clean_text(utterance.get("speaker_staff_name"))
        if staff_id or staff_name:
            return staff_id, staff_name
    return "", ""


def _speaker_preview_text(utterances: list[dict], speaker_id: str, *, max_chars: int = 120) -> str:
    parts: list[str] = []
    total = 0
    for utterance in utterances:
        if not isinstance(utterance, dict) or _speaker_key(utterance) != speaker_id:
            continue
        text = _clean_text(utterance.get("text"))
        if not text:
            continue
        remaining = max_chars - total
        if remaining <= 0:
            break
        snippet = text[:remaining]
        parts.append(snippet)
        total += len(snippet)
        if total >= max_chars or len(parts) >= 2:
            break
    return " ".join(parts).strip()


def _best_staff_match(
    embedding: list[float],
    entries: list[dict[str, Any]],
    *,
    desired_roles: set[str] | None = None,
) -> tuple[dict[str, Any] | None, float, float]:
    scored: list[tuple[float, dict[str, Any]]] = []
    for entry in entries:
        role = _normalize_role(entry.get("staff_role"))
        if role not in _STAFF_ROLES:
            continue
        if desired_roles and role not in desired_roles:
            continue
        candidate_embedding = _normalize_vector(entry.get("embedding"))
        similarity = _cosine_similarity(embedding, candidate_embedding)
        if similarity < 0:
            continue
        scored.append((similarity, entry))

    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return None, -1.0, -1.0
    top_entry = scored[0][1]
    top_score = scored[0][0]
    second_score = scored[1][0] if len(scored) > 1 else -1.0
    return top_entry, top_score, second_score


def _find_staff_entry(
    entries: list[dict[str, Any]],
    *,
    staff_id: str | None,
) -> dict[str, Any] | None:
    normalized_staff_id = _clean_text(staff_id)
    if not normalized_staff_id:
        return None
    return next((entry for entry in entries if _entry_staff_id(entry) == normalized_staff_id), None)


def _best_bound_staff_speaker(
    embeddings_by_speaker: dict[str, list[float]],
    entry: dict[str, Any],
) -> tuple[str | None, float, float]:
    candidate_embedding = _normalize_vector(entry.get("embedding"))
    if not candidate_embedding:
        return None, -1.0, -1.0

    scored: list[tuple[float, str]] = []
    for speaker_id, embedding in embeddings_by_speaker.items():
        similarity = _cosine_similarity(embedding, candidate_embedding)
        if similarity < 0:
            continue
        scored.append((similarity, speaker_id))

    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return None, -1.0, -1.0
    top_score, top_speaker_id = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else -1.0
    return top_speaker_id, top_score, second_score


def _resolve_voiceprint_matches(
    embeddings_by_speaker: dict[str, list[float]],
    utterances: list[dict],
    entries: list[dict[str, Any]],
    *,
    exclude_speaker_ids: set[str] | None = None,
    exclude_staff_ids: set[str] | None = None,
    require_staff_like_role: bool = False,
) -> dict[str, tuple[dict[str, Any], float]]:
    settings = get_settings()
    threshold = float(settings.speaker_voiceprint_match_threshold)
    margin = float(settings.speaker_voiceprint_match_margin)
    current_roles_by_speaker = _speaker_roles_by_id(utterances)
    blocked_speaker_ids = set(exclude_speaker_ids or ())
    blocked_staff_ids = set(exclude_staff_ids or ())

    candidates: list[tuple[float, str, dict[str, Any]]] = []
    for speaker_id, embedding in embeddings_by_speaker.items():
        if speaker_id in blocked_speaker_ids:
            continue
        current_roles = current_roles_by_speaker.get(speaker_id, set())
        if require_staff_like_role and not (current_roles & _STAFF_ROLES):
            continue
        desired_roles = current_roles & _STAFF_ROLES
        entry, score, second_score = _best_staff_match(
            embedding,
            entries,
            desired_roles=desired_roles or None,
        )
        if entry is None:
            continue
        staff_id = _entry_staff_id(entry)
        if not staff_id or staff_id in blocked_staff_ids:
            continue
        if score < threshold:
            continue
        if second_score >= 0 and score - second_score < margin:
            continue
        candidates.append((score, speaker_id, entry))

    candidates.sort(key=lambda item: item[0], reverse=True)
    matches: dict[str, tuple[dict[str, Any], float]] = {}
    used_staff_ids = set(blocked_staff_ids)
    for score, speaker_id, entry in candidates:
        staff_id = _entry_staff_id(entry)
        if speaker_id in matches or staff_id in used_staff_ids:
            continue
        matches[speaker_id] = (entry, score)
        used_staff_ids.add(staff_id)
    return matches


def _apply_staff_match(
    utterances: list[dict],
    *,
    speaker_id: str,
    entry: dict[str, Any],
    score: float,
    source: str,
    force_role: bool = False,
) -> None:
    matched_role = _normalize_role(entry.get("staff_role"))
    matched_staff_id = _entry_staff_id(entry)
    matched_staff_name = _clean_text(entry.get("staff_name"))
    for utterance in utterances:
        if not isinstance(utterance, dict) or _speaker_key(utterance) != speaker_id:
            continue

        current_speaker = _clean_text(utterance.get("speaker"))
        current_role = _normalize_role(current_speaker)
        if force_role:
            utterance["speaker"] = matched_role
        elif current_role == "unknown" or _is_raw_speaker_label(current_speaker):
            utterance["speaker"] = matched_role
        elif current_role != matched_role:
            continue

        utterance["speaker_role"] = matched_role
        utterance["speaker_role_source"] = source
        utterance["speaker_staff_id"] = matched_staff_id
        utterance["speaker_staff_name"] = matched_staff_name
        utterance["speaker_voiceprint_similarity"] = round(score, 4)


def _apply_counterparty_customer(
    utterances: list[dict],
    *,
    speaker_id: str,
    source: str,
) -> None:
    for utterance in utterances:
        if not isinstance(utterance, dict) or _speaker_key(utterance) != speaker_id:
            continue
        current_speaker = _clean_text(utterance.get("speaker"))
        current_role = _normalize_role(current_speaker)
        if current_role == "unknown" or _is_raw_speaker_label(current_speaker):
            utterance["speaker"] = "customer"
            utterance["speaker_role"] = "customer"
            utterance["speaker_role_source"] = source


def apply_staff_voiceprints(
    audio_path: Path,
    utterances: list[dict],
    *,
    staff_id: str | None = None,
) -> list[dict]:
    settings = get_settings()
    if not settings.speaker_voiceprint_enabled or not utterances:
        return utterances

    registry = _load_registry()
    entries = [item for item in registry.get("staff", []) if isinstance(item, dict)]
    if not entries:
        return utterances

    intervals_by_speaker = _eligible_speaker_intervals(utterances)
    if not intervals_by_speaker:
        return utterances

    try:
        embeddings_by_speaker = _extract_embeddings_for_speakers(audio_path, intervals_by_speaker)
    except Exception as exc:
        logger.warning("speaker voiceprint matching skipped: %s", exc)
        return utterances

    matches: dict[str, tuple[dict[str, Any], float, str, bool]] = {}
    matched_staff_ids: set[str] = set()
    threshold = float(settings.speaker_voiceprint_match_threshold)
    margin = float(settings.speaker_voiceprint_match_margin)

    bound_entry = _find_staff_entry(entries, staff_id=staff_id)
    if bound_entry is not None:
        bound_speaker_id, bound_score, bound_second_score = _best_bound_staff_speaker(
            embeddings_by_speaker,
            bound_entry,
        )
        if (
            bound_speaker_id
            and bound_score >= threshold
            and (bound_second_score < 0 or bound_score - bound_second_score >= margin)
        ):
            matched_staff_id = _entry_staff_id(bound_entry)
            matches[bound_speaker_id] = (bound_entry, bound_score, "voiceprint_bound_staff", True)
            if matched_staff_id:
                matched_staff_ids.add(matched_staff_id)

    should_global_match_remaining = not (staff_id and len(intervals_by_speaker) == 2 and matches)
    if should_global_match_remaining:
        remaining_matches = _resolve_voiceprint_matches(
            embeddings_by_speaker,
            utterances,
            entries,
            exclude_speaker_ids=set(matches),
            exclude_staff_ids=matched_staff_ids,
            require_staff_like_role=bool(staff_id),
        )
        for speaker_id, (entry, score) in remaining_matches.items():
            matches[speaker_id] = (entry, score, "voiceprint", False)

    if not matches:
        return utterances

    for speaker_id, (entry, score, source, force_role) in matches.items():
        _apply_staff_match(
            utterances,
            speaker_id=speaker_id,
            entry=entry,
            score=score,
            source=source,
            force_role=force_role,
        )

    if len(intervals_by_speaker) == 2 and len(matches) == 1:
        matched_speaker_id = next(iter(matches))
        other_speaker_ids = [speaker_id for speaker_id in intervals_by_speaker if speaker_id != matched_speaker_id]
        if other_speaker_ids:
            _apply_counterparty_customer(
                utterances,
                speaker_id=other_speaker_ids[0],
                source="voiceprint_counterparty",
            )

    return utterances


def _select_enrollment_speaker(
    utterances: list[dict],
    *,
    staff_id: str | None,
    staff_role: str,
) -> str | None:
    duration_by_speaker = {
        speaker_id: _total_duration_ms(intervals)
        for speaker_id, intervals in _speaker_intervals_by_id(utterances).items()
    }
    preferred: list[str] = []
    fallback: list[str] = []
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        speaker_id = _speaker_key(utterance)
        if _clean_text(utterance.get("speaker_staff_id")) == _clean_text(staff_id):
            preferred.append(speaker_id)
            continue
        if _normalize_role(utterance.get("speaker")) == staff_role:
            fallback.append(speaker_id)

    for group in (preferred, fallback):
        unique = list(dict.fromkeys(group))
        if not unique:
            continue
        if len(unique) == 1:
            return unique[0]
        ranked = sorted(unique, key=lambda key: duration_by_speaker.get(key, 0), reverse=True)
        if duration_by_speaker.get(ranked[0], 0) >= duration_by_speaker.get(ranked[1], 0) * 1.5:
            return ranked[0]
    return None


def _rounded_embedding(values: list[float]) -> list[float]:
    return [round(float(item), 6) for item in values]


def list_staff_voiceprint_reviews(*, status: str | None = None) -> list[dict[str, Any]]:
    items = [item for item in _load_review_queue().get("items", []) if isinstance(item, dict)]
    if status:
        normalized_status = _clean_text(status).lower()
        items = [item for item in items if _clean_text(item.get("status")).lower() == normalized_status]
    items.sort(key=lambda item: (_clean_text(item.get("updated_at")), _clean_text(item.get("created_at"))), reverse=True)
    return items


def get_staff_voiceprint_review(review_id: str) -> dict[str, Any] | None:
    normalized_review_id = _clean_text(review_id)
    if not normalized_review_id:
        return None
    for item in list_staff_voiceprint_reviews():
        if _clean_text(item.get("id")) == normalized_review_id:
            return item
    return None


def queue_staff_voiceprint_review(
    *,
    source_id: str | None,
    staff_id: str | None,
    staff_name: str | None,
    staff_role: str | None,
    speaker_id: str | None,
    utterances: list[dict],
    reasons: list[str],
) -> dict[str, Any] | None:
    normalized_staff_id = _clean_text(staff_id)
    normalized_speaker_id = _clean_text(speaker_id)
    if not normalized_staff_id:
        return None

    with _REVIEW_QUEUE_LOCK:
        payload = _load_review_queue()
        items = [item for item in payload.get("items", []) if isinstance(item, dict)]
        review_key = "::".join((_clean_text(source_id), normalized_staff_id, normalized_speaker_id))
        now = _now_iso()
        bound_staff_id, bound_staff_name = _speaker_bound_staff(utterances, normalized_speaker_id)
        similarity = _speaker_best_similarity(utterances, normalized_speaker_id)
        current = next(
            (
                item
                for item in items
                if _clean_text(item.get("review_key")) == review_key and _clean_text(item.get("status")).lower() == "pending"
            ),
            None,
        )
        if current is None:
            current = {
                "id": uuid.uuid4().hex[:12],
                "review_key": review_key,
                "created_at": now,
            }
            items.append(current)

        current.update(
            {
                "status": "pending",
                "source_id": _clean_text(source_id),
                "recording_id": _clean_text(source_id),
                "staff_id": normalized_staff_id,
                "staff_name": _clean_text(staff_name),
                "staff_role": _normalize_role(staff_role),
                "speaker_id": normalized_speaker_id or None,
                "speaker_role": _speaker_primary_role(utterances, normalized_speaker_id),
                "speaker_role_sources": _speaker_role_sources(utterances, normalized_speaker_id),
                "speaker_duration_ms": _total_duration_ms(_speaker_intervals_by_id(utterances).get(normalized_speaker_id, [])),
                "speaker_voiceprint_similarity": round(similarity, 4) if similarity >= 0 else None,
                "matched_staff_id": bound_staff_id or None,
                "matched_staff_name": bound_staff_name or None,
                "preview_text": _speaker_preview_text(utterances, normalized_speaker_id),
                "reasons": [reason for reason in reasons if _clean_text(reason)],
                "updated_at": now,
                "resolved_at": None,
                "resolved_by": None,
                "resolution_note": None,
            }
        )
        payload["items"] = items
        _save_review_queue(payload)
        return current


def resolve_staff_voiceprint_review(
    review_id: str,
    *,
    status: str,
    resolved_by: str | None,
    note: str | None = None,
    extra_updates: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    normalized_review_id = _clean_text(review_id)
    normalized_status = _clean_text(status).lower()
    if normalized_status not in {"approved", "rejected"}:
        return None

    payload = _load_review_queue()
    with _REVIEW_QUEUE_LOCK:
        items = [item for item in payload.get("items", []) if isinstance(item, dict)]
        now = _now_iso()
        for item in items:
            if _clean_text(item.get("id")) != normalized_review_id:
                continue
            item["status"] = normalized_status
            item["resolved_at"] = now
            item["resolved_by"] = _clean_text(resolved_by)
            item["resolution_note"] = _clean_text(note) or None
            if extra_updates:
                item.update(extra_updates)
            item["updated_at"] = now
            payload["items"] = items
            _save_review_queue(payload)
            return item
    return None


def enroll_staff_voiceprint_for_speaker(
    audio_path: Path,
    utterances: list[dict],
    *,
    speaker_id: str,
    staff_id: str | None,
    staff_name: str | None,
    staff_role: str | None,
    source_id: str | None,
) -> bool:
    normalized_staff_id = _clean_text(staff_id)
    normalized_staff_role = _normalize_role(staff_role)
    if not normalized_staff_id or normalized_staff_role not in _STAFF_ROLES:
        return False

    normalized_speaker_id = _clean_text(speaker_id)
    if not normalized_speaker_id:
        return False

    intervals_by_speaker = _eligible_speaker_intervals(utterances)
    intervals = intervals_by_speaker.get(normalized_speaker_id)
    if not intervals:
        return False

    try:
        embeddings_by_speaker = _extract_embeddings_for_speakers(audio_path, {normalized_speaker_id: intervals})
    except Exception as exc:
        logger.warning("speaker voiceprint enrollment skipped: %s", exc)
        return False

    embedding = _normalize_vector(embeddings_by_speaker.get(normalized_speaker_id))
    if not embedding:
        return False

    with _REGISTRY_LOCK:
        registry = _load_registry()
        entries = [item for item in registry.get("staff", []) if isinstance(item, dict)]
        entry = next((item for item in entries if _clean_text(item.get("staff_id")) == normalized_staff_id), None)
        if entry is None:
            entry = {
                "staff_id": normalized_staff_id,
                "staff_name": _clean_text(staff_name),
                "staff_role": normalized_staff_role,
                "sample_count": 0,
                "total_duration_ms": 0,
                "embedding": _rounded_embedding(embedding),
                "sources": [],
            }
            entries.append(entry)

        current_embedding = _normalize_vector(entry.get("embedding"))
        current_count = max(int(entry.get("sample_count") or 0), 0)
        if current_embedding:
            weight = min(current_count, 10)
            merged = [
                current_embedding[index] * weight + embedding[index]
                for index in range(len(embedding))
            ]
            normalized_merged = _normalize_vector(merged)
            if normalized_merged:
                entry["embedding"] = _rounded_embedding(normalized_merged)
        else:
            entry["embedding"] = _rounded_embedding(embedding)

        entry["staff_name"] = _clean_text(staff_name) or _clean_text(entry.get("staff_name"))
        entry["staff_role"] = normalized_staff_role
        entry["sample_count"] = current_count + 1
        entry["total_duration_ms"] = max(int(entry.get("total_duration_ms") or 0), 0) + _total_duration_ms(intervals)
        entry["updated_at"] = _now_iso()

        sources = entry.get("sources")
        if not isinstance(sources, list):
            sources = []
        sources.append(
            {
                "source_id": _clean_text(source_id),
                "speaker_id": normalized_speaker_id,
                "duration_ms": _total_duration_ms(intervals),
                "updated_at": entry["updated_at"],
            }
        )
        entry["sources"] = sources[-_MAX_SOURCE_HISTORY:]

        registry["staff"] = entries
        _save_registry(registry)
        return True


def auto_enroll_staff_voiceprint(
    audio_path: Path,
    utterances: list[dict],
    *,
    staff_id: str | None,
    staff_name: str | None,
    staff_role: str | None,
    source_id: str | None,
    queue_only: bool = False,
) -> bool:
    settings = get_settings()
    normalized_staff_id = _clean_text(staff_id)
    normalized_staff_role = _normalize_role(staff_role)
    if (
        not settings.speaker_voiceprint_enabled
        or not settings.speaker_voiceprint_auto_enroll_enabled
        or not normalized_staff_id
        or normalized_staff_role not in _STAFF_ROLES
    ):
        return False

    speaker_id = _select_enrollment_speaker(
        utterances,
        staff_id=normalized_staff_id,
        staff_role=normalized_staff_role,
    )
    if not speaker_id:
        return False

    registry = _load_registry()
    entries = [item for item in registry.get("staff", []) if isinstance(item, dict)]
    entry = next((item for item in entries if _clean_text(item.get("staff_id")) == normalized_staff_id), None)
    role_sources = set(_speaker_role_sources(utterances, speaker_id))
    matched_staff_id, _ = _speaker_bound_staff(utterances, speaker_id)
    similarity = _speaker_best_similarity(utterances, speaker_id)
    reasons: list[str] = []

    if entry is None:
        reasons.append("missing_staff_template")
    if matched_staff_id != normalized_staff_id:
        reasons.append("missing_bound_staff_match")
    if not role_sources.intersection({"voiceprint_bound_staff", "voiceprint"}):
        reasons.append("missing_voiceprint_role_source")
    if similarity < float(settings.speaker_voiceprint_auto_enroll_threshold):
        reasons.append("low_voiceprint_similarity")

    if reasons:
        queue_staff_voiceprint_review(
            source_id=source_id,
            staff_id=normalized_staff_id,
            staff_name=staff_name,
            staff_role=normalized_staff_role,
            speaker_id=speaker_id,
            utterances=utterances,
            reasons=reasons,
        )
        return False

    if queue_only:
        return False

    return enroll_staff_voiceprint_for_speaker(
        audio_path,
        utterances,
        speaker_id=speaker_id,
        staff_id=normalized_staff_id,
        staff_name=staff_name,
        staff_role=normalized_staff_role,
        source_id=source_id,
    )
