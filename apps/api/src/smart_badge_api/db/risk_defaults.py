from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smart_badge_api.db.models import RiskRule


DEFAULT_RISK_RULES = [
    {
        "name": "低分接诊预警",
        "match_type": "overall_score_below",
        "severity": "high",
        "risk_label": "低分预警",
        "description": "当综合评分明显偏低时，自动生成风控记录，提醒尽快复盘这次接诊。",
        "match_config": {"threshold": 5.5},
        "note": "适合作为基础兜底规则，帮助运营快速识别高风险接诊。",
    },
    {
        "name": "流程执行不足",
        "match_type": "dimension_score_below",
        "severity": "medium",
        "risk_label": "流程风险",
        "description": "当治疗流程规范或服务流程执行维度评分偏低时，生成风控记录。",
        "match_config": {"dimension_names": ["治疗流程规范", "服务流程执行"], "threshold": 6.0},
        "note": "用于识别流程讲解、风险告知和后续承接动作不到位的场景。",
    },
    {
        "name": "价格顾虑未化解",
        "match_type": "concern_keyword",
        "severity": "medium",
        "risk_label": "价格敏感",
        "description": "当客户持续表达价格、预算或费用顾虑时，生成风控记录。",
        "match_config": {"keywords": ["价格", "预算", "贵", "费用", "分期"]},
        "note": "帮助追踪高价格敏感客户，提醒复盘价值表达是否充分。",
    },
    {
        "name": "恢复期与安全顾虑",
        "match_type": "concern_keyword",
        "severity": "high",
        "risk_label": "安全顾虑",
        "description": "当客户表达恢复期、肿胀、风险或副作用顾虑时，生成风控记录。",
        "match_config": {"keywords": ["恢复", "肿胀", "风险", "副作用", "反弹"]},
        "note": "用于识别安全感不足或风险告知需要补强的接诊。",
    },
]


async def ensure_risk_rule_defaults(db: AsyncSession) -> None:
    result = await db.execute(select(RiskRule))
    existing_names = {item.name for item in result.scalars().all()}
    changed = False

    for item in DEFAULT_RISK_RULES:
        if item["name"] in existing_names:
            continue
        db.add(RiskRule(**item))
        changed = True

    if changed:
        await db.commit()
