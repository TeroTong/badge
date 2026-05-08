from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.db.base import Base
from smart_badge_api.db.models import Customer, Recording, RecordingVisitLink, SapHanaVisitOrder, Staff, Visit, VisitOrder
from smart_badge_api.schemas.visit_order import VisitOrderSyncResult
from smart_badge_api.visit_order_sync import (
    _build_visit_order_records_from_sap_row,
    _comparable_datetime,
    _legacy_remote_row_to_visit_order_record,
    cleanup_out_of_context_visit_orders,
    fetch_latest_remote_visit_order_date,
    prune_stale_legacy_visit_data,
    retry_visit_order_sync,
    sync_customer_center_from_visit_orders,
    sync_sap_hana_customer_birthdays_for_keys,
    sync_pushed_sap_hana_visit_orders_for_recording_contexts,
    sync_visit_orders,
    sync_visit_orders_for_recording_contexts,
    sync_visit_orders_for_recording,
)


def test_comparable_datetime_normalizes_aware_value_to_naive_utc() -> None:
    aware = datetime(2026, 4, 13, 8, 0, tzinfo=timezone.utc)
    normalized = _comparable_datetime(aware)

    assert normalized == datetime(2026, 4, 13, 8, 0)
    assert normalized.tzinfo is None


def test_comparable_datetime_preserves_naive_value() -> None:
    naive = datetime(2026, 4, 13, 16, 30, 0)

    assert _comparable_datetime(naive) == naive


def test_build_visit_order_records_from_sap_row_maps_sap_hana_fields() -> None:
    row = SapHanaVisitOrder(
        id="sap001",
        jgbm="6101",
        dzdh="DZ1001",
        yydh="YY1001",
        crtdt="20260416",
        crttm="093015",
        dzsta="C",
        dztyp="4",
        dzly="Y",
        dymd="A",
        kunr="KH001",
        ninam="测试客户",
        kusex="F",
        kulvl_dq="VIP",
        kutyp_dq="V",
        kut30_dq="V",
        kusta_dq="V1",
        kusrc="线上",
        kusrc2="小红书",
        jgks="JGKS03",
        bhkx="N",
        fzuer="81034062",
        fzuer_long="赵林爽",
        d_fzuer="81034063",
        advyq="82000001",
        vipkf="KF001",
        d_vipkf="KF002",
        remark_dz="想了解眼综合",
        fzdata=[
            {
                "FZDH": "DZ1001-001",
                "ADVXC": "81034063",
                "ADVXC_LONG": "兰四秀",
                "ASSXC": "81034064",
                "FZSJ": "094500",
                "FZSTA": "A",
                "JCSTA": "Y",
                "DDSC": "12",
            }
        ],
    )

    records = _build_visit_order_records_from_sap_row(
        row,
        institution_name_by_code={"6101": "米兰柏羽总院"},
        staff_name_by_external_code={
            "81034062": "钟露",
            "81034063": "兰四秀",
            "82000001": "院前顾问",
        },
    )

    assert len(records) == 1
    first = records[0]
    assert first["dzdh"] == "DZ1001"
    assert first["dzseg"] == "001"
    assert first["jgbm"] == "6101"
    assert first["sjrq"] == "2026-04-16"
    assert first["crttm"] == "09:30:15"
    assert first["fzsj"] == "09:45:00"
    assert first["fzuer_long"] == "赵林爽"
    assert first["fzr_id_dq"] == "81034063"
    assert first["d_vipkf"] == "KF002"
    assert first["advxc_long"] == "兰四秀"
    assert first["advyq_name"] == "院前顾问"
    assert first["jgks"] == "JGKS03"
    assert first["jgks_txt"] == "外科"
    assert first["dzsta_txt"] == "已分诊"
    assert first["dztyp_txt"] == "诊疗"
    assert first["dymd_txt"] == "咨询"
    assert first["dzly_txt"] == "已预约"
    assert first["fzsta_txt"] == "已接诊"
    assert first["jcsta_txt"] == "已成交"
    assert first["kunr"] == "KH001"
    assert first["ninam"] == "测试客户"
    assert first["kusex"] == "F"
    assert first["kusex_txt"] == "女"
    assert first["kutyp_dq"] == "V"
    assert first["kutyp_dq_txt"] == "会员/老客"
    assert first["kut30_dq"] == "V"
    assert first["kut30_dq_txt"] == "会员/老客"
    assert first["kusta_dq"] == "V1"
    assert first["kusta_dq_txt"] == "付费会员"
    assert first["d_fzuer"] == "81034063"
    assert first["vipkf"] == "KF001"
    assert first["dzly"] == "Y"
    assert first["dymd"] == "A"
    assert first["kusrc"] == "线上"
    assert first["kusrc2"] == "小红书"
    assert first["customer_gender"] == "女"
    assert first["jdrq"] == "20260416"


