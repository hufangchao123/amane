"""library scrape input dir

Revision ID: 1f4b0a9e77c2
Revises: 7dac167dcded
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "1f4b0a9e77c2"
down_revision: str | None = "7dac167dcded"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """成功目录改由库根本身承担, 另设输入目录; SQLite 需要重建表, 因此走 batch."""
    with op.batch_alter_table("libraries") as batch:
        batch.add_column(sa.Column("scrape_input_dir", sqlmodel.AutoString(), nullable=True))
        batch.drop_column("scrape_ok_dir")


def downgrade() -> None:
    with op.batch_alter_table("libraries") as batch:
        batch.add_column(sa.Column("scrape_ok_dir", sqlmodel.AutoString(), nullable=True))
        batch.drop_column("scrape_input_dir")
