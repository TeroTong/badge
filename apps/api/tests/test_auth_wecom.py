import asyncio
import os
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.auth import exchange_wecom_code, get_me, get_wecom_authorize_url
from smart_badge_api.core.config import get_settings
from smart_badge_api.core.security import hash_password
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import Staff, User, WecomTenant
from smart_badge_api.schemas.auth import WecomCodeExchangeRequest
from smart_badge_api.wecom import WecomMemberIdentity, _make_wecom_http_client


def _make_request(
    path: str = "/api/v1/auth/wecom/exchange",
    ip: str = "127.0.0.1",
    host: str = "badge.example.com",
) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"host", host.encode("utf-8"))],
        "client": (ip, 8000),
        "scheme": "https",
        "server": (host, 443),
    }
    return Request(scope)


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


def test_get_wecom_authorize_url_builds_working_oauth_link() -> None:
    async def scenario() -> None:
        _set_wecom_env()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            async with session_factory() as db:
                result = await get_wecom_authorize_url(
                    _make_request("/api/v1/auth/wecom/authorize-url"),
                    redirect="/admin/recordings?tab=all&from=badge",
                    db=db,
                )
            assert "open.weixin.qq.com/connect/oauth2/authorize" in result.authorize_url
            parsed = urlparse(result.authorize_url)
            params = parse_qs(parsed.query)
            assert params["appid"] == ["ww-test-corp"]
            assert params["agentid"] == ["1000007"]
            callback_url = params["redirect_uri"][0]
            callback_params = parse_qs(urlparse(callback_url).query)
            assert callback_params["wecom"] == ["1"]
            assert callback_params["redirect"] == ["/admin/recordings?tab=all&from=badge"]
            assert result.authorize_url.endswith("#wechat_redirect")
        finally:
            _clear_wecom_env()
            await engine.dispose()

    asyncio.run(scenario())


def test_get_wecom_authorize_url_rejects_invalid_frontend_url_with_503() -> None:
    async def scenario() -> None:
        os.environ["WECOM_CORP_ID"] = "ww-test-corp"
        os.environ["WECOM_AGENT_ID"] = "1000007"
        os.environ["WECOM_AGENT_SECRET"] = "test-secret"
        os.environ["FRONTEND_URL"] = "http://127.0.0.1:5173"
        get_settings.cache_clear()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            async with session_factory() as db:
                try:
                    await get_wecom_authorize_url(_make_request("/api/v1/auth/wecom/authorize-url", host="127.0.0.1"), redirect="/wecom/badge", db=db)
                except HTTPException as exc:
                    assert exc.status_code == 503
                    assert "入口地址" in exc.detail
                else:
                    raise AssertionError("Invalid FRONTEND_URL should be rejected")
        finally:
            _clear_wecom_env()
            await engine.dispose()

    asyncio.run(scenario())


def test_wecom_http_client_disables_env_proxy_inheritance() -> None:
    async def scenario() -> None:
        async with _make_wecom_http_client() as client:
            assert client._trust_env is False

    asyncio.run(scenario())


def test_wecom_exchange_creates_bound_user_and_returns_tokens() -> None:
    async def scenario() -> None:
        _set_wecom_env()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="杜娟",
                    phone="13800000000",
                    external_account="81019369",
                    wecom_user_id="dujuan_wecom",
                    permission_role="hospital_admin",
                    is_active=True,
                )
                db.add(staff)
                await db.commit()
                await db.refresh(staff)

                with patch(
                    "smart_badge_api.api.routes.auth.fetch_wecom_member_identity",
                    AsyncMock(
                        return_value=WecomMemberIdentity(
                            userid="dujuan_wecom",
                            name="杜娟",
                            mobile="13800000000",
                        )
                    ),
                ):
                    tokens = await exchange_wecom_code(
                        WecomCodeExchangeRequest(code="demo-code"),
                        _make_request(),
                        db=db,
                    )

                assert tokens.access_token
                assert tokens.refresh_token

                created_user = (await db.execute(select(User).where(User.staff_id == staff.id))).scalar_one()
                assert created_user.username == "81019369"
                assert created_user.role == "hospital_admin"
                assert created_user.display_name == "杜娟"

                me = await get_me(db=db, user=created_user)
                assert me.staff_id == staff.id
                assert me.staff_name == "杜娟"
                assert me.staff_wecom_user_id == "dujuan_wecom"
        finally:
            _clear_wecom_env()
            await engine.dispose()

    asyncio.run(scenario())