def test_build_visit_order_records_from_sap_row_prefers_kubsd_birthdate() -> None:
    row = SapHanaVisitOrder(
        id="sap_kubsd",
        jgbm="6101",
        dzdh="DZKUBSD",
        crtdt="20260416",
        crttm="093015",
        kunr="KH001",
        ninam="测试客户",
        kusex="F",
        customer_birthday="1988-01-01",
        source_payload={"KUBSD": "1990-05-06", "CSRQ": "1980-01-01"},
    )

    records = _build_visit_order_records_from_sap_row(
        row,
        institution_name_by_code={"6101": "米兰柏羽总院"},
        staff_name_by_external_code={},
        customer_birthdays_by_code={"KH001": "1979-01-01"},
    )

    assert records[0]["customer_birthday"] == "1990-05-06"


def test_sync_sap_hana_customer_birthdays_for_keys_uses_kubsd_without_remote_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        def fail_remote_lookup(*args, **kwargs):
            raise AssertionError("KUBSD should avoid remote customer birthday lookup")

        monkeypatch.setattr(
            "smart_badge_api.visit_order_sync._fetch_remote_customer_birthdays",
            fail_remote_lookup,
        )

        try:
            async with session_factory() as db:
                sap_row = SapHanaVisitOrder(
                    id="sap_kubsd_sync",
                    jgbm="6101",
                    dzdh="DZKUBSD",
                    crtdt="20260416",
                    crttm="093015",
                    kunr="KH001",
                    ninam="测试客户",
                    source_payload={"KUBSD": "19900506"},
                )
                visit_order = VisitOrder(
                    id="vo_kubsd_sync",
                    jgbm="6101",
                    dzdh="DZKUBSD",
                    dzseg="001",
                    crtdt="2026-04-16",
                    sjrq="2026-04-16",
                    kunr="KH001",
                    ninam="测试客户",
                )
                db.add_all([sap_row, visit_order])
                await db.commit()

                result = await sync_sap_hana_customer_birthdays_for_keys(
                    db,
                    keys={("6101", "DZKUBSD")},
                )

                assert result["checked"] == 1
                assert result["found"] == 1
                assert result["visit_orders_updated"] == 1

                await db.refresh(sap_row)
                await db.refresh(visit_order)
                assert sap_row.customer_birthday == "1990-05-06"
                assert visit_order.customer_birthday == "1990-05-06"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_customer_center_sync_keeps_latest_visit_age() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                customer = Customer(id="cust_latest_age", name="测试客户", external_customer_code="KH001", age=None)
                old_order = VisitOrder(
                    id="vo_old_age",
                    jgbm="6101",
                    dzdh="DZOLD",
                    dzseg="001",
                    sjrq="2024-05-07",
                    crtdt="2024-05-07",
                    crttm="09:00:00",
                    kunr="KH001",
                    ninam="测试客户",
                    customer_birthday="1990-05-08",
                )
                new_order = VisitOrder(
                    id="vo_new_age",
                    jgbm="6101",
                    dzdh="DZNEW",
                    dzseg="001",
                    sjrq="2026-05-07",
                    crtdt="2026-05-07",
                    crttm="09:00:00",
                    kunr="KH001",
                    ninam="测试客户",
                    customer_birthday="1990-05-08",
                )
                db.add_all([customer, old_order, new_order])
                await db.commit()

                await sync_customer_center_from_visit_orders(db)
                await db.refresh(customer)

                assert customer.age == 35
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_fetch_latest_remote_visit_order_date_reads_local_sap_hana_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine, tables=[SapHanaVisitOrder.__table__])
    with engine.begin() as conn:
        conn.execute(
            SapHanaVisitOrder.__table__.insert(),
            [
                {"id": "sap001", "jgbm": "6101", "dzdh": "DZ1001", "crtdt": "2026-04-15"},
                {"id": "sap002", "jgbm": "6101", "dzdh": "DZ1002", "crtdt": "2026-04-16"},
                {"id": "sap003", "jgbm": "6201", "dzdh": "DZ2001", "crtdt": "2026-04-18"},
            ],
        )

    monkeypatch.setattr("smart_badge_api.visit_order_sync._sync_lookup_engine", lambda: engine)

    assert fetch_latest_remote_visit_order_date({"6101"}) == "2026-04-16"
    assert fetch_latest_remote_visit_order_date({"6201"}) == "2026-04-18"
    assert fetch_latest_remote_visit_order_date({"9999"}) is None


