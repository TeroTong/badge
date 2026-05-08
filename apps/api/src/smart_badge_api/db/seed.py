from __future__ import annotations

import asyncio
import os

from sqlalchemy import select

from smart_badge_api.core.security import hash_password
from smart_badge_api.db.default_data import ensure_operation_center_defaults
from smart_badge_api.db.risk_defaults import ensure_risk_rule_defaults
from smart_badge_api.db.models import Hotword, HotwordGroup, Staff, User
from smart_badge_api.db.system_defaults import (
    LEGACY_SUPER_ADMIN_DISPLAY_NAME,
    SUPER_ADMIN_DISPLAY_NAME,
    ensure_system_management_defaults,
)
from smart_badge_api.db.session import _session_factory


DEFAULT_HOTWORD_GROUPS = [
    {
        "name": "竞品机构热词",
        "group_type": "competitor",
        "library_scope": "public",
        "source_label": "行业",
        "words": [
            "联合丽格", "华美", "艺星", "美莱", "伊美尔", "美联臣",
            "薇琳", "丽都", "铭医", "京韩", "瑞丽", "华韩",
            "鹏爱", "壹加壹", "康美", "美立方", "叶子", "紫馨",
        ],
    },
    {
        "name": "项目通用热词",
        "group_type": "project",
        "library_scope": "public",
        "source_label": "行业",
        "words": [
            "热玛吉", "超声炮", "水光", "双眼皮", "隆鼻", "玻尿酸", "胶原蛋白",
            "光子嫩肤", "皮秒", "超皮秒", "点阵激光", "热拉提", "欣菲聆",
            "英伦大提升", "嗨体", "少女针", "童颜针", "线雕", "埋线", "自体脂肪",
            "吸脂", "面部填充", "丰唇", "瘦脸针", "肉毒素", "除皱",
            "祛斑", "祛痘", "果酸焕肤", "黄金微针", "射频", "脱毛",
        ],
    },
    {
        "name": "常见顾虑热词",
        "group_type": "concern",
        "library_scope": "public",
        "source_label": "运营",
        "words": [
            "太贵", "预算不够", "怕疼", "恢复期长", "不自然", "担心翻车",
            "副作用", "后遗症", "失败", "毁容", "过敏", "排异",
            "维持时间短", "效果不好", "需要多次", "留疤", "感染",
            "家人反对", "不敢做", "要考虑一下", "再看看", "对比一下",
        ],
    },
    {
        "name": "材料品牌热词",
        "group_type": "行业",
        "library_scope": "public",
        "source_label": "行业",
        "words": [
            "乔雅登", "瑞蓝", "艾莉薇", "伊婉", "海薇", "润百颜",
            "濡白天使", "熊猫针", "保妥适", "衡力", "吉适", "乐提葆",
            "Fotona", "飞顿", "赛诺秀", "科医人", "奇致",
            "薇旖美", "双美", "爱贝芙", "贝丽菲尔",
        ],
    },
    {
        "name": "通用服务热词",
        "group_type": "通用",
        "library_scope": "public",
        "source_label": "运营",
        "words": [
            "老带新", "转介绍", "优惠", "活动", "折扣", "分期", "免息",
            "会员", "积分", "套餐", "疗程价", "体验价", "首单",
            "复查", "术后回访", "面诊", "方案", "院长", "主任",
            "新氧", "美团", "大众点评", "小红书", "抖音",
        ],
    },
]


DEFAULT_STAFF = [
    {"name": "系统管理员", "role": "consultant", "permission_role": "system_admin"},
    {"name": "杜娟", "role": "consultant", "badge_id": "BADGE-001"},
]


def _sample_staff_enabled() -> bool:
    value = os.getenv("SMART_BADGE_ENABLE_SAMPLE_STAFF", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


async def ensure_hotwords() -> None:
    async with _session_factory() as db:
        changed = False

        for group_data in DEFAULT_HOTWORD_GROUPS:
            result = await db.execute(
                select(HotwordGroup).where(HotwordGroup.name == group_data["name"])
            )
            group = result.scalar_one_or_none()
            existing_words: set[str] = set()

            if group is None:
                group = HotwordGroup(
                    name=group_data["name"],
                    group_type=group_data["group_type"],
                    library_scope=group_data["library_scope"],
                    source_label=group_data["source_label"],
                )
                db.add(group)
                await db.flush()
            else:
                word_result = await db.execute(
                    select(Hotword).where(Hotword.group_id == group.id)
                )
                existing_words = {item.word for item in word_result.scalars().all()}

            for index, word in enumerate(group_data["words"]):
                if word in existing_words:
                    continue
                db.add(
                    Hotword(
                        group_id=group.id,
                        word=word,
                        weight=max(10, 100 - index * 10),
                    )
                )
                changed = True

        if changed:
            await db.commit()


async def ensure_staff() -> None:
    if not _sample_staff_enabled():
        return

    async with _session_factory() as db:
        result = await db.execute(select(Staff))
        existing_names = {item.name for item in result.scalars().all()}
        changed = False

        for staff_data in DEFAULT_STAFF:
            if staff_data["name"] in existing_names:
                continue
            db.add(Staff(**staff_data))
            changed = True

        if changed:
            await db.commit()


async def ensure_admin_user() -> None:
    async with _session_factory() as db:
        admin = (
            await db.execute(select(User).where(User.username == "admin"))
        ).scalar_one_or_none()
        if admin:
            if admin.display_name == LEGACY_SUPER_ADMIN_DISPLAY_NAME:
                admin.display_name = SUPER_ADMIN_DISPLAY_NAME
                await db.commit()
            return

        db.add(
            User(
                username="admin",
                hashed_password=hash_password("admin123"),
                display_name=SUPER_ADMIN_DISPLAY_NAME,
                role="super_admin",
            )
        )
        await db.commit()


async def seed() -> None:
    async with _session_factory() as db:
        await ensure_operation_center_defaults(db)
        await ensure_risk_rule_defaults(db)
        await ensure_system_management_defaults(db)

    await ensure_hotwords()
    await ensure_staff()
    await ensure_admin_user()

    print("Seed completed.")
    print("Default super admin: admin / admin123")


if __name__ == "__main__":
    asyncio.run(seed())
