from typing import Literal

from pydantic import BaseModel, Field, field_validator


MultiRecordingMode = Literal["many_to_many_visit_linking"]
DEFAULT_MULTI_RECORDING_MODE: MultiRecordingMode = "many_to_many_visit_linking"
_LEGACY_MULTI_RECORDING_MODES = {"same_staff_same_visit", "multiple_staff_same_visit"}


class PreferenceSettings(BaseModel):
    multi_recording_mode: MultiRecordingMode = DEFAULT_MULTI_RECORDING_MODE
    auto_match_recording: bool = False
    iot_capabilities: dict[str, bool] = Field(default_factory=dict)

    @field_validator("multi_recording_mode", mode="before")
    @classmethod
    def normalize_multi_recording_mode(cls, value: object) -> MultiRecordingMode:
        if value == DEFAULT_MULTI_RECORDING_MODE or value in _LEGACY_MULTI_RECORDING_MODES:
            return DEFAULT_MULTI_RECORDING_MODE
        return DEFAULT_MULTI_RECORDING_MODE


def build_default_preference_settings() -> PreferenceSettings:
    return PreferenceSettings()


def normalize_preference_settings(raw: dict | PreferenceSettings | None) -> PreferenceSettings:
    if isinstance(raw, PreferenceSettings):
        source = raw.model_dump(mode="json")
    else:
        source = raw or {}

    defaults = build_default_preference_settings()
    return PreferenceSettings.model_validate(
        {
            "multi_recording_mode": source.get("multi_recording_mode", defaults.multi_recording_mode),
            "auto_match_recording": bool(source.get("auto_match_recording", defaults.auto_match_recording)),
            "iot_capabilities": source.get("iot_capabilities") if isinstance(source.get("iot_capabilities"), dict) else {},
        }
    )


class PreferenceProfileOut(BaseModel):
    id: str
    scope_key: str
    name: str
    settings: PreferenceSettings
    created_at: str
    updated_at: str


class PreferenceProfileUpdate(BaseModel):
    settings: PreferenceSettings
