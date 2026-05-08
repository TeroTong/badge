from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.visit_orders import list_visit_orders
from smart_badge_api.api.routes.visit_orders import (
    _build_daily_visit_order_items,
    _daily_visit_order_scope_condition,
    _derive_recording_date_candidates,
    _pick_daily_visit_order_hospital_code,
    _to_out,
    list_daily_visit_orders_for_recording,
)
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import Customer, Recording, Staff, User, WecomTenant
from smart_badge_api.db.models import Visit, VisitOrder


def test_to_out_excludes_legacy_customer_fields() -> None:
    order = VisitOrder(
        id="vo001",
        dzdh="DZ1001",
        sjrq="2026-03-24",
        customer_gender="女",
        customer_birthday="1990-05-01",
    )

    payload = _to_out(order)

    assert payload.dzdh == "DZ1001"
    assert not hasattr(payload, "customer_gender")
    assert not hasattr(payload, "customer_age")


def test_build_daily_visit_order_items_includes_companion_visits() -> None:
    primary_order = VisitOrder(
        id="vo001",
        dzdh="DZ1001",
        dzseg="1",
        kunr="60504241",
        ninam="玲",
        kutyp_dq="V",
        kutyp_dq_txt="会员/老客",
        remark_dz="同行72175385",
    )
    companion_order = VisitOrder(
        id="vo002",
        dzdh="DZ1002",
        dzseg="1",
        kunr="72175385",
        ninam="媛",
        remark_dz="同行60504241",
    )
    primary_visit = Visit(id="visit001", external_visit_order_no="DZ1001", external_visit_order_seg="1")
    companion_visit = Visit(id="visit002", external_visit_order_no="DZ1002", external_visit_order_seg="1")

    items = _build_daily_visit_order_items(
        [primary_order, companion_order],
        [primary_visit, companion_visit],
        recording_id="rec001",
    )

    primary_item = next(item for item in items if item["id"] == "vo001")
    assert primary_item["local_visit_id"] == "visit001"
    assert primary_item["customer_type_code"] == "V"
    assert primary_item["customer_type_label"] == "老客"
    assert primary_item["associated_local_visit_ids"] == []
    assert primary_item["companion_local_visit_ids"] == ["visit002"]
    assert primary_item["companion_visit_order_refs"] == ["DZ1002-1"]
    assert primary_item["companion_customer_codes"] == ["72175385"]


def test_daily_visit_order_scope_condition_uses_current_user_hospital_only() -> None:
    user = User(username="tester", hashed_password="x", role="system_admin", hospital_code="H001")

    condition = _daily_visit_order_scope_condition(user)

    compiled = str(condition.compile(compile_kwargs={"literal_binds": True}))
    assert "visit_orders.jgbm = 'H001'" in compiled


def test_daily_visit_order_scope_condition_without_hospital_returns_false() -> None:
    user = User(username="tester", hashed_password="x", role="system_admin", hospital_code=None)

    condition = _daily_visit_order_scope_condition(user)

    compiled = str(condition.compile(compile_kwargs={"literal_binds": True}))
    assert compiled.lower() in {"false", "0 = 1"}


def test_pick_daily_visit_order_hospital_code_prefers_first_non_empty() -> None:
    assert _pick_daily_visit_order_hospital_code("", None, "6101", "6201") == "6101"


def test_derive_recording_date_candidates_includes_filename_fallback() -> None:
    recording = Recording(
        file_name="0414_114431.mp3",
        file_path="/tmp/0414_114431.mp3",
        status="uploaded",
        created_at=datetime(2026, 4, 13, 23, 59, tzinfo=timezone.utc),
    )

    assert _derive_recording_date_candidates(recording) == ["2026-04-13", "2026-04-14"]


