"""合并后观察期状态 + 事故指派人

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-30

OBSERVING 是新增的枚举值：PostgreSQL 的 ALTER TYPE ADD VALUE 不能在事务块里执行，
用 autocommit_block 单独提交。降级时不回滚枚举值（已被 incident 行引用，删值需要重建类型，
成本高于收益），只删除本次新增的列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE incident_state ADD VALUE IF NOT EXISTS 'OBSERVING'")
        op.execute("ALTER TYPE report_status ADD VALUE IF NOT EXISTS 'suppressed'")
    op.add_column("incident", sa.Column("assignee_id", sa.String(length=26), nullable=True))
    op.create_foreign_key("incident_assignee_id_fkey", "incident", "user", ["assignee_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("incident_assignee_id_fkey", "incident", type_="foreignkey")
    op.drop_column("incident", "assignee_id")
