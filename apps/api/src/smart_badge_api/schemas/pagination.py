"""通用分页方案。"""

from __future__ import annotations

import math
from typing import Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int


async def paginate(
    db: AsyncSession,
    stmt: Select,  # type: ignore[type-arg]
    *,
    page: int,
    page_size: int,
) -> tuple[list, int]:
    """执行分页查询，返回 (rows, total)。

    page_size=0 表示不分页，返回全量。
    """
    # 先查总数
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar_one()

    if page_size > 0:
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    rows = (await db.execute(stmt)).all()
    return rows, total


def make_page_response(
    items: list[T],
    total: int,
    page: int,
    page_size: int,
) -> PaginatedResponse[T]:
    """构造分页响应。"""
    effective_size = page_size if page_size > 0 else max(total, 1)
    return PaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=effective_size,
        pages=math.ceil(total / effective_size) if effective_size else 1,
    )
