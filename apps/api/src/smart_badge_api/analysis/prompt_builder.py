"""从固定分析模板和数据库配置动态构建提示词。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.analysis.reference_data import load_analysis_reference_data
from smart_badge_api.db.default_data import ensure_tag_categories
from smart_badge_api.db.models import (
    HotwordGroup,
    TagCategory,
    WecomTenant,
)
from smart_badge_api.tag_catalog_reference import NEGATIVE_PROJECT_EMPTY_VALUE, NEGATIVE_PROJECT_TAG_CATEGORY

_PROFILE_TAG_EXCLUDED_CATEGORIES = frozenset({"本次消费预算"})


async def _load_tag_categories(db: AsyncSession) -> list[TagCategory]:
    await ensure_tag_categories(db)
    result = await db.execute(
        select(TagCategory)
        .where(TagCategory.is_active.is_(True))
        .options(selectinload(TagCategory.tags))
        .order_by(TagCategory.sort_order)
    )
    return list(result.scalars().all())


async def _load_hotword_groups(db: AsyncSession) -> list[HotwordGroup]:
    result = await db.execute(
        select(HotwordGroup)
        .where(HotwordGroup.is_active.is_(True))
        .options(selectinload(HotwordGroup.words))
    )
    return list(result.scalars().all())


def _build_tag_section(categories: list[TagCategory]) -> str:
    """构建客户画像标签体系文本（含权重级别 + 大类分组）。"""
    lines: list[str] = []
    # Group by weight level
    by_weight: dict[int | None, list[TagCategory]] = {}
    for cat in categories:
        if cat.name in _PROFILE_TAG_EXCLUDED_CATEGORIES:
            continue
        wl = cat.weight_level
        by_weight.setdefault(wl, []).append(cat)

    for wl in sorted(by_weight.keys(), key=lambda x: (x is None, x or 0)):
        if wl is not None:
            lines.append(f"\n【权重 {wl} 级】")
        else:
            lines.append("\n【求美需求标签】")

        # Sub-group by group_name within weight level
        current_group: str | None = None
        for cat in by_weight[wl]:
            gn = cat.group_name
            # If group_name differs from name, show the group header
            if gn and gn != cat.name and gn != current_group:
                lines.append(f"  {gn}：")
                current_group = gn
            elif not gn or gn == cat.name:
                current_group = None  # standalone, reset group

            active_tags = [t for t in cat.tags if t.is_active]
            prefix = "    " if (gn and gn != cat.name) else "  "
            if active_tags:
                examples = "、".join(f'"{t.name}"' for t in active_tags[:4])
                if len(active_tags) > 4:
                    examples += " 等"
                lines.append(f"{prefix}- {cat.name}（可选值如{examples}）")
            elif cat.name == "历史用的设备/原材料名称":
                lines.append(
                    f'{prefix}- {cat.name}（开放值；若客户明确未做过医美治疗可填"无"，否则从对话中提取具体设备或原材料名称）'
                )
            elif cat.name == NEGATIVE_PROJECT_TAG_CATEGORY:
                lines.append(
                    f'{prefix}- {cat.name}（开放值；仅在已明确存在治疗历史但未提取到负面项时，或已明确客户未做过医美治疗时，填"{NEGATIVE_PROJECT_EMPTY_VALUE}"；否则留空；提取到则填具体项目/设备/原材料名称）'
                )
            else:
                lines.append(f"{prefix}- {cat.name}（开放值，从对话中提取）")
    return "\n".join(lines)


def _build_hotword_list(groups: list[HotwordGroup]) -> str:
    """构建热词参考列表。"""
    lines: list[str] = []
    for g in groups:
        words = [w.word for w in g.words if w.is_active]
        if words:
            lines.append(f"- {g.name}：{'、'.join(words[:8])}")
            if len(words) > 8:
                lines[-1] += f" 等（共{len(words)}词）"
    return "\n".join(lines)


def _clean_text(value: object) -> str:
    return str(value or "").strip()


async def _load_tenant_sap_summary_prompt(db: AsyncSession, hospital_code: str | None) -> str:
    code = _clean_text(hospital_code)
    if not code:
        return ""

    tenant = (
        await db.execute(
            select(WecomTenant)
            .where(
                WecomTenant.default_hospital_code == code,
                WecomTenant.is_active.is_(True),
            )
            .order_by(WecomTenant.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if tenant is None:
        return ""

    template_name = _clean_text(tenant.sap_summary_template_name)
    template_version = _clean_text(tenant.sap_summary_template_version)
    template = _clean_text(tenant.sap_summary_template)
    prompt = _clean_text(tenant.sap_summary_prompt)
    if not (template or prompt):
        return ""

    header_bits = [f"机构：{tenant.name}（{code}）"]
    if template_name:
        header_bits.append(f"模板：{template_name}")
    if template_version:
        header_bits.append(f"版本：{template_version}")

    lines = [
        "【机构级 SAP 总结信息生成要求】",
        "；".join(header_bits),
        "以下配置仅用于生成 `sap_summary_materials`，不改变客户主诉、标准适应症、客户标签、推荐方案、成交状态等事实提取口径。",
        "若机构配置与系统默认 SAP 总结写作口径冲突，以机构配置优先；但仍必须基于录音证据，不得为了匹配模板补造事实。",
    ]
    if template:
        lines.append("机构模板：")
        lines.append(template)
    if prompt:
        lines.append("机构补充提示词：")
        lines.append(prompt)
    return "\n".join(lines)


async def build_system_prompt(db: AsyncSession, hospital_code: str | None = None) -> str:
    """从数据库配置构建完整的 SYSTEM_PROMPT。"""
    categories = await _load_tag_categories(db)
    hotword_groups = await _load_hotword_groups(db)
    reference_data = load_analysis_reference_data()

    tag_section = _build_tag_section(categories)
    hotword_section = _build_hotword_list(hotword_groups)

    from smart_badge_api.analysis.extraction_prompts import SYSTEM_PROMPT_TEMPLATE

    base_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        feature_objectives=reference_data.feature_objectives,
        indication_reference=reference_data.indication_reference,
        tag_categories=tag_section,
        hotword_reference=hotword_section,
    )
    tenant_sap_summary_prompt = await _load_tenant_sap_summary_prompt(db, hospital_code)
    if tenant_sap_summary_prompt:
        return f"{base_prompt}\n\n{tenant_sap_summary_prompt}"
    return base_prompt