def test_wecom_exchange_resolves_staff_by_request_host_tenant() -> None:
    async def scenario() -> None:
        _set_wecom_env()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                db.add(
                    WecomTenant(
                        name="第二企微主体",
                        host="second.example.com",
                        corp_id="ww-second-corp",
                        agent_id="1000008",
                        agent_secret="second-secret",
                        frontend_url="https://second.example.com",
                        is_active=True,
                    )
                )
                first = Staff(
                    name="主体一员工",
                    external_account="81000001",
                    wecom_user_id="same_userid",
                    wecom_corp_id="ww-test-corp",
                    permission_role="staff",
                    is_active=True,
                )
                second = Staff(
                    name="主体二员工",
                    external_account="81000002",
                    wecom_user_id="same_userid",
                    wecom_corp_id="ww-second-corp",
                    permission_role="staff",
                    is_active=True,
                )
                db.add_all([first, second])
                await db.commit()
                await db.refresh(second)

                mocked_identity = AsyncMock(
                    return_value=WecomMemberIdentity(
                        userid="same_userid",
                        name="主体二员工",
                        mobile=None,
                    )
                )
                with patch("smart_badge_api.api.routes.auth.fetch_wecom_member_identity", mocked_identity):
                    tokens = await exchange_wecom_code(
                        WecomCodeExchangeRequest(code="second-code"),
                        _make_request(host="second.example.com"),
                        db=db,
                    )

                assert tokens.access_token
                tenant_arg = mocked_identity.await_args.kwargs["tenant"]
                assert tenant_arg.corp_id == "ww-second-corp"
                created_user = (await db.execute(select(User).where(User.staff_id == second.id))).scalar_one()
                assert created_user.username == "81000002"
        finally:
            _clear_wecom_env()
            await engine.dispose()

    asyncio.run(scenario())


def test_get_me_auto_links_existing_user_to_unique_staff_by_display_name() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="Tero",
                    wecom_user_id="15608171708",
                    permission_role="super_admin",
                    is_active=True,
                )
                user = User(
                    username="admin",
                    hashed_password=hash_password("admin123"),
                    display_name="Tero",
                    role="super_admin",
                    is_active=True,
                )
                db.add_all([staff, user])
                await db.commit()
                await db.refresh(user)

                me = await get_me(db=db, user=user)

                assert me.staff_id == staff.id
                assert me.staff_name == "Tero"
                assert me.staff_wecom_user_id == "15608171708"

                refreshed_user = await db.get(User, user.id)
                assert refreshed_user is not None
                assert refreshed_user.staff_id == staff.id
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_wecom_exchange_rejects_unbound_member() -> None:
    async def scenario() -> None:
        _set_wecom_env()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                with patch(
                    "smart_badge_api.api.routes.auth.fetch_wecom_member_identity",
                    AsyncMock(
                        return_value=WecomMemberIdentity(
                            userid="unknown_wecom_user",
                            name="未知成员",
                            mobile="13900000000",
                        )
                    ),
                ):
                    try:
                        await exchange_wecom_code(
                            WecomCodeExchangeRequest(code="demo-code"),
                            _make_request(),
                            db=db,
                        )
                    except HTTPException as exc:
                        assert exc.status_code == 403
                        assert "尚未绑定系统人员" in exc.detail
                    else:
                        raise AssertionError("Unbound WeCom member should be rejected")
        finally:
            _clear_wecom_env()
            await engine.dispose()

    asyncio.run(scenario())


def test_wecom_exchange_rejects_auto_provision_when_staff_has_no_account_identifier() -> None:
    async def scenario() -> None:
        _set_wecom_env()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="缺少账号来源",
                    wecom_user_id="missing_account_source",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.commit()

                with patch(
                    "smart_badge_api.api.routes.auth.fetch_wecom_member_identity",
                    AsyncMock(
                        return_value=WecomMemberIdentity(
                            userid="missing_account_source",
                            name="缺少账号来源",
                            mobile=None,
                        )
                    ),
                ):
                    try:
                        await exchange_wecom_code(
                            WecomCodeExchangeRequest(code="demo-code"),
                            _make_request(),
                            db=db,
                        )
                    except HTTPException as exc:
                        assert exc.status_code == 400
                        assert "联系管理员" in exc.detail
                    else:
                        raise AssertionError("WeCom auto-provision should require employee code or phone")
        finally:
            _clear_wecom_env()
            await engine.dispose()

    asyncio.run(scenario())


