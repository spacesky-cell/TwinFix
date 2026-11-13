"""Patch 多仓库补丁

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-04

附加仓库的补丁与主仓库分开存放；旧数据默认空对象，行为不变。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "patch",
        sa.Column(
            "repos",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("patch", "repos")
