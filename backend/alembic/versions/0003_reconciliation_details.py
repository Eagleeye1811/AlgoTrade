"""reconciliation_events.details (comparison snapshot, account check)

Revision ID: 0003
Revises: 0002
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("reconciliation_events",
                  sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"))
    # migrate details previously embedded in actions as {"action": "_details"}
    op.execute("""
        UPDATE reconciliation_events SET
          details = COALESCE((SELECT a->'details' FROM jsonb_array_elements(actions) a WHERE a->>'action' = '_details' LIMIT 1), '{}'::jsonb),
          actions = COALESCE((SELECT jsonb_agg(a) FROM jsonb_array_elements(actions) a WHERE a->>'action' IS DISTINCT FROM '_details'), '[]'::jsonb)
    """)


def downgrade() -> None:
    op.drop_column("reconciliation_events", "details")
