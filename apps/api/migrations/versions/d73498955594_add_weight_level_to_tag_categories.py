"""add_weight_level_to_tag_categories

Revision ID: d73498955594
Revises: f2c7d9e4a1b3
Create Date: 2026-03-26 18:07:23.674126

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd73498955594'
down_revision: Union[str, Sequence[str], None] = 'f2c7d9e4a1b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tag_categories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('weight_level', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tag_categories', schema=None) as batch_op:
        batch_op.drop_column('weight_level')

    with op.batch_alter_table('risk_records', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_risk_records_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_risk_records_staff_id'), ['staff_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_risk_records_severity'), ['severity'], unique=False)
        batch_op.create_index(batch_op.f('ix_risk_records_created_at'), ['created_at'], unique=False)

    with op.batch_alter_table('quality_dimensions', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_quality_dimensions_rule_group_id_rule_groups'), type_='foreignkey')

    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_customers_external_customer_code'))
        batch_op.create_index(batch_op.f('ix_customers_external_customer_code'), ['external_customer_code'], unique=False)
        batch_op.create_unique_constraint(batch_op.f('uq_customers_external_customer_code'), ['external_customer_code'])

    # ### end Alembic commands ###
