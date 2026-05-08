from smart_badge_api.iot_capabilities import (
    IOT_CAPABILITY_DEFINITIONS,
    default_iot_capabilities,
    iot_capability_definitions_payload,
    normalize_iot_capabilities,
)


def test_iot_capabilities_default_to_disabled() -> None:
    defaults = default_iot_capabilities()

    assert defaults
    assert set(defaults) == {item.key for item in IOT_CAPABILITY_DEFINITIONS}
    assert all(value is False for value in defaults.values())


def test_iot_capabilities_ignore_unknown_keys() -> None:
    normalized = normalize_iot_capabilities(
        {
            "gps_control": True,
            "callback_audio": 1,
            "unknown": True,
        }
    )

    assert normalized["gps_control"] is True
    assert normalized["callback_audio"] is True
    assert "unknown" not in normalized
    assert normalized["device_settings"] is False


def test_iot_capability_definitions_are_frontend_ready() -> None:
    payload = iot_capability_definitions_payload()

    assert payload[0]["key"] == "gps_control"
    assert all({"key", "title", "group", "description", "risk_level"} <= set(item) for item in payload)
