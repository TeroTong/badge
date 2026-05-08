"""add_group_name_to_tag_categories

Revision ID: e5a7b2c3d4f1
Revises: d73498955594
Create Date: 2026-03-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5a7b2c3d4f1'
down_revision: Union[str, Sequence[str], None] = 'd73498955594'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tag_categories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('group_name', sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tag_categories', schema=None) as batch_op:
        batch_op.drop_column('group_name')
