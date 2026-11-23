"""purge rows whose task type no longer exists

移除功能时枚举成员一起删掉了, 但库里还留着那些任务与日程的历史行; SQLAlchemy 读回这些行时会
在枚举转换阶段抛 ``LookupError``, 于是 ``GET /api/tasks`` 直接 500. 历史行无法再用, 删掉比让
列表接口长期报错好.

Revision ID: b7c4e0a15d92
Revises: 5c2d81f0a9b3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c4e0a15d92"
down_revision: str | Sequence[str] | None = "5c2d81f0a9b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REMOVED_TASK_TYPES = ("backup", "trash_purge", "diagnose")
REMOVED_ROUTINE_TYPES = ("backup", "trash_purge", "transfer_115")


def upgrade() -> None:
    op.execute(
        sa.text("DELETE FROM tasks WHERE type IN :types").bindparams(
            sa.bindparam("types", value=REMOVED_TASK_TYPES, expanding=True)
        )
    )
    op.execute(
        sa.text("DELETE FROM schedules WHERE task_type IN :types").bindparams(
            sa.bindparam("types", value=REMOVED_ROUTINE_TYPES, expanding=True)
        )
    )


def downgrade() -> None:
    """删除的历史行无法恢复, 回退不改数据."""
