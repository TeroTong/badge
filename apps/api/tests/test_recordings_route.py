from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.recordings import (
    _split_part_file_name,
    RecordingVisitOrderLocalVisitRequest,
    ensure_recording_visit_order_local_visit,
    get_recording_visit_order_match,
    split_recording,
    update_recording,
)
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import Customer, Recording, RecordingVisitLink, Staff, Transcript, User, Visit, VisitOrder
from smart_badge_api.schemas.matching import RecordingVisitOrderMatchOut, VisitOrderMatchCandidateOut
from smart_badge_api.schemas.recordings import RecordingSplitRequest, RecordingUpdate

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_split_part_file_name_uses_segment_start_time() -> None:
    recording = Recording(
        id="rec_name",
        file_name="0506_153320.mp3",
        file_path="/tmp/0506_153320.mp3",
        created_at=datetime(2026, 5, 6, 15, 33, 20, tzinfo=timezone.utc),
    )

    assert _split_part_file_name(recording, 600_000, 1) == "0506_153320_153320.mp3"
    assert _split_part_file_name(recording, 600_000, 2) == "0506_153320_154320.mp3"


def test_split_part_file_name_uses_created_at_for_existing_split_child() -> None:
    recording = Recording(
        id="rec_old_child",
        file_name="0506_153320_part2.mp3",
        file_path="/tmp/0506_153320_part2.mp3",
        split_parent_recording_id="rec_parent",
        created_at=datetime(2026, 5, 6, 15, 43, 20, tzinfo=TZ_SHANGHAI),
    )

    assert _split_part_file_name(recording, 300_000, 1) == "0506_153320_154320.mp3"
    assert _split_part_file_name(recording, 300_000, 2) == "0506_153320_154820.mp3"


def test_split_part_file_name_uses_real_archive_path_when_file_name_is_technical() -> None:
    recording = Recording(
        id="rec_archive",
        file_name="dingtalk_SSYX51049748_ae086fba-cbfb-4ff4-8bc6-b5292fa5a4c7.mp3",
        file_path="/opt/badge/apps/api/uploads/dingtalk_staging/archive/SSYX51049748/202605/0506_155926.mp3",
        created_at=datetime(2026, 5, 6, 7, 59, 26, tzinfo=timezone.utc),
    )

    assert _split_part_file_name(recording, 442_000, 1) == "0506_155926_155926.mp3"
    assert _split_part_file_name(recording, 442_000, 2) == "0506_155926_160648.mp3"


def test_split_part_file_name_uses_existing_split_segment_start_time() -> None:
    recording = Recording(
        id="rec_split_child",
        file_name="0506_153320_154320.mp3",
        file_path="/tmp/0506_153320_154320.mp3",
        split_parent_recording_id="rec_parent",
        created_at=datetime(2026, 5, 6, 15, 43, 20, tzinfo=timezone.utc),
    )

    assert _split_part_file_name(recording, 300_000, 1) == "0506_153320_154320.mp3"
    assert _split_part_file_name(recording, 300_000, 2) == "0506_153320_154820.mp3"


