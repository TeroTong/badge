import asyncio
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.recordings import _refresh_customer_profile_scores_for_recording_links
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import AnalysisTask, Customer, Recording, RecordingVisitLink, Visit, VisitOrder
from smart_badge_api.schemas.recordings import RecordingOut, RecordingUpdate
from smart_badge_api.visit_linking import sync_recording_visit_links


def test_recording_update_validates_status() -> None:
    RecordingUpdate(status="uploaded")

    try:
        RecordingUpdate(status="unknown")
    except ValidationError:
        pass
    else:
        raise AssertionError("RecordingUpdate should reject invalid status")


def test_recording_out_supports_workspace_fields() -> None:
    payload = RecordingOut(
        id="rec001",
        visit_id="visit001",
        visit_status="consulting",
        staff_id="staff001",
        staff_name="杜娟",
        staff_badge_id="SSYX41013471",
        staff_role="consultant",
        customer_name="周文婧",
        customer_phone="13800000000",
        device_id="device-1",
        file_name="录音20260320192145",
        file_size=1024,
        duration_seconds=240,
        status="uploaded",
        has_transcript=False,
        created_at="2026-03-21T09:00:00+08:00",
    )

    assert payload.staff_badge_id == "SSYX41013471"
    assert payload.customer_name == "周文婧"
    assert payload.visit_status == "consulting"