def test_legacy_remote_row_prefers_current_institution_code_from_yybm() -> None:
    record = _legacy_remote_row_to_visit_order_record(
        {
            "dzdh": "LEGACY001",
            "dzseg": "110",
            "sjrq": "2026-03-20",
            "jgbm": "6100",
            "yybm": "6101",
            "yyjc": "米兰柏羽总院",
            "crtdt": "20260320",
            "crttm": "132059",
            "csrq": "19900101",
        }
    )

    assert record["jgbm"] == "6101"
    assert record["crtdt"] == "2026-03-20"
    assert record["crttm"] == "13:20:59"
    assert record["customer_birthday"] == "1990-01-01"


def test_prune_stale_legacy_visit_data_preserves_recent_and_recording_backed_history() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                db.add(
                    SapHanaVisitOrder(
                        id="sap001",
                        jgbm="6101",
                        dzdh="SAP1001",
                        crtdt="2026-04-16",
                        crttm="093000",
                        dzsta="C",
                    )
                )

                customer_a = Customer(id="cust_a", name="客户A")
                customer_b = Customer(id="cust_b", name="客户B")
                customer_c = Customer(id="cust_c", name="客户C")
                db.add_all([customer_a, customer_b, customer_c])

                recent_date = datetime.now(timezone.utc).date() - timedelta(days=1)
                recent_date_string = recent_date.isoformat()
                stale_order = VisitOrder(id="vo_stale", dzdh="LEGACY001", dzseg="110", crtdt="2026-03-20", sjrq="2026-03-20", jgbm="6100")
                recording_backed_order = VisitOrder(id="vo_keep_recording", dzdh="LEGACY002", dzseg="110", crtdt="2026-03-20", sjrq="2026-03-20", jgbm="6100")
                recent_order = VisitOrder(id="vo_keep_recent", dzdh="LEGACY003", dzseg="110", crtdt=recent_date_string, sjrq=recent_date_string, jgbm="6100")
                db.add_all([stale_order, recording_backed_order, recent_order])

                stale_visit = Visit(
                    id="visit_stale",
                    customer_id=customer_a.id,
                    external_visit_order_no="LEGACY001",
                    external_visit_order_seg="110",
                    visit_date=datetime(2026, 3, 20).date(),
                )
                recording_backed_visit = Visit(
                    id="visit_recording",
                    customer_id=customer_b.id,
                    external_visit_order_no="LEGACY002",
                    external_visit_order_seg="110",
                    visit_date=datetime(2026, 3, 20).date(),
                )
                recent_visit = Visit(
                    id="visit_recent",
                    customer_id=customer_c.id,
                    external_visit_order_no="LEGACY003",
                    external_visit_order_seg="110",
                    visit_date=recent_date,
                )
                db.add_all([stale_visit, recording_backed_visit, recent_visit])
                await db.flush()

                recording = Recording(
                    id="rec001",
                    visit_id=recording_backed_visit.id,
                    file_name="legacy.mp3",
                    file_path="/tmp/legacy.mp3",
                    status="uploaded",
                )
                link = RecordingVisitLink(recording_id=recording.id, visit_id=recording_backed_visit.id, is_primary=True)
                db.add_all([recording, link])
                await db.commit()

                result = await prune_stale_legacy_visit_data(db, preserve_recent_days=3)

                remaining_order_ids = set((await db.execute(select(VisitOrder.id))).scalars().all())
                remaining_visit_ids = set((await db.execute(select(Visit.id))).scalars().all())

                assert result["deleted_visit_orders"] == 1
                assert result["deleted_visits"] == 1
                assert "vo_stale" not in remaining_order_ids
                assert "visit_stale" not in remaining_visit_ids
                assert {"vo_keep_recording", "vo_keep_recent"} <= remaining_order_ids
                assert {"visit_recording", "visit_recent"} <= remaining_visit_ids
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_sync_visit_orders_without_recordings_does_not_fallback_to_full_sap_hana_sync() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                db.add(
                    SapHanaVisitOrder(
                        id="sap001",
                        jgbm="6101",
                        dzdh="DZ1001",
                        crtdt="2026-04-16",
                        crttm="093000",
                        dzsta="C",
                        fzuer="86000995",
                        fzdata=[
                            {
                                "FZDH": "DZ1001-110",
                                "ADVXC": "86000995",
                                "FZSJ": "094500",
                                "FZSTA": "A",
                                "JCSTA": "Y",
                            }
                        ],
                    )
                )
                await db.commit()

                result = await sync_visit_orders(db)

                assert result.synced_count == 0
                assert result.new_count == 0
                assert result.updated_count == 0
                rows = (await db.execute(select(VisitOrder))).scalars().all()
                assert rows == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_retry_visit_order_sync_retries_transient_failure() -> None:
    async def scenario() -> None:
        attempts = 0

        async def operation():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary database hiccup")
            return VisitOrderSyncResult(
                synced_count=1,
                new_count=1,
                updated_count=0,
                date_range="2026-04-25 ~ 2026-04-25",
            )

        result = await retry_visit_order_sync(
            operation,
            label="unit-test",
            attempts=2,
            initial_delay_seconds=0,
        )

        assert attempts == 2
        assert result.new_count == 1

    asyncio.run(scenario())


