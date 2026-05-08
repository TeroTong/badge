from __future__ import annotations

from dataclasses import dataclass

from smart_badge_api.core.config import get_settings


@dataclass(slots=True)
class DingTalkAudioQualityDecision:
    passed: bool
    reason: str | None = None
    stage: str | None = None


def duration_ms_to_seconds(duration_ms: int | None) -> int | None:
    if duration_ms is None or duration_ms <= 0:
        return None
    return max(duration_ms // 1000, 1)


def pre_asr_quality_decision(duration_seconds: int | None) -> DingTalkAudioQualityDecision:
    settings = get_settings()
    min_duration = max(settings.dingtalk_audio_min_duration_seconds, 0)
    if min_duration > 0 and duration_seconds is not None and duration_seconds < min_duration:
        return DingTalkAudioQualityDecision(
            False,
            f"录音时长 {duration_seconds} 秒，低于最小时长 {min_duration} 秒",
            "pre_asr",
        )

    max_duration = max(settings.dingtalk_audio_max_duration_seconds, 0)
    if max_duration > 0 and duration_seconds is not None and duration_seconds > max_duration:
        return DingTalkAudioQualityDecision(
            False,
            f"录音时长 {duration_seconds} 秒，超过最长时长 {max_duration} 秒",
            "pre_asr",
        )
    return DingTalkAudioQualityDecision(True)
