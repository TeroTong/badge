from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from smart_badge_api.api.routes.wecom_tenants import create_wecom_tenant, list_wecom_tenants, update_wecom_tenant
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import User, WecomTenant
from smart_badge_api.schemas.wecom_tenants import WecomTenantCreate, WecomTenantUpdate


def _make_request(ip: str = "127.0.0.1", path: str = "/api/v1/wecom/tenants") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [],
        "client": (ip, 8000),
    }
    return Request(scope)


async def _make_admin_user(db) -> User:
    user = User(
        username="system_admin",
        hashed_password="hashed",
        display_name="系统管理员",
        role="system_admin",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_hospital_admin_user(db, *, hospital_code: str = "6101") -> User:
    user = User(
        username=f"hospital_admin_{hospital_code}",
        hashed_password="hashed",
        display_name="机构管理员",
        role="hospital_admin",
        hospital_code=hospital_code,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _tenant_create(**overrides) -> WecomTenantCreate:
    payload = {
        "name": "米兰柏羽总院",
        "host": "gongpai.example.com",
        "corp_id": "ww-test-corp",
        "agent_id": "1000007",
        "agent_secret": "secret",
        "frontend_url": "https://gongpai.example.com",
        "default_hospital_code": "6101",
        "default_hospital_name": "旧简称",
        "is_default": True,
        "is_active": True,
    }
    payload.update(overrides)
    return WecomTenantCreate(**payload)


def test_hospital_admin_lists_only_own_institution() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_hospital_admin_user(db, hospital_code="6501")
                own = WecomTenant(
                    name="长沙雅美",
                    default_hospital_code="6501",
                    host="wx.csyamei.com",
                    is_active=True,
                )
                other = WecomTenant(
                    name="其他机构",
                    default_hospital_code="6101",
                    host="gongpai.example.com",
                    is_active=True,
                )
                db.add_all([own, other])
                await db.commit()

                result = await list_wecom_tenants(db=db, current_user=admin)

                assert result.total == 1
                assert result.items[0].name == "长沙雅美"
                assert result.items[0].default_hospital_code == "6501"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_hospital_admin_can_only_update_own_department_assistant_config() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_hospital_admin_user(db, hospital_code="6501")
                tenant = WecomTenant(
                    name="长沙雅美",
                    default_hospital_code="6501",
                    host="wx.csyamei.com",
                    is_active=True,
                )
                other = WecomTenant(
                    name="其他机构",
                    default_hospital_code="6101",
                    host="gongpai.example.com",
                    is_active=True,
                )
                db.add_all([tenant, other])
                await db.commit()
                await db.refresh(tenant)
                await db.refresh(other)

                result = await update_wecom_tenant(
                    tenant.id,
                    WecomTenantUpdate(
                        department_assistant_match_config={
                            "enabled": True,
                            "departments": [
                                {
                                    "department_code": "JGKS03",
                                    "department_name": "外科",
                                    "assistant_staff_ids": ["staff_1"],
                                }
                            ],
                        }
                    ),
                    _make_request(path=f"/api/v1/wecom/tenants/{tenant.id}"),
                    db=db,
                    current_user=admin,
                )
                assert result.department_assistant_match_config["departments"][0]["department_code"] == "JGKS03"

                with pytest.raises(HTTPException) as exc_info:
                    await update_wecom_tenant(
                        tenant.id,
                        WecomTenantUpdate(name="不允许改名"),
                        _make_request(path=f"/api/v1/wecom/tenants/{tenant.id}"),
                        db=db,
                        current_user=admin,
                    )
                assert exc_info.value.status_code == 403

                with pytest.raises(HTTPException) as exc_info:
                    await update_wecom_tenant(
                        other.id,
                        WecomTenantUpdate(department_assistant_match_config={"enabled": True, "departments": []}),
                        _make_request(path=f"/api/v1/wecom/tenants/{other.id}"),
                        db=db,
                        current_user=admin,
                    )
                assert exc_info.value.status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_hospital_admin_cannot_create_institution() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_hospital_admin_user(db, hospital_code="6501")
                with pytest.raises(HTTPException) as exc_info:
                    await create_wecom_tenant(
                        _tenant_create(default_hospital_code="6501"),
                        _make_request(),
                        db=db,
                        current_user=admin,
                    )
                assert exc_info.value.status_code == 403
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_create_wecom_tenant_persists_department_assistant_config() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                result = await create_wecom_tenant(
                    _tenant_create(
                        default_hospital_code="6501",
                        department_assistant_match_config={
                            "enabled": True,
                            "departments": [
                                {
                                    "department_code": "JGKS03",
                                    "department_name": "外科",
                                    "assistant_staff_ids": ["staff_1", "staff_1", " "],
                                },
                                {
                                    "department_code": "UNKNOWN",
                                    "department_name": "未知",
                                    "assistant_staff_ids": ["staff_2"],
                                },
                            ],
                        },
                    ),
                    _make_request(),
                    db=db,
                    current_user=admin,
                )

                assert result.department_assistant_match_config == {
                    "enabled": True,
                    "departments": [
                        {
                            "department_code": "JGKS03",
                            "department_name": "外科",
                            "assistant_staff_ids": ["staff_1"],
                        }
                    ],
                }
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_create_wecom_tenant_requires_hospital_code_and_clears_short_name() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                with pytest.raises(HTTPException) as exc_info:
                    await create_wecom_tenant(
                        _tenant_create(default_hospital_code=" "),
                        _make_request(),
                        db=db,
                        current_user=admin,
                    )
                assert exc_info.value.status_code == 400
                assert exc_info.value.detail == "请填写机构编码"

                result = await create_wecom_tenant(
                    _tenant_create(),
                    _make_request(),
                    db=db,
                    current_user=admin,
                )

                assert result.name == "米兰柏羽总院"
                assert result.default_hospital_code == "6101"
                assert result.default_hospital_name is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_create_wecom_tenant_allows_missing_wecom_app_fields() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                result = await create_wecom_tenant(
                    _tenant_create(
                        host=None,
                        corp_id=None,
                        agent_id=None,
                        agent_secret=None,
                        frontend_url=None,
                    ),
                    _make_request(),
                    db=db,
                    current_user=admin,
                )

                assert result.default_hospital_code == "6101"
                assert result.host is None
                assert result.corp_id is None
                assert result.agent_id is None
                assert result.frontend_url is None
                assert result.agent_secret_configured is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_create_wecom_tenant_rejects_duplicate_hospital_code() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                await create_wecom_tenant(
                    _tenant_create(host=None),
                    _make_request(),
                    db=db,
                    current_user=admin,
                )

                with pytest.raises(HTTPException) as exc_info:
                    await create_wecom_tenant(
                        _tenant_create(
                            name="重复机构",
                            host=None,
                            default_hospital_code="6101",
                        ),
                        _make_request(),
                        db=db,
                        current_user=admin,
                    )

                assert exc_info.value.status_code == 400
                assert exc_info.value.detail == "该机构编码已绑定其他机构"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_update_wecom_tenant_uses_institution_labels_and_clears_short_name() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                admin = await _make_admin_user(db)
                tenant = WecomTenant(
                    name="旧机构",
                    host="old.example.com",
                    corp_id="ww-old",
                    agent_id="1000001",
                    agent_secret="secret",
                    frontend_url="https://old.example.com",
                    default_hospital_code="6101",
                    default_hospital_name="旧简称",
                    is_default=True,
                    is_active=True,
                )
                db.add(tenant)
                await db.commit()
                await db.refresh(tenant)

                with pytest.raises(HTTPException) as exc_info:
                    await update_wecom_tenant(
                        tenant.id,
                        WecomTenantUpdate(name=" "),
                        _make_request(path=f"/api/v1/wecom/tenants/{tenant.id}"),
                        db=db,
                        current_user=admin,
                    )
                assert exc_info.value.status_code == 400
                assert exc_info.value.detail == "请填写机构名称"

                result = await update_wecom_tenant(
                    tenant.id,
                    WecomTenantUpdate(
                        name="新机构",
                        default_hospital_code="6201",
                        default_hospital_name="不再保留",
                    ),
                    _make_request(path=f"/api/v1/wecom/tenants/{tenant.id}"),
                    db=db,
                    current_user=admin,
                )

                assert result.name == "新机构"
                assert result.default_hospital_code == "6201"
                assert result.default_hospital_name is None
                saved = (await db.execute(select(WecomTenant).where(WecomTenant.id == tenant.id))).scalar_one()
                assert saved.default_hospital_name is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())
