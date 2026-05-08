from datetime import datetime
from zoneinfo import ZoneInfo

from smart_badge_api.dingtalk_iot import iot_device_to_dvi_device


def test_iot_device_report_time_without_timezone_is_shanghai_time() -> None:
    normalized = iot_device_to_dvi_device(
        {
            "deviceNo": "SSYX51049784",
            "onlineStatus": 1,
            "remainPower": 82,
            "reportTime": "2026-05-07 15:30:00",
        }
    )

    expected_timestamp = int(
        datetime(2026, 5, 7, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
    )

    assert normalized is not None
    assert normalized["status"] == {"value": "online", "timestamp": expected_timestamp}
    assert normalized["battery"] == {"value": 82, "timestamp": expected_timestamp}
