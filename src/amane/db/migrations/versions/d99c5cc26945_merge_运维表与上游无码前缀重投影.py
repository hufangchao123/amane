"""merge: 运维表与上游无码前缀重投影

Revision ID: d99c5cc26945
Revises: 9c3f21a7de54, dc04a8ae624e
Create Date: 2026-11-23 17:22:24.224136
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "d99c5cc26945"
down_revision: str | None = ("9c3f21a7de54", "dc04a8ae624e")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
