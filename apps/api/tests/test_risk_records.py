import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.risk_records import (
    get_risk_record,
    get_risk_record_overview,
    list_risk_records,
    update_risk_record_status,
)
from smart_badge_api.api.routes.risk_rules import list_risk_rules
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import AnalysisTask, Customer, Recording, Staff, Transcript, User, Visit
from smart_badge_api.risk.service import sync_risk_records_for_tasks
from smart_badge_api.schemas.risk import RiskRecordStatusUpdate


def test_list_risk_rules_seeds_defaults() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                rows = await list_risk_rules(db=db)
                names = {item.name for item in rows}
                assert "低分接诊预警" in names
                assert "流程执行不足" in names
                assert "价格顾虑未化解" in names
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_sync_risk_records_creates_contextual_records() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    id="staff001",
                    name="杜娟",
                    badge_id="BADGE-001",
                    role="consultant",
                    hospital_code="6101",
                )
                customer = Customer(id="cust001", name="周文婧", phone="13800000000")
                current_user = User(
                    username="hospital_admin",
                    hashed_password="hashed",
                    display_name="院管理员",
                    role="hospital_admin",
                    staff_id=staff.id,
                    hospital_code="6101",
                    is_active=True,
                )
                db.add_all([staff, customer, current_user])
                await db.flush()

                visit = Visit(id="visit001", customer_id=customer.id, consultant_id=staff.id, status="consulting")
                db.add(visit)
                await db.flush()

                recording = Recording(
                    id="rec001",
                    visit_id=visit.id,
                    staff_id=staff.id,
                    device_id="DEV-001",
                    file_name="recording.mp3",
                    file_path="uploads/recordings/recording.mp3",
                    status="analyzed",
                )
                db.add(recording)
                db.add(
                    Transcript(
                        recording_id=recording.id,
                        status="completed",
                        full_text="客户担心价格太贵，也担心恢复期太长。",
                        utterances=[
                            {"speaker": "customer", "text": "我担心价格太贵，预算有限。", "begin_ms": 0, "end_ms": 3000},
                            {"speaker": "customer", "text": "恢复期会不会很长？", "begin_ms": 3000, "end_ms": 5000},
                        ],
                    )
                )
                db.add(
                    AnalysisTask(
                        id="task001",
                        file_name="recording_rec001.json",
                        file_path="uploads/analysis_input/recording_rec001.json",
                        status="done",
                        overall_score=4.8,
                        result={
                            "customer_concerns": {
                                "items": [{"type": "价格", "content": "预算有限，担心太贵"}],
                            },
                            "consultation_evaluation": {
                                "overall_score": 4.8,
                                "dimensions": [
                                    {"name": "治疗流程规范", "score": 4.2, "comment": "未完整说明恢复期和风险"},
                                ],
                            },
                        },
                    )
                )
                await db.commit()

                created = await sync_risk_records_for_tasks(db, ["task001"])
                assert created == 3

                page = await list_risk_records(db=db, page=1, page_size=20, current_user=current_user)
                assert page.total == 3
                rule_names = {item.rule_name for item in page.items}
                assert "低分接诊预警" in rule_names
                assert "流程执行不足" in rule_names
                assert "价格顾虑未化解" in rule_names
                assert {item.staff_name for item in page.items} == {"杜娟"}
                assert {item.customer_name for item in page.items} == {"周文婧"}
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_get_risk_record_detail_and_status_update() -> None:
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
                        overall_score=4.5,
                        result={
                            "customer_concerns": {
                                "items": [{"type": "恢复期", "content": "担心恢复慢"}],
                            },
                            "consultation_evaluation": {
                                "overall_score": 4.5,
                                "dimensions": [],
                            },
                        },
                    )
                )
                await db.commit()

                await sync_risk_records_for_tasks(db, ["task_ext"])
                page = await list_risk_records(db=db, page=1, page_size=20, current_user=current_user)
                record = page.items[0]

                detail = await get_risk_record(record.id, db=db, current_user=current_user)
                assert detail.source_type == "uploaded_json"
                assert detail.task_id == "task_ext"
                assert detail.evidence is not None

                updated = await update_risk_record_status(
                    record.id,
                    RiskRecordStatusUpdate(status="resolved"),
                    db=db,
                    current_user=current_user,
                )
                assert updated.status == "resolved"

                overview = await get_risk_record_overview(db=db, current_user=current_user)
                assert overview.total >= 1
                assert overview.resolved_count >= 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())
