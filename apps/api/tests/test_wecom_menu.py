from types import SimpleNamespace

from smart_badge_api.api.routes import wecom_menu


def test_build_default_wecom_menu_payload_uses_wecom_login_redirect_urls() -> None:
    original_get_settings = wecom_menu.get_settings
    wecom_menu.get_settings = lambda: SimpleNamespace(
        frontend_url="https://badge.example.com",
        wecom_agent_id="1000007",
    )
    try:
        payload = wecom_menu.build_default_wecom_menu_payload()
        entries = wecom_menu.flatten_wecom_menu_entries(payload)

        assert [button["name"] for button in payload["button"]] == ["我的工牌", "录音中心", "客户中心"]
        assert len(entries) == 3
        assert entries[0].target_path == "/wecom/badge"
        assert entries[1].target_path == "/wecom/recordings?tab=recordings"
        assert entries[2].target_path == "/wecom/customers"
        assert str(payload["button"][0]["url"]).startswith("https://badge.example.com/login?wecom=1&redirect=")
    finally:
        wecom_menu.get_settings = original_get_settings
