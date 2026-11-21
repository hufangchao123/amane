"""ops: transfer_items + wash_records

Revision ID: 416dd97d33a0
Revises: e06def65f72a
Create Date: 2026-11-22 02:01:18.169757
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "416dd97d33a0"
down_revision: str | None = "e06def65f72a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 自动生成的差异里还有 metadata 表达式索引与 tasks.type 的枚举形态, 那些是模型与既有迁移的
    # 既有偏差 (SQLite 上枚举本就落成 VARCHAR), 不属于本次改动, 因此只保留两张新表.
    op.create_table(
        "transfer_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fid", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("file_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "state",
            sa.Enum("PENDING", "MOVED", "SKIPPED", "FAILED", name="transferitemstate"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("moved_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
        sa.Column("updated_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_transfer_items_fid"), "transfer_items", ["fid"], unique=True)
    op.create_index(op.f("ix_transfer_items_state"), "transfer_items", ["state"], unique=False)

    op.create_table(
        "wash_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Enum("LOCAL", "STRM", name="washrecordkind"), nullable=False),
        sa.Column("group_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("kept_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("removed_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.Enum("TRASHED", "RESTORED", "PURGED", name="washrecordstate"),
            nullable=False,
        ),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("expires_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_wash_records_group_key", "wash_records", ["group_key", "kind"], unique=False)
    op.create_index(op.f("ix_wash_records_kind"), "wash_records", ["kind"], unique=False)
    op.create_index(op.f("ix_wash_records_state"), "wash_records", ["state"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_wash_records_state"), table_name="wash_records")
    op.drop_index(op.f("ix_wash_records_kind"), table_name="wash_records")
    op.drop_index("ix_wash_records_group_key", table_name="wash_records")
    op.drop_table("wash_records")
    op.drop_index(op.f("ix_transfer_items_state"), table_name="transfer_items")
    op.drop_index(op.f("ix_transfer_items_fid"), table_name="transfer_items")
    op.drop_table("transfer_items")
