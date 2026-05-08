import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes import analysis as analysis_routes
from smart_badge_api.api.routes.analysis import get_result, list_results
from smart_badge_api.api.routes.customers import (
    get_customer,
    get_customer_detail,
    get_customer_merged_analysis,
    list_customers,
)
from smart_badge_api.api.routes.recordings import get_recording, list_recordings
from smart_badge_api.api.routes.segments import get_segment, list_segments
from smart_badge_api.api.routes.transcripts import get_transcript, list_transcripts
from smart_badge_api.api.routes.visits import get_visit_detail, list_visits
from smart_badge_api.api.routes.visit_orders import list_visit_orders
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import AnalysisTask, Customer, Recording, Segment, Staff, StaffManagementRelation, Transcript, User, Visit, VisitOrder
from smart_badge_api.visit_linking import sync_recording_visit_links


def test_staff_scope_limits_lists_and_detail_access() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff_a = Staff(id="staff_a", name="顾问A", external_account="ADV001", permission_role="staff")
                staff_b = Staff(id="staff_b", name="顾问B", external_account="ADV002", permission_role="staff")
                customer_a = Customer(id="cust_a", name="客户A")
                customer_b = Customer(id="cust_b", name="客户B")
                visit_a = Visit(id="visit_a", customer_id=customer_a.id, consultant_id=staff_a.id, status="consulting")
                visit_b = Visit(id="visit_b", customer_id=customer_b.id, consultant_id=staff_b.id, status="consulting")
                recording_a = Recording(
                    id="rec_a",
                    visit_id=visit_a.id,
                    staff_id=staff_a.id,
                    file_name="a.mp3",
                    file_path="recordings/a.mp3",
                    status="uploaded",
                )
                recording_b = Recording(
                    id="rec_b",
                    visit_id=visit_b.id,
                    staff_id=staff_b.id,
                    file_name="b.mp3",
                    file_path="recordings/b.mp3",
                    status="uploaded",
                )
                current_user = User(
                    username="ADV001",
                    hashed_password="hashed",
                    display_name="顾问A",
                    staff_id=staff_a.id,
                    role="staff",
                    hospital_code="6101",
                    hospital_name="总院",
                    is_active=True,
                )

                db.add_all([
                    staff_a,
                    staff_b,
                    customer_a,
                    customer_b,
                    visit_a,
                    visit_b,
                    recording_a,
                    recording_b,
                    current_user,
                ])
                await db.flush()
                await sync_recording_visit_links(db, recording_a, [visit_a.id], primary_visit_id=visit_a.id, source="test")
                await sync_recording_visit_links(db, recording_b, [visit_b.id], primary_visit_id=visit_b.id, source="test")
                await db.commit()

                customers = await list_customers(
                    keyword="",
                    is_active=None,
                    consultant_id=None,
                    has_visits=None,
                    has_recordings=None,
                    has_positive_recharge=None,
                    date_from=None,
                    date_to=None,
                    page=1,
                    page_size=20,
                    db=db,
                    current_user=current_user,
                )
                assert customers.total == 1
                assert customers.items[0].id == customer_a.id

                visits = await list_visits(
                    customer_id=None,
                    status=None,
                    has_recharge=None,
                    keyword=None,
                    consultant_id=None,
                    source=None,
                    date_from=None,
                    date_to=None,
                    has_recordings=None,
                    page=1,
                    page_size=20,
                    db=db,
                    current_user=current_user,
                )
                assert visits.total == 1
                assert visits.items[0].id == visit_a.id
                assert visits.items[0].recording_count == 1

                recordings = await list_recordings(
                    visit_id=None,
                    staff_id=None,
                    status=None,
                    keyword=None,
                    customer_keyword=None,
                    badge_id=None,
                    role=None,
                    has_visit=None,
                    date_from=None,
                    date_to=None,
                    page=1,
                    page_size=20,
                    db=db,
                    current_user=current_user,
                )
                assert recordings.total == 1
                assert recordings.items[0].id == recording_a.id

                assert (await get_customer_detail(customer_a.id, db=db, current_user=current_user)).id == customer_a.id
                assert (await get_visit_detail(visit_a.id, db=db, current_user=current_user)).id == visit_a.id
                assert (await get_recording(recording_a.id, db=db, current_user=current_user)).id == recording_a.id

                for fn, target_id in (
                    (get_customer_detail, customer_b.id),
                    (get_visit_detail, visit_b.id),
                    (get_recording, recording_b.id),
                ):
                    try:
                        await fn(target_id, db=db, current_user=current_user)
                    except HTTPException as exc:
                        assert exc.status_code == 404
                    else:
                        raise AssertionError("Cross-staff detail access should be rejected")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_staff_management_relation_extends_staff_scope() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                manager = Staff(
                    id="manager",
                    name="主管",
                    external_account="M001",
                    hospital_code="6101",
                    permission_role="staff",
                )
                subordinate = Staff(
                    id="subordinate",
                    name="下属",
                    external_account="S001",
                    hospital_code="6101",
                    permission_role="staff",
                )
                outside = Staff(
                    id="outside",
                    name="其他员工",
                    external_account="O001",
                    hospital_code="6101",
                    permission_role="staff",
                )
                higher_permission = Staff(
                    id="higher_permission",
                    name="高权限员工",
                    external_account="H001",
                    hospital_code="6101",
                    permission_role="system_admin",
                )
                customer_manager = Customer(id="cust_manager", name="主管客户")
                customer_subordinate = Customer(id="cust_subordinate", name="下属客户")
                customer_outside = Customer(id="cust_outside", name="其他客户")
                customer_higher_permission = Customer(id="cust_higher_permission", name="高权限客户")
                visit_manager = Visit(
                    id="visit_manager",
                    customer_id=customer_manager.id,
                    consultant_id=manager.id,
                    status="consulting",
                )
                visit_subordinate = Visit(
                    id="visit_subordinate",
                    customer_id=customer_subordinate.id,
                    consultant_id=subordinate.id,
                    status="consulting",
                )
                visit_outside = Visit(
                    id="visit_outside",
                    customer_id=customer_outside.id,
                    consultant_id=outside.id,
                    status="consulting",
                )
                visit_higher_permission = Visit(
                    id="visit_higher_permission",
                    customer_id=customer_higher_permission.id,
                    consultant_id=higher_permission.id,
                    status="consulting",
                )
                recording_manager = Recording(
                    id="rec_manager",
                    visit_id=visit_manager.id,
                    staff_id=manager.id,
                    file_name="manager.mp3",
                    file_path="recordings/manager.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 4, 19, tzinfo=timezone.utc),
                )
                recording_subordinate = Recording(
                    id="rec_subordinate",
                    visit_id=visit_subordinate.id,
                    staff_id=subordinate.id,
                    file_name="subordinate.mp3",
                    file_path="recordings/subordinate.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 4, 19, tzinfo=timezone.utc),
                )
                recording_outside = Recording(
                    id="rec_outside",
                    visit_id=visit_outside.id,
                    staff_id=outside.id,
                    file_name="outside.mp3",
                    file_path="recordings/outside.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 4, 19, tzinfo=timezone.utc),
                )
                recording_higher_permission = Recording(
                    id="rec_higher_permission",
                    visit_id=visit_higher_permission.id,
                    staff_id=higher_permission.id,
                    file_name="higher.mp3",
                    file_path="recordings/higher.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 4, 19, tzinfo=timezone.utc),
                )
                visit_order_manager = VisitOrder(
                    id="vo_manager",
                    dzdh="VO_MANAGER",
                    jgbm="6101",
                    advxc=manager.external_account,
                    crtdt="2026-04-19",
                    sjrq="2026-04-19",
                )
                visit_order_subordinate = VisitOrder(
                    id="vo_subordinate",
                    dzdh="VO_SUBORDINATE",
                    jgbm="6101",
                    advxc=subordinate.external_account,
                    crtdt="2026-04-19",
                    sjrq="2026-04-19",
                )
                visit_order_outside = VisitOrder(
                    id="vo_outside",
                    dzdh="VO_OUTSIDE",
                    jgbm="6101",
                    advxc=outside.external_account,
                    crtdt="2026-04-19",
                    sjrq="2026-04-19",
                )
                visit_order_higher_permission = VisitOrder(
                    id="vo_higher_permission",
                    dzdh="VO_HIGHER",
                    jgbm="6101",
                    advxc=higher_permission.external_account,
                    crtdt="2026-04-19",
                    sjrq="2026-04-19",
                )
                relation = StaffManagementRelation(
                    hospital_code="6101",
                    manager_staff_id=manager.id,
                    subordinate_staff_id=subordinate.id,
                )
                higher_relation = StaffManagementRelation(
                    hospital_code="6101",
                    manager_staff_id=manager.id,
                    subordinate_staff_id=higher_permission.id,
                )
                current_user = User(
                    username="M001",
                    hashed_password="hashed",
                    display_name="主管",
                    staff_id=manager.id,
                    role="staff",
                    hospital_code="6101",
                    hospital_name="总院",
                    is_active=True,
                )

                db.add_all(
                    [
                        manager,
                        subordinate,
                        outside,
                        higher_permission,
                        customer_manager,
                        customer_subordinate,
                        customer_outside,
                        customer_higher_permission,
                        visit_manager,
                        visit_subordinate,
                        visit_outside,
                        visit_higher_permission,
                        recording_manager,
                        recording_subordinate,
                        recording_outside,
                        recording_higher_permission,
                        visit_order_manager,
                        visit_order_subordinate,
                        visit_order_outside,
                        visit_order_higher_permission,
                        relation,
                        higher_relation,
                        current_user,
                    ]
                )
                await db.flush()
                await sync_recording_visit_links(
                    db,
                    recording_manager,
                    [visit_manager.id],
                    primary_visit_id=visit_manager.id,
                    source="test",
                )
                await sync_recording_visit_links(
                    db,
                    recording_subordinate,
                    [visit_subordinate.id],
                    primary_visit_id=visit_subordinate.id,
                    source="test",
                )
                await sync_recording_visit_links(
                    db,
                    recording_outside,
                    [visit_outside.id],
                    primary_visit_id=visit_outside.id,
                    source="test",
                )
                await sync_recording_visit_links(
                    db,
                    recording_higher_permission,
                    [visit_higher_permission.id],
                    primary_visit_id=visit_higher_permission.id,
                    source="test",
                )
                await db.commit()

                customers = await list_customers(
                    keyword="",
                    is_active=None,
                    consultant_id=None,
                    has_visits=None,
                    has_recordings=None,
                    has_positive_recharge=None,
                    date_from=None,
                    date_to=None,
                    page=1,
                    page_size=20,
                    db=db,
                    current_user=current_user,
                )
                assert {item.id for item in customers.items} == {customer_manager.id, customer_subordinate.id}

                visits = await list_visits(
                    customer_id=None,
                    status=None,
                    has_recharge=None,
                    keyword=None,
                    consultant_id=None,
                    source=None,
                    date_from=None,
                    date_to=None,
                    has_recordings=None,
                    page=1,
                    page_size=20,
                    db=db,
                    current_user=current_user,
                )
                assert {item.id for item in visits.items} == {visit_manager.id, visit_subordinate.id}

                recordings = await list_recordings(
                    visit_id=None,
                    staff_id=None,
                    status=None,
                    keyword=None,
                    customer_keyword=None,
                    badge_id=None,
                    role=None,
                    has_visit=None,
                    date_from=None,
                    date_to=None,
                    page=1,
                    page_size=20,
                    db=db,
                    current_user=current_user,
                )
                assert {item.id for item in recordings.items} == {recording_manager.id, recording_subordinate.id}

                visit_orders = await list_visit_orders(
                    db=db,
                    page=1,
                    page_size=20,
                    keyword=None,
                    fzuer=None,
                    sjrq_start=None,
                    sjrq_end=None,
                    jcsta_txt=None,
                    current_user=current_user,
                )
                assert {item.id for item in visit_orders["items"]} == {
                    visit_order_manager.id,
                    visit_order_subordinate.id,
                }
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_staff_scope_limits_transcripts_and_segments() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff_a = Staff(id="staff_a", name="顾问A", external_account="ADV001", permission_role="staff")
                staff_b = Staff(id="staff_b", name="顾问B", external_account="ADV002", permission_role="staff")
                recording_a = Recording(
                    id="rec_a",
                    staff_id=staff_a.id,
                    file_name="a.mp3",
                    file_path="recordings/a.mp3",
                    status="uploaded",
                )
                recording_b = Recording(
                    id="rec_b",
                    staff_id=staff_b.id,
                    file_name="b.mp3",
                    file_path="recordings/b.mp3",
                    status="uploaded",
                )
                transcript_a = Transcript(
                    id="tr_a",
                    recording_id=recording_a.id,
                    asr_provider="manual",
                    status="completed",
                    full_text="自己的转写",
                )
                transcript_b = Transcript(
                    id="tr_b",
                    recording_id=recording_b.id,
                    asr_provider="manual",
                    status="completed",
                    full_text="别人的转写",
                )
                segment_a = Segment(id="seg_a", recording_id=recording_a.id, segment_index=0, text="自己的片段")
                segment_b = Segment(id="seg_b", recording_id=recording_b.id, segment_index=0, text="别人的片段")
                current_user = User(
                    username="ADV001",
                    hashed_password="hashed",
                    display_name="顾问A",
                    staff_id=staff_a.id,
                    role="staff",
                    is_active=True,
                )

                db.add_all([
                    staff_a,
                    staff_b,
                    recording_a,
                    recording_b,
                    transcript_a,
                    transcript_b,
                    segment_a,
                    segment_b,
                    current_user,
                ])
                await db.commit()

                transcripts = await list_transcripts(
                    recording_id=None,
                    status=None,
                    page=1,
                    page_size=20,
                    db=db,
                    current_user=current_user,
                )
                assert transcripts.total == 1
                assert transcripts.items[0].id == transcript_a.id
                assert (await get_transcript(transcript_a.id, db=db, current_user=current_user)).id == transcript_a.id

                segments = await list_segments(
                    recording_id=None,
                    visit_id=None,
                    status=None,
                    page=1,
                    page_size=20,
                    db=db,
                    current_user=current_user,
                )
                assert segments.total == 1
                assert segments.items[0].id == segment_a.id
                assert (await get_segment(segment_a.id, db=db, current_user=current_user)).id == segment_a.id

                for fn, target_id in ((get_transcript, transcript_b.id), (get_segment, segment_b.id)):
                    try:
                        await fn(target_id, db=db, current_user=current_user)
                    except HTTPException as exc:
                        assert exc.status_code == 404
                    else:
                        raise AssertionError("Cross-staff transcript/segment access should be rejected")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_staff_customer_and_visit_detail_only_include_own_recordings() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff_a = Staff(id="staff_a", name="顾问A", external_account="ADV001", permission_role="staff")
                staff_b = Staff(id="staff_b", name="医生B", external_account="DOC001", permission_role="staff", is_doctor=True)
                customer = Customer(id="cust_shared", name="共享客户")
                visit = Visit(
                    id="visit_shared",
                    customer_id=customer.id,
                    consultant_id=staff_a.id,
                    doctor_id=staff_b.id,
                    status="consulting",
                )
                recording_a = Recording(
                    id="rec_self",
                    visit_id=visit.id,
                    staff_id=staff_a.id,
                    file_name="self.mp3",
                    file_path="recordings/self.mp3",
                    status="analyzed",
                )
                recording_b = Recording(
                    id="rec_other",
                    visit_id=visit.id,
                    staff_id=staff_b.id,
                    file_name="other.mp3",
                    file_path="recordings/other.mp3",
                    status="analyzed",
                )
                current_user = User(
                    username="ADV001",
                    hashed_password="hashed",
                    display_name="顾问A",
                    staff_id=staff_a.id,
                    role="staff",
                    is_active=True,
                )

                db.add_all([staff_a, staff_b, customer, visit, recording_a, recording_b, current_user])
                await db.flush()
                await sync_recording_visit_links(db, recording_a, [visit.id], primary_visit_id=visit.id, source="test")
                await sync_recording_visit_links(db, recording_b, [visit.id], primary_visit_id=visit.id, source="test")
                db.add_all(
                    [
                        AnalysisTask(
                            id="task_self",
                            file_name="recording_rec_self.json",
                            file_path="uploads/analysis_input/recording_rec_self.json",
                            status="done",
                            overall_score=7.2,
                            completed_at=datetime.now(timezone.utc),
                            result={
                                "customer_demands": {"focus_areas": [{"area": "祛斑", "surface_need": "想淡斑"}]},
                                "customer_concerns": {"items": [{"type": "价格", "content": "预算有限"}]},
                                "customer_profile": {"tags": [{"category": "意向度", "value": "高"}]},
                                "consultation_evaluation": {"overall_score": 7.2, "dimensions": []},
                            },
                        ),
                        AnalysisTask(
                            id="task_other",
                            file_name="recording_rec_other.json",
                            file_path="uploads/analysis_input/recording_rec_other.json",
                            status="done",
                            overall_score=4.8,
                            completed_at=datetime.now(timezone.utc),
                            result={
                                "customer_demands": {"focus_areas": [{"area": "塑形", "surface_need": "想瘦脸"}]},
                                "customer_concerns": {"items": [{"type": "恢复期", "content": "怕影响上班"}]},
                                "customer_profile": {"tags": [{"category": "意向度", "value": "中"}]},
                                "consultation_evaluation": {"overall_score": 4.8, "dimensions": []},
                            },
                        ),
                    ]
                )
                await db.commit()

                visit_detail = await get_visit_detail(visit.id, db=db, current_user=current_user)
                assert [item.id for item in visit_detail.recordings] == [recording_a.id]

                customer_detail = await get_customer_detail(customer.id, db=db, current_user=current_user)
                assert customer_detail.recording_count == 1
                assert len(customer_detail.visits) == 1
                assert [item.id for item in customer_detail.visits[0].recordings] == [recording_a.id]

                merged = await get_customer_merged_analysis(customer.id, db=db, current_user=current_user)
                assert merged.total_recordings == 1
                assert merged.analyzed_recordings == 1
                assert all(item.recording_id == recording_a.id for item in merged.timeline)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_staff_can_open_visit_detail_with_direct_visit_recording_without_link() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(id="staff_direct", name="顾问直连", external_account="ADV900", permission_role="staff")
                customer = Customer(id="cust_direct", name="直连客户")
                visit = Visit(
                    id="visit_direct",
                    customer_id=customer.id,
                    status="consulting",
                )
                recording = Recording(
                    id="rec_direct",
                    visit_id=visit.id,
                    staff_id=staff.id,
                    file_name="direct.mp3",
                    file_path="recordings/direct.mp3",
                    status="uploaded",
                )
                current_user = User(
                    username="ADV900",
                    hashed_password="hashed",
                    display_name="顾问直连",
                    staff_id=staff.id,
                    role="staff",
                    is_active=True,
                )

                db.add_all([staff, customer, visit, recording, current_user])
                await db.commit()

                visit_detail = await get_visit_detail(visit.id, db=db, current_user=current_user)
                assert visit_detail.id == visit.id
                assert [item.id for item in visit_detail.recordings] == [recording.id]

                customer_detail = await get_customer_detail(customer.id, db=db, current_user=current_user)
                assert len(customer_detail.visits) == 1
                assert customer_detail.visits[0].id == visit.id
                assert [item.id for item in customer_detail.visits[0].recordings] == [recording.id]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_staff_can_open_visit_detail_via_visit_order_participation_without_recordings() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    id="staff_participant",
                    name="顾问参与",
                    external_account="86000995",
                    hospital_code="6101",
                    permission_role="staff",
                )
                customer = Customer(id="cust_participant", name="参与客户")
                visit = Visit(
                    id="visit_participant",
                    customer_id=customer.id,
                    external_visit_order_no="DZ9001",
                    external_visit_order_seg="110",
                    status="consulting",
                )
                visit_order = VisitOrder(
                    id="vo_participant",
                    dzdh="DZ9001",
                    dzseg="110",
                    jgbm="6101",
                    advxc="86000995",
                    crtdt="2026-04-19",
                    sjrq="2026-04-19",
                )
                current_user = User(
                    username="86000995",
                    hashed_password="hashed",
                    display_name="顾问参与",
                    staff_id=staff.id,
                    role="staff",
                    hospital_code="6101",
                    is_active=True,
                )

                db.add_all([staff, customer, visit, visit_order, current_user])
                await db.commit()

                visit_detail = await get_visit_detail(visit.id, db=db, current_user=current_user)
                assert visit_detail.id == visit.id
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_customer_visit_metrics_only_count_visits_with_recordings() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(id="staff_a", name="顾问A", external_account="ADV001", permission_role="staff")
                customer = Customer(id="cust_a", name="客户A")
                visit_with_recording = Visit(
                    id="visit_old",
                    customer_id=customer.id,
                    consultant_id=staff.id,
                    status="consulting",
                    visit_date=date(2026, 4, 17),
                    visit_time="10:00:00",
                )
                visit_without_recording = Visit(
                    id="visit_new",
                    customer_id=customer.id,
                    consultant_id=staff.id,
                    status="consulting",
                    visit_date=date(2026, 4, 18),
                    visit_time="11:00:00",
                )
                recording = Recording(
                    id="rec_a",
                    visit_id=visit_with_recording.id,
                    staff_id=staff.id,
                    file_name="a.mp3",
                    file_path="recordings/a.mp3",
                    status="analyzed",
                )
                current_user = User(
                    username="admin",
                    hashed_password="hashed",
                    display_name="管理员",
                    role="super_admin",
                    staff_id=staff.id,
                    is_active=True,
                )

                db.add_all([
                    staff,
                    customer,
                    visit_with_recording,
                    visit_without_recording,
                    recording,
                    current_user,
                ])
                await db.flush()
                await sync_recording_visit_links(
                    db,
                    recording,
                    [visit_with_recording.id],
                    primary_visit_id=visit_with_recording.id,
                    source="test",
                )
                await db.commit()

                customers = await list_customers(
                    keyword="",
                    is_active=None,
                    consultant_id=None,
                    has_visits=None,
                    has_recordings=None,
                    has_positive_recharge=None,
                    date_from=None,
                    date_to=None,
                    page=1,
                    page_size=20,
                    db=db,
                    current_user=current_user,
                )
                assert customers.total == 1
                assert customers.items[0].visit_count == 1
                assert customers.items[0].recording_count == 1
                assert customers.items[0].last_visit_at.startswith("2026-04-17")

                customer_summary = await get_customer(customer.id, db=db, current_user=current_user)
                assert customer_summary.visit_count == 1
                assert customer_summary.last_visit_at.startswith("2026-04-17")

                customer_detail = await get_customer_detail(customer.id, db=db, current_user=current_user)
                assert customer_detail.visit_count == 1
                assert customer_detail.last_visit_at.startswith("2026-04-17")
                assert len(customer_detail.visits) == 1
                assert customer_detail.visits[0].id == visit_with_recording.id
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_hospital_admin_recordings_list_stays_within_management_scope() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff_a = Staff(
                    id="staff_a",
                    name="顾问A",
                    external_account="ADV001",
                    permission_role="staff",
                    hospital_code="6101",
                )
                staff_b = Staff(
                    id="staff_b",
                    name="顾问B",
                    external_account="ADV002",
                    permission_role="staff",
                    hospital_code="6101",
                )
                manager_staff = Staff(
                    id="manager_staff",
                    name="Manager",
                    external_account="M001",
                    permission_role="hospital_admin",
                    hospital_code="6101",
                )
                customer_a = Customer(id="cust_a", name="客户A")
                customer_b = Customer(id="cust_b", name="客户B")
                visit_a = Visit(id="visit_a", customer_id=customer_a.id, consultant_id=staff_a.id, status="consulting")
                visit_b = Visit(id="visit_b", customer_id=customer_b.id, consultant_id=staff_b.id, status="consulting")
                recording_a = Recording(
                    id="rec_a",
                    visit_id=visit_a.id,
                    staff_id=staff_a.id,
                    file_name="a.mp3",
                    file_path="recordings/a.mp3",
                    status="uploaded",
                )
                recording_b = Recording(
                    id="rec_b",
                    visit_id=visit_b.id,
                    staff_id=staff_b.id,
                    file_name="b.mp3",
                    file_path="recordings/b.mp3",
                    status="uploaded",
                )
                current_user = User(
                    username="admin6101",
                    hashed_password="hashed",
                    display_name="机构管理员",
                    role="hospital_admin",
                    staff_id=manager_staff.id,
                    hospital_code="6101",
                    is_active=True,
                )

                db.add_all([
                    staff_a,
                    staff_b,
                    manager_staff,
                    customer_a,
                    customer_b,
                    visit_a,
                    visit_b,
                    recording_a,
                    recording_b,
                    StaffManagementRelation(
                        hospital_code="6101",
                        manager_staff_id=manager_staff.id,
                        subordinate_staff_id=staff_a.id,
                    ),
                    current_user,
                ])
                await db.flush()
                await sync_recording_visit_links(db, recording_a, [visit_a.id], primary_visit_id=visit_a.id, source="test")
                await sync_recording_visit_links(db, recording_b, [visit_b.id], primary_visit_id=visit_b.id, source="test")
                await db.commit()

                recordings = await list_recordings(
                    visit_id=None,
                    staff_id=None,
                    status=None,
                    keyword=None,
                    customer_keyword=None,
                    badge_id=None,
                    role=None,
                    has_visit=None,
                    date_from=None,
                    date_to=None,
                    page=1,
                    page_size=20,
                    db=db,
                    current_user=current_user,
                )

                assert recordings.total == 1
                assert recordings.items[0].id == recording_a.id
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_staff_analysis_detail_only_allows_own_recording() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff_a = Staff(id="staff_a", name="顾问A", external_account="ADV001", permission_role="staff")
                staff_b = Staff(id="staff_b", name="顾问B", external_account="ADV002", permission_role="staff")
                customer_a = Customer(id="cust_a", name="客户A")
                customer_b = Customer(id="cust_b", name="客户B")
                visit_a = Visit(id="visit_a", customer_id=customer_a.id, consultant_id=staff_a.id, status="consulting")
                visit_b = Visit(id="visit_b", customer_id=customer_b.id, consultant_id=staff_b.id, status="consulting")
                recording_a = Recording(
                    id="recstaffa001",
                    visit_id=visit_a.id,
                    staff_id=staff_a.id,
                    file_name="a.mp3",
                    file_path="recordings/a.mp3",
                    status="analyzed",
                )
                recording_b = Recording(
                    id="recstaffb002",
                    visit_id=visit_b.id,
                    staff_id=staff_b.id,
                    file_name="b.mp3",
                    file_path="recordings/b.mp3",
                    status="analyzed",
                )
                current_user = User(
                    username="ADV001",
                    hashed_password="hashed",
                    display_name="顾问A",
                    staff_id=staff_a.id,
                    role="staff",
                    is_active=True,
                )

                db.add_all([
                    staff_a,
                    staff_b,
                    customer_a,
                    customer_b,
                    visit_a,
                    visit_b,
                    recording_a,
                    recording_b,
                    current_user,
                ])
                await db.flush()
                await sync_recording_visit_links(db, recording_a, [visit_a.id], primary_visit_id=visit_a.id, source="test")
                await sync_recording_visit_links(db, recording_b, [visit_b.id], primary_visit_id=visit_b.id, source="test")
                await db.commit()

                result_payload = {
                    "customer_primary_demands": {"summary": "主诉总结", "items": []},
                    "staff_recommendations": {"summary": "推荐总结", "items": []},
                    "standardized_indications": {"summary": "适应症总结", "items": []},
                    "customer_demands": {"focus_areas": [], "expectation": {"dialogue_type": "初诊咨询"}},
                    "customer_concerns": {"summary": "顾虑总结", "items": []},
                    "customer_profile": {"tags": []},
                    "consultation_evaluation": {"overall_score": 8.5, "overall_summary": "评价总结", "dimensions": []},
                }

                with TemporaryDirectory() as tmp_dir:
                    base_path = Path(tmp_dir)
                    results_dir = base_path / "results"
                    upload_dir = base_path / "uploads"
                    results_dir.mkdir(parents=True, exist_ok=True)
                    upload_dir.mkdir(parents=True, exist_ok=True)

                    (results_dir / "recording_recstaffa001.result.json").write_text(
                        json.dumps(result_payload, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    (results_dir / "recording_recstaffb002.result.json").write_text(
                        json.dumps(result_payload, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    (results_dir / "legacy_unscoped.result.json").write_text(
                        json.dumps(result_payload, ensure_ascii=False),
                        encoding="utf-8",
                    )

                    original_results_dir = analysis_routes._results_dir
                    original_raw_dir = analysis_routes._raw_dir
                    analysis_routes._results_dir = lambda: results_dir
                    analysis_routes._raw_dir = lambda: upload_dir
                    try:
                        result_list = await list_results(
                            sort_by="time",
                            sort_order="desc",
                            min_score=None,
                            max_score=None,
                            page=1,
                            page_size=20,
                            db=db,
                            current_user=current_user,
                        )
                        assert {item["file_id"] for item in result_list["items"]} == {"recording_recstaffa001"}

                        own = await get_result("recording_recstaffa001", db=db, current_user=current_user)
                        assert own["file_id"] == "recording_recstaffa001"
                        assert own["consultation_evaluation"]["overall_summary"] == "评价总结"

                        try:
                            await get_result("recording_recstaffb002", db=db, current_user=current_user)
                        except HTTPException as exc:
                            assert exc.status_code == 404
                        else:
                            raise AssertionError("Cross-staff analysis detail access should be rejected")

                        try:
                            await get_result("legacy_unscoped", db=db, current_user=current_user)
                        except HTTPException as exc:
                            assert exc.status_code == 404
                        else:
                            raise AssertionError("Unscoped analysis detail access should be rejected")
                    finally:
                        analysis_routes._results_dir = original_results_dir
                        analysis_routes._raw_dir = original_raw_dir
        finally:
            await engine.dispose()

    asyncio.run(scenario())
