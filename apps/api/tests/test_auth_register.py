import asyncio

from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.auth import register
from smart_badge_api.db.base import Base
from smart_badge_api.schemas.auth import RegisterRequest


def _make_request(ip: str = "127.0.0.1") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/register",
        "headers": [],
        "client": (ip, 8000),
    }
    return Request(scope)


def test_register_is_disabled_for_self_service_signup() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                try:
                    await register(
                        RegisterRequest(
                            username="dujuan",
                            password="secret123",
                            display_name="杜娟",
                            advisor_code="81019369",
                        ),
                        _make_request(),
                        db=db,
                    )
                except HTTPException as exc:
                    assert exc.status_code == 403
                    assert "不开放自主注册" in exc.detail
                else:
                    raise AssertionError("Self-service register should be disabled")
        finally:
            await engine.dispose()

    asyncio.run(scenario())
