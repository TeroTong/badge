import asyncio
from datetime import date

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.customers import list_customers
from smart_badge_api.api.routes.visits import list_visits
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import Customer, Recording, Staff, User, Visit


def test_visit_date_summaries_cover_filtered_result_not_current_page() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(id="staff_admin", name="Admin", permission_role="system_admin")
                customers = [
                    Customer(id="cust_a", name="客户A"),
                    Customer(id="cust_b", name="客户B"),
                    Customer(id="cust_c", name="客户C"),
                ]
                visits = [
                    Visit(id="visit_a", customer_id="cust_a", consultant_id=staff.id, visit_date=date(2026, 5, 6), visit_time="10:00:00"),
                    Visit(id="visit_b", customer_id="cust_b", consultant_id=staff.id, visit_date=date(2026, 5, 6), visit_time="09:00:00"),
                    Visit(id="visit_c", customer_id="cust_c", consultant_id=staff.id, visit_date=date(2026, 5, 5), visit_time="11:00:00"),
                ]
                current_user = User(username="admin", hashed_password="hashed", role="system_admin", staff_id=staff.id, is_active=True)
                db.add_all([staff, *customers, *visits, current_user])
                await db.commit()

                page = await list_visits(
                    customer_id=None,
                    status=None,
                    has_recharge=None,
                    keyword=None,
                    consultant_id=None,
                    participant_staff_id=None,
                    source=None,
                    date_from=None,
                    date_to=None,
                    has_recordings=None,
                    page=1,
                    page_size=1,
                    db=db,
                    current_user=current_user,
                )

                assert len(page.items) == 1
                summary_by_date = {item.date: item.total for item in page.date_summaries}
                assert summary_by_date["2026-05-06"] == 2
                assert summary_by_date["2026-05-05"] == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_customer_date_summaries_cover_filtered_result_not_current_page() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(id="staff_admin", name="Admin", permission_role="system_admin")
                customers = [
                    Customer(id="cust_a", name="客户A"),
                    Customer(id="cust_b", name="客户B"),
                    Customer(id="cust_c", name="客户C"),
                ]
                visits = [
                    Visit(id="visit_a", customer_id="cust_a", consultant_id=staff.id, visit_date=date(2026, 5, 6), visit_time="10:00:00"),
                    Visit(id="visit_b", customer_id="cust_b", consultant_id=staff.id, visit_date=date(2026, 5, 6), visit_time="09:00:00"),
                    Visit(id="visit_c", customer_id="cust_c", consultant_id=staff.id, visit_date=date(2026, 5, 5), visit_time="11:00:00"),
                ]
                recordings = [
                    Recording(id="rec_a", visit_id="visit_a", staff_id=staff.id, file_name="a.mp3", file_path="/tmp/a.mp3", status="analyzed"),
                    Recording(id="rec_b", visit_id="visit_b", staff_id=staff.id, file_name="b.mp3", file_path="/tmp/b.mp3", status="analyzed"),
                    Recording(id="rec_c", visit_id="visit_c", staff_id=staff.id, file_name="c.mp3", file_path="/tmp/c.mp3", status="analyzed"),
                ]
                current_user = User(username="admin", hashed_password="hashed", role="system_admin", staff_id=staff.id, is_active=True)
                db.add_all([staff, *customers, *visits, *recordings, current_user])
                await db.commit()

                page = await list_customers(
                    keyword="",
                    is_active=None,
                    consultant_id=None,
                    has_visits=None,
                    has_recordings=True,
                    has_positive_recharge=None,
                    date_from=None,
                    date_to=None,
                    page=1,
                    page_size=1,
                    db=db,
                    current_user=current_user,
                )

                assert len(page.items) == 1
                summary_by_date = {item.date: item.total for item in page.date_summaries}
                assert summary_by_date["2026-05-06"] == 2
                assert summary_by_date["2026-05-05"] == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_customer_fast_page_probes_next_row_without_date_summaries() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(id="staff_admin", name="Admin", permission_role="system_admin")
                customers = [
                    Customer(id="cust_a", name="Customer A"),
                    Customer(id="cust_b", name="Customer B"),
                    Customer(id="cust_c", name="Customer C"),
                ]
                visits = [
                    Visit(id="visit_a", customer_id="cust_a", consultant_id=staff.id, visit_date=date(2026, 5, 6), visit_time="10:00:00"),
                    Visit(id="visit_b", customer_id="cust_b", consultant_id=staff.id, visit_date=date(2026, 5, 5), visit_time="10:00:00"),
                    Visit(id="visit_c", customer_id="cust_c", consultant_id=staff.id, visit_date=date(2026, 5, 4), visit_time="10:00:00"),
                ]
                recordings = [
                    Recording(id="rec_a", visit_id="visit_a", staff_id=staff.id, file_name="a.mp3", file_path="/tmp/a.mp3", status="analyzed"),
                    Recording(id="rec_b", visit_id="visit_b", staff_id=staff.id, file_name="b.mp3", file_path="/tmp/b.mp3", status="analyzed"),
                    Recording(id="rec_c", visit_id="visit_c", staff_id=staff.id, file_name="c.mp3", file_path="/tmp/c.mp3", status="analyzed"),
                ]
                current_user = User(username="admin", hashed_password="hashed", role="system_admin", staff_id=staff.id, is_active=True)
                db.add_all([staff, *customers, *visits, *recordings, current_user])
                await db.commit()

                page = await list_customers(
                    keyword="",
                    is_active=None,
                    consultant_id=None,
                    has_visits=None,
                    has_recordings=True,
                    has_positive_recharge=None,
                    date_from=None,
                    date_to=None,
                    include_date_summaries=False,
                    fast_page=True,
                    page=1,
                    page_size=1,
                    db=db,
                    current_user=current_user,
                )

                assert len(page.items) == 1
                assert page.items[0].id == "cust_a"
                assert page.total == 2
                assert page.pages == 2
                assert page.date_summaries == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())
