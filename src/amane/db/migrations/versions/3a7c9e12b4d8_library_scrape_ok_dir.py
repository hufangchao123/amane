"""library scrape ok dir

Revision ID: 3a7c9e12b4d8
Revises: 1f4b0a9e77c2
Create Date: 2026-11-23

刮削后转移的三个目录都可空: 成功目录加回来, 只填成功与失败目录时库根就是待整理目录.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "3a7c9e12b4d8"
down_revision: str | Sequence[str] | None = "1f4b0a9e77c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("libraries") as batch:
        batch.add_column(sa.Column("scrape_ok_dir", sqlmodel.AutoString(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("libraries") as batch:
        batch.drop_column("scrape_ok_dir")
