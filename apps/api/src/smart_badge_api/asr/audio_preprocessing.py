from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from smart_badge_api.core.config import get_settings

logger = logging.getLogger(__name__)

_MEAN_VOLUME_PATTERN = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")
_MAX_VOLUME_PATTERN = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")
_SILENCE_START_PATTERN = re.compile(r"silence_start:\s*(\d+(?:\.\d+)?)")
_SILENCE_END_PATTERN = re.compile(r"silence_end:\s*(\d+(?:\.\d+)?)")
_FFMPEG_DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_FFMPEG_BITRATE_PATTERN = re.compile(r"bitrate:\s*(\d+)\s*kb/s")
_FFMPEG_AUDIO_PATTERN = re.compile(
    r"Audio:\s*(?P<codec>[^,\s]+).*?(?P<sample_rate>\d+)\s*Hz,\s*(?P<channels>mono|stereo|\d+\s*channels?)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class AudioQualityDiagnostic:
    audio_path: str
    file_name: str
    file_size_bytes: int | None
    duration_ms: int | None
    bitrate_bps: int | None
    codec_name: str | None
    sample_rate_hz: int | None
    channels: int | None
    format_name: str | None
    mean_volume_db: float | None
    max_volume_db: float | None
    silence_ratio: float | None
    silence_duration_ms: int | None
    is_too_long: bool
    matches_asr_transcode_profile: bool
    needs_low_volume_gain: bool
    recommended_gain_db: float | None
    diagnostic_error: str | None = None


@dataclass(slots=True)
class AudioPreprocessReport:
    source_id: str | None
    provider: str
    original_path: str
    asr_path: str
    action: str
    applied_gain_db: float | None
    diagnostic: AudioQualityDiagnostic


def _resolve_ffmpeg_executable() -> str:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - defensive runtime path
        raise FileNotFoundError("ffmpeg") from exc


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)


def _coerce_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _coerce_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _probe_format(audio_path: Path) -> dict:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        command = [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(audio_path),
        ]
        result = _run_command(command)
        payload = json.loads(result.stdout or "{}")
        return payload if isinstance(payload, dict) else {}

    command = [_resolve_ffmpeg_executable(), "-hide_banner", "-i", str(audio_path)]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    output = f"{result.stdout}\n{result.stderr}"
    duration_match = _FFMPEG_DURATION_PATTERN.search(output)
    duration_seconds: float | None = None
    if duration_match:
        hours = int(duration_match.group(1))
        minutes = int(duration_match.group(2))
        seconds = float(duration_match.group(3))
        duration_seconds = hours * 3600 + minutes * 60 + seconds

    bitrate_match = _FFMPEG_BITRATE_PATTERN.search(output)
    bitrate_bps = int(bitrate_match.group(1)) * 1000 if bitrate_match else None
    audio_match = _FFMPEG_AUDIO_PATTERN.search(output)
    stream: dict[str, object] = {"codec_type": "audio"}
    if audio_match:
        stream["codec_name"] = audio_match.group("codec").strip().lower()
        stream["sample_rate"] = audio_match.group("sample_rate")
        channel_text = audio_match.group("channels").lower()
        if "mono" in channel_text:
            stream["channels"] = 1
        elif "stereo" in channel_text:
            stream["channels"] = 2
        else:
            channel_match = re.search(r"\d+", channel_text)
            stream["channels"] = channel_match.group(0) if channel_match else None
        if bitrate_bps is not None:
            stream["bit_rate"] = bitrate_bps
        if duration_seconds is not None:
            stream["duration"] = duration_seconds

    return {
        "format": {
            "duration": duration_seconds,
            "bit_rate": bitrate_bps,
            "format_name": audio_path.suffix.lower().lstrip(".") or None,
        },
        "streams": [stream] if len(stream) > 1 else [],
    }


