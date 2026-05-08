from __future__ import annotations

import argparse
import asyncio
import time

from sqlalchemy import func, select, text

from smart_badge_api.api.routes.customers import list_customers
from smart_badge_api.api.routes.recordings import list_archive_recordings
from smart_badge_api.core.security import create_access_token
from smart_badge_api.db.models import Customer, Recording, StaffManagementRelation, User, Visit
from smart_badge_api.db.session import _session_factory


async def _load_user(username: str):
    async with _session_factory() as db:
        return (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one()


async def _print_counts(usernames: list[str]) -> None:
    async with _session_factory() as db:
        print("table_counts")
        for name, model in (
            ("customers", Customer),
            ("visits", Visit),
            ("recordings", Recording),
            ("management_relations", StaffManagementRelation),
        ):
            count = (await db.execute(select(func.count()).select_from(model))).scalar_one()
            print(f"  {name}: {count}")
        for username in usernames:
            user = (
                await db.execute(select(User).where(User.username == username))
            ).scalar_one()
            managed_count = (
                await db.execute(
                    select(func.count())
                    .select_from(StaffManagementRelation)
                    .where(StaffManagementRelation.manager_staff_id == user.staff_id)
                )
            ).scalar_one()
            print(
                f"  user={username} role={user.role} staff_id={user.staff_id} managed={managed_count}",
            )


async def _print_index_counts() -> None:
    async with _session_factory() as db:
        indexes = (
            await db.execute(
                text(
                    """
                    select relname
                    from pg_class
                    where relkind = 'i'
                      and relname like 'ix_perf_%'
                    order by relname
                    """
                )
            )
        ).all()
        print(f"perf_indexes: {len(indexes)}")


async def _time_route(username: str) -> None:
    user = await _load_user(username)
    _ = create_access_token(user.id)
    print(f"user={username}")
    async with _session_factory() as db:
        started = time.perf_counter()
        archive_page = await list_archive_recordings(
            visit_id=None,
            staff_id=None,
            hospital_code=None,
            status=None,
            keyword=None,
            link_state=None,
            sort_mode="date_grouped_link_state",
            exclude_filtered=False,
            exclude_quality_filtered=True,
            problem_only=False,
            include_date_summaries=False,
            include_analysis_summary=False,
            fast_page=True,
            date_from=None,
            date_to=None,
            page=1,
            page_size=20,
            db=db,
            current_user=user,
        )
        print(
            f"  archive_route: {time.perf_counter() - started:.3f}s "
            f"items={len(archive_page.items)} total={archive_page.total}",
        )

    async with _session_factory() as db:
        started = time.perf_counter()
        archive_page = await list_archive_recordings(
            visit_id=None,
            staff_id=None,
            hospital_code=None,
            status=None,
            keyword=None,
            link_state=None,
            sort_mode="date_grouped_link_state",
            exclude_filtered=False,
            exclude_quality_filtered=True,
            problem_only=False,
            include_date_summaries=False,
            include_analysis_summary=False,
            fast_page=True,
            date_from=None,
            date_to=None,
            page=1,
            page_size=20,
            db=db,
            current_user=user,
        )
        print(
            f"  archive_route_warm: {time.perf_counter() - started:.3f}s "
            f"items={len(archive_page.items)} total={archive_page.total}",
        )

    async with _session_factory() as db:
        started = time.perf_counter()
        customer_page = await list_customers(
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
            page_size=12,
            db=db,
            current_user=user,
        )
        print(
            f"  customers_route: {time.perf_counter() - started:.3f}s "
            f"items={len(customer_page.items)} total={customer_page.total}",
        )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("usernames", nargs="*", default=["admin", "81021570"])
    args = parser.parse_args()
    await _print_counts(args.usernames)
    await _print_index_counts()
    for username in args.usernames:
        await _time_route(username)


if __name__ == "__main__":
    asyncio.run(main())
