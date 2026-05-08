from pydantic import BaseModel


# ── Tag ─────────────────────────────────────────

class TagCreate(BaseModel):
    name: str
    sort_order: int = 0
    is_active: bool = True


class TagUpdate(BaseModel):
    name: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class TagOut(BaseModel):
    id: str
    category_id: str
    name: str
    sort_order: int
    is_active: bool

    model_config = {"from_attributes": True}


# ── TagCategory ─────────────────────────────────

class TagCategoryCreate(BaseModel):
    name: str
    description: str = ""
    weight_level: int | None = None
    group_name: str | None = None
    sort_order: int = 0




class TagCategoryBulkItem(BaseModel):
    name: str
    group: str
    weight: int
    description: str = ""
    options: list[str] = []


class TagCategoryBulkImport(BaseModel):
    items: list[TagCategoryBulkItem]


class BulkImportResult(BaseModel):
    categories_created: int
    tags_created: int

class TagCategoryUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class TagCategoryOut(BaseModel):
    id: str
    name: str
    description: str
    group_name: str | None = None
    weight_level: int | None = None
    sort_order: int
    is_active: bool
    tags: list[TagOut] = []

    model_config = {"from_attributes": True}

    model_config = {"from_attributes": True}
