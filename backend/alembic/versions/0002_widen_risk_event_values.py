"""widen risk_events.actual / allowed (block reasons can be long)

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("risk_events", "actual", type_=sa.Text(), existing_type=sa.String(64))
    op.alter_column("risk_events", "allowed", type_=sa.Text(), existing_type=sa.String(64))


def downgrade() -> None:
    op.alter_column("risk_events", "actual", type_=sa.String(64), existing_type=sa.Text(), postgresql_using="left(actual, 64)")
    op.alter_column("risk_events", "allowed", type_=sa.String(64), existing_type=sa.Text(), postgresql_using="left(allowed, 64)")