def test_staff_list_visit_orders_only_returns_same_institution_same_recording_date_and_participated_orders() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    id="staff_1",
                    name="测试员工",
                    external_account="86000995",
                    hospital_code="6101",
                    permission_role="staff",
                )
                user = User(
                    username="86000995",
                    hashed_password="x",
                    role="staff",
                    staff_id=staff.id,
                    hospital_code="6101",
                    is_active=True,
                )
                recording = Recording(
                    id="rec_1",
                    staff_id=staff.id,
                    file_name="20260416_100000.mp3",
                    file_path="/tmp/20260416_100000.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc),
                )
                matching_primary = VisitOrder(
                    id="vo_match_1",
                    dzdh="DZ1001",
                    dzseg="110",
                    jgbm="6101",
                    crtdt="2026-04-16",
                    sjrq="2026-04-16",
                    fzuer="86000995",
                )
                matching_vipkf = VisitOrder(
                    id="vo_match_2",
                    dzdh="DZ1002",
                    dzseg="110",
                    jgbm="6101",
                    crtdt="2026-04-16",
                    sjrq="2026-04-16",
                    vipkf="86000995",
                )
                grouped_same_dzdh = VisitOrder(
                    id="vo_match_3",
                    dzdh="DZ1002",
                    dzseg="120",
                    jgbm="6101",
                    crtdt="2026-04-16",
                    sjrq="2026-04-16",
                    advxc="86000009",
                )
                wrong_institution = VisitOrder(
                    id="vo_wrong_inst",
                    dzdh="DZ2001",
                    dzseg="110",
                    jgbm="6201",
                    crtdt="2026-04-16",
                    sjrq="2026-04-16",
                    fzuer="86000995",
                )
                wrong_date = VisitOrder(
                    id="vo_wrong_date",
                    dzdh="DZ2002",
                    dzseg="110",
                    jgbm="6101",
                    crtdt="2026-04-17",
                    sjrq="2026-04-17",
                    fzuer="86000995",
                )
                wrong_participant = VisitOrder(
                    id="vo_wrong_participant",
                    dzdh="DZ2003",
                    dzseg="110",
                    jgbm="6101",
                    crtdt="2026-04-16",
                    sjrq="2026-04-16",
                    fzuer="86000000",
                    advxc="86000001",
                    assxc="86000002",
                )

                db.add_all([
                    staff,
                    user,
                    recording,
                    matching_primary,
                    matching_vipkf,
                    grouped_same_dzdh,
                    wrong_institution,
                    wrong_date,
                    wrong_participant,
                ])
                await db.commit()

                result = await list_visit_orders(
                    db=db,
                    page=1,
                    page_size=20,
                    keyword=None,
                    fzuer=None,
                    sjrq_start=None,
                    sjrq_end=None,
                    jcsta_txt=None,
                    current_user=user,
                )

                assert result["total"] == 3
                assert {item.id for item in result["items"]} == {"vo_match_1", "vo_match_2", "vo_match_3"}
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_daily_visit_orders_do_not_expose_inaccessible_local_visit_ids_for_staff() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    id="staff_1",
                    name="测试员工",
                    external_account="86000995",
                    hospital_code="6101",
                    permission_role="staff",
                )
                other_consultant = Staff(
                    id="staff_2",
                    name="其他咨询师",
                    external_account="81000001",
                    hospital_code="6101",
                    permission_role="staff",
                )
                customer = Customer(
                    id="cust_org_1",
                    name="机构客户",
                    external_customer_code="K2001",
                )
                user = User(
                    username="86000995",
                    hashed_password="x",
                    role="staff",
                    staff_id=staff.id,
                    hospital_code="6101",
                    is_active=True,
                )
                customer = Customer(
                    id="cust_1",
                    name="测试客户",
                    external_customer_code="K1001",
                )
                recording = Recording(
                    id="rec_1",
                    staff_id=staff.id,
                    file_name="20260418_101500.mp3",
                    file_path="/tmp/20260418_101500.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 4, 18, 10, 15, tzinfo=timezone.utc),
                )
                visible_order = VisitOrder(
                    id="vo_visible",
                    dzdh="DZ1001",
                    dzseg="110",
                    jgbm="6101",
                    crtdt="2026-04-18",
                    sjrq="2026-04-18",
                    advxc="81000001",
                )
                participant_sibling_order = VisitOrder(
                    id="vo_participant",
                    dzdh="DZ1001",
                    dzseg="120",
                    jgbm="6101",
                    crtdt="2026-04-18",
                    sjrq="2026-04-18",
                    advxc="86000995",
                )
                inaccessible_visit = Visit(
                    id="visit_inaccessible",
                    customer_id=customer.id,
                    external_visit_order_no="DZ1001",
                    external_visit_order_seg="110",
                    consultant_id=other_consultant.id,
                )

                db.add_all([
                    staff,
                    other_consultant,
                    user,
                    customer,
                    recording,
                    visible_order,
                    participant_sibling_order,
                    inaccessible_visit,
                ])
                await db.commit()

                result = await list_daily_visit_orders_for_recording(
                    recording_id=recording.id,
                    db=db,
                    current_user=user,
                )

                items_by_id = {item["id"]: item for item in result["items"]}
                assert "vo_visible" in items_by_id
                assert items_by_id["vo_visible"]["local_visit_id"] == "visit_inaccessible"
                assert items_by_id["vo_visible"]["detail_local_visit_id"] == "visit_inaccessible"
                assert items_by_id["vo_visible"]["associated_local_visit_ids"] == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_daily_visit_orders_self_scope_includes_configured_department_assistant_orders() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                tenant = WecomTenant(
                    id="tenant_csyamei",
                    name="长沙雅美",
                    default_hospital_code="6501",
                    is_active=True,
                    department_assistant_match_config={
                        "enabled": True,
                        "departments": [
                            {
                                "department_code": "JGKS03",
                                "department_name": "外科",
                                "assistant_staff_ids": ["staff_dept_assistant"],
                            }
                        ],
                    },
                )
                staff = Staff(
                    id="staff_dept_assistant",
                    name="科室助理A",
                    external_account="86000995",
                    hospital_code="6501",
                    role="consultant",
                    permission_role="staff",
                )
                other_staff = Staff(
                    id="staff_other",
                    name="现场咨询",
                    external_account="81000001",
                    hospital_code="6501",
                    permission_role="staff",
                )
                user = User(
                    username="86000995",
                    hashed_password="x",
                    role="staff",
                    staff_id=staff.id,
                    hospital_code="6501",
                    is_active=True,
                )
                recording = Recording(
                    id="rec_dept_assistant",
                    staff_id=staff.id,
                    file_name="20260418_101500.mp3",
                    file_path="/tmp/20260418_101500.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 4, 18, 10, 15, tzinfo=timezone.utc),
                )
                department_order = VisitOrder(
                    id="vo_department",
                    dzdh="DZ1001",
                    dzseg="110",
                    jgbm="6501",
                    crtdt="2026-04-18",
                    sjrq="2026-04-18",
                    advxc=other_staff.external_account,
                    fzuer=other_staff.external_account,
                    yyuer="82000001",
                    jgks="JGKS03",
                    jgks_txt="外科",
                    ninam="科室客户",
                )
                other_department_order = VisitOrder(
                    id="vo_other_department",
                    dzdh="DZ1002",
                    dzseg="110",
                    jgbm="6501",
                    crtdt="2026-04-18",
                    sjrq="2026-04-18",
                    advxc=other_staff.external_account,
                    jgks="JGKS02",
                    jgks_txt="皮肤科",
                    ninam="其他科室客户",
                )

                db.add_all([tenant, staff, other_staff, user, recording, department_order, other_department_order])
                await db.commit()

                result = await list_daily_visit_orders_for_recording(
                    recording_id=recording.id,
                    scope_mode="self",
                    db=db,
                    current_user=user,
                )

                assert result["scope_mode"] == "self"
                assert result["total"] == 1
                assert result["items"][0]["id"] == "vo_department"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_daily_visit_orders_org_scope_returns_same_institution_orders_and_supports_keyword_search() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    id="staff_1",
                    name="测试员工",
                    external_account="86000995",
                    hospital_code="6101",
                    permission_role="staff",
                )
                other_consultant = Staff(
                    id="staff_2",
                    name="其他咨询师",
                    external_account="81000001",
                    hospital_code="6101",
                    permission_role="staff",
                )
                customer = Customer(
                    id="cust_org_1",
                    name="机构客户",
                    external_customer_code="K2001",
                )
                user = User(
                    username="86000995",
                    hashed_password="x",
                    role="staff",
                    staff_id=staff.id,
                    hospital_code="6101",
                    is_active=True,
                )
                recording = Recording(
                    id="rec_1",
                    staff_id=staff.id,
                    file_name="20260418_101500.mp3",
                    file_path="/tmp/20260418_101500.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 4, 18, 10, 15, tzinfo=timezone.utc),
                )
                self_order = VisitOrder(
                    id="vo_self",
                    dzdh="DZ1001",
                    dzseg="110",
                    jgbm="6101",
                    crtdt="2026-04-18",
                    sjrq="2026-04-18",
                    advxc="86000995",
                    ninam="自己客户",
                )
                org_order = VisitOrder(
                    id="vo_org",
                    dzdh="DZ2001",
                    dzseg="110",
                    jgbm="6101",
                    crtdt="2026-04-18",
                    sjrq="2026-04-18",
                    advxc="81000001",
                    ninam="王小美",
                    remark_dz="鼻部咨询",
                )
                other_org_order = VisitOrder(
                    id="vo_other_org",
                    dzdh="DZ3001",
                    dzseg="110",
                    jgbm="6201",
                    crtdt="2026-04-18",
                    sjrq="2026-04-18",
                    advxc="81000001",
                    ninam="不应出现",
                )
                inaccessible_visit = Visit(
                    id="visit_org_hidden",
                    customer_id=customer.id,
                    external_visit_order_no="DZ2001",
                    external_visit_order_seg="110",
                    consultant_id=other_consultant.id,
                )

                db.add_all([
                    staff,
                    other_consultant,
                    customer,
                    user,
                    recording,
                    self_order,
                    org_order,
                    other_org_order,
                    inaccessible_visit,
                ])
                await db.commit()

                result = await list_daily_visit_orders_for_recording(
                    recording_id=recording.id,
                    scope_mode="org",
                    keyword="鼻部",
                    db=db,
                    current_user=user,
                )

                assert result["scope_mode"] == "org"
                assert result["keyword"] == "鼻部"
                assert result["total"] == 1
                item = result["items"][0]
                assert item["id"] == "vo_org"
                assert item["local_visit_id"] == "visit_org_hidden"
                assert item["detail_local_visit_id"] is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())
