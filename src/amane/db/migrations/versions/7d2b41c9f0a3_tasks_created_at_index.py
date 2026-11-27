"""tasks 列表索引 (created_at DESC, id)

任务列表默认按 ``created_at DESC, id ASC`` 排序. 没有索引时 SQLite 必须全表扫描再建
临时 B-TREE, 把 ``payload``/``result`` 两列 JSON 一起读出来; 表一大就退化成磁盘随机读.

Revision ID: 7d2b41c9f0a3
Revises: ceee2c1190f2
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d2b41c9f0a3"
down_revision: str | None = "ceee2c1190f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_tasks_created_at_id"


def upgrade() -> None:
    op.create_index(_INDEX, "tasks", [sa.text("created_at DESC"), "id"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="tasks")
