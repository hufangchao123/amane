"""wash_choices: 人对某一组洗版候选的显式决定

自动生成时夹带了既有偏差 (``metadata`` 的两个表达式索引、``tasks.type`` / ``schedules.task_type``
的 Enum 形态), 与本次改动无关, 已按惯例剔除, 只保留新表.

Revision ID: ceee2c1190f2
Revises: d99c5cc26945
Create Date: 2026-11-23 22:52:40.327672
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ceee2c1190f2"
down_revision: str | None = "d99c5cc26945"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wash_choices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("action", sa.Enum("KEEP", "IGNORE", name="washchoiceaction"), nullable=False),
        sa.Column("keep_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("updated_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_wash_choices_group_key"), "wash_choices", ["group_key"], unique=True)
    op.create_index(op.f("ix_wash_choices_updated_at"), "wash_choices", ["updated_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_wash_choices_updated_at"), table_name="wash_choices")
    op.drop_index(op.f("ix_wash_choices_group_key"), table_name="wash_choices")
    op.drop_table("wash_choices")
