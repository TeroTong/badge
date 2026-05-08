from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smart_badge_api.db.models import (
    QualityCheckpoint,
    QualityDimension,
    RuleGroup,
    SummaryTemplate,
    Tag,
    TagCategory,
)
from smart_badge_api.tag_catalog_reference import (
    legacy_tag_category_aliases,
    load_tag_catalog_definitions,
)


DEFAULT_RULE_GROUPS = [
    {
        "name": "通用接诊匹配规则",
        "detail": "适用于门店常规咨询、到诊记录和基础分析模板的默认规则组。",
        "note": "默认启用，建议作为新模板和新质检维度的基础规则组。",
    },
    {
        "name": "新客首诊规则",
        "detail": "适用于首次到诊、需求探索阶段较长的咨询场景。",
        "note": "重点关注需求挖掘、机构优势讲解和顾虑化解。",
    },
    {
        "name": "高价值客户规则",
        "detail": "适用于预算明确、成交意向较高、需要更强转化策略的客户。",
        "note": "重点关注方案对比、价值呈现和成交推进。",
    },
]


DEFAULT_SUMMARY_TEMPLATES = [
    {
        "name": "通用一句话总结",
        "template_type": "one_line_summary",
        "content": (
            "请用一句话总结本次接诊，覆盖客户核心诉求、推荐方案和当前成交状态，"
            "长度控制在 20 个字以内。"
        ),
        "rule_group_name": "通用接诊匹配规则",
    },
    {
        "name": "咨询详细总结模板",
        "template_type": "visit_summary",
        "content": (
            "请输出结构化咨询总结，至少包含以下章节：\n"
            "1. 客户背景与来访动机\n"
            "2. 核心诉求与深层顾虑\n"
            "3. 推荐方案与顾问表达亮点\n"
            "4. 异议处理与未成交风险\n"
            "5. 下一步跟进建议"
        ),
        "rule_group_name": "通用接诊匹配规则",
    },
    {
        "name": "接诊质量评估包",
        "template_type": "visit_quality",
        "content": (
            "请从需求探寻、专业讲解、机构优势、方案设计、风险告知、成交推进、"
            "情绪引导和流程执行度等方面，对本次接诊质量进行评估。"
        ),
        "rule_group_name": "新客首诊规则",
    },
]


