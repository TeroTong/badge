"""Whisper ASR provider — 基于 faster-whisper + pyannote 的本地转写。

使用 CTranslate2 加速推理，支持 CPU 和 CUDA。
通过 BatchedInferencePipeline 进行分块并行推理以大幅提升速度。
pyannote.audio 提供说话人分离（speaker diarization），将 SPEAKER_XX 标签
与 ASR 时间戳对齐，输出带说话人标识的 utterance 列表。
模型在首次使用时自动从 HuggingFace 下载。
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from threading import Lock

from smart_badge_api.core.config import get_settings

# Windows: nvidia-cublas-cu12 / nvidia-cudnn-cu12 install DLLs into
# site-packages/nvidia/<lib>/bin which is NOT on PATH by default.
# Add them so CTranslate2 can find cublas64_12.dll etc.
if sys.platform == "win32":
    _sp = Path(sys.prefix, "Lib", "site-packages", "nvidia")
    if _sp.is_dir():
        for _sub in _sp.iterdir():
            _bin = _sub / "bin"
            if _bin.is_dir():
                os.add_dll_directory(str(_bin))
                os.environ["PATH"] = str(_bin) + os.pathsep + os.environ.get("PATH", "")

logger = logging.getLogger(__name__)

_pipeline = None
_pipeline_lock = Lock()

_diarization_pipeline = None
_diarization_lock = Lock()


def _get_pipeline():
    """懒加载 Whisper BatchedInferencePipeline（进程级单例）。"""
    global _pipeline
    if _pipeline is not None:
        return _pipeline

    with _pipeline_lock:
        if _pipeline is not None:
            return _pipeline

        from faster_whisper import BatchedInferencePipeline, WhisperModel

        settings = get_settings()
        logger.info(
            "Loading Whisper model: size=%s device=%s compute=%s",
            settings.whisper_model_size,
            settings.whisper_device,
            settings.whisper_compute_type,
        )
        model = WhisperModel(
            settings.whisper_model_size,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
        )
        _pipeline = BatchedInferencePipeline(model=model)
        logger.info("Whisper BatchedInferencePipeline loaded successfully")
        return _pipeline


def _get_diarization_pipeline():
    """懒加载 pyannote speaker-diarization pipeline（进程级单例）。"""
    global _diarization_pipeline
    if _diarization_pipeline is not None:
        return _diarization_pipeline

    with _diarization_lock:
        if _diarization_pipeline is not None:
            return _diarization_pipeline

        from pyannote.audio import Pipeline as PyannotePipeline

        settings = get_settings()
        if not settings.hf_token:
            logger.warning("HF_TOKEN not set, speaker diarization disabled")
            return None

        logger.info("Loading pyannote speaker-diarization-3.1 ...")
        pipe = PyannotePipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=settings.hf_token,
        )
        import torch
        if torch.cuda.is_available():
            pipe.to(torch.device("cuda"))
            logger.info("pyannote pipeline moved to CUDA")
        _diarization_pipeline = pipe
        logger.info("pyannote speaker-diarization pipeline loaded successfully")
        return _diarization_pipeline


def _run_diarization(audio_path: str) -> dict[tuple[float, float], str] | None:
    """对音频执行说话人分离，返回 {(start_sec, end_sec): speaker_label} 映射。"""
    pipe = _get_diarization_pipeline()
    if pipe is None:
        return None

    logger.info("Running speaker diarization: %s", audio_path)
    t0 = time.perf_counter()
    diarization = pipe(audio_path)
    elapsed = time.perf_counter() - t0

    # 构建时间段→说话人映射
    segments: dict[tuple[float, float], str] = {}
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        segments[(turn.start, turn.end)] = speaker

    speakers = set(segments.values())
    logger.info(
        "Diarization done: %d turns, %d speakers in %.1fs",
        len(segments), len(speakers), elapsed,
    )
    return segments


def _assign_speakers(
    utterances: list[dict],
    diarization_segments: dict[tuple[float, float], str],
) -> list[dict]:
    """将 diarization 的说话人标签对齐到 ASR utterance。

    策略：每条 utterance 取中心时间点，找与之重叠最多的 diarization turn。
    """
    for utt in utterances:
        utt_start = utt["begin_ms"] / 1000.0
        utt_end = utt["end_ms"] / 1000.0
        utt_mid = (utt_start + utt_end) / 2.0

        best_speaker = "unknown"
        best_overlap = 0.0

        for (seg_start, seg_end), speaker in diarization_segments.items():
            # 计算重叠
            overlap_start = max(utt_start, seg_start)
            overlap_end = min(utt_end, seg_end)
            overlap = max(0.0, overlap_end - overlap_start)

            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = speaker

            # 备用：中心点落在区间内
            if best_overlap == 0.0 and seg_start <= utt_mid <= seg_end:
                best_speaker = speaker
                best_overlap = 0.001  # 标记找到了

        utt["speaker"] = best_speaker

    return utterances


def transcribe_audio(audio_path: str | Path) -> list[dict]:
    """对音频文件执行 Whisper 转写 + pyannote 说话人分离。

    返回格式：
    [{"speaker": str, "text": str, "begin_ms": int, "end_ms": int}, ...]
    speaker 为 "SPEAKER_00" / "SPEAKER_01" / ... 或 "unknown"（无 diarization 时）
    """
    pipeline = _get_pipeline()
    audio_path = str(audio_path)

    logger.info("Transcribing (batched): %s", audio_path)
    t0 = time.perf_counter()

    segments_iter, info = pipeline.transcribe(
        audio_path,
        language="zh",
        batch_size=16,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )

    logger.info(
        "Detected language: %s (prob=%.2f), duration=%.1fs",
        info.language,
        info.language_probability,
        info.duration,
    )

    utterances: list[dict] = []
    for seg in segments_iter:
        utterances.append({
            "speaker": "unknown",
            "text": seg.text.strip(),
            "begin_ms": int(seg.start * 1000),
            "end_ms": int(seg.end * 1000),
        })

    elapsed_asr = time.perf_counter() - t0
    ratio = info.duration / elapsed_asr if elapsed_asr > 0 else 0
    logger.info(
        "ASR done: %d utterances in %.1fs (%.1fx realtime)",
        len(utterances), elapsed_asr, ratio,
    )

    # 说话人分离
    diarization_segments = _run_diarization(audio_path)
    if diarization_segments:
        utterances = _assign_speakers(utterances, diarization_segments)
        speakers_found = set(u["speaker"] for u in utterances)
        logger.info("Speaker assignment done: %s", speakers_found)

    total_elapsed = time.perf_counter() - t0
    logger.info(
        "Transcription + diarization done: %d utterances in %.1fs",
        len(utterances), total_elapsed,
    )
    return utterances
