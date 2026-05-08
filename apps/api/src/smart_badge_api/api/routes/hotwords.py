from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.db.models import Hotword, HotwordGroup
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.hotwords import (
    HotwordCreate,
    HotwordGroupCreate,
    HotwordGroupOut,
    HotwordGroupUpdate,
    HotwordLibraryScope,
    HotwordOut,
    HotwordUpdate,
)

router = APIRouter(prefix="/hotwords", tags=["热词管理"])


def _normalize_word(value: str) -> str:
    text = value.strip()
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "热词不能为空")
    return text


async def _get_group_or_404(db: AsyncSession, group_id: str) -> HotwordGroup:
    group = await db.get(HotwordGroup, group_id, options=[selectinload(HotwordGroup.words)])
    if not group:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "热词库不存在")
    return group


async def _ensure_unique_word(
    db: AsyncSession,
    *,
    group_id: str,
    normalized_word: str,
    current_word_id: str | None = None,
) -> None:
    result = await db.execute(select(Hotword).where(Hotword.group_id == group_id))
    for existing in result.scalars():
        if existing.id == current_word_id:
            continue
        if existing.word.strip().casefold() == normalized_word.casefold():
            raise HTTPException(status.HTTP_409_CONFLICT, "该词库中已存在相同热词")


@router.get("/groups", response_model=list[HotwordGroupOut])
async def list_groups(
    library_scope: HotwordLibraryScope | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(HotwordGroup).options(selectinload(HotwordGroup.words))
    if library_scope:
        stmt = stmt.where(HotwordGroup.library_scope == library_scope)
    stmt = stmt.order_by(HotwordGroup.updated_at.desc(), HotwordGroup.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("/groups", response_model=HotwordGroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(body: HotwordGroupCreate, db: AsyncSession = Depends(get_db)):
    payload = body.model_dump()
    payload["name"] = payload["name"].strip()
    payload["source_label"] = payload["source_label"].strip()
    group = HotwordGroup(**payload)
    db.add(group)
    await db.commit()
    await db.refresh(group, ["words"])
    return group


@router.put("/groups/{group_id}", response_model=HotwordGroupOut)
async def update_group(group_id: str, body: HotwordGroupUpdate, db: AsyncSession = Depends(get_db)):
    group = await _get_group_or_404(db, group_id)
    updates = body.model_dump(exclude_unset=True)
    if "name" in updates:
        updates["name"] = updates["name"].strip()
    if "source_label" in updates:
        updates["source_label"] = updates["source_label"].strip()
    for key, value in updates.items():
        setattr(group, key, value)
    await db.commit()
    await db.refresh(group, ["words"])
    return group


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(group_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    group = await db.get(HotwordGroup, group_id)
    if not group:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "热词库不存在")
    await db.delete(group)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/groups/{group_id}/words", response_model=HotwordOut, status_code=status.HTTP_201_CREATED)
async def create_word(group_id: str, body: HotwordCreate, db: AsyncSession = Depends(get_db)):
    await _get_group_or_404(db, group_id)
    normalized_word = _normalize_word(body.word)
    await _ensure_unique_word(db, group_id=group_id, normalized_word=normalized_word)

    word = Hotword(
        group_id=group_id,
        word=normalized_word,
        weight=body.weight,
        is_active=body.is_active,
    )
    db.add(word)
    await db.commit()
    await db.refresh(word)
    return word


@router.put("/words/{word_id}", response_model=HotwordOut)
async def update_word(word_id: str, body: HotwordUpdate, db: AsyncSession = Depends(get_db)):
    word = await db.get(Hotword, word_id)
    if not word:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "热词不存在")

    updates = body.model_dump(exclude_unset=True)
    if "word" in updates:
        updates["word"] = _normalize_word(updates["word"])
        await _ensure_unique_word(
            db,
            group_id=word.group_id,
            normalized_word=updates["word"],
            current_word_id=word.id,
        )

    for key, value in updates.items():
        setattr(word, key, value)
    await db.commit()
    await db.refresh(word)
    return word


@router.delete("/words/{word_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_word(word_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    word = await db.get(Hotword, word_id)
    if not word:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "热词不存在")
    await db.delete(word)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
