from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes import dashboard as dashboard_routes
from smart_badge_api.api.routes.dashboard import get_dashboard
from smart_badge_api.db.base import Base
from smart_badge_api.core.permissions import PermissionScope
from smart_badge_api.db.models import AnalysisTask, Customer, Recording, RecordingVisitLink, Staff, StaffManagementRelation, User, Visit, VisitOrder


def test_dashboard_supports_filtering_single_staff_within_hospital_scope() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        dashboard_routes._cache.clear()

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff_a = Staff(
                    id="staff_a",
                    name="顾问A",
                    external_account="ADV001",
                    hospital_code="6101",
                    permission_role="staff",
                )
                staff_b = Staff(
                    id="staff_b",
                    name="顾问B",
                    external_account="ADV002",
                    hospital_code="6101",
                    permission_role="staff",
                )
                staff_outside = Staff(
                    id="staff_outside",
                    name="Outside",
                    external_account="ADV003",
                    hospital_code="6101",
                    permission_role="staff",
                )
                manager_staff = Staff(
                    id="manager_staff",
                    name="Manager",
                    external_account="M001",
                    hospital_code="6101",
                    permission_role="hospital_admin",
                )
                customer_a = Customer(id="cust_a", name="客户A")
                customer_b = Customer(id="cust_b", name="客户B")
                customer_outside = Customer(id="cust_outside", name="客户C")
                visit_a = Visit(
                    id="visit_a",
                    customer_id=customer_a.id,
                    consultant_id=staff_a.id,
                    status="consulting",
                )
                visit_b = Visit(
                    id="visit_b",
                    customer_id=customer_b.id,
                    consultant_id=staff_b.id,
                    status="consulting",
                )
                visit_outside = Visit(
                    id="visit_outside",
                    customer_id=customer_outside.id,
                    consultant_id=staff_outside.id,
                    status="consulting",
                )
                recording_a = Recording(
                    id="rec_a",
                    visit_id=visit_a.id,
                    staff_id=staff_a.id,
                    file_name="a.mp3",
                    file_path="recordings/a.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc),
                )
                recording_b = Recording(
                    id="rec_b",
                    visit_id=visit_b.id,
                    staff_id=staff_b.id,
                    file_name="b.mp3",
                    file_path="recordings/b.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 4, 20, 11, 0, tzinfo=timezone.utc),
                )
                recording_outside = Recording(
                    id="rec_outside",
                    visit_id=visit_outside.id,
                    staff_id=staff_outside.id,
                    file_name="outside.mp3",
                    file_path="recordings/outside.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc),
                )
                current_user = User(
                    username="manager",
                    hashed_password="hashed",
                    display_name="机构管理员",
                    role="hospital_admin",
                    staff_id="manager_staff",
                    hospital_code="6101",
                    hospital_name="测试机构",
                    is_active=True,
                )

                db.add_all([
                    staff_a,
                    staff_b,
                    staff_outside,
                    manager_staff,
                    customer_a,
                    customer_b,
                    customer_outside,
                    visit_a,
                    visit_b,
                    visit_outside,
                    recording_a,
                    recording_b,
                    recording_outside,
                    StaffManagementRelation(
                        hospital_code="6101",
                        manager_staff_id=manager_staff.id,
                        subordinate_staff_id=staff_a.id,
                    ),
                    StaffManagementRelation(
                        hospital_code="6101",
                        manager_staff_id=manager_staff.id,
                        subordinate_staff_id=staff_b.id,
                    ),
                    current_user,
                ])
                await db.commit()

                dashboard_all = await get_dashboard(
                    hospital_code=None,
                    scope_mode="all",
                    staff_id=None,
                    date_from=None,
                    date_to=None,
                    db=db,
                    current_user=current_user,
                )
                assert dashboard_all.total_recordings == 2
                assert dashboard_all.total_visits == 2
                assert dashboard_all.dashboard_staff_id is None
                staff_stats_by_id = {item.staff_id: item for item in dashboard_all.staff_stats}
                assert staff_stats_by_id[staff_a.id].linked_visit_count == 1
                assert staff_stats_by_id[staff_b.id].linked_visit_count == 1

                dashboard_single = await get_dashboard(
                    hospital_code=None,
                    scope_mode="all",
                    staff_id=staff_a.id,
                    date_from=None,
                    date_to=None,
                    db=db,
                    current_user=current_user,
                )

                assert dashboard_single.dashboard_can_select_staff is True
                assert dashboard_single.dashboard_staff_id == staff_a.id
                assert dashboard_single.dashboard_staff_name == staff_a.name
                assert {item.staff_id for item in dashboard_single.dashboard_staff_options} >= {staff_a.id, staff_b.id}
                assert dashboard_single.total_recordings == 1
                assert dashboard_single.total_visits == 1
                assert dashboard_single.total_customers == 1
                assert [item.staff_id for item in dashboard_single.staff_stats] == [staff_a.id]
                assert dashboard_single.staff_stats[0].linked_visit_count == 1
        finally:
            dashboard_routes._cache.clear()
            await engine.dispose()

    asyncio.run(scenario())


