from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.db.base import Base
from smart_badge_api.db.models import (
    AnalysisTask,
    Customer,
    Recording,
    RecordingVisitAnalysis,
    RecordingVisitLink,
    SapConsultationReview,
    SapPushLog,
    Visit,
)
from smart_badge_api.sap_push_scheduler import _find_auto_push_candidate_ids, _find_auto_push_candidate_refs


def test_auto_push_waits_until_analysis_result_is_done(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        monkeypatch.setattr("smart_badge_api.sap_push_scheduler._session_factory", session_factory)

        try:
            stable_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            async with session_factory() as db:
                customer = Customer(id="cust001", name="客户A")
                visit = Visit(
                    id="visit001",
                    customer_id=customer.id,
                    external_visit_order_no="DZ001",
                    external_visit_order_seg="110",
                )
                recording = Recording(
                    id="rec001",
                    visit_id=visit.id,
                    file_name="demo.mp3",
                    file_path="/tmp/demo.mp3",
                    status="analyzed",
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                link = RecordingVisitLink(
                    recording_id=recording.id,
                    visit_id=visit.id,
                    is_primary=True,
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                db.add_all([customer, visit, recording, link])
                await db.commit()

                assert await _find_auto_push_candidate_ids(10) == []

                db.add(
                    AnalysisTask(
                        id="task001",
                        file_name="recording_rec001.json",
                        file_path="/tmp/recording_rec001.json",
                        status="done",
                        result={"consultation_result": {}},
                        created_at=stable_at,
                        updated_at=stable_at,
                        completed_at=stable_at,
                    )
                )
                await db.commit()

                assert await _find_auto_push_candidate_ids(10) == ["rec001"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_auto_push_ignores_pending_or_empty_analysis(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        monkeypatch.setattr("smart_badge_api.sap_push_scheduler._session_factory", session_factory)

        try:
            stable_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            async with session_factory() as db:
                customer = Customer(id="cust001", name="客户A")
                visit = Visit(
                    id="visit001",
                    customer_id=customer.id,
                    external_visit_order_no="DZ001",
                    external_visit_order_seg="110",
                )
                recording = Recording(
                    id="rec001",
                    visit_id=visit.id,
                    file_name="demo.mp3",
                    file_path="/tmp/demo.mp3",
                    status="analyzed",
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                link = RecordingVisitLink(
                    recording_id=recording.id,
                    visit_id=visit.id,
                    is_primary=True,
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                pending_task = AnalysisTask(
                    id="task001",
                    file_name="recording_rec001.json",
                    file_path="/tmp/recording_rec001.json",
                    status="pending",
                    result={"consultation_result": {}},
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                empty_done_task = AnalysisTask(
                    id="task002",
                    file_name="recording_rec001.json",
                    file_path="/tmp/recording_rec001_retry.json",
                    status="done",
                    result=None,
                    created_at=stable_at,
                    updated_at=stable_at,
                    completed_at=stable_at,
                )
                db.add_all([customer, visit, recording, link, pending_task, empty_done_task])
                await db.commit()

                assert await _find_auto_push_candidate_ids(10) == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_auto_push_groups_multiple_recordings_for_same_visit(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        monkeypatch.setattr("smart_badge_api.sap_push_scheduler._session_factory", session_factory)

        try:
            stable_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            async with session_factory() as db:
                customer = Customer(id="cust001", name="客户A")
                visit = Visit(
                    id="visit001",
                    customer_id=customer.id,
                    external_visit_order_no="DZ001",
                    external_visit_order_seg="110",
                )
                recording_one = Recording(
                    id="rec001",
                    visit_id=visit.id,
                    file_name="consultant.mp3",
                    file_path="/tmp/consultant.mp3",
                    status="analyzed",
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                recording_two = Recording(
                    id="rec002",
                    visit_id=visit.id,
                    file_name="doctor.mp3",
                    file_path="/tmp/doctor.mp3",
                    status="analyzed",
                    created_at=stable_at + timedelta(minutes=1),
                    updated_at=stable_at,
                )
                link_one = RecordingVisitLink(
                    recording_id=recording_one.id,
                    visit_id=visit.id,
                    is_primary=True,
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                link_two = RecordingVisitLink(
                    recording_id=recording_two.id,
                    visit_id=visit.id,
                    is_primary=True,
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                task_one = AnalysisTask(
                    id="task001",
                    file_name="recording_rec001.json",
                    file_path="/tmp/recording_rec001.json",
                    status="done",
                    result={"consultation_result": {}},
                    created_at=stable_at,
                    updated_at=stable_at,
                    completed_at=stable_at,
                )
                task_two = AnalysisTask(
                    id="task002",
                    file_name="recording_rec002.json",
                    file_path="/tmp/recording_rec002.json",
                    status="done",
                    result={"consultation_result": {}},
                    created_at=stable_at,
                    updated_at=stable_at,
                    completed_at=stable_at,
                )
                db.add_all([customer, visit, recording_one, recording_two, link_one, link_two, task_one, task_two])
                await db.commit()

                assert await _find_auto_push_candidate_refs(10) == [("rec001", "visit001")]
                assert await _find_auto_push_candidate_ids(10) == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_auto_push_includes_multi_visit_recording_before_customer_mapping(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        monkeypatch.setattr("smart_badge_api.sap_push_scheduler._session_factory", session_factory)

        try:
            stable_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            async with session_factory() as db:
                customer_one = Customer(id="cust001", name="Customer One")
                customer_two = Customer(id="cust002", name="Customer Two")
                visit_one = Visit(
                    id="visit001",
                    customer_id=customer_one.id,
                    external_visit_order_no="DZ001",
                    external_visit_order_seg="110",
                )
                visit_two = Visit(
                    id="visit002",
                    customer_id=customer_two.id,
                    external_visit_order_no="DZ002",
                    external_visit_order_seg="110",
                )
                recording = Recording(
                    id="rec001",
                    visit_id=visit_one.id,
                    file_name="multi_customer.mp3",
                    file_path="/tmp/multi_customer.mp3",
                    status="analyzed",
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                link_one = RecordingVisitLink(
                    recording_id=recording.id,
                    visit_id=visit_one.id,
                    is_primary=True,
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                link_two = RecordingVisitLink(
                    recording_id=recording.id,
                    visit_id=visit_two.id,
                    is_primary=False,
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                task = AnalysisTask(
                    id="task001",
                    file_name="recording_rec001.json",
                    file_path="/tmp/recording_rec001.json",
                    status="done",
                    result={"consultation_result": {}},
                    created_at=stable_at,
                    updated_at=stable_at,
                    completed_at=stable_at,
                )
                db.add_all([customer_one, customer_two, visit_one, visit_two, recording, link_one, link_two, task])
                await db.commit()

                assert await _find_auto_push_candidate_refs(10) == [
                    ("rec001", "visit001"),
                    ("rec001", "visit002"),
                ]

                db.add(
                    RecordingVisitAnalysis(
                        id="rva001",
                        recording_id=recording.id,
                        visit_id=visit_one.id,
                        mapping_status="confirmed",
                        analysis_status="running",
                        created_at=stable_at,
                        updated_at=stable_at,
                    )
                )
                await db.commit()

                assert await _find_auto_push_candidate_refs(10) == [("rec001", "visit002")]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_auto_push_waits_for_all_recordings_on_same_visit(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        monkeypatch.setattr("smart_badge_api.sap_push_scheduler._session_factory", session_factory)

        try:
            stable_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            async with session_factory() as db:
                customer = Customer(id="cust001", name="客户A")
                visit = Visit(
                    id="visit001",
                    customer_id=customer.id,
                    external_visit_order_no="DZ001",
                    external_visit_order_seg="110",
                )
                recording_one = Recording(
                    id="rec001",
                    visit_id=visit.id,
                    file_name="consultant.mp3",
                    file_path="/tmp/consultant.mp3",
                    status="analyzed",
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                recording_two = Recording(
                    id="rec002",
                    visit_id=visit.id,
                    file_name="doctor.mp3",
                    file_path="/tmp/doctor.mp3",
                    status="transcribed",
                    created_at=stable_at + timedelta(minutes=1),
                    updated_at=stable_at,
                )
                link_one = RecordingVisitLink(
                    recording_id=recording_one.id,
                    visit_id=visit.id,
                    is_primary=True,
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                link_two = RecordingVisitLink(
                    recording_id=recording_two.id,
                    visit_id=visit.id,
                    is_primary=True,
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                task_one = AnalysisTask(
                    id="task001",
                    file_name="recording_rec001.json",
                    file_path="/tmp/recording_rec001.json",
                    status="done",
                    result={"consultation_result": {}},
                    created_at=stable_at,
                    updated_at=stable_at,
                    completed_at=stable_at,
                )
                db.add_all([customer, visit, recording_one, recording_two, link_one, link_two, task_one])
                await db.commit()

                assert await _find_auto_push_candidate_refs(10) == []

                recording_two.status = "analyzed"
                db.add(
                    AnalysisTask(
                        id="task002",
                        file_name="recording_rec002.json",
                        file_path="/tmp/recording_rec002.json",
                        status="done",
                        result={"consultation_result": {}},
                        created_at=stable_at,
                        updated_at=stable_at,
                        completed_at=stable_at,
                    )
                )
                await db.commit()

                assert await _find_auto_push_candidate_refs(10) == [("rec001", "visit001")]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_auto_push_retries_failed_log_after_delay(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        monkeypatch.setattr("smart_badge_api.sap_push_scheduler._session_factory", session_factory)

        try:
            now = datetime.now(timezone.utc)
            stable_at = now - timedelta(hours=1)
            async with session_factory() as db:
                customer = Customer(id="cust001", name="客户A")
                visit = Visit(
                    id="visit001",
                    customer_id=customer.id,
                    external_visit_order_no="DZ001",
                    external_visit_order_seg="110",
                )
                recording = Recording(
                    id="rec001",
                    visit_id=visit.id,
                    file_name="demo.mp3",
                    file_path="/tmp/demo.mp3",
                    status="analyzed",
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                link = RecordingVisitLink(
                    recording_id=recording.id,
                    visit_id=visit.id,
                    is_primary=True,
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                task = AnalysisTask(
                    id="task001",
                    file_name="recording_rec001.json",
                    file_path="/tmp/recording_rec001.json",
                    status="done",
                    result={"consultation_result": {}},
                    created_at=stable_at,
                    updated_at=stable_at,
                    completed_at=stable_at,
                )
                recent_failed_log = SapPushLog(
                    id="log001",
                    recording_id=recording.id,
                    visit_id=visit.id,
                    visit_order_no="DZ001",
                    visit_order_seg="110",
                    trigger_mode="auto_bind",
                    status="failed",
                    created_at=now - timedelta(minutes=5),
                    updated_at=now - timedelta(minutes=5),
                )
                db.add_all([customer, visit, recording, link, task, recent_failed_log])
                await db.commit()

                assert await _find_auto_push_candidate_ids(10) == []

                recent_failed_log.created_at = now - timedelta(minutes=40)
                recent_failed_log.updated_at = recent_failed_log.created_at
                await db.commit()

                assert await _find_auto_push_candidate_ids(10) == ["rec001"]

                db.add(
                    SapPushLog(
                        id="log002",
                        recording_id=recording.id,
                        visit_id=visit.id,
                        visit_order_no="DZ001",
                        visit_order_seg="110",
                        trigger_mode="auto_bind",
                        status="succeeded",
                        created_at=now - timedelta(minutes=35),
                        updated_at=now - timedelta(minutes=35),
                    )
                )
                await db.commit()

                assert await _find_auto_push_candidate_ids(10) == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_auto_push_picks_pending_review_updated_after_success(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        monkeypatch.setattr("smart_badge_api.sap_push_scheduler._session_factory", session_factory)

        try:
            now = datetime.now(timezone.utc)
            stable_at = now - timedelta(hours=1)
            review_changed_at = now - timedelta(minutes=10)
            async with session_factory() as db:
                customer = Customer(id="cust001", name="customer")
                visit = Visit(
                    id="visit001",
                    customer_id=customer.id,
                    external_visit_order_no="DZ001",
                    external_visit_order_seg="110",
                )
                recording = Recording(
                    id="rec001",
                    visit_id=visit.id,
                    file_name="demo.mp3",
                    file_path="/tmp/demo.mp3",
                    status="analyzed",
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                link = RecordingVisitLink(
                    recording_id=recording.id,
                    visit_id=visit.id,
                    is_primary=True,
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                task = AnalysisTask(
                    id="task001",
                    file_name="recording_rec001.json",
                    file_path="/tmp/recording_rec001.json",
                    status="done",
                    result={"consultation_result": {}},
                    created_at=stable_at,
                    updated_at=stable_at,
                    completed_at=stable_at,
                )
                old_success_log = SapPushLog(
                    id="log001",
                    recording_id=recording.id,
                    visit_id=visit.id,
                    visit_order_no="DZ001",
                    visit_order_seg="110",
                    trigger_mode="auto_bind",
                    status="succeeded",
                    created_at=now - timedelta(minutes=30),
                    updated_at=now - timedelta(minutes=30),
                    sent_at=now - timedelta(minutes=30),
                )
                review = SapConsultationReview(
                    id="review001",
                    visit_id=visit.id,
                    visit_order_no="DZ001",
                    visit_order_seg="110",
                    hospital_code="6501",
                    recording_ids=[recording.id],
                    blocks=[{"recording_id": recording.id}],
                    generated_text="new",
                    effective_text="new",
                    payload_snapshot=[{"text": "new"}],
                    status="pending",
                    created_at=review_changed_at,
                    updated_at=review_changed_at,
                )
                db.add_all([customer, visit, recording, link, task, old_success_log, review])
                await db.commit()

                assert await _find_auto_push_candidate_refs(10) == [("rec001", "visit001")]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_auto_push_skips_pending_review_when_newer_success_exists(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        monkeypatch.setattr("smart_badge_api.sap_push_scheduler._session_factory", session_factory)

        try:
            now = datetime.now(timezone.utc)
            stable_at = now - timedelta(hours=1)
            review_changed_at = now - timedelta(minutes=10)
            async with session_factory() as db:
                customer = Customer(id="cust001", name="customer")
                visit = Visit(
                    id="visit001",
                    customer_id=customer.id,
                    external_visit_order_no="DZ001",
                    external_visit_order_seg="110",
                )
                recording = Recording(
                    id="rec001",
                    visit_id=visit.id,
                    file_name="demo.mp3",
                    file_path="/tmp/demo.mp3",
                    status="analyzed",
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                link = RecordingVisitLink(
                    recording_id=recording.id,
                    visit_id=visit.id,
                    is_primary=True,
                    created_at=stable_at,
                    updated_at=stable_at,
                )
                task = AnalysisTask(
                    id="task001",
                    file_name="recording_rec001.json",
                    file_path="/tmp/recording_rec001.json",
                    status="done",
                    result={"consultation_result": {}},
                    created_at=stable_at,
                    updated_at=stable_at,
                    completed_at=stable_at,
                )
                review = SapConsultationReview(
                    id="review001",
                    visit_id=visit.id,
                    visit_order_no="DZ001",
                    visit_order_seg="110",
                    hospital_code="6501",
                    recording_ids=[recording.id],
                    blocks=[{"recording_id": recording.id}],
                    generated_text="new",
                    effective_text="new",
                    payload_snapshot=[{"text": "new"}],
                    status="pending",
                    created_at=review_changed_at,
                    updated_at=review_changed_at,
                )
                newer_success_log = SapPushLog(
                    id="log001",
                    recording_id=recording.id,
                    visit_id=visit.id,
                    visit_order_no="DZ001",
                    visit_order_seg="110",
                    trigger_mode="auto_bind",
                    status="succeeded",
                    created_at=now - timedelta(minutes=5),
                    updated_at=now - timedelta(minutes=5),
                    sent_at=now - timedelta(minutes=5),
                )
                db.add_all([customer, visit, recording, link, task, review, newer_success_log])
                await db.commit()

                assert await _find_auto_push_candidate_refs(10) == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())
