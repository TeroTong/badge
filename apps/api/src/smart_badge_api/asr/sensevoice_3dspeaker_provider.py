"""SenseVoice + 3D-Speaker provider.

高精度转写链路：
- SenseVoice 负责中文 ASR 与时间戳输出
- 3D-Speaker 负责说话人分离
- 现有 LLM 角色分类负责把 SPEAKER_XX 映射为 consultant/customer/doctor

说明：
- 为了避免把整个 3D-Speaker 推理脚本直接拉进主服务，这里只复用其
  `speakerlab` 模型代码，并保留不依赖 pyannote overlap 的音频分离主链路。
- `speaker_id` 永远保留原始 SPEAKER_XX，方便后续做员工声纹绑定。
"""

from __future__ import annotations

import importlib
import logging
import math
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from smart_badge_api.core.config import get_settings

logger = logging.getLogger(__name__)

_sensevoice_model: Any = None
_sensevoice_postprocess = None
_sensevoice_lock = Lock()

_diarizer = None
_diarizer_lock = Lock()

_PUNCTUATION_ENDINGS = tuple(",.!?;:，。！？；：")
_SPECIAL_TAG_PATTERN = re.compile(r"<\|[^>]+?\|>")
_CONTENT_PATTERN = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]")
_MAX_MERGED_UTTERANCE_MS = 45_000

_EMBEDDING_MODEL_CONFIGS: dict[str, dict[str, Any]] = {
    "iic/speech_campplus_sv_zh_en_16k-common_advanced": {
        "revision": "v1.0.0",
        "module": "speakerlab.models.campplus.DTDNN",
        "class_name": "CAMPPlus",
        "model_kwargs": {
            "feat_dim": 80,
            "embedding_size": 192,
        },
        "checkpoint_name": "campplus_cn_en_common.pt",
    },
    "iic/speech_eres2netv2_sv_zh-cn_16k-common": {
        "revision": "v1.0.1",
        "module": "speakerlab.models.eres2net.ERes2NetV2",
        "class_name": "ERes2NetV2",
        "model_kwargs": {
            "feat_dim": 80,
            "embedding_size": 192,
            "baseWidth": 26,
            "scale": 2,
            "expansion": 2,
        },
        "checkpoint_name": "pretrained_eres2netv2.ckpt",
    },
}


@dataclass(slots=True)
class _TimedToken:
    text: str
    begin_ms: int
    end_ms: int
    speaker_id: str = "unknown"


@dataclass(slots=True)
class _DiarizationSegment:
    begin_ms: int
    end_ms: int
    speaker_id: str


def _normalize_device_label(value: str) -> str:
    if value != "auto":
        return value
    import torch

    return "cuda:0" if torch.cuda.is_available() else "cpu"


def _ensure_3dspeaker_repo_on_path() -> Path:
    repo_path = get_settings().resolved_threed_speaker_repo_path
    if not repo_path.exists():
        raise RuntimeError(
            "3D-Speaker repo not found. 请先执行安装脚本，或设置 THREED_SPEAKER_REPO_PATH 指向有效目录。"
        )
    repo_str = str(repo_path)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)
    return repo_path


def _dynamic_import(module_name: str, class_name: str) -> Any:
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def _download_model(model_id: str, revision: str, cache_dir: Path) -> Path:
    from modelscope.hub.snapshot_download import snapshot_download

    cache_dir.mkdir(parents=True, exist_ok=True)
    return Path(snapshot_download(model_id, revision=revision, cache_dir=str(cache_dir)))


def _clean_token_text(value: object) -> str:
    text = str(value or "")
    text = _SPECIAL_TAG_PATTERN.sub("", text)
    return text.strip()


def _finalize_text(value: object) -> str:
    text = _clean_token_text(value)
    return re.sub(r"\s+", " ", text).strip()


def _is_contentful_text(text: str) -> bool:
    return bool(_CONTENT_PATTERN.search(text))