def _detect_volume(audio_path: Path) -> tuple[float | None, float | None]:
    command = [
        _resolve_ffmpeg_executable(),
        "-hide_banner",
        "-nostats",
        "-i",
        str(audio_path),
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    try:
        result = _run_command(command)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        logger.debug("audio volume diagnostic failed for %s: %s", audio_path.name, exc)
        return None, None

    output = f"{result.stdout}\n{result.stderr}"
    mean_match = _MEAN_VOLUME_PATTERN.search(output)
    max_match = _MAX_VOLUME_PATTERN.search(output)
    mean_volume = float(mean_match.group(1)) if mean_match else None
    max_volume = float(max_match.group(1)) if max_match else None
    return mean_volume, max_volume


def _detect_silence_ratio(audio_path: Path, duration_ms: int | None) -> tuple[float | None, int | None]:
    if not duration_ms or duration_ms <= 0:
        return None, None

    command = [
        _resolve_ffmpeg_executable(),
        "-hide_banner",
        "-nostats",
        "-i",
        str(audio_path),
        "-af",
        "silencedetect=noise=-35dB:d=0.5",
        "-f",
        "null",
        "-",
    ]
    try:
        result = _run_command(command)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        logger.debug("audio silence diagnostic failed for %s: %s", audio_path.name, exc)
        return None, None

    output = f"{result.stdout}\n{result.stderr}"
    silence_starts: list[float] = []
    total_silence_seconds = 0.0
    for line in output.splitlines():
        start_match = _SILENCE_START_PATTERN.search(line)
        if start_match:
            silence_starts.append(float(start_match.group(1)))
            continue
        end_match = _SILENCE_END_PATTERN.search(line)
        if end_match and silence_starts:
            start = silence_starts.pop()
            end = float(end_match.group(1))
            total_silence_seconds += max(end - start, 0.0)

    duration_seconds = duration_ms / 1000
    if silence_starts:
        total_silence_seconds += max(duration_seconds - silence_starts[-1], 0.0)

    silence_ms = max(int(round(total_silence_seconds * 1000)), 0)
    return min(max(silence_ms / duration_ms, 0.0), 1.0), silence_ms


def _choose_low_volume_gain(mean_volume_db: float | None, max_volume_db: float | None) -> float | None:
    settings = get_settings()
    if not settings.asr_low_volume_gain_enabled:
        return None
    if mean_volume_db is None or mean_volume_db > settings.asr_low_volume_mean_db_threshold:
        return None

    gain_candidates = [
        max(settings.asr_low_volume_target_mean_db - mean_volume_db, 0.0),
        max(settings.asr_low_volume_max_gain_db, 0.0),
    ]
    if max_volume_db is not None:
        gain_candidates.append(max(-settings.asr_low_volume_headroom_db - max_volume_db, 0.0))

    gain_db = min(gain_candidates)
    if gain_db < settings.asr_low_volume_min_gain_db:
        return None
    return round(gain_db, 2)


def _matches_asr_transcode_profile(
    *,
    codec_name: str | None,
    sample_rate_hz: int | None,
    channels: int | None,
    bitrate_bps: int | None,
) -> bool:
    if codec_name != "mp3" or sample_rate_hz != 16000 or channels != 1:
        return False
    if bitrate_bps is None:
        return True
    return 32000 <= bitrate_bps <= 48000


def diagnose_audio_quality(audio_path: Path, *, duration_seconds: int | None = None) -> AudioQualityDiagnostic:
    settings = get_settings()
    resolved_path = audio_path.resolve()
    file_size_bytes = resolved_path.stat().st_size if resolved_path.exists() else None
    try:
        payload = _probe_format(resolved_path)
        format_payload = payload.get("format") if isinstance(payload.get("format"), dict) else {}
        streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
        audio_stream = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"), {})

        duration_value = _coerce_float(format_payload.get("duration")) or _coerce_float(audio_stream.get("duration"))
        resolved_duration_ms = (
            max(int(round(duration_value * 1000)), 1)
            if duration_value and duration_value > 0
            else (duration_seconds * 1000 if duration_seconds else None)
        )
        bitrate_bps = _coerce_int(format_payload.get("bit_rate")) or _coerce_int(audio_stream.get("bit_rate"))
        codec_name = str(audio_stream.get("codec_name") or "").strip() or None
        sample_rate_hz = _coerce_int(audio_stream.get("sample_rate"))
        channels = _coerce_int(audio_stream.get("channels"))
        format_name = str(format_payload.get("format_name") or "").strip() or None
        mean_volume_db, max_volume_db = _detect_volume(resolved_path)
        silence_ratio, silence_duration_ms = _detect_silence_ratio(resolved_path, resolved_duration_ms)
        recommended_gain_db = _choose_low_volume_gain(mean_volume_db, max_volume_db)
        is_too_long = bool(
            resolved_duration_ms
            and settings.dingtalk_audio_max_duration_seconds > 0
            and resolved_duration_ms > settings.dingtalk_audio_max_duration_seconds * 1000
        )
        return AudioQualityDiagnostic(
            audio_path=str(resolved_path),
            file_name=resolved_path.name,
            file_size_bytes=file_size_bytes,
            duration_ms=resolved_duration_ms,
            bitrate_bps=bitrate_bps,
            codec_name=codec_name,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
            format_name=format_name,
            mean_volume_db=mean_volume_db,
            max_volume_db=max_volume_db,
            silence_ratio=silence_ratio,
            silence_duration_ms=silence_duration_ms,
            is_too_long=is_too_long,
            matches_asr_transcode_profile=_matches_asr_transcode_profile(
                codec_name=codec_name,
                sample_rate_hz=sample_rate_hz,
                channels=channels,
                bitrate_bps=bitrate_bps,
            ),
            needs_low_volume_gain=recommended_gain_db is not None,
            recommended_gain_db=recommended_gain_db,
        )
    except Exception as exc:
        logger.warning("audio quality diagnostic failed for %s: %s", resolved_path.name, exc)
        return AudioQualityDiagnostic(
            audio_path=str(resolved_path),
            file_name=resolved_path.name,
            file_size_bytes=file_size_bytes,
            duration_ms=duration_seconds * 1000 if duration_seconds else None,
            bitrate_bps=None,
            codec_name=None,
            sample_rate_hz=None,
            channels=None,
            format_name=None,
            mean_volume_db=None,
            max_volume_db=None,
            silence_ratio=None,
            silence_duration_ms=None,
            is_too_long=False,
            matches_asr_transcode_profile=False,
            needs_low_volume_gain=False,
            recommended_gain_db=None,
            diagnostic_error=str(exc),
        )


def _write_audio_quality_report(report: AudioPreprocessReport) -> None:
    path = get_settings().resolved_asr_audio_quality_log_path
    payload = {
        "occurred_at": datetime.now(UTC).isoformat(),
        **asdict(report),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("failed to write audio quality diagnostic log: %s", path)


def _render_gain_filter(gain_db: float) -> str:
    return f"volume={gain_db:.2f}dB,alimiter=limit=0.95"


def _normalize_low_volume_audio(source_path: Path, target_path: Path, gain_db: float) -> None:
    bitrate_kbps = max(get_settings().asr_low_volume_output_bitrate_kbps, 40)
    command = [
        _resolve_ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-af",
        _render_gain_filter(gain_db),
        "-b:a",
        f"{bitrate_kbps}k",
        str(target_path),
    ]
    _run_command(command)


@contextmanager
def prepare_audio_for_asr(
    audio_path: Path,
    *,
    provider: str,
    source_id: str | None = None,
    duration_seconds: int | None = None,
) -> Iterator[Path]:
    if not get_settings().asr_audio_quality_diagnostics_enabled:
        yield audio_path
        return

    diagnostic = diagnose_audio_quality(audio_path, duration_seconds=duration_seconds)
    report = AudioPreprocessReport(
        source_id=source_id,
        provider=provider,
        original_path=str(audio_path.resolve()),
        asr_path=str(audio_path.resolve()),
        action="diagnose_only",
        applied_gain_db=None,
        diagnostic=diagnostic,
    )

    gain_db = diagnostic.recommended_gain_db
    if gain_db is None:
        _write_audio_quality_report(report)
        yield audio_path
        return

    with tempfile.TemporaryDirectory(prefix="asr-low-volume-") as temp_dir:
        target_path = Path(temp_dir) / f"{audio_path.stem}.normalized.mp3"
        try:
            _normalize_low_volume_audio(audio_path, target_path, gain_db)
            report.asr_path = str(target_path)
            report.action = "low_volume_gain"
            report.applied_gain_db = gain_db
            logger.info(
                "Applied low-volume ASR gain for %s: gain_db=%.2f mean_db=%s max_db=%s",
                audio_path.name,
                gain_db,
                diagnostic.mean_volume_db,
                diagnostic.max_volume_db,
            )
            _write_audio_quality_report(report)
            yield target_path
        except Exception as exc:
            report.action = "low_volume_gain_failed"
            report.applied_gain_db = gain_db
            _write_audio_quality_report(report)
            logger.warning("low-volume normalization failed for %s, using original audio: %s", audio_path.name, exc)
            yield audio_path
