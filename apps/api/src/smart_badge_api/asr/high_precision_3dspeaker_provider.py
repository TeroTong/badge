"""High-precision ASR provider based on Whisper large-v3 + 3D-Speaker.

设计目标：
- 文本精度优先：Whisper large-v3 负责高精度中文转写
- 说话人精度优先：3D-Speaker 负责 diarization 与 SPEAKER_XX 保留
- 为后续员工声纹绑定保留原始 speaker_id
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

from smart_badge_api.core.config import get_settings

from .sensevoice_3dspeaker_provider import (
    _DiarizationSegment,
    _TimedToken,
    _apply_role_classification,
    _assign_speakers_to_tokens,
    _finalize_text,
    _get_diarizer,
    _is_contentful_text,
    _merge_sentence_utterances,
    _ordered_speakers_for_interval,
    _prepare_audio_path,
)

logger = logging.getLogger(__name__)

_whisper_model: Any = None
_whisper_lock = Lock()
_MAX_WHISPER_HOTWORDS = 48
_MAX_WHISPER_HOTWORD_CHARS = 160

_DEFAULT_HOTWORD_FILE = Path(__file__).resolve().parents[3] / "scripts" / "asr_hotwords_default.txt"


def _load_hotwords(path: Path | None) -> list[str]:
    if path is None or not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _resolve_hotword_file() -> Path | None:
    settings = get_settings()
    explicit = settings.whisper_hotword_file.strip()
    if explicit:
        return settings.resolve_path(explicit)
    if _DEFAULT_HOTWORD_FILE.exists():
        return _DEFAULT_HOTWORD_FILE
    return None


def _build_whisper_hotwords(hotwords: list[str]) -> str | None:
    if not hotwords:
        return None
    unique_hotwords: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    for item in hotwords:
        term = item.strip()
        if not term or term in seen:
            continue
        next_chars = total_chars + len(term) + (1 if unique_hotwords else 0)
        if unique_hotwords and next_chars > _MAX_WHISPER_HOTWORD_CHARS:
            break
        seen.add(term)
        unique_hotwords.append(term)
        total_chars = next_chars
        if len(unique_hotwords) >= _MAX_WHISPER_HOTWORDS or total_chars >= _MAX_WHISPER_HOTWORD_CHARS:
            break
    return ",".join(unique_hotwords) if unique_hotwords else None


def _resolve_whisper_device_label() -> str:
    settings = get_settings()
    if settings.whisper_device != "auto":
        return settings.whisper_device
    try:
        import torch
    except Exception:
        return "cpu"
    return "cuda" if torch.cuda.is_available() else "cpu"


def _get_whisper_model() -> Any:
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model

    with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model

        from faster_whisper import WhisperModel

        settings = get_settings()
        device = _resolve_whisper_device_label()
        download_root = str(settings.resolved_whisper_cache_path)
        logger.info(
            "Loading high-precision Whisper model: size=%s device=%s compute=%s download_root=%s",
            settings.whisper_model_size,
            device,
            settings.whisper_compute_type,
            download_root,
        )
        _whisper_model = WhisperModel(
            settings.whisper_model_size,
            device=device,
            compute_type=settings.whisper_compute_type,
            download_root=download_root,
            local_files_only=settings.whisper_local_files_only,
        )
        logger.info("High-precision Whisper model loaded successfully")
        return _whisper_model


def _segments_to_word_tokens(segments: Iterable[Any]) -> tuple[list[_TimedToken], list[dict]]:
    tokens: list[_TimedToken] = []
    fallback_utterances: list[dict] = []

    for segment in segments:
        segment_text = _finalize_text(getattr(segment, "text", ""))
        segment_start = getattr(segment, "start", None)
        segment_end = getattr(segment, "end", None)
        words = list(getattr(segment, "words", None) or [])

        if words:
            for word in words:
                text = _finalize_text(getattr(word, "word", ""))
                start = getattr(word, "start", None)
                end = getattr(word, "end", None)
                if not text or start is None or end is None:
                    continue
                begin_ms = max(int(round(float(start) * 1000)), 0)
                end_ms = max(int(round(float(end) * 1000)), begin_ms)
                tokens.append(_TimedToken(text=text, begin_ms=begin_ms, end_ms=end_ms))
            continue

        if segment_text and segment_start is not None and segment_end is not None and _is_contentful_text(segment_text):
            fallback_utterances.append(
                {
                    "speaker": "unknown",
                    "speaker_id": "unknown",
                    "text": segment_text,
                    "begin_ms": max(int(round(float(segment_start) * 1000)), 0),
                    "end_ms": max(int(round(float(segment_end) * 1000)), 0),
                }
            )

    return tokens, fallback_utterances


def _assign_speakers_to_fallback_utterances(
    utterances: list[dict],
    diarization_segments: list[_DiarizationSegment],
) -> list[dict]:
    for utterance in utterances:
        ordered = _ordered_speakers_for_interval(
            int(utterance.get("begin_ms") or 0),
            int(utterance.get("end_ms") or 0),
            diarization_segments,
        )
        speaker_id = ordered[0] if ordered else "unknown"
        utterance["speaker"] = speaker_id
        utterance["speaker_id"] = speaker_id
    return utterances


def _run_transcription(audio_path: Path) -> tuple[list[_TimedToken], list[dict]]:
    settings = get_settings()
    model = _get_whisper_model()
    hotword_file = _resolve_hotword_file()
    hotwords = _load_hotwords(hotword_file)
    hotword_text = _build_whisper_hotwords(hotwords) if settings.whisper_hotwords_enabled else None

    segments_iter, info = model.transcribe(
        str(audio_path),
        language="zh",
        beam_size=settings.whisper_beam_size,
        best_of=settings.whisper_best_of,
        patience=settings.whisper_patience,
        length_penalty=settings.whisper_length_penalty,
        repetition_penalty=settings.whisper_repetition_penalty,
        condition_on_previous_text=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": settings.whisper_vad_min_silence_duration_ms},
        word_timestamps=settings.whisper_word_timestamps,
        hotwords=hotword_text,
        temperature=[0.0, 0.2, 0.4],
    )
    segments = list(segments_iter)
    logger.info(
        "High-precision Whisper done: language=%s prob=%.2f duration=%.1fs segments=%d",
        info.language,
        info.language_probability,
        info.duration,
        len(segments),
    )
    return _segments_to_word_tokens(segments)


def _sort_utterances(utterances: list[dict]) -> list[dict]:
    return sorted(
        utterances,
        key=lambda item: (
            int(item.get("begin_ms") or 0),
            int(item.get("end_ms") or 0),
            str(item.get("speaker_id") or ""),
            str(item.get("text") or ""),
        ),
    )


def transcribe_audio(audio_path: str | Path) -> list[dict]:
    resolved_audio_path = Path(audio_path)
    diarizer = _get_diarizer()
    prepared_audio_path, cleanup = _prepare_audio_path(resolved_audio_path)

    logger.info("Transcribing with high-precision Whisper + 3D-Speaker: %s", resolved_audio_path)
    started_at = time.perf_counter()
    try:
        diarization_segments = diarizer.diarize(prepared_audio_path)
        tokens, fallback_utterances = _run_transcription(prepared_audio_path)
        _assign_speakers_to_tokens(tokens, diarization_segments)

        settings = get_settings()
        utterances = _merge_sentence_utterances(
            tokens,
            diarization_segments,
            gap_ms=max(int(settings.sensevoice_utterance_gap_seconds * 1000), 200),
            punctuation_pause_ms=max(int(settings.sensevoice_punctuation_pause_seconds * 1000), 100),
        )

        if not utterances and fallback_utterances:
            utterances = _assign_speakers_to_fallback_utterances(fallback_utterances, diarization_segments)

        for utterance in utterances:
            utterance["text"] = _finalize_text(utterance.get("text") or "")

        utterances = [item for item in utterances if str(item.get("text") or "").strip()]
        utterances = _sort_utterances(utterances)
        utterances = _apply_role_classification(utterances)

        elapsed = time.perf_counter() - started_at
        logger.info(
            "High-precision Whisper + 3D-Speaker done: %d utterances, %d diar segments in %.1fs",
            len(utterances),
            len(diarization_segments),
            elapsed,
        )
        return utterances
    finally:
        cleanup()
