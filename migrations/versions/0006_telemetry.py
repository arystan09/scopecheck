"""Persist enqueue trace context; no changes to evaluation evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("run_jobs", sa.Column("trace_parent", sa.String(55), nullable=True))


def downgrade() -> None:
    op.drop_column("run_jobs", "trace_parent")