def test_visible_hospital_options_prefer_staff_hospital_short_name() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                db.add_all([
                    Staff(
                        id="staff_a",
                        name="顾问A",
                        hospital_code="6101",
                        hospital_short_name="米兰柏羽总院",
                        permission_role="staff",
                    ),
                    VisitOrder(
                        id="visit_order_a",
                        dzdh="DZ001",
                        dzseg="001",
                        jgbm="6101",
                    ),
                ])
                await db.commit()

                options = await dashboard_routes._load_visible_hospitals(
                    db,
                    PermissionScope(role="super_admin", staff_id="staff_a", hospital_code=None),
                    User(
                        username="admin",
                        hashed_password="hashed",
                        display_name="超级管理员",
                        role="super_admin",
                        staff_id="staff_a",
                        is_active=True,
                    ),
                )

                assert [(item.hospital_code, item.hospital_name) for item in options] == [("6101", "米兰柏羽总院")]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_dashboard_result_stats_dedupe_duplicate_done_tasks_for_same_recording() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        dashboard_routes._cache.clear()

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    id="staff_a",
                    name="顾问A",
                    hospital_code="6101",
                    permission_role="staff",
                )
                manager_staff = Staff(
                    id="manager_staff",
                    name="Manager",
                    hospital_code="6101",
                    permission_role="hospital_admin",
                )
                recording = Recording(
                    id="rec_a",
                    staff_id=staff.id,
                    file_name="a.mp3",
                    file_path="recordings/a.mp3",
                    status="analyzed",
                    created_at=datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc),
                )
                duplicated_result = {
                    "consultation_process_evaluation": {
                        "total_score": 6.0,
                        "max_total_score": 9.0,
                        "sections": [
                            {
                                "code": "opening",
                                "name": "开场",
                                "point_score": 1.0,
                                "max_score": 1.0,
                                "passed": True,
                                "issues": [],
                            }
                        ],
                    },
                    "standardized_indications": {
                        "items": [
                            {
                                "indication_name": "补水",
                            }
                        ]
                    },
                }
                task_a = AnalysisTask(
                    id="task_a",
                    file_name="recording_rec_a.json",
                    file_path="results/recording_rec_a.json",
                    status="done",
                    result=duplicated_result,
                    created_at=datetime(2026, 4, 20, 10, 5, tzinfo=timezone.utc),
                    completed_at=datetime(2026, 4, 20, 10, 15, tzinfo=timezone.utc),
                )
                task_b = AnalysisTask(
                    id="task_b",
                    file_name="recording_rec_a.json",
                    file_path="results/recording_rec_a_retry.json",
                    status="done",
                    result=duplicated_result,
                    created_at=datetime(2026, 4, 20, 10, 6, tzinfo=timezone.utc),
                    completed_at=datetime(2026, 4, 20, 10, 16, tzinfo=timezone.utc),
                )
                current_user = User(
                    username="manager",
                    hashed_password="hashed",
                    display_name="机构管理员",
                    role="hospital_admin",
                    staff_id="manager_staff",
                    hospital_code="6101",
                    hospital_name="测试机构",
                    is_active=True,
                )

                db.add_all([
                    staff,
                    manager_staff,
                    recording,
                    task_a,
                    task_b,
                    StaffManagementRelation(
                        hospital_code="6101",
                        manager_staff_id=manager_staff.id,
                        subordinate_staff_id=staff.id,
                    ),
                    current_user,
                ])
                await db.commit()

                dashboard = await get_dashboard(
                    hospital_code=None,
                    scope_mode="all",
                    staff_id=None,
                    date_from=None,
                    date_to=None,
                    db=db,
                    current_user=current_user,
                )

                assert dashboard.done_count == 2
                assert dashboard.total_recordings == 1
                assert dashboard.process_evaluation_summary.evaluated_count == 1
                assert dashboard.result_analysis_modules[0].analyzed_count == 1
                assert dashboard.staff_stats[0].analyzed_count == 1
        finally:
            dashboard_routes._cache.clear()
            await engine.dispose()

    asyncio.run(scenario())


def test_dashboard_quality_passed_recordings_excludes_filtered_recordings() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        dashboard_routes._cache.clear()

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    id="staff_a",
                    name="顾问A",
                    hospital_code="6101",
                    permission_role="staff",
                )
                manager_staff = Staff(
                    id="manager_staff",
                    name="Manager",
                    hospital_code="6101",
                    permission_role="hospital_admin",
                )
                current_user = User(
                    username="manager",
                    hashed_password="hashed",
                    display_name="机构管理员",
                    role="hospital_admin",
                    staff_id="manager_staff",
                    hospital_code="6101",
                    hospital_name="测试机构",
                    is_active=True,
                )
                db.add_all([
                    staff,
                    manager_staff,
                    Customer(id="cust_pass", name="有效客户"),
                    Visit(id="visit_pass", customer_id="cust_pass"),
                    Recording(
                        id="rec_pass",
                        staff_id=staff.id,
                        visit_id="visit_pass",
                        file_name="pass.mp3",
                        file_path="recordings/pass.mp3",
                        status="analyzed",
                        created_at=datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc),
                    ),
                    Recording(
                        id="rec_filtered",
                        staff_id=staff.id,
                        file_name="filtered.mp3",
                        file_path="recordings/filtered.mp3",
                        status="filtered",
                        created_at=datetime(2026, 4, 20, 11, 0, tzinfo=timezone.utc),
                    ),
                    RecordingVisitLink(recording_id="rec_pass", visit_id="visit_pass", is_primary=True),
                    StaffManagementRelation(
                        hospital_code="6101",
                        manager_staff_id=manager_staff.id,
                        subordinate_staff_id=staff.id,
                    ),
                    current_user,
                ])
                await db.commit()

                dashboard = await get_dashboard(
                    hospital_code=None,
                    scope_mode="all",
                    staff_id=None,
                    date_from=None,
                    date_to=None,
                    db=db,
                    current_user=current_user,
                )

                assert dashboard.total_recordings == 1
                assert dashboard.quality_passed_recordings == 1
                assert dashboard.staff_stats[0].recording_count == 1
                assert dashboard.staff_stats[0].linked_visit_count == 1
                assert dashboard.score_staff_stats[0].recording_count == 1
                assert dashboard.score_staff_stats[0].linked_visit_count == 1
        finally:
            dashboard_routes._cache.clear()
            await engine.dispose()

    asyncio.run(scenario())
