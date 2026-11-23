"""删除 115 中转: 历史任务行与 transfer_items 表

中转整体挪到了下游的 STRM 工具 (faststrm 侧的「离线搬运」), 应用里不再有这个任务类型.
枚举成员删掉后库里留着的历史行会在枚举转换阶段抛 ``LookupError``, 让 ``GET /api/tasks`` 变成
500, 因此这里把那些行删掉, 并连同只服务于它的 ``transfer_items`` 表一起删除.

Revision ID: 9c3f21a7de54
Revises: 4d81c0f7b2ae
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "9c3f21a7de54"
down_revision: str | Sequence[str] | None = "4d81c0f7b2ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enum 列按成员名字落库 (``TRANSFER_115``); 手写 SQL 或旧版本可能写进小写值, 两种都删.
REMOVED_TYPES = ("TRANSFER_115", "transfer_115")


def upgrade() -> None:
    # 先清后继边, 否则删掉任务行会留下指向空号的 task_links.
    op.execute(
        sa.text(
            "DELETE FROM task_links WHERE parent_task_id IN (SELECT id FROM tasks WHERE type IN :types)"
            " OR child_task_id IN (SELECT id FROM tasks WHERE type IN :types)"
        ).bindparams(sa.bindparam("types", value=REMOVED_TYPES, expanding=True))
    )
    op.execute(
        sa.text("DELETE FROM tasks WHERE type IN :types").bindparams(
            sa.bindparam("types", value=REMOVED_TYPES, expanding=True)
        )
    )
    op.execute(
        sa.text("DELETE FROM schedules WHERE task_type IN :types").bindparams(
            sa.bindparam("types", value=REMOVED_TYPES, expanding=True)
        )
    )
    op.drop_index(op.f("ix_transfer_items_state"), table_name="transfer_items")
    op.drop_index(op.f("ix_transfer_items_fid"), table_name="transfer_items")
    op.drop_table("transfer_items")


def downgrade() -> None:
    """表可以重建, 中转记录无法恢复."""
    op.create_table(
        "transfer_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fid", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("file_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
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
