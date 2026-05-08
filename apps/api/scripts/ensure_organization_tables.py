from __future__ import annotations

import asyncio

from smart_badge_api.db.models import OrganizationUnit, OrganizationUnitMember, StaffManagementRelation
from smart_badge_api.db.session import _engine


async def main() -> None:
    async with _engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: OrganizationUnit.__table__.create(sync_conn, checkfirst=True)
        )
        await conn.run_sync(
            lambda sync_conn: OrganizationUnitMember.__table__.create(sync_conn, checkfirst=True)
        )
        await conn.run_sync(
            lambda sync_conn: StaffManagementRelation.__table__.create(sync_conn, checkfirst=True)
        )


if __name__ == "__main__":
    asyncio.run(main())
