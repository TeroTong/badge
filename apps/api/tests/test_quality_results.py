import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.quality_results import get_quality_result, list_quality_results
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import AnalysisTask, Customer, Recording, Staff, User, Visit


def test_list_quality_results_includes_recording_staff_and_customer_context() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(name="杜娟", badge_id="SSYX41013471", role="consultant")
                customer = Customer(name="周文婧", phone="13800000000")
                current_user = User(
                    username="SSYX41013471",
                    hashed_password="hashed",
                    display_name="杜娟",
                    role="staff",
                    is_active=True,
                )
                db.add_all([staff, customer, current_user])
                await db.flush()
                current_user.staff_id = staff.id

                visit = Visit(customer_id=customer.id, consultant_id=staff.id, status="consulting")
                db.add(visit)
                await db.flush()

                recording = Recording(
                    id="rec001",
                    visit_id=visit.id,
                    staff_id=staff.id,
                    device_id="DEV-001",
                    file_name="录音20260321.mp3",
                    file_path="uploads/recordings/rec001.mp3",
                    status="analyzed",
                )
                db.add(recording)
                db.add(
                    AnalysisTask(
                        id="task001",
                        file_name="recording_rec001.json",
                        file_path="uploads/analysis_input/recording_rec001.json",
                        status="done",
                        overall_score=7.6,
                        result={
                            "customer_demands": {
                                "focus_areas": [{"area": "祛斑"}],
                                "expectation": {"dialogue_type": "到院咨询"},
                            },
                            "customer_concerns": {"items": [{"type": "价格", "content": "预算有限"}]},
                            "customer_profile": {"tags": [{"category": "价值分", "value": "B"}]},
                            "consultation_evaluation": {
                                "overall_score": 7.6,
                                "dimensions": [{"name": "客户洞察", "score": 7.8, "comment": "良好"}],
                            },
                        },
                    )
                )
                await db.commit()

                page = await list_quality_results(db=db, page=1, page_size=20, current_user=current_user)

                assert page.total == 1
                row = page.items[0]
                assert row.recording_id == "rec001"
                assert row.staff_name == "杜娟"
                assert row.customer_name == "周文婧"
                assert row.quality_label == "良好"
                assert row.focus_areas == ["祛斑"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_list_quality_results_can_filter_by_staff_id() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff_a = Staff(id="staff_a", name="顾问A", role="consultant")
                staff_b = Staff(id="staff_b", name="顾问B", role="consultant")
                customer = Customer(name="客户A")
                current_user = User(
                    username="ADV001",
                    hashed_password="hashed",
                    display_name="顾问A",
                    role="staff",
                    is_active=True,
                )
                db.add_all([staff_a, staff_b, customer, current_user])
                await db.flush()
                current_user.staff_id = staff_a.id

                visit = Visit(customer_id=customer.id, consultant_id=staff_a.id)
                db.add(visit)
                await db.flush()

                db.add_all(
                    [
                        Recording(
                            id="rec_a",
                            visit_id=visit.id,
                            staff_id=staff_a.id,
                            file_name="a.mp3",
                            file_path="uploads/recordings/a.mp3",
                            status="analyzed",
                        ),
                        Recording(
                            id="rec_b",
                            visit_id=visit.id,
                            staff_id=staff_b.id,
                            file_name="b.mp3",
                            file_path="uploads/recordings/b.mp3",
                            status="analyzed",
                        ),
                        AnalysisTask(
                            id="task_a",
                            file_name="recording_rec_a.json",
                            file_path="uploads/analysis_input/recording_rec_a.json",
                            status="done",
                            overall_score=8.1,
                            result={"consultation_evaluation": {"overall_score": 8.1, "dimensions": []}},
                        ),
                        AnalysisTask(
                            id="task_b",
                            file_name="recording_rec_b.json",
                            file_path="uploads/analysis_input/recording_rec_b.json",
                            status="done",
                            overall_score=5.2,
                            result={"consultation_evaluation": {"overall_score": 5.2, "dimensions": []}},
                        ),
                    ]
                )
                await db.commit()

                page = await list_quality_results(db=db, staff_id="staff_a", page=1, page_size=20, current_user=current_user)

                assert page.total == 1
                assert page.items[0].staff_id == "staff_a"
                assert page.items[0].id == "task_a"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_get_quality_result_hides_uploaded_json_tasks_without_recording() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                current_user = User(
                    username="sysadmin",
                    hashed_password="hashed",
                    display_name="系统管理员",
                    role="system_admin",
                    is_active=True,
                )
                db.add(current_user)
                db.add(
                    AnalysisTask(
                        id="task_ext",
                        file_name="external_payload.json",
                        file_path="uploads/external_payload.json",
                        status="done",
                        overall_score=4.8,
                        result={
                            "customer_demands": {"focus_areas": [{"area": "隆鼻"}]},
                            "customer_concerns": {"items": [{"type": "恢复期", "content": "担心恢复慢"}]},
                            "customer_profile": {"tags": [{"category": "意向度", "value": "中"}]},
                            "consultation_evaluation": {
                                "overall_score": 4.8,
                                "dimensions": [{"name": "方案讲解", "score": 4.8, "comment": "待提升"}],
                            },
                        },
                    )
                )
                await db.commit()

                with pytest.raises(HTTPException) as exc_info:
                    await get_quality_result("task_ext", db=db, current_user=current_user)
                assert exc_info.value.status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(scenario())
