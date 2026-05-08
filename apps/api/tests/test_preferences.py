from smart_badge_api.schemas.preferences import (
    build_default_preference_settings,
    normalize_preference_settings,
)


def test_default_preference_settings_have_expected_sections() -> None:
    settings = build_default_preference_settings()

    assert settings.multi_recording_mode == "many_to_many_visit_linking"
    assert settings.auto_match_recording is False
    assert settings.iot_capabilities == {}


def test_normalize_preference_settings_keeps_only_live_fields() -> None:
    settings = normalize_preference_settings(
        {
            "multi_recording_mode": "multiple_staff_same_visit",
            "auto_match_recording": True,
            "recording_push_mode": "push",
            "pending_customer_scope": "hospital_shared",
            "role_bridge_settings": [{"role_key": "staff", "display_scope": "all"}],
        }
    )

    assert settings.multi_recording_mode == "many_to_many_visit_linking"
    assert settings.auto_match_recording is True
    assert settings.model_dump(mode="json") == {
        "multi_recording_mode": "many_to_many_visit_linking",
        "auto_match_recording": True,
        "iot_capabilities": {},
    }


def test_normalize_preference_settings_upgrades_legacy_multi_recording_modes() -> None:
    for legacy_mode in ("same_staff_same_visit", "multiple_staff_same_visit"):
        settings = normalize_preference_settings({"multi_recording_mode": legacy_mode})

        assert settings.multi_recording_mode == "many_to_many_visit_linking"


def test_normalize_preference_settings_falls_back_for_invalid_values() -> None:
    settings = normalize_preference_settings(
        {
            "multi_recording_mode": "bad-value",
            "auto_match_recording": 0,
        }
    )

    assert settings.multi_recording_mode == "many_to_many_visit_linking"
    assert settings.auto_match_recording is False