DEFAULT_QUALITY_DIMENSIONS = [
    {
        "name": "老带新政策宣讲",
        "description": "是否主动介绍转介绍政策、推荐权益和邀约机制。",
        "weight": 1.2,
        "sort_order": 0,
        "rule_group_name": "高价值客户规则",
        "checkpoints": [
            ("转介绍权益", "是否清晰说明老带新权益和适用范围。", 1.0),
            ("推荐激励", "是否告知推荐成功后的奖励或服务升级。", 0.8),
        ],
    },
    {
        "name": "机构优势",
        "description": "是否充分讲清门店、品牌和平台优势。",
        "weight": 1.0,
        "sort_order": 1,
        "rule_group_name": "通用接诊匹配规则",
        "checkpoints": [
            ("品牌背书", "是否说明机构资质、品牌背景或口碑案例。", 1.0),
            ("服务保障", "是否说明术前术后服务与风险保障。", 0.8),
        ],
    },
    {
        "name": "医生优势",
        "description": "是否准确讲解医生资历、擅长项目与适配人群。",
        "weight": 1.0,
        "sort_order": 2,
        "rule_group_name": "通用接诊匹配规则",
        "checkpoints": [
            ("资历介绍", "是否介绍医生履历、认证或案例经验。", 1.0),
            ("方案适配", "是否说明医生和客户需求的匹配原因。", 0.8),
        ],
    },
    {
        "name": "服务流程执行",
        "description": "是否覆盖接待、需求确认、方案说明和后续承接等关键动作。",
        "weight": 1.1,
        "sort_order": 3,
        "rule_group_name": "通用接诊匹配规则",
        "checkpoints": [
            ("接待礼仪", "是否完成基础问候、身份确认和接待引导。", 0.8),
            ("跟进承接", "是否给出明确后续安排与责任人。", 0.8),
        ],
    },
    {
        "name": "治疗流程规范",
        "description": "是否按规范讲解治疗流程、风险告知和恢复周期。",
        "weight": 1.1,
        "sort_order": 4,
        "rule_group_name": "新客首诊规则",
        "checkpoints": [
            ("流程讲解", "是否完整介绍项目流程和时间安排。", 0.9),
            ("风险告知", "是否说明禁忌、风险和恢复预期。", 1.0),
        ],
    },
    {
        "name": "医美既往史",
        "description": "是否主动询问并记录过往医美经历、修复史和敏感信息。",
        "weight": 0.9,
        "sort_order": 5,
        "rule_group_name": "新客首诊规则",
        "checkpoints": [
            ("既往项目", "是否问清已做过的项目和时间。", 0.8),
            ("不良反应", "是否追问过往不适、翻车或修复史。", 0.8),
        ],
    },
    {
        "name": "经济状态",
        "description": "是否合理判断预算边界、支付方式和价格敏感度。",
        "weight": 1.0,
        "sort_order": 6,
        "rule_group_name": "高价值客户规则",
        "checkpoints": [
            ("预算确认", "是否确认客户预算区间和可接受价格。", 0.9),
            ("支付方式", "是否沟通分期、组合方案或支付节奏。", 0.8),
        ],
    },
    {
        "name": "需求探寻",
        "description": "是否通过追问明确主诉、场景需求和优先级。",
        "weight": 1.4,
        "sort_order": 7,
        "rule_group_name": "新客首诊规则",
        "checkpoints": [
            ("主诉明确", "是否确认客户最想解决的问题。", 1.0),
            ("场景追问", "是否追问客户工作、社交或拍照等触发场景。", 0.9),
        ],
    },
    {
        "name": "喜爱偏好",
        "description": "是否识别客户审美方向、风格偏好和接受边界。",
        "weight": 0.8,
        "sort_order": 8,
        "rule_group_name": "通用接诊匹配规则",
        "checkpoints": [
            ("风格偏好", "是否识别自然风、精致风等偏好。", 0.7),
            ("接受边界", "是否确认客户对恢复期、创伤和效果幅度的接受度。", 0.7),
        ],
    },
    {
        "name": "竞品情况",
        "description": "是否了解客户比较过的机构、项目和决策依据。",
        "weight": 0.9,
        "sort_order": 9,
        "rule_group_name": "高价值客户规则",
        "checkpoints": [
            ("竞品对比", "是否询问比较过的机构或医生。", 0.8),
            ("决策差异", "是否说明本机构方案与竞品的差异。", 0.8),
        ],
    },
]


# Old aesthetic-need tags removed. The active W1-W4 tag catalog is code-owned
# in tag_catalog_reference.py and no longer sourced from repository Excel files.
DEFAULT_TAG_CATEGORIES: list[dict] = []


async def ensure_rule_groups(db: AsyncSession) -> dict[str, RuleGroup]:
    result = await db.execute(select(RuleGroup))
    existing = {item.name: item for item in result.scalars().all()}
    changed = False

    for item in DEFAULT_RULE_GROUPS:
        if item["name"] in existing:
            continue
        rule_group = RuleGroup(**item)
        db.add(rule_group)
        existing[rule_group.name] = rule_group
        changed = True

    if changed:
        await db.commit()
        result = await db.execute(select(RuleGroup))
        existing = {item.name: item for item in result.scalars().all()}

    return existing


async def ensure_summary_templates(db: AsyncSession) -> None:
    rule_groups = await ensure_rule_groups(db)
    result = await db.execute(select(SummaryTemplate))
    existing_names = {item.name for item in result.scalars().all()}
    changed = False

    for item in DEFAULT_SUMMARY_TEMPLATES:
        if item["name"] in existing_names:
            continue
        payload = {
            "name": item["name"],
            "template_type": item["template_type"],
            "content": item["content"],
            "rule_group_id": rule_groups[item["rule_group_name"]].id,
        }
        db.add(SummaryTemplate(**payload))
        changed = True

    if changed:
        await db.commit()


