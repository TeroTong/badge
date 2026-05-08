import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes import customers as customers_route
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import AnalysisTask, Customer, Recording, RecordingVisitLink, Staff, Tag, TagCategory, User, Visit, VisitOrder


async def _noop_ensure_tag_categories(_db) -> None:
    return None


def test_customer_tag_completion_includes_secondary_linked_recording_tags() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(id="staff001", name="Admin", permission_role="system_admin")
                primary_customer = Customer(id="cust_primary", name="主客户")
                target_customer = Customer(id="cust_target", name="目标客户")
                primary_visit = Visit(id="visit_primary", customer_id=primary_customer.id, consultant_id=staff.id)
                target_visit = Visit(id="visit_target", customer_id=target_customer.id, consultant_id=staff.id)
                recording = Recording(
                    id="rec_secondary",
                    visit_id=primary_visit.id,
                    staff_id=staff.id,
                    file_name="0414_114431.mp3",
                    file_path="/tmp/0414_114431.mp3",
                    status="analyzed",
                )
                task = AnalysisTask(
                    id="task_secondary",
                    file_name="recording_rec_secondary.json",
                    file_path="/tmp/recording_rec_secondary.json",
                    status="done",
                    overall_score=0.0,
                    result={
                        "customer_profile": {
                            "tags": [
                                {"category": "健康风险/禁忌", "value": "疤痕体质", "weight_level": 1},
                            ]
                        },
                        "consultation_evaluation": {"dimensions": []},
                    },
                )
                db.add_all(
                    [
                        primary_customer,
                        target_customer,
                        staff,
                        primary_visit,
                        target_visit,
                        recording,
                        task,
                        RecordingVisitLink(recording_id=recording.id, visit_id=primary_visit.id, is_primary=True),
                        RecordingVisitLink(recording_id=recording.id, visit_id=target_visit.id, is_primary=False),
                        TagCategory(id="cat001", name="健康风险/禁忌", weight_level=1, sort_order=1, is_active=True),
                        Tag(id="tag001", category_id="cat001", name="疤痕体质", is_active=True),
                    ]
                )
                await db.commit()

                original_ensure = customers_route.ensure_tag_categories
                customers_route.ensure_tag_categories = _noop_ensure_tag_categories
                try:
                    payload = await customers_route.get_customer_tag_completion(
                        target_customer.id,
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
                finally:
                    customers_route.ensure_tag_categories = original_ensure

                category = next(item for item in payload.categories if item.category_name == "健康风险/禁忌")
                assert payload.extracted_categories >= 1
                assert category.status == "extracted"
                assert category.extracted_values == ["疤痕体质"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_customer_tag_completion_uses_customer_archive_birthdate_tag() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(id="staff_age", name="Admin", permission_role="system_admin")
                customer = Customer(id="cust_age", name="周琴", age=33)
                visit = Visit(id="visit_age", customer_id=customer.id, consultant_id=staff.id)
                visit_order = VisitOrder(
                    id="order_age",
                    dzdh="VO001",
                    customer_birthday="1991-05-01",
                )
                visit.external_visit_order_no = "VO001"
                db.add_all(
                    [
                        staff,
                        customer,
                        visit,
                        visit_order,
                        TagCategory(id="cat_age", name="出生日期/年龄", weight_level=1, sort_order=1, is_active=True),
                    ]
                )
                await db.commit()

                original_ensure = customers_route.ensure_tag_categories
                customers_route.ensure_tag_categories = _noop_ensure_tag_categories
                try:
                    payload = await customers_route.get_customer_tag_completion(
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
                finally:
                    customers_route.ensure_tag_categories = original_ensure

                age_item = next(item for item in payload.categories if item.category_name == "出生日期/年龄")
                assert payload.extracted_categories >= 1
                assert age_item.status == "extracted"
                assert age_item.extracted_values == ["1991-05-01"]
                assert age_item.evidence == "已从客户档案同步"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_customer_tag_completion_does_not_use_age_as_birthdate_tag() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(id="staff_age_only", name="Admin", permission_role="system_admin")
                customer = Customer(id="cust_age_only", name="周琴", age=33)
                visit = Visit(id="visit_age_only", customer_id=customer.id, consultant_id=staff.id)
                db.add_all(
                    [
                        staff,
                        customer,
                        visit,
                        TagCategory(id="cat_age_only", name="出生日期/年龄", weight_level=1, sort_order=1, is_active=True),
                    ]
                )
                await db.commit()

                original_ensure = customers_route.ensure_tag_categories
                customers_route.ensure_tag_categories = _noop_ensure_tag_categories
                try:
                    payload = await customers_route.get_customer_tag_completion(
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
                finally:
                    customers_route.ensure_tag_categories = original_ensure

                age_item = next(item for item in payload.categories if item.category_name == "出生日期/年龄")
                assert age_item.status != "extracted"
                assert "33岁" not in age_item.extracted_values
        finally:
            await engine.dispose()

    asyncio.run(scenario())
