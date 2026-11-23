"""library keep source dirs

Revision ID: 5c2d81f0a9b3
Revises: 3a7c9e12b4d8
Create Date: 2026-11-23

库级开关: 文件搬走后是否保留空的来源目录.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5c2d81f0a9b3"
down_revision: str | Sequence[str] | None = "3a7c9e12b4d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("libraries") as batch:
        batch.add_column(sa.Column("keep_source_dirs", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("libraries") as batch:
        batch.drop_column("keep_source_dirs")
