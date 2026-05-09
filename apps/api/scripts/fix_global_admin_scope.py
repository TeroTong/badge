from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from smart_badge_api.db.models import Staff, User
from smart_badge_api.db.session import _session_factory

GLOBAL_ROLES = ("super_admin", "system_admin")
ALL_INSTITUTIONS_LABEL = "所有机构"


async def main() -> None:
    async with _session_factory() as db:
        users = (await db.execute(select(User).where(User.role.in_(GLOBAL_ROLES)))).scalars().all()
        staff_members = (
            await db.execute(select(Staff).where(Staff.permission_role.in_(GLOBAL_ROLES)))
        ).scalars().all()

        users_updated = 0
        for user in users:
            changed = False
            if user.hospital_code is not None:
                user.hospital_code = None
                changed = True
            if user.hospital_name != ALL_INSTITUTIONS_LABEL:
                user.hospital_name = ALL_INSTITUTIONS_LABEL
                changed = True
            if changed:
                users_updated += 1

        staff_updated = 0
        for staff in staff_members:
            changed = False
            if staff.hospital_code is not None:
                staff.hospital_code = None
                changed = True
            if staff.hospital_short_name != ALL_INSTITUTIONS_LABEL:
                staff.hospital_short_name = ALL_INSTITUTIONS_LABEL
                changed = True
            if staff.wecom_corp_id is not None:
                staff.wecom_corp_id = None
                changed = True
            if changed:
                staff_updated += 1

        await db.commit()

    print(
        json.dumps(
            {
                "global_users_seen": len(users),
                "global_users_updated": users_updated,
                "global_staff_seen": len(staff_members),
                "global_staff_updated": staff_updated,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