def test_sync_recording_visit_links_keeps_primary_and_secondary_links() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                customer = Customer(id="cust001", name="周琴")
                primary_visit = Visit(id="visit001", customer_id=customer.id)
                secondary_visit = Visit(id="visit002", customer_id=customer.id)
                recording = Recording(
                    id="rec001",
                    file_name="audio_136.mp3",
                    file_path="uploads/recordings/audio_136.mp3",
                    status="uploaded",
                )
                db.add_all([customer, primary_visit, secondary_visit, recording])
                await db.flush()

                await sync_recording_visit_links(
                    db,
                    recording,
                    [primary_visit.id, secondary_visit.id],
                    primary_visit_id=primary_visit.id,
                    source="test",
                )
                await db.commit()
                await db.refresh(recording)
                assert recording.visit_id == primary_visit.id

                await sync_recording_visit_links(
                    db,
                    recording,
                    [secondary_visit.id],
                    primary_visit_id=secondary_visit.id,
                    source="test",
                )
                await db.commit()
                await db.refresh(recording)
                assert recording.visit_id == secondary_visit.id
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_sync_recording_visit_links_refreshes_profile_score_with_customer_history() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                customer = Customer(id="cust001", name="周琴")
                old_visit = Visit(id="visit001", customer_id=customer.id)
                current_visit = Visit(id="visit002", customer_id=customer.id)
                old_recording = Recording(
                    id="rec_hist_001",
                    file_name="audio_101.mp3",
                    file_path="uploads/recordings/audio_101.mp3",
                    status="analyzed",
                )
                current_recording = Recording(
                    id="rec_now_002",
                    file_name="audio_102.mp3",
                    file_path="uploads/recordings/audio_102.mp3",
                    status="analyzed",
                )
                old_task = AnalysisTask(
                    id="task_hist01",
                    file_name="recording_rec_hist_001.json",
                    file_path="uploads/analysis_input/recording_rec_hist_001.json",
                    status="done",
                    overall_score=3.6,
                    result={
                        "customer_profile": {
                            "tags": [
                                {"category": "出生日期", "value": "30岁", "weight_level": 1},
                                {"category": "健康风险/禁忌", "value": "疤痕体质", "weight_level": 1},
                                {"category": "创伤倾向", "value": "微创", "weight_level": 1},
                                {"category": "效果要求", "value": "即刻", "weight_level": 1},
                                {"category": "价格敏感度", "value": "高", "weight_level": 2},
                            ]
                        },
                        "consultation_evaluation": {"dimensions": []},
                    },
                )
                current_task = AnalysisTask(
                    id="task_curr01",
                    file_name="recording_rec_now_002.json",
                    file_path="uploads/analysis_input/recording_rec_now_002.json",
                    status="done",
                    overall_score=2.1,
                    result={
                        "customer_profile": {
                            "tags": [
                                {"category": "出生日期", "value": "30岁", "weight_level": 1},
                                {"category": "疼痛耐受度", "value": "低", "weight_level": 1},
                                {"category": "常驻城市", "value": "外地", "weight_level": 2},
                            ]
                        },
                        "consultation_evaluation": {"dimensions": []},
                    },
                )
                db.add_all([
                    customer,
                    old_visit,
                    current_visit,
                    old_recording,
                    current_recording,
                    old_task,
                    current_task,
                ])
                await db.flush()

                await sync_recording_visit_links(
                    db,
                    old_recording,
                    [old_visit.id],
                    primary_visit_id=old_visit.id,
                    source="test",
                )
                await sync_recording_visit_links(
                    db,
                    current_recording,
                    [current_visit.id],
                    primary_visit_id=current_visit.id,
                    source="test",
                )
                await db.commit()
                await db.refresh(current_task)

                evaluation = (current_task.result or {}).get("consultation_evaluation", {})
                current_tags = (current_task.result or {}).get("customer_profile", {}).get("tags", [])
                profile_dimension = next(
                    item
                    for item in evaluation.get("dimensions", [])
                    if item.get("name") == "顾客标签获取"
                )
                assert profile_dimension["point_score"] == 0.43
                assert current_task.overall_score == (current_task.result or {}).get(
                    "consultation_process_evaluation", {}
                ).get("overall_score")
                assert "当前已获取 6/14 个必问/重要标签" in profile_dimension["summary"]
                assert {
                    (item.get("category"), item.get("value"), item.get("source"))
                    for item in current_tags
                    if isinstance(item, dict)
                } >= {
                    ("健康风险/禁忌", "疤痕体质", "customer_history_sync"),
                    ("创伤倾向", "微创", "customer_history_sync"),
                    ("效果要求", "即刻", "customer_history_sync"),
                    ("价格敏感度", "高", "customer_history_sync"),
                }
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_sync_recording_visit_links_backfills_birthdate_from_customer_archive() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                customer = Customer(id="cust001", name="周琴", age=25)
                visit = Visit(id="visit001", customer_id=customer.id, external_visit_order_no="VO001")
                visit_order = VisitOrder(id="order001", dzdh="VO001", customer_birthday="1999-05-01")
                recording = Recording(
                    id="rec001",
                    file_name="audio_201.mp3",
                    file_path="uploads/recordings/audio_201.mp3",
                    status="analyzed",
                )
                task = AnalysisTask(
                    id="task001",
                    file_name="recording_rec001.json",
                    file_path="uploads/analysis_input/recording_rec001.json",
                    status="done",
                    overall_score=0.0,
                    result={
                        "customer_profile": {
                            "tags": [
                                {"category": "出生日期", "value": "20岁", "weight_level": 1},
                            ]
                        },
                        "consultation_evaluation": {"dimensions": []},
                    },
                )
                db.add_all([customer, visit, visit_order, recording, task])
                await db.flush()

                await sync_recording_visit_links(
                    db,
                    recording,
                    [visit.id],
                    primary_visit_id=visit.id,
                    source="test",
                )
                await db.commit()
                await db.refresh(task)

                current_tags = (task.result or {}).get("customer_profile", {}).get("tags", [])
                evaluation = (task.result or {}).get("consultation_evaluation", {})
                profile_dimension = next(
                    item
                    for item in evaluation.get("dimensions", [])
                    if item.get("name") == "顾客标签获取"
                )

                assert {
                    (item.get("category"), item.get("value"), item.get("source"))
                    for item in current_tags
                    if isinstance(item, dict)
                } >= {("出生日期", "1999-05-01", "customer_archive_sync")}
                assert not any(
                    isinstance(item, dict)
                    and item.get("category") == "出生日期"
                    and item.get("value") in {"20岁", "25岁"}
                    for item in current_tags
                )
                assert profile_dimension["point_score"] > 0
                assert task.overall_score is not None
                assert task.overall_score > 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_sync_recording_visit_links_does_not_backfill_birthdate_from_age_only() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                customer = Customer(id="cust001", name="周琴", age=25)
                visit = Visit(id="visit001", customer_id=customer.id)
                recording = Recording(
                    id="rec001",
                    file_name="audio_201.mp3",
                    file_path="uploads/recordings/audio_201.mp3",
                    status="analyzed",
                )
                task = AnalysisTask(
                    id="task001",
                    file_name="recording_rec001.json",
                    file_path="uploads/analysis_input/recording_rec001.json",
                    status="done",
                    overall_score=0.0,
                    result={
                        "customer_profile": {"tags": []},
                        "consultation_evaluation": {"dimensions": []},
                    },
                )
                db.add_all([customer, visit, recording, task])
                await db.flush()

                await sync_recording_visit_links(
                    db,
                    recording,
                    [visit.id],
                    primary_visit_id=visit.id,
                    source="test",
                )
                await db.commit()
                await db.refresh(task)

                current_tags = (task.result or {}).get("customer_profile", {}).get("tags", [])
                assert not any(
                    isinstance(item, dict)
                    and item.get("category") == "出生日期"
                    and item.get("source") == "customer_archive_sync"
                    for item in current_tags
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_sync_recording_visit_links_uses_latest_history_value_for_same_category() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                now = datetime.now(timezone.utc)
                customer = Customer(id="cust001", name="周琴")
                target_visit = Visit(id="visit001", customer_id=customer.id)
                old_visit = Visit(id="visit002", customer_id=customer.id)
                new_visit = Visit(id="visit003", customer_id=customer.id)
                target_recording = Recording(
                    id="rec_target",
                    file_name="audio_301.mp3",
                    file_path="uploads/recordings/audio_301.mp3",
                    status="analyzed",
                )
                old_recording = Recording(
                    id="rec_old",
                    file_name="audio_302.mp3",
                    file_path="uploads/recordings/audio_302.mp3",
                    status="analyzed",
                )
                new_recording = Recording(
                    id="rec_new",
                    file_name="audio_303.mp3",
                    file_path="uploads/recordings/audio_303.mp3",
                    status="analyzed",
                )
                target_task = AnalysisTask(
                    id="task_target",
                    file_name="recording_rec_target.json",
                    file_path="uploads/analysis_input/recording_rec_target.json",
                    status="done",
                    overall_score=0.0,
                    created_at=now,
                    completed_at=now,
                    result={
                        "customer_profile": {"tags": []},
                        "consultation_evaluation": {"dimensions": []},
                    },
                )
                old_task = AnalysisTask(
                    id="task_old",
                    file_name="recording_rec_old.json",
                    file_path="uploads/analysis_input/recording_rec_old.json",
                    status="done",
                    overall_score=0.0,
                    created_at=now - timedelta(days=2),
                    completed_at=now - timedelta(days=2),
                    result={
                        "customer_profile": {
                            "tags": [
                                {"category": "价格敏感度", "value": "高", "weight_level": 2},
                            ]
                        },
                        "consultation_evaluation": {"dimensions": []},
                    },
                )
                new_task = AnalysisTask(
                    id="task_new",
                    file_name="recording_rec_new.json",
                    file_path="uploads/analysis_input/recording_rec_new.json",
                    status="done",
                    overall_score=0.0,
                    created_at=now - timedelta(days=1),
                    completed_at=now - timedelta(days=1),
                    result={
                        "customer_profile": {
                            "tags": [
                                {"category": "价格敏感度", "value": "低", "weight_level": 2},
                            ]
                        },
                        "consultation_evaluation": {"dimensions": []},
                    },
                )
                db.add_all([
                    customer,
                    target_visit,
                    old_visit,
                    new_visit,
                    target_recording,
                    old_recording,
                    new_recording,
                    target_task,
                    old_task,
                    new_task,
                ])
                await db.flush()

                await sync_recording_visit_links(
                    db,
                    old_recording,
                    [old_visit.id],
                    primary_visit_id=old_visit.id,
                    source="test",
                )
                await sync_recording_visit_links(
                    db,
                    new_recording,
                    [new_visit.id],
                    primary_visit_id=new_visit.id,
                    source="test",
                )
                await sync_recording_visit_links(
                    db,
                    target_recording,
                    [target_visit.id],
                    primary_visit_id=target_visit.id,
                    source="test",
                )
                await db.commit()
                await db.refresh(target_task)

                current_tags = (target_task.result or {}).get("customer_profile", {}).get("tags", [])
                history_sensitive_tags = [
                    item for item in current_tags if isinstance(item, dict) and item.get("category") == "价格敏感度"
                ]

                assert history_sensitive_tags == [
                    {
                        "category": "价格敏感度",
                        "value": "低",
                        "weight_level": 2,
                        "evidence": "已从客户历史标签同步",
                        "source": "customer_history_sync",
                    }
                ]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_refresh_customer_profile_scores_for_recording_links_backfills_customer_birthdate() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                customer = Customer(id="cust001", name="周琴", age=23)
                visit = Visit(id="visit001", customer_id=customer.id, external_visit_order_no="VO002")
                visit_order = VisitOrder(id="order002", dzdh="VO002", customer_birthday="2001-09-02")
                recording = Recording(
                    id="rec001",
                    file_name="0404_142136.mp3",
                    file_path="uploads/recordings/0404_142136.mp3",
                    status="analyzed",
                )
                task = AnalysisTask(
                    id="task001",
                    file_name="recording_rec001.json",
                    file_path="uploads/analysis_input/recording_rec001.json",
                    status="done",
                    overall_score=0.0,
                    result={
                        "customer_profile": {
                            "tags": [
                                {"category": "常驻城市", "value": "未提及", "weight_level": 1},
                            ]
                        },
                        "consultation_evaluation": {"dimensions": []},
                    },
                )
                db.add_all([
                    customer,
                    visit,
                    visit_order,
                    recording,
                    task,
                    RecordingVisitLink(recording_id=recording.id, visit_id=visit.id, is_primary=True),
                ])
                await db.flush()
                recording.visit_id = visit.id
                await db.commit()

                await _refresh_customer_profile_scores_for_recording_links(db, recording.id)
                await db.commit()
                await db.refresh(task)

                current_tags = (task.result or {}).get("customer_profile", {}).get("tags", [])
                assert {
                    (item.get("category"), item.get("value"), item.get("source"))
                    for item in current_tags
                    if isinstance(item, dict)
                } >= {("出生日期", "2001-09-02", "customer_archive_sync")}
        finally:
            await engine.dispose()

    asyncio.run(scenario())
