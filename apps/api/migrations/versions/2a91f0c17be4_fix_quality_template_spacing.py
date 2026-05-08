"""repair quality template content after sop cleanup

Revision ID: 2a91f0c17be4
Revises: f18b2a4c9d0e
Create Date: 2026-04-16 04:25:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "2a91f0c17be4"
down_revision: Union[str, Sequence[str], None] = "f18b2a4c9d0e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_QUALITY_TEMPLATE_NAME = "接诊质量评估包"
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

    op.execute(
        sa.update(summary_templates)
        .where(summary_templates.c.name == _QUALITY_TEMPLATE_NAME)
        .values(content=_FLOW_QUALITY_TEMPLATE_CONTENT)
    )


def downgrade() -> None:
    # The previous revision now writes the same canonical content.
    # This repair migration only corrects databases that had already run
    # the older buggy string replacement.
    return None
