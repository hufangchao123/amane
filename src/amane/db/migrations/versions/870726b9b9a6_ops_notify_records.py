"""ops: notify_records

Revision ID: 870726b9b9a6
Revises: 416dd97d33a0
Create Date: 2026-11-22 17:29:42.675241
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "870726b9b9a6"
down_revision: str | None = "416dd97d33a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 只建通知记录表. 自动生成还提议改动 metadata 的表达式索引与两个枚举列的存储形态,
    # 那些差异来自模型声明与实际 DDL 的历史偏差, 与本次改动无关, 因此丢弃.
    op.create_table(
        "notify_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("channel", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("body", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_notify_records_created_at"), "notify_records", ["created_at"], unique=False)
    op.create_index(op.f("ix_notify_records_kind"), "notify_records", ["kind"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_notify_records_kind"), table_name="notify_records")
    op.drop_index(op.f("ix_notify_records_created_at"), table_name="notify_records")
    op.drop_table("notify_records")
