from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.db.models import AuditLog


async def append_audit_log(
    db: AsyncSession,
    *,
    operator_name: str,
    ip_address: str,
    module_name: str,
    action_name: str,
    content: str,
) -> None:
    db.add(
        AuditLog(
            operator_name=operator_name or "系统",
            ip_address=ip_address or "",
            module_name=module_name,
            action_name=action_name,
            content=content,
        )
    )
    await db.commit()
