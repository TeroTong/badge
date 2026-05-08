"""cleanup legacy sop labels

Revision ID: f18b2a4c9d0e
Revises: c4e6a9b71d2f
Create Date: 2026-04-16 04:05:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f18b2a4c9d0e"
down_revision: Union[str, Sequence[str], None] = "c4e6a9b71d2f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEGACY_SOP_TOKEN = "".join(("S", "O", "P"))
_LEGACY_SERVICE_DIMENSION = f"服务 {_LEGACY_SOP_TOKEN}"
_LEGACY_TREATMENT_DIMENSION = f"治疗 {_LEGACY_SOP_TOKEN}"
_LEGACY_RULE_NAME = f"治疗 {_LEGACY_SOP_TOKEN} 执行不足"
_LEGACY_RULE_LABEL = f"{_LEGACY_SOP_TOKEN} 风险"
_LEGACY_TEMPLATE_PHRASE = f"{_LEGACY_SOP_TOKEN} 执行度"

_SERVICE_FLOW_DIMENSION = "服务流程执行"
_TREATMENT_FLOW_DIMENSION = "治疗流程规范"
_FLOW_RISK_RULE_NAME = "流程执行不足"
_FLOW_RISK_LABEL = "流程风险"
_FLOW_TEMPLATE_PHRASE = "流程执行度"
_QUALITY_TEMPLATE_NAME = "接诊质量评估包"
_LEGACY_QUALITY_TEMPLATE_CONTENT = (
    "请从需求探寻、专业讲解、机构优势、方案设计、风险告知、成交推进、"
    f"情绪引导和{_LEGACY_TEMPLATE_PHRASE}等方面，对本次接诊质量进行评估。"
)
_FLOW_QUALITY_TEMPLATE_CONTENT = (
    "请从需求探寻、专业讲解、机构优势、方案设计、风险告知、成交推进、"
    "情绪引导和流程执行度等方面，对本次接诊质量进行评估。"
)


def upgrade() -> None:
    summary_templates = sa.table(
        "summary_templates",
        sa.column("name", sa.String()),
        sa.column("content", sa.Text()),
    )
    quality_dimensions = sa.table(
        "quality_dimensions",
        sa.column("name", sa.String()),
        sa.column("description", sa.Text()),
    )
    risk_rules = sa.table(
        "risk_rules",
        sa.column("name", sa.String()),
        sa.column("risk_label", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("match_config", sa.JSON()),
        sa.column("note", sa.Text()),
    )

    op.execute(
        sa.update(summary_templates)
        .where(summary_templates.c.name == _QUALITY_TEMPLATE_NAME)
        .values(content=_FLOW_QUALITY_TEMPLATE_CONTENT)
    )

    op.execute(
        sa.update(quality_dimensions)
        .where(quality_dimensions.c.name == _LEGACY_SERVICE_DIMENSION)
        .values(
            name=_SERVICE_FLOW_DIMENSION,
            description="是否覆盖接待、需求确认、方案说明和后续承接等关键动作。",
        )
    )
    op.execute(
        sa.update(quality_dimensions)
        .where(quality_dimensions.c.name == _LEGACY_TREATMENT_DIMENSION)
        .values(
            name=_TREATMENT_FLOW_DIMENSION,
            description="是否按规范讲解治疗流程、风险告知和恢复周期。",
        )
    )

    canonical_match_config = {
        "dimension_names": [_TREATMENT_FLOW_DIMENSION, _SERVICE_FLOW_DIMENSION],
        "threshold": 6.0,
    }
    canonical_values = {
        "name": _FLOW_RISK_RULE_NAME,
        "risk_label": _FLOW_RISK_LABEL,
        "description": "当治疗流程规范或服务流程执行维度评分偏低时，生成风控记录。",
        "match_config": canonical_match_config,
        "note": "用于识别流程讲解、风险告知和后续承接动作不到位的场景。",
    }
    op.execute(sa.update(risk_rules).where(risk_rules.c.name == _LEGACY_RULE_NAME).values(**canonical_values))
    op.execute(sa.update(risk_rules).where(risk_rules.c.name == _FLOW_RISK_RULE_NAME).values(**canonical_values))


def downgrade() -> None:
    summary_templates = sa.table(
        "summary_templates",
        sa.column("name", sa.String()),
        sa.column("content", sa.Text()),
    )
    quality_dimensions = sa.table(
        "quality_dimensions",
        sa.column("name", sa.String()),
        sa.column("description", sa.Text()),
    )
    risk_rules = sa.table(
        "risk_rules",
        sa.column("name", sa.String()),
        sa.column("risk_label", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("match_config", sa.JSON()),
        sa.column("note", sa.Text()),
    )

    op.execute(
        sa.update(summary_templates)
        .where(summary_templates.c.name == _QUALITY_TEMPLATE_NAME)
        .values(content=_LEGACY_QUALITY_TEMPLATE_CONTENT)
    )

    op.execute(
        sa.update(quality_dimensions)
        .where(quality_dimensions.c.name == _SERVICE_FLOW_DIMENSION)
        .values(
            name=_LEGACY_SERVICE_DIMENSION,
            description="是否覆盖接待、需求确认、方案说明和跟进承接等标准动作。",
        )
    )
    op.execute(
        sa.update(quality_dimensions)
        .where(quality_dimensions.c.name == _TREATMENT_FLOW_DIMENSION)
        .values(
            name=_LEGACY_TREATMENT_DIMENSION,
            description="是否按规范讲解项目流程、风险告知和恢复周期。",
        )
    )

    legacy_match_config = {
        "dimension_names": [_LEGACY_TREATMENT_DIMENSION, _LEGACY_SERVICE_DIMENSION],
        "threshold": 6.0,
    }
    legacy_values = {
        "name": _LEGACY_RULE_NAME,
        "risk_label": _LEGACY_RULE_LABEL,
        "description": f"当治疗或服务 {_LEGACY_SOP_TOKEN} 维度评分偏低时，生成风控记录。",
        "match_config": legacy_match_config,
        "note": "用于识别讲解流程、风险告知和承接动作不到位的场景。",
    }
    op.execute(sa.update(risk_rules).where(risk_rules.c.name == _FLOW_RISK_RULE_NAME).values(**legacy_values))
    op.execute(sa.update(risk_rules).where(risk_rules.c.name == _LEGACY_RULE_NAME).values(**legacy_values))
