from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.db.default_data import ensure_tag_categories
from smart_badge_api.db.models import Tag, TagCategory
from smart_badge_api.db.session import get_db
from smart_badge_api.schemas.tags import (
    BulkImportResult,
    TagCategoryBulkImport,
    TagCategoryCreate,
    TagCategoryOut,
    TagCategoryUpdate,
    TagCreate,
    TagOut,
    TagUpdate,
)
from smart_badge_api.tag_catalog_reference import removed_tag_category_names

router = APIRouter(prefix="/tags", tags=["标签配置"])

_HIDDEN_TAG_CATEGORY_NAMES = removed_tag_category_names()


def _is_hidden_tag_category_name(name: str | None) -> bool:
    return str(name or "").strip() in _HIDDEN_TAG_CATEGORY_NAMES


# ── Categories ──────────────────────────────────


@router.get("/categories", response_model=list[TagCategoryOut])
async def list_categories(db: AsyncSession = Depends(get_db)):
    await ensure_tag_categories(db)
    result = await db.execute(
        select(TagCategory)
        .where(TagCategory.is_active.is_(True))
        .options(selectinload(TagCategory.tags))
        .order_by(TagCategory.sort_order)
    )
    return [item for item in result.scalars().all() if not _is_hidden_tag_category_name(item.name)]


@router.post("/categories", response_model=TagCategoryOut, status_code=201)
async def create_category(body: TagCategoryCreate, db: AsyncSession = Depends(get_db)):
    if _is_hidden_tag_category_name(body.name):
        raise HTTPException(400, "该分类已改为消费意向字段，不再作为标签配置项")
    cat = TagCategory(**body.model_dump())
    db.add(cat)
    await db.commit()
    await db.refresh(cat, ["tags"])
    return cat


@router.put("/categories/{cat_id}", response_model=TagCategoryOut)
async def update_category(cat_id: str, body: TagCategoryUpdate, db: AsyncSession = Depends(get_db)):
    cat = await db.get(TagCategory, cat_id, options=[selectinload(TagCategory.tags)])
    if not cat:
        raise HTTPException(404, "分类不存在")
    if "name" in body.model_dump(exclude_unset=True) and _is_hidden_tag_category_name(body.name):
        raise HTTPException(400, "该分类已改为消费意向字段，不再作为标签配置项")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(cat, k, v)
    await db.commit()
    await db.refresh(cat, ["tags"])
    return cat


@router.delete("/categories/{cat_id}", status_code=204)
async def delete_category(cat_id: str, db: AsyncSession = Depends(get_db)):
    cat = await db.get(TagCategory, cat_id)
    if not cat:
        raise HTTPException(404, "分类不存在")
    await db.delete(cat)
    await db.commit()


@router.post("/import", response_model=BulkImportResult, status_code=201)
async def bulk_import_tags(body: TagCategoryBulkImport, db: AsyncSession = Depends(get_db)):
    """从标签目录批量导入标签分类和标签。已存在的分类（按 name 判重）会被跳过。"""
    existing = (await db.execute(select(TagCategory.name))).scalars().all()
    existing_names = set(existing)

    cats_created = 0
    tags_created = 0
    sort_counter = len(existing_names)

    for item in body.items:
        if _is_hidden_tag_category_name(item.name):
            continue
        if item.name in existing_names:
            continue
        cat = TagCategory(
            name=item.name,
            description=item.description,
            group_name=item.group,
            weight_level=item.weight,
            sort_order=sort_counter,
        )
        db.add(cat)
        await db.flush()  # get cat.id

        for idx, opt in enumerate(item.options):
            tag = Tag(category_id=cat.id, name=opt.strip(), sort_order=idx)
            db.add(tag)
            tags_created += 1

        existing_names.add(item.name)
        cats_created += 1
        sort_counter += 1

    await db.commit()
    return BulkImportResult(categories_created=cats_created, tags_created=tags_created)


# ── Tags ────────────────────────────────────────


@router.post("/categories/{cat_id}/tags", response_model=TagOut, status_code=201)
async def create_tag(cat_id: str, body: TagCreate, db: AsyncSession = Depends(get_db)):
    cat = await db.get(TagCategory, cat_id)
    if not cat:
        raise HTTPException(404, "分类不存在")
    tag = Tag(category_id=cat_id, **body.model_dump())
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return tag


@router.put("/tags/{tag_id}", response_model=TagOut)
async def update_tag(tag_id: str, body: TagUpdate, db: AsyncSession = Depends(get_db)):
    tag = await db.get(Tag, tag_id)
    if not tag:
        raise HTTPException(404, "标签不存在")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(tag, k, v)
    await db.commit()
    await db.refresh(tag)
    return tag


@router.delete("/tags/{tag_id}", status_code=204)
async def delete_tag(tag_id: str, db: AsyncSession = Depends(get_db)):
    tag = await db.get(Tag, tag_id)
    if not tag:
        raise HTTPException(404, "标签不存在")
    await db.delete(tag)
    await db.commit()
