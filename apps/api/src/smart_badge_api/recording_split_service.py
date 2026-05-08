from __future__ import annotations

import hashlib
import json
import math
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smart_badge_api.asr.audio_preprocessing import _resolve_ffmpeg_executable
from smart_badge_api.core.config import get_settings


class RecordingSplitError(RuntimeError):
    """Raised when the audio file cannot be split safely."""


@dataclass(slots=True)
class SplitTranscriptPart:
    utterances: list[dict[str, Any]]
    full_text: str
    duration_ms: int


def _coerce_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _run_ffmpeg(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RecordingSplitError("ffmpeg 不可用，暂不能裁切录音") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = "录音裁切失败，请确认文件格式是否可播放"
        if detail:
            message = f"{message}：{detail[-300:]}"
        raise RecordingSplitError(message) from exc


def split_audio_file(
    source_path: Path,
    first_output_path: Path,
    second_output_path: Path,
    *,
    split_at_ms: int,
) -> None:
    """Split an audio file into two files using ffmpeg stream copy."""

    if split_at_ms <= 0:
        raise RecordingSplitError("裁切时间点必须大于 0")
    if not source_path.is_file():
        raise RecordingSplitError("录音文件不存在")

    first_output_path.parent.mkdir(parents=True, exist_ok=True)
    second_output_path.parent.mkdir(parents=True, exist_ok=True)
    first_output_path.unlink(missing_ok=True)
    second_output_path.unlink(missing_ok=True)

    ffmpeg = _resolve_ffmpeg_executable()
    split_seconds = split_at_ms / 1000
    common = [ffmpeg, "-hide_banner", "-nostdin", "-y"]

    first_command = [
        *common,
        "-i",
        str(source_path),
        "-t",
        f"{split_seconds:.3f}",
        "-map",
        "0:a:0?",
        "-vn",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(first_output_path),
    ]
    second_command = [
        *common,
        "-ss",
        f"{split_seconds:.3f}",
        "-i",
        str(source_path),
        "-map",
        "0:a:0?",
        "-vn",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(second_output_path),
    ]

    try:
        _run_ffmpeg(first_command)
        _run_ffmpeg(second_command)
        if first_output_path.stat().st_size <= 0 or second_output_path.stat().st_size <= 0:
            raise RecordingSplitError("裁切后的录音为空，请调整裁切时间点")
    except Exception:
        first_output_path.unlink(missing_ok=True)
        second_output_path.unlink(missing_ok=True)
        raise


def _copy_utterance_for_part(item: dict[str, Any], *, begin_ms: int, end_ms: int, offset_ms: int) -> dict[str, Any]:
    copied = dict(item)
    copied.setdefault("source_begin_ms", begin_ms)
    copied.setdefault("source_end_ms", end_ms)
    copied["begin_ms"] = max(0, begin_ms - offset_ms)
    copied["end_ms"] = max(copied["begin_ms"], end_ms - offset_ms)
    return copied


def split_transcript_utterances(
    utterances: list[Any],
    *,
    split_at_ms: int,
    total_duration_ms: int | None,
) -> tuple[SplitTranscriptPart, SplitTranscriptPart]:
    first: list[dict[str, Any]] = []
    second: list[dict[str, Any]] = []

    for raw_item in utterances:
        if not isinstance(raw_item, dict):
            continue
        begin_ms = _coerce_int(raw_item.get("begin_ms"))
        end_ms = _coerce_int(raw_item.get("end_ms"))
        text = str(raw_item.get("text") or "").strip()
        if begin_ms is None or end_ms is None or end_ms <= begin_ms or not text:
            continue

        if end_ms <= split_at_ms:
            first.append(_copy_utterance_for_part(raw_item, begin_ms=begin_ms, end_ms=end_ms, offset_ms=0))
            continue
        if begin_ms >= split_at_ms:
            second.append(_copy_utterance_for_part(raw_item, begin_ms=begin_ms, end_ms=end_ms, offset_ms=split_at_ms))
            continue

        before_overlap = max(0, split_at_ms - begin_ms)
        after_overlap = max(0, end_ms - split_at_ms)
        if before_overlap >= after_overlap:
            first.append(_copy_utterance_for_part(raw_item, begin_ms=begin_ms, end_ms=split_at_ms, offset_ms=0))
        else:
            second.append(_copy_utterance_for_part(raw_item, begin_ms=split_at_ms, end_ms=end_ms, offset_ms=split_at_ms))

    first_duration_ms = max(
        [split_at_ms, *[int(item.get("end_ms") or 0) for item in first]],
        default=split_at_ms,
    )
    if total_duration_ms and total_duration_ms > split_at_ms:
        second_duration_ms = total_duration_ms - split_at_ms
    else:
        second_duration_ms = max([int(item.get("end_ms") or 0) for item in second], default=0)

    return (
        SplitTranscriptPart(
            utterances=first,
            full_text="\n".join(str(item.get("text") or "").strip() for item in first if str(item.get("text") or "").strip()),
            duration_ms=max(first_duration_ms, 0),
        ),
        SplitTranscriptPart(
            utterances=second,
            full_text="\n".join(str(item.get("text") or "").strip() for item in second if str(item.get("text") or "").strip()),
            duration_ms=max(second_duration_ms, 0),
        ),
    )


def _archive_recording_id(device_code: str | None, file_id: str) -> str:
    seed = f"{device_code or ''}:{file_id}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def write_split_archive_manifest(
    *,
    recording_id: str,
    parent_recording_id: str,
    part_index: int,
    split_at_ms: int,
    file_name: str,
    audio_path: Path,
    file_size: int | None,
    duration_ms: int | None,
    duration_seconds: int | None,
    status: str,
    created_at: datetime | None,
    device_code: str | None,
    device_id: str | None,
    staff_id: str | None,
    staff_name: str | None,
    staff_role: str | None,
    staff_hospital_code: str | None,
    staff_hospital_short_name: str | None,
    transcript: SplitTranscriptPart | None,
) -> str:
    settings = get_settings()
    stage_root = settings.dingtalk_audio_stage_path
    manifest_dir = stage_root / "manifests"
    transcript_dir = stage_root / "transcripts"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir.mkdir(parents=True, exist_ok=True)

    stage_key = f"split_{parent_recording_id}_part{part_index}_{recording_id}"
    transcript_path: Path | None = None
    if transcript is not None and (transcript.utterances or transcript.full_text):
        transcript_path = transcript_dir / f"{stage_key}.transcript.json"
        transcript_payload = {
            "stageKey": stage_key,
            "deviceCode": device_code,
            "fileId": recording_id,
            "remoteFileName": file_name,
            "audioPath": str(audio_path),
            "asrProvider": "split_from_parent",
            "durationMs": transcript.duration_ms,
            "fullText": transcript.full_text,
            "utterances": transcript.utterances,
        }
        transcript_path.write_text(json.dumps(transcript_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    now = datetime.now(timezone.utc)
    manifest = {
        "stageKey": stage_key,
        "deviceCode": device_code or device_id or "split",
        "deviceId": device_id,
        "staffId": staff_id,
        "staffName": staff_name or "",
        "staffRole": staff_role or "consultant",
        "staffHospitalCode": staff_hospital_code,
        "staffHospitalShortName": staff_hospital_short_name,
        "fileId": recording_id,
        "remoteFileName": file_name,
        "stagedFileName": file_name,
        "audioPath": str(audio_path),
        "fileSize": file_size,
        "durationMs": duration_ms,
        "durationSeconds": duration_seconds,
        "remoteCreatedAt": (created_at or now).isoformat(),
        "status": status,
        "createdAt": now.isoformat(),
        "updatedAt": now.isoformat(),
        "splitParentRecordingId": parent_recording_id,
        "splitPartIndex": part_index,
        "splitAtMs": split_at_ms,
    }
    if transcript_path is not None:
        manifest["transcriptPath"] = str(transcript_path)
        manifest["utteranceCount"] = len(transcript.utterances) if transcript else 0
        manifest["fullTextLength"] = len(transcript.full_text) if transcript else 0

    (manifest_dir / f"{stage_key}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return _archive_recording_id(str(manifest["deviceCode"] or ""), recording_id)