def test_recording_visit_order_match_hides_inaccessible_local_visit_ids_for_staff(monkeypatch) -> None:
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
                customer = Customer(
                    id="cust_1",
                    name="测试客户",
                    external_customer_code="K1001",
                )
                accessible_visit = Visit(
                    id="visit_accessible",
                    customer_id=customer.id,
                    consultant_id=staff.id,
                    external_visit_order_no="DZ1001",
                    external_visit_order_seg="110",
                )
                inaccessible_visit = Visit(
                    id="visit_inaccessible",
                    customer_id=customer.id,
                    consultant_id="other_staff",
                    external_visit_order_no="DZ1002",
                    external_visit_order_seg="110",
                )
                recording = Recording(
                    id="rec_1",
                    staff_id=staff.id,
                    visit_id=accessible_visit.id,
                    file_name="20260420_101500.mp3",
                    file_path="/tmp/20260420_101500.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 4, 20, 10, 15, tzinfo=timezone.utc),
                )

                db.add_all([staff, user, customer, accessible_visit, inaccessible_visit, recording])
                await db.commit()

                async def fake_analyze(*args, **kwargs):
                    return RecordingVisitOrderMatchOut(
                        recording_id=recording.id,
                        file_name=recording.file_name,
                        record_date="2026-04-20",
                        linked_visit_id=accessible_visit.id,
                        linked_visit_ids=[accessible_visit.id, inaccessible_visit.id],
                        linked_visit_order_refs=["DZ1001-110", "DZ1002-110"],
                        summary="ok",
                        analyzed_at="2026-04-20T10:20:00+00:00",
                        candidates=[
                            VisitOrderMatchCandidateOut(
                                visit_order_id="vo_accessible",
                                local_visit_id=accessible_visit.id,
                                associated_local_visit_ids=[inaccessible_visit.id],
                                dzdh="DZ1001",
                                dzseg="110",
                                confidence=0.91,
                                decision="recommend",
                                method="heuristic",
                                reasons=["test"],
                                evidence=[],
                            ),
                            VisitOrderMatchCandidateOut(
                                visit_order_id="vo_inaccessible",
                                local_visit_id=inaccessible_visit.id,
                                associated_local_visit_ids=[],
                                dzdh="DZ1002",
                                dzseg="110",
                                confidence=0.52,
                                decision="recommend",
                                method="heuristic",
                                reasons=["test"],
                                evidence=[],
                            ),
                        ],
                    )

                monkeypatch.setattr(
                    "smart_badge_api.api.routes.recordings.analyze_recording_visit_order_match",
                    fake_analyze,
                )

                result = await get_recording_visit_order_match(
                    recording_id=recording.id,
                    apply_auto=False,
                    use_llm=False,
                    db=db,
                    current_user=user,
                )

                assert result.linked_visit_id == "visit_accessible"
                assert result.linked_visit_ids == ["visit_accessible"]
                assert result.candidates[0].local_visit_id == "visit_accessible"
                assert result.candidates[0].associated_local_visit_ids == []
                assert result.candidates[1].local_visit_id is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_split_recording_creates_two_parts_and_hides_original(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
        source_dir = settings.upload_path / "recordings"
        source_dir.mkdir(parents=True, exist_ok=True)
        source_path = source_dir / "source.mp3"
        source_path.write_bytes(b"original audio")

        def fake_split_audio_file(source_path_arg, first_output_path, second_output_path, *, split_at_ms):
            assert source_path_arg == source_path
            assert split_at_ms == 120_000
            first_output_path.parent.mkdir(parents=True, exist_ok=True)
            second_output_path.parent.mkdir(parents=True, exist_ok=True)
            first_output_path.write_bytes(b"first")
            second_output_path.write_bytes(b"second")

        monkeypatch.setattr(
            "smart_badge_api.api.routes.recordings.split_audio_file",
            fake_split_audio_file,
        )

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    id="staff_split",
                    name="测试咨询师",
                    hospital_code="6101",
                    role="consultant",
                    permission_role="staff",
                    badge_id="B001",
                )
                user = User(
                    username="staff_split",
                    hashed_password="x",
                    role="staff",
                    staff_id=staff.id,
                    hospital_code="6101",
                    is_active=True,
                )
                customer = Customer(id="cust_split", name="测试客户")
                visit = Visit(id="visit_split", customer_id=customer.id, consultant_id=staff.id)
                recording = Recording(
                    id="rec_split",
                    staff_id=staff.id,
                    visit_id=visit.id,
                    file_name="0506_153320.mp3",
                    file_path=settings.make_relative_path(source_path),
                    file_size=source_path.stat().st_size,
                    duration_seconds=240,
                    status="analyzed",
                    created_at=datetime(2026, 5, 6, 15, 33, 20, tzinfo=timezone.utc),
                )
                transcript = Transcript(
                    recording_id=recording.id,
                    asr_provider="manual",
                    status="completed",
                    full_text="第一位客户\n第二位客户",
                    utterances=[
                        {"speaker": "consultant", "text": "第一位客户", "begin_ms": 10_000, "end_ms": 20_000},
                        {"speaker": "customer", "text": "第二位客户", "begin_ms": 130_000, "end_ms": 140_000},
                    ],
                    duration_ms=240_000,
                    completed_at=datetime(2026, 5, 6, 15, 40, tzinfo=timezone.utc),
                )
                link = RecordingVisitLink(recording_id=recording.id, visit_id=visit.id, is_primary=True)
                db.add_all([staff, user, customer, visit, recording, transcript, link])
                await db.commit()

                result = await split_recording(
                    recording.id,
                    RecordingSplitRequest(split_at_seconds=120, confirm=True),
                    db=db,
                    current_user=user,
                )

                assert result.original_recording_id == "rec_split"
                assert result.split_at_ms == 120_000
                assert [part.part_index for part in result.parts] == [1, 2]
                assert all(part.archive_item_id for part in result.parts)

                original = await db.get(Recording, "rec_split")
                assert original is not None
                assert original.status == "filtered"
                assert original.visit_id is None
                remaining_links = (
                    await db.execute(select(RecordingVisitLink).where(RecordingVisitLink.recording_id == "rec_split"))
                ).scalars().all()
                assert remaining_links == []

                children = (
                    await db.execute(
                        select(Recording)
                        .where(Recording.split_parent_recording_id == "rec_split")
                        .order_by(Recording.split_part_index.asc())
                    )
                ).scalars().all()
                assert len(children) == 2
                assert [child.file_name for child in children] == [
                    "0506_153320_153320.mp3",
                    "0506_153320_153520.mp3",
                ]
                assert [child.status for child in children] == ["transcribed", "transcribed"]
                assert [child.duration_seconds for child in children] == [120, 120]

                child_transcripts = (
                    await db.execute(
                        select(Transcript)
                        .where(Transcript.recording_id.in_([child.id for child in children]))
                        .order_by(Transcript.recording_id.asc())
                    )
                ).scalars().all()
                assert len(child_transcripts) == 2
                utterance_texts = [item.utterances[0]["text"] for item in child_transcripts]
                assert sorted(utterance_texts) == ["第一位客户", "第二位客户"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_split_recording_rejects_non_owner_staff(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
        source_dir = settings.upload_path / "recordings"
        source_dir.mkdir(parents=True, exist_ok=True)
        source_path = source_dir / "source.mp3"
        source_path.write_bytes(b"original audio")

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                owner = Staff(id="owner", name="上传者", hospital_code="6101")
                other = Staff(id="other", name="其他员工", hospital_code="6101")
                user = User(
                    username="other",
                    hashed_password="x",
                    role="staff",
                    staff_id=other.id,
                    hospital_code="6101",
                    is_active=True,
                )
                recording = Recording(
                    id="rec_forbidden",
                    staff_id=owner.id,
                    file_name="source.mp3",
                    file_path=settings.make_relative_path(source_path),
                    duration_seconds=100,
                    status="transcribed",
                )
                db.add_all([owner, other, user, recording])
                await db.commit()

                with pytest.raises(HTTPException) as exc_info:
                    await split_recording(
                        recording.id,
                        RecordingSplitRequest(split_at_seconds=30, confirm=True),
                        db=db,
                        current_user=user,
                    )
                assert exc_info.value.status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_update_recording_visit_id_preserves_existing_secondary_links() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                user = User(
                    username="admin",
                    hashed_password="x",
                    role="system_admin",
                    staff_id="staff_admin",
                    is_active=True,
                )
                staff = Staff(id="staff_admin", name="Admin", permission_role="system_admin")
                customer = Customer(id="cust_multi", name="同行客户")
                old_visit = Visit(
                    id="visit_old",
                    customer_id=customer.id,
                    consultant_id=staff.id,
                    external_visit_order_no="DZOLD",
                )
                new_visit = Visit(
                    id="visit_new",
                    customer_id=customer.id,
                    consultant_id=staff.id,
                    external_visit_order_no="DZNEW",
                )
                recording = Recording(
                    id="rec_multi",
                    visit_id=old_visit.id,
                    staff_id=staff.id,
                    file_name="multi-link.mp3",
                    file_path="/tmp/multi-link.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 4, 30, 9, 0, tzinfo=timezone.utc),
                )
                db.add_all(
                    [
                        user,
                        staff,
                        customer,
                        old_visit,
                        new_visit,
                        recording,
                        RecordingVisitLink(recording_id=recording.id, visit_id=old_visit.id, is_primary=True),
                    ]
                )
                await db.commit()

                result = await update_recording(
                    recording_id=recording.id,
                    body=RecordingUpdate(visit_id=new_visit.id),
                    db=db,
                    user=user,
                )

                assert result.visit_id == new_visit.id
                assert result.linked_visit_ids == [new_visit.id, old_visit.id]

                links = (
                    await db.execute(
                        select(RecordingVisitLink).where(RecordingVisitLink.recording_id == recording.id)
                    )
                ).scalars().all()
                assert {link.visit_id for link in links} == {old_visit.id, new_visit.id}
                assert [link.visit_id for link in links if link.is_primary] == [new_visit.id]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_update_recording_allows_same_institution_day_visit_from_org_daily_orders() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    id="staff_owner",
                    name="录音咨询师",
                    external_account="A001",
                    hospital_code="6101",
                    permission_role="staff",
                )
                other_staff = Staff(
                    id="staff_other",
                    name="其他咨询师",
                    external_account="B001",
                    hospital_code="6101",
                    permission_role="staff",
                )
                user = User(
                    username="A001",
                    hashed_password="x",
                    role="staff",
                    staff_id=staff.id,
                    hospital_code="6101",
                    is_active=True,
                )
                customer = Customer(id="cust_org_day", name="机构当天客户")
                visit = Visit(
                    id="visit_org_day",
                    customer_id=customer.id,
                    consultant_id=other_staff.id,
                    external_visit_order_no="DZORGDAY",
                    external_visit_order_seg="110",
                )
                visit_order = VisitOrder(
                    id="vo_org_day",
                    dzdh="DZORGDAY",
                    dzseg="110",
                    jgbm="6101",
                    crtdt="2026-05-05",
                    sjrq="2026-05-05",
                    fzuer="B001",
                )
                recording = Recording(
                    id="rec_org_day",
                    staff_id=staff.id,
                    file_name="0505_130615.mp3",
                    file_path="/tmp/0505_130615.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 5, 5, 13, 6, tzinfo=timezone.utc),
                )
                db.add_all([staff, other_staff, user, customer, visit, visit_order, recording])
                await db.commit()

                result = await update_recording(
                    recording_id=recording.id,
                    body=RecordingUpdate(visit_id=visit.id, linked_visit_ids=[visit.id]),
                    db=db,
                    user=user,
                )

                assert result.visit_id == visit.id
                assert result.linked_visit_ids == [visit.id]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_ensure_recording_visit_order_local_visit_creates_visit_for_same_day_order() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    id="staff_owner",
                    name="科室助理",
                    external_account="A001",
                    hospital_code="6501",
                    permission_role="staff",
                )
                other_staff = Staff(
                    id="staff_other",
                    name="现场咨询",
                    external_account="B001",
                    hospital_code="6501",
                    permission_role="staff",
                )
                user = User(
                    username="A001",
                    hashed_password="x",
                    role="staff",
                    staff_id=staff.id,
                    hospital_code="6501",
                    is_active=True,
                )
                recording = Recording(
                    id="rec_create_visit",
                    staff_id=staff.id,
                    file_name="0506_131800.mp3",
                    file_path="/tmp/0506_131800.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 5, 6, 13, 18, tzinfo=timezone.utc),
                )
                visit_order = VisitOrder(
                    id="vo_create_visit",
                    dzdh="2118323978",
                    dzseg="110",
                    jgbm="6501",
                    crtdt="2026-05-06",
                    sjrq="2026-05-06",
                    kunr="66512186",
                    ninam="朱中元",
                    fzuer=other_staff.external_account,
                    fzr_id_dq=other_staff.external_account,
                    fzsj="13:32:00",
                    jgks="JGKS04",
                    jgks_txt="微整科",
                    remark_dz="微整面诊",
                )
                db.add_all([staff, other_staff, user, recording, visit_order])
                await db.commit()

                result = await ensure_recording_visit_order_local_visit(
                    recording_id=recording.id,
                    body=RecordingVisitOrderLocalVisitRequest(visit_order_id=visit_order.id),
                    db=db,
                    current_user=user,
                )

                assert result.visit_order_id == visit_order.id
                assert result.dzdh == "2118323978"
                visit = await db.get(Visit, result.visit_id)
                assert visit is not None
                assert visit.external_visit_order_no == "2118323978"
                assert visit.external_visit_order_seg == "110"
                assert visit.consultant_id == other_staff.id
                customer = await db.get(Customer, visit.customer_id)
                assert customer is not None
                assert customer.external_customer_code == "66512186"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
