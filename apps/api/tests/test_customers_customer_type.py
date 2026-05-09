import asyncio
from datetime import date, datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes.customers import get_customer, get_customer_detail, list_customers
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import Customer, Recording, Staff, User, Visit, VisitOrder


def test_customer_outputs_include_latest_scoped_kut30_customer_type() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    id="staff_admin",
                    name="Admin",
                    external_account="ADMIN001",
                    hospital_code="6101",
                    permission_role="hospital_admin",
                )
                customer = Customer(id="cust001", name="周琴", external_customer_code="C001")
                older_visit = Visit(
                    id="visit_old",
                    customer_id=customer.id,
                    consultant_id=staff.id,
                    external_visit_order_no="DZ001",
                    visit_date=date(2026, 4, 20),
                )
                latest_visit = Visit(
                    id="visit_latest",
                    customer_id=customer.id,
                    consultant_id=staff.id,
                    external_visit_order_no="DZ002",
                    visit_date=date(2026, 4, 21),
                )
                other_institution_visit = Visit(
                    id="visit_other",
                    customer_id=customer.id,
                    external_visit_order_no="DZ003",
                    visit_date=date(2026, 4, 22),
                )
                db.add_all(
                    [
                        staff,
                        customer,
                        older_visit,
                        latest_visit,
                        other_institution_visit,
                        Recording(
                            id="rec_old",
                            visit_id=older_visit.id,
                            staff_id=staff.id,
                            file_name="old.mp3",
                            file_path="/tmp/old.mp3",
                            status="analyzed",
                            created_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
                        ),
                        Recording(
                            id="rec_latest",
                            visit_id=latest_visit.id,
                            staff_id=staff.id,
                            file_name="latest.mp3",
                            file_path="/tmp/latest.mp3",
                            status="analyzed",
                            created_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
                        ),
                        Recording(
                            id="rec_other",
                            visit_id=other_institution_visit.id,
                            file_name="other.mp3",
                            file_path="/tmp/other.mp3",
                            status="analyzed",
                        ),
                        VisitOrder(
                            id="order_old",
                            dzdh="DZ001",
                            dzseg="110",
                            jgbm="6101",
                            advxc=staff.external_account,
                            kutyp_dq="V",
                            kutyp_dq_txt="会员/老客",
                            kut30_dq="Q",
                            kut30_dq_txt="潜客/新客",
                            sjrq="2026-04-20",
                            crtdt="2026-04-20",
                        ),
                        VisitOrder(
                            id="order_latest",
                            dzdh="DZ002",
                            dzseg="110",
                            jgbm="6101",
                            advxc=staff.external_account,
                            kutyp_dq="Q",
                            kutyp_dq_txt="潜客/新客",
                            kut30_dq="V",
                            kut30_dq_txt="会员/老客",
                            sjrq="2026-04-21",
                            crtdt="2026-04-21",
                        ),
                        VisitOrder(
                            id="order_other",
                            dzdh="DZ003",
                            dzseg="110",
                            jgbm="6102",
                            kutyp_dq="V",
                            kutyp_dq_txt="会员/老客",
                            kut30_dq="Q",
                            kut30_dq_txt="潜客/新客",
                            sjrq="2026-04-22",
                            crtdt="2026-04-22",
                        ),
                    ]
                )
                current_user = User(
                    username="hospital_admin",
                    hashed_password="hashed",
                    display_name="机构管理员",
                    role="hospital_admin",
                    staff_id=staff.id,
                    hospital_code="6101",
                    is_active=True,
                )
                db.add(current_user)
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
                assert customers.items[0].customer_type_code == "V"
                assert customers.items[0].customer_type_label == "老客"
                assert customers.items[0].customer_type_institution_code == "6101"

                customer_summary = await get_customer(customer.id, db=db, current_user=current_user)
                assert customer_summary.customer_type_code == "V"
                assert customer_summary.customer_type_label == "老客"

                customer_detail = await get_customer_detail(customer.id, db=db, current_user=current_user)
                assert customer_detail.customer_type_code == "V"
                assert customer_detail.customer_type_label == "老客"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