def test_sync_visit_orders_for_recording_contexts_imports_all_same_institution_day_orders() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                db.add_all([
                    SapHanaVisitOrder(
                        id="sap001",
                        jgbm="6101",
                        dzdh="DZ1001",
                        crtdt="2026-04-16",
                        crttm="093000",
                        dzsta="C",
                        fzuer="A001",
                        fzdata=[{"FZDH": "DZ1001-110", "ADVXC": "A001", "FZSJ": "094500", "FZSTA": "A", "JCSTA": "Y"}],
                    ),
                    SapHanaVisitOrder(
                        id="sap002",
                        jgbm="6201",
                        dzdh="DZ1002",
                        crtdt="2026-04-17",
                        crttm="103000",
                        dzsta="C",
                        fzuer="B001",
                        fzdata=[{"FZDH": "DZ1002-110", "ADVXC": "B001", "FZSJ": "104500", "FZSTA": "A", "JCSTA": "Y"}],
                    ),
                    SapHanaVisitOrder(
                        id="sap003",
                        jgbm="6201",
                        dzdh="DZ1003",
                        crtdt="2026-04-16",
                        crttm="113000",
                        dzsta="C",
                        fzuer="B001",
                        fzdata=[{"FZDH": "DZ1003-110", "ADVXC": "B001", "FZSJ": "114500", "FZSTA": "A", "JCSTA": "Y"}],
                    ),
                    SapHanaVisitOrder(
                        id="sap004",
                        jgbm="6101",
                        dzdh="DZ1004",
                        crtdt="2026-04-16",
                        crttm="123000",
                        dzsta="C",
                        fzuer="Z999",
                        fzdata=[{"FZDH": "DZ1004-110", "ADVXC": "Z999", "FZSJ": "124500", "FZSTA": "A", "JCSTA": "N"}],
                    ),
                ])
                await db.commit()

                result = await sync_visit_orders_for_recording_contexts(
                    db,
                    contexts={
                        ("2026-04-16", "6101", "A001"),
                        ("2026-04-17", "6201", "B001"),
                    },
                )

                assert result.synced_count == 3
                rows = (await db.execute(select(VisitOrder.dzdh).order_by(VisitOrder.dzdh))).all()
                assert rows == [("DZ1001",), ("DZ1002",), ("DZ1004",)]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_sync_visit_orders_for_recording_syncs_all_same_institution_day_orders() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(id="staff_a", name="顾问A", external_account="A001", hospital_code="6101")
                recording = Recording(
                    id="rec_a",
                    staff_id=staff.id,
                    file_name="20260425_100000.mp3",
                    file_path="/tmp/20260425_100000.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 4, 25, 10, 0, tzinfo=timezone.utc),
                )
                db.add_all([
                    staff,
                    recording,
                    SapHanaVisitOrder(
                        id="sap001",
                        jgbm="6101",
                        dzdh="DZ1001",
                        crtdt="2026-04-25",
                        crttm="090000",
                        fzuer="A001",
                        fzdata=[{"FZDH": "DZ1001-110", "ADVXC": "A001", "JCSTA": "Y"}],
                    ),
                    SapHanaVisitOrder(
                        id="sap002",
                        jgbm="6101",
                        dzdh="DZ1002",
                        crtdt="2026-04-25",
                        crttm="110000",
                        fzuer="B001",
                        fzdata=[{"FZDH": "DZ1002-110", "ADVXC": "A001", "JCSTA": "N"}],
                    ),
                    SapHanaVisitOrder(
                        id="sap003",
                        jgbm="6101",
                        dzdh="DZ1003",
                        crtdt="2026-04-25",
                        crttm="120000",
                        fzuer="B001",
                        fzdata=[{"FZDH": "DZ1003-110", "ADVXC": "B001", "JCSTA": "N"}],
                    ),
                    SapHanaVisitOrder(
                        id="sap004",
                        jgbm="6201",
                        dzdh="DZ1004",
                        crtdt="2026-04-25",
                        crttm="130000",
                        fzuer="A001",
                        fzdata=[{"FZDH": "DZ1004-110", "ADVXC": "A001", "JCSTA": "N"}],
                    ),
                ])
                await db.commit()

                result = await sync_visit_orders_for_recording(db, recording)

                assert result.new_count == 3
                rows = (await db.execute(select(VisitOrder.dzdh).order_by(VisitOrder.dzdh))).all()
                assert rows == [("DZ1001",), ("DZ1002",), ("DZ1003",)]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_pushed_sap_hana_orders_sync_all_same_institution_day_orders() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(id="staff_a", name="顾问A", external_account="A001", hospital_code="6101")
                recording = Recording(
                    id="rec_a",
                    staff_id=staff.id,
                    file_name="20260425_100000.mp3",
                    file_path="/tmp/20260425_100000.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 4, 25, 10, 0, tzinfo=timezone.utc),
                )
                db.add_all([
                    staff,
                    recording,
                    SapHanaVisitOrder(
                        id="sap001",
                        jgbm="6101",
                        dzdh="DZ1001",
                        crtdt="2026-04-25",
                        crttm="150000",
                        fzuer="B001",
                        fzdata=[{"FZDH": "DZ1001-110", "ADVXC": "B001", "ASSXC": "A001", "JCSTA": "Y"}],
                    ),
                    SapHanaVisitOrder(
                        id="sap002",
                        jgbm="6101",
                        dzdh="DZ1002",
                        crtdt="2026-04-25",
                        crttm="151000",
                        fzuer="B001",
                        fzdata=[{"FZDH": "DZ1002-110", "ADVXC": "B001", "JCSTA": "N"}],
                    ),
                ])
                await db.commit()

                result = await sync_pushed_sap_hana_visit_orders_for_recording_contexts(
                    db,
                    keys={("6101", "DZ1001"), ("6101", "DZ1002")},
                )

                assert result.synced_count == 2
                assert result.new_count == 2
                rows = (await db.execute(select(VisitOrder.dzdh, VisitOrder.assxc).order_by(VisitOrder.dzdh))).all()
                assert rows == [("DZ1001", "A001"), ("DZ1002", None)]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_pushed_sap_hana_orders_sync_without_recording_context() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                db.add(
                    SapHanaVisitOrder(
                        id="sap_no_context",
                        jgbm="6501",
                        dzdh="DZ_NO_CONTEXT",
                        crtdt="2026-05-08",
                        crttm="162000",
                        ninam="顾客A",
                        fzuer="86003084",
                        fzdata=[{"FZDH": "DZ_NO_CONTEXT-110", "ADVXC": "81040303", "JCSTA": "N"}],
                    )
                )
                await db.commit()

                result = await sync_pushed_sap_hana_visit_orders_for_recording_contexts(
                    db,
                    keys={("6501", "DZ_NO_CONTEXT")},
                )

                assert result.synced_count == 1
                assert result.new_count == 1
                row = (
                    await db.execute(select(VisitOrder).where(VisitOrder.dzdh == "DZ_NO_CONTEXT"))
                ).scalar_one()
                assert row.jgbm == "6501"
                assert row.advxc == "81040303"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_cleanup_out_of_context_visit_orders_keeps_all_rows_for_matching_dzdh_group() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(id="staff_1", name="顾问A", external_account="86000995", hospital_code="6101")
                recording = Recording(
                    id="rec_1",
                    staff_id=staff.id,
                    file_name="20260416_100000.mp3",
                    file_path="/tmp/20260416_100000.mp3",
                    status="uploaded",
                    created_at=datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc),
                )
                matching_row = VisitOrder(
                    id="vo_keep_1",
                    dzdh="DZKEEP",
                    dzseg="110",
                    jgbm="6101",
                    crtdt="2026-04-16",
                    sjrq="2026-04-16",
                    advxc="86000995",
                )
                grouped_row = VisitOrder(
                    id="vo_keep_2",
                    dzdh="DZKEEP",
                    dzseg="120",
                    jgbm="6101",
                    crtdt="2026-04-16",
                    sjrq="2026-04-16",
                    advxc="86000001",
                )
                stale_row = VisitOrder(
                    id="vo_drop_1",
                    dzdh="DZDROP",
                    dzseg="110",
                    jgbm="6101",
                    crtdt="2026-04-17",
                    sjrq="2026-04-17",
                    advxc="86000002",
                )
                stale_visit = Visit(id="visit_drop", customer_id="cust_1", external_visit_order_no="DZDROP", external_visit_order_seg="110")
                keep_visit = Visit(id="visit_keep", customer_id="cust_2", external_visit_order_no="DZKEEP", external_visit_order_seg="110")
                customer_1 = Customer(id="cust_1", name="客户1")
                customer_2 = Customer(id="cust_2", name="客户2")

                db.add_all([staff, recording, customer_1, customer_2, matching_row, grouped_row, stale_row, stale_visit, keep_visit])
                await db.commit()

                summary = await cleanup_out_of_context_visit_orders(db)

                assert summary["deleted_visit_orders"] == 1
                remaining = (await db.execute(select(VisitOrder.id, VisitOrder.dzdh).order_by(VisitOrder.id))).all()
                assert remaining == [("vo_keep_1", "DZKEEP"), ("vo_keep_2", "DZKEEP")]
                visits = (await db.execute(select(Visit.id, Visit.external_visit_order_no).order_by(Visit.id))).all()
                assert visits == [("visit_keep", "DZKEEP")]
        finally:
            await engine.dispose()

    asyncio.run(scenario())
