"""ASR 转写服务层 — 管理录音→转写→片段拆分的完整流程。

支持三种 ASR provider：
- mock: 本地模拟（开发测试用）
- whisper: 基于 faster-whisper 的本地转写（真实 ASR）
- sensevoice_3dspeaker: SenseVoice + 3D-Speaker 高精度转写
- high_precision_3dspeaker: Whisper large-v3 + 3D-Speaker 高精度转写
- tencent_asr: 腾讯云录音文件识别（云端异步转写）
- xfyun_asr: 科大讯飞录音文件转写大模型（云端异步转写）
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import Recording, Segment, Transcript
from smart_badge_api.db.session import _session_factory

logger = logging.getLogger(__name__)

_provider_semaphores: dict[tuple[str, int], asyncio.Semaphore] = {}


def _get_asr_semaphore(provider: str) -> asyncio.Semaphore:
    if provider == "tencent_asr":
        limit = max(get_settings().tencent_asr_max_concurrency, 1)
    elif provider == "xfyun_asr":
        limit = max(get_settings().xfyun_asr_max_concurrency, 1)
    else:
        # 本地模型是 CPU/GPU 密集型，默认串行避免资源争抢。
        limit = 1

    key = (provider, limit)
    semaphore = _provider_semaphores.get(key)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        _provider_semaphores[key] = semaphore
    return semaphore


def _should_build_tencent_hotword_word_weights() -> bool:
    settings = get_settings()
    if settings.tencent_asr_hotword_vocab_sync_enabled:
        return True
    return not settings.tencent_asr_hotword_vocab_id.strip()


# ── Mock ASR 结果生成 ──────────────────────────────


def _generate_mock_utterances(duration_seconds: int | None) -> list[dict]:
    """生成模拟的 ASR 转写结果（仅用于开发测试）。"""
    total_ms = (duration_seconds or 60) * 1000
    chunk_ms = 8000  # 每段 8 秒
    utterances = []
    speakers = ["consultant", "customer"]
    templates = [
        "您好，欢迎来到我们机构，请问今天想咨询什么项目呢？",
        "我想了解一下面部抗衰方面的项目，之前朋友推荐过来的。",
        "好的，您朋友做的是热玛吉还是超声刀呢？我先帮您了解一下基本情况。",
        "应该是热玛吉，她说效果很好。我主要是觉得法令纹比较深。",
        "明白了，我先帮您看一下皮肤状态。您平时有什么护肤习惯吗？",
        "就是基础的水乳防晒，没有做过医美项目。",
        "了解，像您这种情况其实很适合做热玛吉，它主要通过射频能量刺激胶原蛋白再生。",
        "那做一次大概需要多少钱？恢复期长吗？",
    ]

    t = 0
    idx = 0
    while t < total_ms:
        end = min(t + chunk_ms, total_ms)
        utterances.append({
            "speaker": speakers[idx % 2],
            "text": templates[idx % len(templates)],
            "begin_ms": t,
            "end_ms": end,
        })
        t = end
        idx += 1

    return utterances


# ── ASR 调度 ──────────────────────────────────────


def _run_asr(audio_path: Path, provider: str, duration_seconds: int | None) -> list[dict]:
    """同步执行 ASR 转写 + 说话人角色分类，返回 utterance 列表。在线程池中调用。"""
    if provider == "whisper":
        from smart_badge_api.asr.whisper_provider import transcribe_audio
        from smart_badge_api.asr.speaker_classifier import classify_speakers
        utterances = transcribe_audio(audio_path)
        utterances = classify_speakers(utterances)
        return utterances
    if provider == "sensevoice_3dspeaker":
        from smart_badge_api.asr.sensevoice_3dspeaker_provider import transcribe_audio

        return transcribe_audio(audio_path)
    if provider == "high_precision_3dspeaker":
        from smart_badge_api.asr.high_precision_3dspeaker_provider import transcribe_audio

        return transcribe_audio(audio_path)

    # mock provider
    return _generate_mock_utterances(duration_seconds)


# ── 转写任务执行 ──────────────────────────────────


async def transcribe_audio_file(
    audio_path: Path,
    *,
    duration_seconds: int | None = None,
    provider: str | None = None,
    staff_id: str | None = None,
    staff_name: str | None = None,
    staff_role: str | None = None,
    source_id: str | None = None,
) -> tuple[list[dict], str, int]:
    resolved_provider = provider or get_settings().asr_provider
    from smart_badge_api.asr.audio_preprocessing import prepare_audio_for_asr

    with prepare_audio_for_asr(
        audio_path,
        provider=resolved_provider,
        source_id=source_id,
        duration_seconds=duration_seconds,
    ) as prepared_audio_path:
        if prepared_audio_path != audio_path:
            logger.info(
                "Using preprocessed audio for ASR: original=%s prepared=%s",
                audio_path.name,
                prepared_audio_path,
            )
        async with _get_asr_semaphore(resolved_provider):
            if resolved_provider == "tencent_asr":
                from smart_badge_api.asr.tencent_cloud_provider import transcribe_audio

                hotword_word_weights: list[dict[str, object]] | None = None
                if _should_build_tencent_hotword_word_weights():
                    from smart_badge_api.asr.domain_terms import build_tencent_hotword_word_weights

                    hotword_word_weights = await build_tencent_hotword_word_weights()

                utterances, full_text, duration_ms = await transcribe_audio(
                    prepared_audio_path,
                    hotword_word_weights=hotword_word_weights,
                    source_id=source_id,
                )
            elif resolved_provider == "xfyun_asr":
                from smart_badge_api.asr.xfyun_asr_provider import transcribe_audio

                utterances, full_text, duration_ms = await transcribe_audio(prepared_audio_path)
            else:
                loop = asyncio.get_running_loop()
                utterances = await loop.run_in_executor(
                    None,
                    _run_asr,
                    prepared_audio_path,
                    resolved_provider,
                    duration_seconds,
                )
                full_text = " ".join(str(item.get("text") or "") for item in utterances).strip()
                duration_ms = utterances[-1]["end_ms"] if utterances else 0

        from smart_badge_api.asr.independent_diarization import maybe_apply_independent_diarization

        utterances = await maybe_apply_independent_diarization(
            prepared_audio_path,
            utterances,
            provider=resolved_provider,
            source_id=source_id,
        )

    from smart_badge_api.asr.domain_terms import apply_medical_aesthetic_term_normalization
    utterances, correction_count = apply_medical_aesthetic_term_normalization(utterances)
    if correction_count:
        full_text = " ".join(str(item.get("text") or "") for item in utterances).strip()
        logger.info(
            "Applied medical term normalization for %s: provider=%s corrections=%d",
            audio_path.name,
            resolved_provider,
            correction_count,
        )

    from smart_badge_api.asr.speaker_role_resolver import resolve_speaker_roles

    utterances = resolve_speaker_roles(
        utterances,
        staff_id=staff_id,
        staff_name=staff_name,
        staff_role=staff_role,
        respect_speaker_diarization=True,
    )

    if resolved_provider in {"sensevoice_3dspeaker", "high_precision_3dspeaker", "tencent_asr", "xfyun_asr"}:
        from smart_badge_api.asr.speaker_voiceprint import (
            apply_staff_voiceprints,
            auto_enroll_staff_voiceprint,
        )

        utterances = apply_staff_voiceprints(
            audio_path,
            utterances,
            staff_id=staff_id,
        )
        utterances = resolve_speaker_roles(
            utterances,
            staff_id=staff_id,
            staff_name=staff_name,
            staff_role=staff_role,
            respect_speaker_diarization=True,
        )
        auto_enroll_staff_voiceprint(
            audio_path,
            utterances,
            staff_id=staff_id,
            staff_name=staff_name,
            staff_role=staff_role,
            source_id=source_id,
        )

    resolved_full_text = " ".join(str(item.get("text") or "") for item in utterances).strip()
    resolved_duration_ms = utterances[-1]["end_ms"] if utterances else duration_ms
    return utterances, resolved_full_text or full_text, resolved_duration_ms


async def execute_transcription(recording_id: str) -> None:
    """对一段录音执行 ASR 转写。

    流程：
    1. 创建 Transcript 记录（pending → processing）
    2. 更新 Recording 状态为 transcribing
    3. 执行 ASR（mock / whisper / sensevoice_3dspeaker / high_precision_3dspeaker / tencent_asr / xfyun_asr）
    4. 写入转写结果
    5. 更新 Recording 状态为 transcribed
    6. 自动触发片段拆分
    """
    settings = get_settings()
    provider = settings.asr_provider

    async with _session_factory() as db:
        recording = await db.get(
            Recording,
            recording_id,
            options=[selectinload(Recording.transcript), selectinload(Recording.staff)],
        )
        if not recording:
            logger.warning("Recording %s not found, skipping transcription", recording_id)
            return
        if recording.status != "uploaded":
            logger.info("Recording %s status is %s, skipping transcription", recording_id, recording.status)
            return
        if recording.transcript:
            logger.info("Recording %s already has transcript, skipping", recording_id)
            return

        # 创建转写记录
        transcript = Transcript(
            recording_id=recording_id,
            asr_provider=provider,
            status="processing",
        )
        db.add(transcript)
        recording.status = "transcribing"
        await db.commit()
        transcript_id = transcript.id
        audio_path = settings.resolve_file_path(recording.file_path)
        duration_seconds = recording.duration_seconds
        staff_id = recording.staff_id
        staff_name = recording.staff.name if recording.staff else None
        staff_role = recording.staff.role if recording.staff else None

    try:
        utterances, full_text, duration_ms = await transcribe_audio_file(
            audio_path,
            duration_seconds=duration_seconds,
            provider=provider,
            staff_id=staff_id,
            staff_name=staff_name,
            staff_role=staff_role,
            source_id=recording_id,
        )

        async with _session_factory() as db:
            recording = await db.get(Recording, recording_id)
            if not recording:
                return

            transcript = await db.get(Transcript, transcript_id)
            if not transcript:
                return

            transcript.status = "completed"
            transcript.full_text = full_text
            transcript.utterances = utterances
            transcript.duration_ms = duration_ms
            transcript.completed_at = datetime.now(timezone.utc)

            # 同步写回 Recording 的冗余字段（向后兼容）
            recording.transcript_text = full_text
            recording.transcript_segments = utterances
            recording.status = "transcribed"

            await db.commit()

        logger.info("Transcription completed for recording %s (%s, %d utterances)", recording_id, provider, len(utterances))

        # 自动触发片段拆分
        await execute_segmentation(recording_id)

    except Exception as exc:
        logger.exception("Transcription failed for recording %s: %s", recording_id, exc)
        async with _session_factory() as db:
            transcript = await db.get(Transcript, transcript_id)
            if transcript:
                transcript.status = "failed"
                transcript.error_message = str(exc)
            recording = await db.get(Recording, recording_id)
            if recording:
                recording.status = "failed"
            await db.commit()


# ── 片段拆分 ──────────────────────────────────────


async def execute_segmentation(recording_id: str) -> None:
    """将转写结果拆分为对话片段。

    拆分策略（mock）：
    - 按说话人交替检测对话边界
    - 连续同一说话人的发言合并为同一片段
    - 静音间隔 > 阈值时创建新片段
    当前简化为：每 4 条 utterance 一个 segment（模拟多轮对话拆分）。
    """
    async with _session_factory() as db:
        transcript = (await db.execute(
            select(Transcript).where(Transcript.recording_id == recording_id, Transcript.status == "completed")
        )).scalar_one_or_none()

        if not transcript or not transcript.utterances:
            logger.info("No completed transcript for recording %s, skipping segmentation", recording_id)
            return

        # 清除旧片段（支持重新拆分）— 保留已关联到诊单的片段
        existing = (await db.execute(
            select(Segment).where(Segment.recording_id == recording_id)
        )).scalars().all()
        linked_count = 0
        for seg in existing:
            if seg.visit_id:
                linked_count += 1
            else:
                await db.delete(seg)
        if linked_count:
            logger.info("Preserved %d segments with visit links for recording %s", linked_count, recording_id)

        utterances = transcript.utterances
        chunk_size = 4  # 每 4 条 utterance 为一个片段
        segments_created = []
        next_index = linked_count  # 新片段序号从保留片段数开始

        for i in range(0, len(utterances), chunk_size):
            chunk = utterances[i:i + chunk_size]
            begin_ms = chunk[0]["begin_ms"]
            end_ms = chunk[-1]["end_ms"]
            text = " ".join(u["text"] for u in chunk)
            # 判定片段主要说话人
            speaker_counts: dict[str, int] = {}
            for u in chunk:
                sp = u.get("speaker", "unknown")
                speaker_counts[sp] = speaker_counts.get(sp, 0) + 1
            primary_speaker = max(speaker_counts, key=speaker_counts.get) if speaker_counts else None  # type: ignore[arg-type]

            segment = Segment(
                recording_id=recording_id,
                segment_index=next_index + len(segments_created),
                begin_ms=begin_ms,
                end_ms=end_ms,
                speaker_label=primary_speaker,
                text=text,
                utterances=chunk,
                status="created",
            )
            db.add(segment)
            segments_created.append(segment)

        await db.commit()
        logger.info("Created %d segments for recording %s", len(segments_created), recording_id)


# ── 任务分发 ──────────────────────────────────────


# 保持后台任务引用，防止被 GC 回收
_background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]


async def dispatch_transcription(recording_id: str) -> None:
    """分发转写任务。

    默认走后台异步执行，避免上传接口阻塞超时。
    可通过 ASR_DISPATCH_MODE=eager 临时切换为同步执行（调试用）。
    """
    settings = get_settings()
    if settings.asr_dispatch_mode == "eager":
        await execute_transcription(recording_id)
    else:
        task = asyncio.create_task(execute_transcription(recording_id))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