def _join_text(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    if right[0] in _PUNCTUATION_ENDINGS or left[-1] in "([{" or right[0] in ")]}":
        return f"{left}{right}"
    if left[-1].isascii() and right[0].isascii() and left[-1].isalnum() and right[0].isalnum():
        return f"{left} {right}"
    return f"{left}{right}"


def _compress_segments(segments: list[_DiarizationSegment]) -> list[_DiarizationSegment]:
    if not segments:
        return []
    merged = [segments[0]]
    for item in segments[1:]:
        prev = merged[-1]
        if item.speaker_id == prev.speaker_id and item.begin_ms <= prev.end_ms + 50:
            prev.end_ms = max(prev.end_ms, item.end_ms)
            continue
        if item.begin_ms < prev.end_ms:
            pivot = (item.begin_ms + prev.end_ms) // 2
            prev.end_ms = pivot
            item.begin_ms = pivot
        merged.append(item)
    return merged


def _assign_speakers_to_tokens(
    tokens: list[_TimedToken],
    diarization_segments: list[_DiarizationSegment],
) -> list[_TimedToken]:
    if not diarization_segments:
        return tokens

    for token in tokens:
        best_speaker = "unknown"
        best_overlap = -1
        token_mid = (token.begin_ms + token.end_ms) / 2
        for segment in diarization_segments:
            overlap = min(token.end_ms, segment.end_ms) - max(token.begin_ms, segment.begin_ms)
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = segment.speaker_id
            elif best_overlap <= 0 and segment.begin_ms <= token_mid <= segment.end_ms:
                best_overlap = 0
                best_speaker = segment.speaker_id
        token.speaker_id = best_speaker
    return tokens


def _merge_tokens_into_utterances(
    tokens: list[_TimedToken],
    *,
    gap_ms: int,
    punctuation_pause_ms: int,
) -> list[dict]:
    utterances: list[dict] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        text = _finalize_text(current.get("text") or "")
        if text and _is_contentful_text(text):
            utterances.append(
                {
                    "speaker": current["speaker_id"],
                    "speaker_id": current["speaker_id"],
                    "text": text,
                    "begin_ms": int(current["begin_ms"]),
                    "end_ms": int(current["end_ms"]),
                }
            )
        current = None

    for token in tokens:
        text = token.text.strip()
        if not text:
            continue
        if current is None:
            current = {
                "speaker_id": token.speaker_id,
                "text": text,
                "begin_ms": token.begin_ms,
                "end_ms": token.end_ms,
            }
            continue

        gap = max(token.begin_ms - int(current["end_ms"]), 0)
        current_text = str(current["text"])
        should_split = (
            token.speaker_id != current["speaker_id"]
            or gap > gap_ms
            or (gap > punctuation_pause_ms and current_text.endswith(_PUNCTUATION_ENDINGS))
        )

        if should_split:
            flush()
            current = {
                "speaker_id": token.speaker_id,
                "text": text,
                "begin_ms": token.begin_ms,
                "end_ms": token.end_ms,
            }
            continue

        current["text"] = _join_text(current_text, text)
        current["end_ms"] = max(int(current["end_ms"]), token.end_ms)

    flush()
    return utterances


def _ordered_speakers_for_interval(
    begin_ms: int,
    end_ms: int,
    diarization_segments: list[_DiarizationSegment],
) -> list[str]:
    overlap_per_speaker: dict[str, int] = {}
    for segment in diarization_segments:
        overlap = min(end_ms, segment.end_ms) - max(begin_ms, segment.begin_ms)
        if overlap > 0:
            overlap_per_speaker[segment.speaker_id] = overlap_per_speaker.get(segment.speaker_id, 0) + overlap

    if overlap_per_speaker:
        return [
            speaker_id
            for speaker_id, _ in sorted(
                overlap_per_speaker.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]

    midpoint = (begin_ms + end_ms) / 2
    for segment in diarization_segments:
        if segment.begin_ms <= midpoint <= segment.end_ms:
            return [segment.speaker_id]
    return []


def _merge_sentence_utterances(
    tokens: list[_TimedToken],
    diarization_segments: list[_DiarizationSegment],
    *,
    gap_ms: int,
    punctuation_pause_ms: int,
) -> list[dict]:
    if not tokens:
        return []

    sentences: list[list[_TimedToken]] = []
    current_sentence: list[_TimedToken] = []
    last_end_ms: int | None = None

    def flush_sentence() -> None:
        nonlocal current_sentence, last_end_ms
        if current_sentence:
            sentences.append(current_sentence)
        current_sentence = []
        last_end_ms = None

    for token in tokens:
        text = _finalize_text(token.text)
        if not text:
            continue

        if current_sentence and last_end_ms is not None:
            gap = max(token.begin_ms - last_end_ms, 0)
            prev_text = _finalize_text(current_sentence[-1].text)
            if gap > gap_ms or (gap > punctuation_pause_ms and prev_text.endswith(_PUNCTUATION_ENDINGS)):
                flush_sentence()

        current_sentence.append(token)
        last_end_ms = token.end_ms

        if text.endswith(_PUNCTUATION_ENDINGS):
            flush_sentence()

    flush_sentence()

    utterances: list[dict] = []
    last_speaker_id = "unknown"
    for sentence in sentences:
        begin_ms = sentence[0].begin_ms
        end_ms = sentence[-1].end_ms
        text = ""
        for token in sentence:
            text = _join_text(text, _finalize_text(token.text))
        text = _finalize_text(text)
        if not text or not _is_contentful_text(text):
            continue

        ordered_speakers = _ordered_speakers_for_interval(begin_ms, end_ms, diarization_segments)
        speaker_id = (
            ordered_speakers[0]
            if ordered_speakers
            else sentence[0].speaker_id
        )
        if speaker_id == "unknown" and last_speaker_id != "unknown":
            speaker_id = last_speaker_id

        if (
            utterances
            and utterances[-1]["speaker_id"] == speaker_id
            and begin_ms <= int(utterances[-1]["end_ms"]) + gap_ms
            and end_ms - int(utterances[-1]["begin_ms"]) <= _MAX_MERGED_UTTERANCE_MS
        ):
            utterances[-1]["text"] = _join_text(str(utterances[-1]["text"]), text)
            utterances[-1]["end_ms"] = max(int(utterances[-1]["end_ms"]), end_ms)
        else:
            utterances.append(
                {
                    "speaker": speaker_id,
                    "speaker_id": speaker_id,
                    "text": text,
                    "begin_ms": begin_ms,
                    "end_ms": end_ms,
                }
            )
        last_speaker_id = speaker_id

    return utterances


def _split_diarization_segment(
    segment: _DiarizationSegment,
    *,
    max_window_ms: int,
) -> list[_DiarizationSegment]:
    if max_window_ms <= 0 or segment.end_ms - segment.begin_ms <= max_window_ms:
        return [segment]

    windows: list[_DiarizationSegment] = []
    cursor = segment.begin_ms
    while cursor < segment.end_ms:
        window_end = min(cursor + max_window_ms, segment.end_ms)
        windows.append(
            _DiarizationSegment(
                begin_ms=cursor,
                end_ms=window_end,
                speaker_id=segment.speaker_id,
            )
        )
        cursor = window_end
    return windows


def _build_speaker_windows(
    diarization_segments: list[_DiarizationSegment],
    *,
    max_window_ms: int,
    merge_gap_ms: int,
) -> list[_DiarizationSegment]:
    windows: list[_DiarizationSegment] = []
    for raw_segment in diarization_segments:
        if raw_segment.end_ms <= raw_segment.begin_ms:
            continue
        for segment in _split_diarization_segment(raw_segment, max_window_ms=max_window_ms):
            if (
                windows
                and windows[-1].speaker_id == segment.speaker_id
                and segment.begin_ms <= windows[-1].end_ms + merge_gap_ms
                and segment.end_ms - windows[-1].begin_ms <= max_window_ms
            ):
                windows[-1].end_ms = max(windows[-1].end_ms, segment.end_ms)
                continue
            windows.append(
                _DiarizationSegment(
                    begin_ms=segment.begin_ms,
                    end_ms=segment.end_ms,
                    speaker_id=segment.speaker_id,
                )
            )
    return windows


def _apply_role_classification(utterances: list[dict]) -> list[dict]:
    if not utterances or not get_settings().sensevoice_role_classification_enabled:
        for utterance in utterances:
            utterance.setdefault("speaker", utterance.get("speaker_id") or "unknown")
        return utterances

    from smart_badge_api.asr.speaker_classifier import classify_speakers

    prepared: list[dict] = []
    for utterance in utterances:
        prepared.append(
            {
                "speaker": utterance.get("speaker_id") or "unknown",
                "text": utterance.get("text") or "",
                "begin_ms": utterance.get("begin_ms") or 0,
                "end_ms": utterance.get("end_ms") or 0,
            }
        )

    classified = classify_speakers(prepared)
    for original, resolved in zip(utterances, classified):
        speaker_role = str(resolved.get("speaker") or original.get("speaker_id") or "unknown")
        original["speaker"] = speaker_role
        original["speaker_role"] = speaker_role
    return utterances


def _normalize_sensevoice_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        return [payload]
    if isinstance(payload, list):
        if payload and isinstance(payload[0], list):
            nested = payload[0]
            return [item for item in nested if isinstance(item, dict)]
        return [item for item in payload if isinstance(item, dict)]
    return []


def _extract_timed_tokens(sensevoice_item: dict[str, Any]) -> list[_TimedToken]:
    raw_timestamps = sensevoice_item.get("timestamp")
    if not isinstance(raw_timestamps, list):
        return []

    tokens: list[_TimedToken] = []
    if raw_timestamps and isinstance(raw_timestamps[0], (list, tuple)) and len(raw_timestamps[0]) >= 3:
        for item in raw_timestamps:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            text = _finalize_text(item[0])
            if not text:
                continue
            try:
                begin_ms = max(int(round(float(item[1]) * 1000)), 0)
                end_ms = max(int(round(float(item[2]) * 1000)), begin_ms)
            except (TypeError, ValueError):
                continue
            tokens.append(_TimedToken(text=text, begin_ms=begin_ms, end_ms=end_ms))
        return tokens

    raw_text = _SPECIAL_TAG_PATTERN.sub("", str(sensevoice_item.get("text") or ""))
    raw_units = [char for char in raw_text if not char.isspace()]
    usable_count = min(len(raw_units), len(raw_timestamps))
    if usable_count == 0:
        return []
    if usable_count != len(raw_units) or usable_count != len(raw_timestamps):
        logger.warning(
            "SenseVoice timestamp/text length mismatch: text_units=%d timestamps=%d usable=%d",
            len(raw_units),
            len(raw_timestamps),
            usable_count,
        )

    for text, item in zip(raw_units[:usable_count], raw_timestamps[:usable_count], strict=False):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            begin_ms = max(int(round(float(item[0]))), 0)
            end_ms = max(int(round(float(item[1]))), begin_ms)
        except (TypeError, ValueError):
            continue
        tokens.append(_TimedToken(text=text, begin_ms=begin_ms, end_ms=end_ms))
    return tokens


def _get_sensevoice_model() -> tuple[Any, Any]:
    global _sensevoice_model, _sensevoice_postprocess
    if _sensevoice_model is not None and _sensevoice_postprocess is not None:
        return _sensevoice_model, _sensevoice_postprocess

    with _sensevoice_lock:
        if _sensevoice_model is not None and _sensevoice_postprocess is not None:
            return _sensevoice_model, _sensevoice_postprocess

        from funasr import AutoModel
        from funasr.utils.postprocess_utils import rich_transcription_postprocess

        settings = get_settings()
        kwargs: dict[str, Any] = {
            "model": settings.sensevoice_model_id,
            "trust_remote_code": True,
            "device": _normalize_device_label(settings.sensevoice_device),
        }
        if settings.sensevoice_enable_vad and settings.sensevoice_vad_model.strip():
            kwargs["vad_model"] = settings.sensevoice_vad_model.strip()
            kwargs["vad_kwargs"] = {
                "max_single_segment_time": settings.sensevoice_vad_max_single_segment_time_ms,
            }

        logger.info(
            "Loading SenseVoice model: model=%s device=%s",
            settings.sensevoice_model_id,
            kwargs["device"],
        )
        _sensevoice_model = AutoModel(**kwargs)
        _sensevoice_postprocess = rich_transcription_postprocess
        logger.info("SenseVoice model loaded successfully")
        return _sensevoice_model, _sensevoice_postprocess


def _prepare_audio_path(audio_path: Path) -> tuple[Path, callable]:
    if audio_path.suffix.lower() == ".wav":
        return audio_path, lambda: None

    import soundfile as sf

    data, sample_rate = sf.read(str(audio_path))
    fd, temp_name = tempfile.mkstemp(suffix=".wav", prefix="sensevoice_")
    Path(temp_name).unlink(missing_ok=True)
    temp_path = Path(temp_name)
    sf.write(str(temp_path), data, sample_rate)

    def cleanup() -> None:
        temp_path.unlink(missing_ok=True)

    return temp_path, cleanup


def _load_waveform_for_sensevoice(audio_path: Path, *, target_sample_rate: int = 16000) -> Any:
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    waveform, sample_rate = sf.read(str(audio_path), always_2d=False)
    if isinstance(waveform, np.ndarray) and waveform.ndim > 1:
        waveform = waveform.mean(axis=1)

    waveform = np.asarray(waveform, dtype=np.float32).reshape(-1)
    if not waveform.size:
        raise RuntimeError(f"Audio file is empty: {audio_path}")

    if sample_rate != target_sample_rate:
        up = target_sample_rate
        down = sample_rate
        factor = math.gcd(up, down)
        waveform = resample_poly(waveform, up // factor, down // factor).astype(np.float32)

    return waveform


def _run_sensevoice_generate(model: Any, waveform: Any) -> dict[str, Any] | None:
    settings = get_settings()
    generate_kwargs: dict[str, Any] = {
        "input": waveform,
        "cache": {},
        "fs": 16000,
        "language": settings.sensevoice_language,
        "use_itn": settings.sensevoice_use_itn,
        "output_timestamp": True,
    }
    if settings.sensevoice_enable_vad:
        generate_kwargs["batch_size_s"] = settings.sensevoice_batch_size_s
        generate_kwargs["merge_vad"] = True
        generate_kwargs["merge_length_s"] = settings.sensevoice_merge_length_s
    else:
        generate_kwargs["batch_size"] = 64

    items = _normalize_sensevoice_items(model.generate(**generate_kwargs))
    if not items:
        return None
    return items[0]


def _slice_waveform(
    waveform: Any,
    *,
    begin_ms: int,
    end_ms: int,
    sample_rate: int = 16000,
) -> Any:
    start_index = max(int(begin_ms * sample_rate / 1000), 0)
    end_index = max(int(math.ceil(end_ms * sample_rate / 1000)), start_index + 1)
    return waveform[start_index:end_index]


def _transcribe_speaker_windows(
    model: Any,
    waveform: Any,
    diarization_segments: list[_DiarizationSegment],
) -> tuple[list[_TimedToken], list[dict]]:
    settings = get_settings()
    max_window_ms = max(int(settings.sensevoice_speaker_window_seconds * 1000), 5_000)
    merge_gap_ms = max(int(settings.sensevoice_speaker_merge_gap_seconds * 1000), 0)
    padding_ms = max(int(settings.sensevoice_speaker_padding_ms), 0)
    total_duration_ms = int(round(len(waveform) * 1000 / 16000))

    windows = _build_speaker_windows(
        diarization_segments,
        max_window_ms=max_window_ms,
        merge_gap_ms=merge_gap_ms,
    )

    tokens: list[_TimedToken] = []
    fallback_utterances: list[dict] = []
    for window in windows:
        padded_begin_ms = max(window.begin_ms - padding_ms, 0)
        padded_end_ms = min(window.end_ms + padding_ms, total_duration_ms)
        chunk_waveform = _slice_waveform(
            waveform,
            begin_ms=padded_begin_ms,
            end_ms=padded_end_ms,
        )
        if len(chunk_waveform) == 0:
            continue

        item = _run_sensevoice_generate(model, chunk_waveform)
        if not item:
            continue

        chunk_tokens = _extract_timed_tokens(item)
        if chunk_tokens:
            for token in chunk_tokens:
                token.begin_ms += padded_begin_ms
                token.end_ms += padded_begin_ms
                token.speaker_id = window.speaker_id
                tokens.append(token)
            continue

        raw_text = _finalize_text(item.get("text") or "")
        if raw_text:
            fallback_utterances.append(
                {
                    "speaker": window.speaker_id,
                    "speaker_id": window.speaker_id,
                    "text": raw_text,
                    "begin_ms": window.begin_ms,
                    "end_ms": window.end_ms,
                }
            )

    tokens.sort(key=lambda item: (item.begin_ms, item.end_ms, item.speaker_id, item.text))
    return tokens, fallback_utterances


def _transcribe_whole_audio(
    model: Any,
    waveform: Any,
    diarization_segments: list[_DiarizationSegment],
) -> tuple[list[_TimedToken], list[dict]]:
    item = _run_sensevoice_generate(model, waveform)
    if not item:
        return [], []

    timed_tokens = _extract_timed_tokens(item)
    _assign_speakers_to_tokens(timed_tokens, diarization_segments)

    fallback_utterances: list[dict] = []
    raw_text = _finalize_text(item.get("text") or "")
    if raw_text:
        speaker_id = diarization_segments[0].speaker_id if diarization_segments else "unknown"
        fallback_utterances.append(
            {
                "speaker": speaker_id,
                "speaker_id": speaker_id,
                "text": raw_text,
                "begin_ms": diarization_segments[0].begin_ms if diarization_segments else 0,
                "end_ms": diarization_segments[-1].end_ms if diarization_segments else 0,
            }
        )

    return timed_tokens, fallback_utterances


class _Simple3DSpeakerDiarizer:
    def __init__(self) -> None:
        _ensure_3dspeaker_repo_on_path()

        import torch
        from modelscope.pipelines import pipeline
        from modelscope.utils.constant import Tasks
        from speakerlab.process.processor import FBank

        self._torch = torch
        self._pipeline = pipeline
        self._tasks = Tasks
        self._fbank_cls = FBank
        self._device = self._normalize_device(get_settings().threed_speaker_device)
        self._embedding_model, self._feature_extractor = self._load_embedding_model()
        self._vad_model = self._load_vad_model()
        self._batch_size = 64
        self._sample_rate = self._feature_extractor.sample_rate

    def _normalize_device(self, value: str) -> Any:
        if value == "auto":
            return self._torch.device("cuda:0" if self._torch.cuda.is_available() else "cpu")
        return self._torch.device(value)

    def _load_embedding_model(self) -> tuple[Any, Any]:
        settings = get_settings()
        config = _EMBEDDING_MODEL_CONFIGS["iic/speech_campplus_sv_zh_en_16k-common_advanced"]
        cache_dir = _download_model(
            "iic/speech_campplus_sv_zh_en_16k-common_advanced",
            config["revision"],
            settings.resolved_threed_speaker_model_cache_path,
        )
        model_cls = _dynamic_import(config["module"], config["class_name"])
        feature_extractor = self._fbank_cls(n_mels=80, sample_rate=16000, mean_nor=True)
        embedding_model = model_cls(**config["model_kwargs"])
        checkpoint_path = cache_dir / config["checkpoint_name"]
        state = self._torch.load(checkpoint_path, map_location="cpu")
        embedding_model.load_state_dict(state)
        embedding_model.eval()
        embedding_model.to(self._device)
        return embedding_model, feature_extractor

    def _load_vad_model(self) -> Any:
        settings = get_settings()
        cache_dir = _download_model(
            "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
            "v2.0.4",
            settings.resolved_threed_speaker_model_cache_path,
        )
        device_name = "cpu"
        if self._device.type == "cuda":
            device_name = f"cuda:{self._device.index or 0}"
        return self._pipeline(
            task=self._tasks.voice_activity_detection,
            model=str(cache_dir),
            device=device_name,
            disable_pbar=True,
            disable_update=True,
        )

    def _run_vad(self, wav: Any) -> list[tuple[float, float]]:
        result = self._vad_model(wav[0])[0]
        raw_segments = result.get("value") or []
        segments: list[tuple[float, float]] = []
        for item in raw_segments:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            start_sec = max(float(item[0]) / 1000.0, 0.0)
            end_sec = max(float(item[1]) / 1000.0, start_sec)
            segments.append((start_sec, end_sec))
        return segments

    def _chunk(self, start_sec: float, end_sec: float, duration: float = 1.5, step: float = 0.75) -> list[tuple[float, float]]:
        chunks: list[tuple[float, float]] = []
        cursor = start_sec
        while cursor + duration < end_sec + step:
            chunk_end = min(cursor + duration, end_sec)
            chunks.append((cursor, chunk_end))
            cursor += step
        return chunks

    def _spectral_cluster(self, embeddings: Any) -> Any:
        import numpy as np
        from scipy.cluster.hierarchy import fcluster, linkage
        from scipy.spatial.distance import squareform
        from scipy.sparse.linalg import eigsh
        from sklearn.cluster import KMeans
        from sklearn.metrics.pairwise import cosine_similarity

        if embeddings.shape[0] <= 1:
            return np.zeros(embeddings.shape[0], dtype=int)

        similarity = cosine_similarity(embeddings, embeddings)
        pruned = similarity.copy()
        min_pnum = 6
        pval = 0.012
        n_elems = int((1 - pval) * pruned.shape[0])
        n_elems = min(n_elems, max(pruned.shape[0] - min_pnum, 0))
        for row_idx in range(pruned.shape[0]):
            low_indexes = np.argsort(pruned[row_idx, :])[:n_elems]
            pruned[row_idx, low_indexes] = 0
        affinity = 0.5 * (pruned + pruned.T)
        laplacian = -affinity
        np.fill_diagonal(laplacian, np.sum(np.abs(affinity), axis=1))

        max_num_spks = min(15, embeddings.shape[0])
        if embeddings.shape[0] < 40:
            condensed = squareform(-similarity, checks=False)
            tree = linkage(condensed, method="average")
            labels = fcluster(tree, t=0.4, criterion="distance") - 1
            return labels.astype(int)

        eig_values, eig_vectors = eigsh(laplacian, k=min(max_num_spks + 1, laplacian.shape[0] - 1), which="SM")
        min_num_spks = 1
        window = eig_values[min_num_spks - 1 : max_num_spks + 1]
        gaps = [float(window[i + 1]) - float(window[i]) for i in range(len(window) - 1)]
        num_speakers = max(int(np.argmax(gaps)) + min_num_spks, 1) if gaps else 1
        embeddings_spec = eig_vectors[:, :num_speakers]
        labels = KMeans(n_clusters=num_speakers, n_init=10).fit_predict(embeddings_spec)
        return labels.astype(int)

    def _extract_embeddings(self, chunks: list[tuple[float, float]], wav: Any) -> Any:
        import numpy as np
        from speakerlab.utils.utils import circle_pad

        total_samples = int(wav.shape[-1]) if getattr(wav, "shape", None) else 0
        wavs = []
        for start, end in chunks:
            start_index = max(int(start * self._sample_rate), 0)
            end_index = min(max(int(end * self._sample_rate), start_index), total_samples)
            if end_index <= start_index:
                continue
            chunk = wav[0, start_index:end_index]
            if int(getattr(chunk, "shape", [0])[0]) <= 0:
                continue
            wavs.append(chunk)
        if not wavs:
            return np.empty((0, 0), dtype=float)

        max_len = max(int(chunk.shape[0]) for chunk in wavs)
        if max_len <= 0:
            return np.empty((0, 0), dtype=float)
        wavs = [circle_pad(chunk, max_len) for chunk in wavs]
        wav_batch = self._torch.stack(wavs).unsqueeze(1)

        outputs = []
        batch_start = 0
        with self._torch.no_grad():
            while batch_start < len(wavs):
                current = wav_batch[batch_start : batch_start + self._batch_size].to(self._device)
                feats = self._torch.vmap(self._feature_extractor)(current)
                embeddings = self._embedding_model(feats).cpu()
                outputs.append(embeddings)
                batch_start += self._batch_size
        return self._torch.cat(outputs, dim=0).numpy()

    def _chunks_from_ms_intervals(self, intervals_ms: list[tuple[int, int]]) -> list[tuple[float, float]]:
        merged: list[tuple[int, int]] = []
        for begin_ms, end_ms in sorted(intervals_ms):
            start = max(int(begin_ms), 0)
            end = max(int(end_ms), start)
            if end - start < 400:
                continue
            if merged and start <= merged[-1][1] + 200:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                continue
            merged.append((start, end))

        chunks: list[tuple[float, float]] = []
        for begin_ms, end_ms in merged:
            start_sec = begin_ms / 1000.0
            end_sec = end_ms / 1000.0
            duration_sec = max(end_sec - start_sec, 0.0)
            if duration_sec <= 0.0:
                continue
            if duration_sec <= 1.8:
                chunks.append((start_sec, end_sec))
                continue

            generated = self._chunk(start_sec, end_sec, duration=1.5, step=0.75)
            if generated:
                chunks.extend(generated)
            else:
                chunks.append((start_sec, end_sec))
        return chunks

    def extract_speaker_embeddings(
        self,
        audio_path: Path,
        intervals_by_speaker: dict[str, list[tuple[int, int]]],
    ) -> dict[str, list[float]]:
        import numpy as np

        if not intervals_by_speaker:
            return {}

        waveform = _load_waveform_for_sensevoice(audio_path, target_sample_rate=self._sample_rate)
        wav = self._torch.from_numpy(waveform).unsqueeze(0)
        result: dict[str, list[float]] = {}
        for speaker_id, intervals in intervals_by_speaker.items():
            chunks = self._chunks_from_ms_intervals(intervals)
            if not chunks:
                continue
            embeddings = self._extract_embeddings(chunks, wav)
            if getattr(embeddings, "size", 0) == 0:
                continue
            mean_embedding = embeddings.mean(axis=0)
            norm = float(np.linalg.norm(mean_embedding))
            if norm <= 0:
                continue
            normalized = (mean_embedding / norm).astype(float).tolist()
            result[speaker_id] = normalized
        return result

    def diarize(self, audio_path: Path) -> list[_DiarizationSegment]:
        waveform = _load_waveform_for_sensevoice(audio_path, target_sample_rate=self._sample_rate)
        wav = self._torch.from_numpy(waveform).unsqueeze(0)
        vad_ranges = self._run_vad(wav)
        chunks = [chunk for start_sec, end_sec in vad_ranges for chunk in self._chunk(start_sec, end_sec)]
        if not chunks:
            return []

        embeddings = self._extract_embeddings(chunks, wav)
        cluster_labels = self._spectral_cluster(embeddings)
        raw_segments = [
            _DiarizationSegment(
                begin_ms=int(round(start_sec * 1000)),
                end_ms=int(round(end_sec * 1000)),
                speaker_id=f"SPEAKER_{int(label):02d}",
            )
            for (start_sec, end_sec), label in zip(chunks, cluster_labels)
        ]
        return _compress_segments(raw_segments)


def _get_diarizer() -> _Simple3DSpeakerDiarizer:
    global _diarizer
    if _diarizer is not None:
        return _diarizer

    with _diarizer_lock:
        if _diarizer is not None:
            return _diarizer

        logger.info(
            "Loading 3D-Speaker diarizer: repo=%s cache=%s",
            get_settings().resolved_threed_speaker_repo_path,
            get_settings().resolved_threed_speaker_model_cache_path,
        )
        _diarizer = _Simple3DSpeakerDiarizer()
        logger.info("3D-Speaker diarizer loaded successfully")
        return _diarizer


def transcribe_audio(audio_path: str | Path) -> list[dict]:
    settings = get_settings()
    resolved_audio_path = Path(audio_path)

    model, _ = _get_sensevoice_model()
    diarizer = _get_diarizer()

    prepared_audio_path, cleanup_prepared_audio = _prepare_audio_path(resolved_audio_path)

    logger.info("Transcribing with SenseVoice + 3D-Speaker: %s", resolved_audio_path)
    started_at = time.perf_counter()
    try:
        sensevoice_input = _load_waveform_for_sensevoice(prepared_audio_path, target_sample_rate=16000)
        diarization_segments = diarizer.diarize(prepared_audio_path)

        if settings.sensevoice_diarization_first_enabled and diarization_segments:
            logger.info(
                "Using diarization-first ASR: diar_segments=%d max_window=%.1fs",
                len(diarization_segments),
                settings.sensevoice_speaker_window_seconds,
            )
            timed_tokens, fallback_utterances = _transcribe_speaker_windows(
                model,
                sensevoice_input,
                diarization_segments,
            )
            if not timed_tokens and not fallback_utterances:
                logger.warning("Diarization-first ASR produced no output, falling back to whole-audio ASR")
                timed_tokens, fallback_utterances = _transcribe_whole_audio(
                    model,
                    sensevoice_input,
                    diarization_segments,
                )
        else:
            timed_tokens, fallback_utterances = _transcribe_whole_audio(
                model,
                sensevoice_input,
                diarization_segments,
            )

        utterances = _merge_sentence_utterances(
            timed_tokens,
            diarization_segments,
            gap_ms=max(int(settings.sensevoice_utterance_gap_seconds * 1000), 200),
            punctuation_pause_ms=max(int(settings.sensevoice_punctuation_pause_seconds * 1000), 100),
        )

        if not utterances:
            utterances = fallback_utterances

        for utterance in utterances:
            utterance["text"] = _finalize_text(utterance.get("text") or "")

        utterances = [utterance for utterance in utterances if str(utterance.get("text") or "").strip()]
        utterances = _apply_role_classification(utterances)

        elapsed = time.perf_counter() - started_at
        logger.info(
            "SenseVoice + 3D-Speaker done: %d utterances, %d diar segments in %.1fs",
            len(utterances),
            len(diarization_segments),
            elapsed,
        )
        return utterances
    finally:
        cleanup_prepared_audio()
