"""strategy subscriptions: owner user and STOPPED (unsubscribed) status

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("strategies", sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))
    op.create_index("ix_strategies_user_id", "strategies", ["user_id"])
    # existing strategies belong to the first admin (they were the platform's demo subscriptions)
    op.execute("UPDATE strategies SET user_id = (SELECT id FROM users WHERE role = 'ADMIN' ORDER BY id LIMIT 1)")
    op.drop_constraint("ck_strategy_status", "strategies", type_="check")
    op.create_check_constraint("ck_strategy_status", "strategies", "status in ('RUNNING','PAUSED','HALTED','STOPPED')")


def downgrade() -> None:
    op.drop_constraint("ck_strategy_status", "strategies", type_="check")
    op.execute("UPDATE strategies SET status = 'PAUSED' WHERE status = 'STOPPED'")
    op.create_check_constraint("ck_strategy_status", "strategies", "status in ('RUNNING','PAUSED','HALTED')")
    op.drop_index("ix_strategies_user_id", "strategies")
    op.drop_column("strategies", "user_id")