def test_wecom_exchange_reuses_existing_bound_account_and_updates_last_login() -> None:
    async def scenario() -> None:
        _set_wecom_env()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="兰四秀",
                    phone="13800138000",
                    external_account="81047230",
                    wecom_user_id="lansixiu_wecom",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.flush()

                user = User(
                    username="81047230",
                    hashed_password=hash_password("7230@Abcd"),
                    display_name="兰四秀",
                    staff_id=staff.id,
                    role="staff",
                    is_active=True,
                )
                db.add(user)
                await db.commit()
                await db.refresh(user)

                with patch(
                    "smart_badge_api.api.routes.auth.fetch_wecom_member_identity",
                    AsyncMock(
                        return_value=WecomMemberIdentity(
                            userid="lansixiu_wecom",
                            name="兰四秀",
                            mobile="13800138000",
                        )
                    ),
                ):
                    tokens = await exchange_wecom_code(
                        WecomCodeExchangeRequest(code="reuse-code"),
                        _make_request(),
                        db=db,
                    )

                assert tokens.access_token
                assert tokens.refresh_token

                users = (await db.execute(select(User).where(User.staff_id == staff.id))).scalars().all()
                assert len(users) == 1
                assert users[0].username == "81047230"
                assert users[0].last_login_at is not None
        finally:
            _clear_wecom_env()
            await engine.dispose()

    asyncio.run(scenario())


def test_wecom_exchange_auto_binds_staff_by_external_account() -> None:
    async def scenario() -> None:
        _set_wecom_env()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="钟露",
                    external_account="86000995",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.commit()
                await db.refresh(staff)

                with patch(
                    "smart_badge_api.api.routes.auth.fetch_wecom_member_identity",
                    AsyncMock(
                        return_value=WecomMemberIdentity(
                            userid="86000995",
                            name="钟露",
                            mobile=None,
                        )
                    ),
                ):
                    tokens = await exchange_wecom_code(
                        WecomCodeExchangeRequest(code="external-account-code"),
                        _make_request(),
                        db=db,
                    )

                assert tokens.access_token
                assert tokens.refresh_token

                await db.refresh(staff)
                assert staff.wecom_user_id == "86000995"

                created_user = (await db.execute(select(User).where(User.staff_id == staff.id))).scalar_one()
                assert created_user.username == "86000995"
                assert created_user.display_name == "钟露"
        finally:
            _clear_wecom_env()
            await engine.dispose()

    asyncio.run(scenario())


def test_wecom_exchange_normalizes_legacy_wecom_prefixed_numeric_phone_username() -> None:
    async def scenario() -> None:
        _set_wecom_env()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="Tero",
                    wecom_user_id="15608171708",
                    permission_role="system_admin",
                    is_active=True,
                )
                db.add(staff)
                await db.flush()

                user = User(
                    username="wecom_15608171708",
                    hashed_password=hash_password("1708@Abcd"),
                    display_name="Tero",
                    staff_id=staff.id,
                    role="system_admin",
                    is_active=True,
                )
                db.add(user)
                await db.commit()
                await db.refresh(user)

                with patch(
                    "smart_badge_api.api.routes.auth.fetch_wecom_member_identity",
                    AsyncMock(
                        return_value=WecomMemberIdentity(
                            userid="15608171708",
                            name="Tero",
                            mobile="15608171708",
                        )
                    ),
                ):
                    tokens = await exchange_wecom_code(
                        WecomCodeExchangeRequest(code="legacy-prefix-code"),
                        _make_request(),
                        db=db,
                    )

                assert tokens.access_token
                assert tokens.refresh_token

                users = (await db.execute(select(User).where(User.staff_id == staff.id))).scalars().all()
                assert len(users) == 1
                assert users[0].username == "15608171708"
                assert users[0].last_login_at is not None
        finally:
            _clear_wecom_env()
            await engine.dispose()

    asyncio.run(scenario())
