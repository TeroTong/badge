import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.customers import get_customer_merged_analysis
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import AnalysisTask, Customer, Recording, Staff, User, Visit


def test_customer_merged_analysis_aggregates_multi_visit_results() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(id="staff001", name="杜娟", badge_id="SSYX41013471", role="consultant")
                customer = Customer(id="cust001", name="周文婧", phone="13800000000")
                db.add_all([staff, customer])
                await db.flush()

                older_visit = Visit(id="visit001", customer_id=customer.id, consultant_id=staff.id, status="consulted")
                latest_visit = Visit(id="visit002", customer_id=customer.id, consultant_id=staff.id, status="consulting")
                db.add_all([older_visit, latest_visit])
                await db.flush()

                older_recording = Recording(
                    id="rec001",
                    visit_id=older_visit.id,
                    staff_id=staff.id,
                    file_name="older.mp3",
                    file_path="uploads/recordings/older.mp3",
                    status="analyzed",
                )
                latest_recording = Recording(
                    id="rec002",
                    visit_id=latest_visit.id,
                    staff_id=staff.id,
                    file_name="latest.mp3",
                    file_path="uploads/recordings/latest.mp3",
                    status="analyzed",
                )
                db.add_all([older_recording, latest_recording])

                older_time = datetime.now(timezone.utc) - timedelta(days=7)
                latest_time = datetime.now(timezone.utc) - timedelta(days=1)

                db.add_all(
                    [
                        User(
                            id="user001",
                            username="admin",
                            hashed_password="hashed",
                            display_name="管理员",
                            role="system_admin",
                            staff_id=staff.id,
                            is_active=True,
                        ),
                        AnalysisTask(
                            id="task001",
                            file_name="recording_rec001.json",
                            file_path="uploads/analysis_input/recording_rec001.json",
                            status="done",
                            overall_score=5.8,
                            completed_at=older_time,
                            result={
                                "customer_demands": {
                                    "focus_areas": [{"area": "祛斑", "surface_need": "想淡化面部色斑"}],
                                },
                                "customer_concerns": {
                                    "items": [{"type": "价格", "content": "预算有限，希望控制整体花费"}],
                                },
                                "customer_profile": {"tags": [{"category": "意向度", "value": "中"}]},
                                "consultation_evaluation": {
                                    "overall_score": 5.8,
                                    "dimensions": [
                                        {"name": "方案讲解", "score": 5.4, "comment": "方案解释不够具体"},
                                        {"name": "需求探寻", "score": 6.2, "comment": "有基础沟通"},
                                    ],
                                },
                            },
                        ),
                        AnalysisTask(
                            id="task002",
                            file_name="recording_rec002.json",
                            file_path="uploads/analysis_input/recording_rec002.json",
                            status="done",
                            overall_score=7.6,
                            completed_at=latest_time,
                            result={
                                "customer_demands": {
                                    "focus_areas": [{"area": "祛斑", "surface_need": "关注淡斑和肤色均匀"}],
                                },
                                "customer_concerns": {
                                    "items": [{"type": "价格", "content": "仍在对比价格和疗程预算"}],
                                },
                                "customer_profile": {"tags": [{"category": "意向度", "value": "高"}]},
                                "consultation_evaluation": {
                                    "overall_score": 7.6,
                                    "dimensions": [
                                        {"name": "方案讲解", "score": 7.1, "comment": "方案更清晰"},
                                        {"name": "需求探寻", "score": 7.8, "comment": "能承接客户问题"},
                                    ],
                                },
                            },
                        ),
                    ]
                )
                await db.commit()

                merged = await get_customer_merged_analysis(
                    customer.id,
                    db=db,
                    current_user=User(
                        username="admin",
                        hashed_password="hashed",
                        display_name="管理员",
                        role="system_admin",
                        staff_id=staff.id,
                        is_active=True,
                    ),
                )

                assert merged.customer_id == customer.id
                assert merged.total_visits == 2
                assert merged.total_recordings == 2
                assert merged.analyzed_recordings == 2
                assert merged.average_score == 6.7
                assert merged.latest_score == 7.6
                assert merged.score_trend == "improving"
                assert merged.timeline[0].task_id == "task002"
                assert merged.recurring_focus_areas[0].label == "祛斑"
                assert merged.recurring_focus_areas[0].count == 2
                assert merged.recurring_concerns[0].label == "价格"
                assert merged.recurring_concerns[0].count == 2
                assert merged.profile_tags == []
                assert merged.dimension_averages[0].name == "方案讲解"
                assert merged.dimension_averages[0].average_score == 6.25
                assert merged.merged_summary
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_customer_merged_analysis_returns_empty_payload_without_done_tasks() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(id="staff001", name="Admin", permission_role="system_admin")
                customer = Customer(id="cust001", name="空白客户")
                visit = Visit(id="visit001", customer_id=customer.id, consultant_id=staff.id)
                db.add_all([staff, customer, visit])
                await db.commit()

                merged = await get_customer_merged_analysis(
                    customer.id,
                    db=db,
                    current_user=User(
                        username="admin",
                        hashed_password="hashed",
                        display_name="管理员",
                        role="system_admin",
                        staff_id=staff.id,
                        is_active=True,
                    ),
                )

                assert merged.customer_id == customer.id
                assert merged.analyzed_recordings == 0
                assert merged.average_score is None
                assert merged.timeline == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())