async def ensure_quality_dimensions(db: AsyncSession) -> None:
    rule_groups = await ensure_rule_groups(db)
    result = await db.execute(
        select(QualityDimension).options(selectinload(QualityDimension.checkpoints))
    )
    existing_names = {item.name for item in result.scalars().all()}
    changed = False

    for item in DEFAULT_QUALITY_DIMENSIONS:
        if item["name"] in existing_names:
            continue
        dimension = QualityDimension(
            name=item["name"],
            description=item["description"],
            rule_group_id=rule_groups[item["rule_group_name"]].id,
            weight=item["weight"],
            sort_order=item["sort_order"],
        )
        db.add(dimension)
        await db.flush()

        for index, (name, description, score_weight) in enumerate(item["checkpoints"]):
            db.add(
                QualityCheckpoint(
                    dimension_id=dimension.id,
                    name=name,
                    description=description,
                    score_weight=score_weight,
                    sort_order=index,
                )
            )
        changed = True

    if changed:
        await db.commit()


async def ensure_tag_categories(db: AsyncSession) -> None:
    await ensure_rule_groups(db)
    definitions = load_tag_catalog_definitions()
    result = await db.execute(select(TagCategory).options(selectinload(TagCategory.tags)))
    categories = list(result.scalars().all())
    existing_by_name = {item.name: item for item in categories}
    changed = False

    aliases = legacy_tag_category_aliases()
    for old_name, new_name in aliases.items():
        old_category = existing_by_name.get(old_name)
        new_category = existing_by_name.get(new_name)
        if old_category is None:
            continue
        if new_category is None:
            old_category.name = new_name
            if (old_category.group_name or "").strip() == old_name:
                old_category.group_name = new_name
            existing_by_name.pop(old_name, None)
            existing_by_name[new_name] = old_category
            changed = True
            continue
        if old_category.is_active:
            old_category.is_active = False
            changed = True
        for tag in old_category.tags:
            if tag.is_active:
                tag.is_active = False
                changed = True

    definition_names = {item.name for item in definitions}

    for definition in definitions:
        category = existing_by_name.get(definition.name)
        if category is None:
            category = TagCategory(
                name=definition.name,
                description=definition.description,
                group_name=definition.group_name,
                weight_level=definition.weight_level,
                sort_order=definition.sort_order,
                is_active=True,
            )
            db.add(category)
            await db.flush()
            existing_by_name[definition.name] = category
            changed = True
        else:
            if category.description != definition.description:
                category.description = definition.description
                changed = True
            if category.group_name != definition.group_name:
                category.group_name = definition.group_name
                changed = True
            if category.weight_level != definition.weight_level:
                category.weight_level = definition.weight_level
                changed = True
            if category.sort_order != definition.sort_order:
                category.sort_order = definition.sort_order
                changed = True
            if not category.is_active:
                category.is_active = True
                changed = True

        category_tags = list(category.tags) if "tags" in category.__dict__ else []
        existing_tags_by_name = {tag.name: tag for tag in category_tags}
        active_option_names = set(definition.options)
        for index, option_name in enumerate(definition.options):
            tag = existing_tags_by_name.get(option_name)
            if tag is None:
                tag = Tag(category_id=category.id, name=option_name, sort_order=index, is_active=True)
                db.add(tag)
                category_tags.append(tag)
                existing_tags_by_name[option_name] = tag
                changed = True
                continue
            if tag.sort_order != index:
                tag.sort_order = index
                changed = True
            if not tag.is_active:
                tag.is_active = True
                changed = True

        for tag in category_tags:
            should_be_active = tag.name in active_option_names
            if tag.is_active != should_be_active:
                tag.is_active = should_be_active
                changed = True

    for category in categories:
        if category.name in definition_names:
            continue
        if category.is_active:
            category.is_active = False
            changed = True
        for tag in category.tags:
            if tag.is_active:
                tag.is_active = False
                changed = True

    if changed:
        await db.commit()


async def ensure_operation_center_defaults(db: AsyncSession) -> None:
    await ensure_rule_groups(db)
    await ensure_summary_templates(db)
    await ensure_quality_dimensions(db)
    await ensure_tag_categories(db)
