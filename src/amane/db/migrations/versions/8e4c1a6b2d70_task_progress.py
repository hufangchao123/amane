"""任务进度快照

WS 的 task.progress 事件只到已打开的页面; 落三列才能在刷新后看到跑到哪了.

Revision ID: 8e4c1a6b2d70
Revises: 7d2b41c9f0a3
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8e4c1a6b2d70"
down_revision: str | Sequence[str] | None = "7d2b41c9f0a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("progress_current", sa.Integer(), "0"),
    ("progress_total", sa.Integer(), "0"),
    ("progress_message", sa.String(), ""),
)


def upgrade() -> None:
    for name, type_, default in _COLUMNS:
        op.add_column("tasks", sa.Column(name, type_, nullable=False, server_default=default))


def downgrade() -> None:
    for name, _type, _default in reversed(_COLUMNS):
        op.drop_column("tasks", name)
