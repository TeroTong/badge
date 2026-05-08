from __future__ import annotations

from smart_badge_api.asr.audio_preprocessing import _choose_low_volume_gain
from smart_badge_api.core.config import get_settings


def test_low_volume_gain_is_only_selected_for_obviously_quiet_audio(monkeypatch) -> None:
    monkeypatch.setenv("ASR_LOW_VOLUME_GAIN_ENABLED", "true")
    monkeypatch.setenv("ASR_LOW_VOLUME_MEAN_DB_THRESHOLD", "-32")
    monkeypatch.setenv("ASR_LOW_VOLUME_TARGET_MEAN_DB", "-26")
    monkeypatch.setenv("ASR_LOW_VOLUME_MAX_GAIN_DB", "8")
    monkeypatch.setenv("ASR_LOW_VOLUME_MIN_GAIN_DB", "2")
    monkeypatch.setenv("ASR_LOW_VOLUME_HEADROOM_DB", "1")
    get_settings.cache_clear()

    try:
        assert _choose_low_volume_gain(-35.0, -12.0) == 8.0
        assert _choose_low_volume_gain(-28.0, -12.0) is None
    finally:
        get_settings.cache_clear()


def test_low_volume_gain_respects_peak_headroom(monkeypatch) -> None:
    monkeypatch.setenv("ASR_LOW_VOLUME_GAIN_ENABLED", "true")
    monkeypatch.setenv("ASR_LOW_VOLUME_MEAN_DB_THRESHOLD", "-32")
    monkeypatch.setenv("ASR_LOW_VOLUME_TARGET_MEAN_DB", "-26")
    monkeypatch.setenv("ASR_LOW_VOLUME_MAX_GAIN_DB", "8")
    monkeypatch.setenv("ASR_LOW_VOLUME_MIN_GAIN_DB", "2")
    monkeypatch.setenv("ASR_LOW_VOLUME_HEADROOM_DB", "1")
    get_settings.cache_clear()

    try:
        assert _choose_low_volume_gain(-35.0, -2.0) is None
        assert _choose_low_volume_gain(-35.0, -6.0) == 5.0
    finally:
        get_settings.cache_clear()
