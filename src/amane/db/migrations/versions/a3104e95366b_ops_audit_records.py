"""ops: audit_records

Revision ID: a3104e95366b
Revises: 870726b9b9a6
Create Date: 2026-11-22 17:41:28.908463
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3104e95366b"
down_revision: str | None = "870726b9b9a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 只建留痕表. 自动生成还提议改动 metadata 的表达式索引与两个枚举列的存储形态,
    # 那些差异与本次改动无关, 因此丢弃.
    op.create_table(
        "audit_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("method", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("query", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("client", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_records_created_at"), "audit_records", ["created_at"], unique=False)
    op.create_index(op.f("ix_audit_records_method"), "audit_records", ["method"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_records_method"), table_name="audit_records")
    op.drop_index(op.f("ix_audit_records_created_at"), table_name="audit_records")
    op.drop_table("audit_records")
