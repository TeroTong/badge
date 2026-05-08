"""add preference_profiles table

Revision ID: f14f7b7b0d21
Revises: cab9cc12ed8d
Create Date: 2026-03-20 19:05:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f14f7b7b0d21"
down_revision: Union[str, Sequence[str], None] = "cab9cc12ed8d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "preference_profiles" in inspector.get_table_names():
        return

    op.create_table(
        "preference_profiles",
        sa.Column("id", sa.String(length=12), nullable=False),
        sa.Column("scope_key", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_preference_profiles")),
        sa.UniqueConstraint("scope_key", name=op.f("uq_preference_profiles_scope_key")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("preference_profiles")
