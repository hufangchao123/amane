"""ops transfer item size

Revision ID: 4d81c0f7b2ae
Revises: b7c4e0a15d92
Create Date: 2026-11-23

``transfer_items`` 记录每一轮见到的体积: 两轮相同才算写完, 半截搬走会让下游的 STRM 指向残缺文件.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4d81c0f7b2ae"
down_revision: str | Sequence[str] | None = "b7c4e0a15d92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("transfer_items") as batch:
        batch.add_column(sa.Column("size", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("transfer_items") as batch:
        batch.drop_column("size")
