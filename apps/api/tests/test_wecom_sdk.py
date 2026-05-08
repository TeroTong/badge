import asyncio
import os
from unittest.mock import AsyncMock, patch

from starlette.requests import Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.wecom_sdk import get_wecom_js_sdk_config
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.base import Base
from smart_badge_api.wecom import WecomJsSdkSignature


def _make_request(host: str = "badge.example.com") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/wecom/sdk/config",
            "headers": [(b"host", host.encode("utf-8"))],
            "scheme": "https",
            "server": (host, 443),
            "client": ("127.0.0.1", 8000),
        }
    )


def _set_wecom_env() -> None:
    os.environ["WECOM_CORP_ID"] = "ww-test-corp"
    os.environ["WECOM_AGENT_ID"] = "1000007"
    os.environ["WECOM_AGENT_SECRET"] = "test-secret"
    os.environ["FRONTEND_URL"] = "https://badge.example.com"
    get_settings.cache_clear()


def _clear_wecom_env() -> None:
    for key in ("WECOM_CORP_ID", "WECOM_AGENT_ID", "WECOM_AGENT_SECRET", "FRONTEND_URL"):
        os.environ.pop(key, None)
    get_settings.cache_clear()


def test_get_wecom_js_sdk_config_returns_signature_payload() -> None:
    async def scenario() -> None:
        _set_wecom_env()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            with patch(
                "smart_badge_api.api.routes.wecom_sdk.build_wecom_js_sdk_signature_for_url",
                AsyncMock(
                    return_value=WecomJsSdkSignature(
                        timestamp=1710000000,
                        nonceStr="nonce-demo",
                        signature="signature-demo",
                    )
                ),
            ) as mocked_builder:
                async with session_factory() as db:
                    result = await get_wecom_js_sdk_config(
                        _make_request(),
                        "https://badge.example.com/wecom/badge",
                        db=db,
                    )

            mocked_builder.assert_awaited_once()
            assert mocked_builder.await_args.args[0] == "https://badge.example.com/wecom/badge"
            assert mocked_builder.await_args.kwargs["tenant"].corp_id == "ww-test-corp"
            assert result.corp_id == "ww-test-corp"
            assert result.agent_id == "1000007"
            assert result.timestamp == 1710000000
            assert result.nonceStr == "nonce-demo"
            assert result.signature == "signature-demo"
        finally:
            _clear_wecom_env()
            await engine.dispose()

    asyncio.run(scenario())
