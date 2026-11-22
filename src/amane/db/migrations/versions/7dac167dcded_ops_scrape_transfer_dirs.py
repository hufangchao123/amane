"""ops scrape transfer dirs

Revision ID: 7dac167dcded
Revises: a3104e95366b
Create Date: 2026-11-22 20:57:51.589253
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7dac167dcded"
down_revision: str | None = "a3104e95366b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 只保留三列: autogenerate 另外提议的表达式索引与枚举形态还原是既有偏差, 与本次无关.
    # 开关给 server_default, 已有库按关闭处理.
    op.add_column("libraries", sa.Column("scrape_transfer", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("libraries", sa.Column("scrape_ok_dir", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column("libraries", sa.Column("scrape_fail_dir", sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    op.drop_column("libraries", "scrape_fail_dir")
    op.drop_column("libraries", "scrape_ok_dir")
    op.drop_column("libraries", "scrape_transfer")
