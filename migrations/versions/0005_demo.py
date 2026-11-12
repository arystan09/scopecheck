"""Durable global public demo admission; preserves existing MVP records."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "demo_admission",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("admitted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_admitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name="demo_singleton"),
        sa.CheckConstraint(
            "admitted >= 0 AND admitted <= 100", name="demo_total_bound"
        ),
    )
    op.execute("INSERT INTO demo_admission (id, admitted) VALUES (1, 0)")


def downgrade() -> None:
    op.drop_table("demo_admission")
