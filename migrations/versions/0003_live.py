"""Conservative append-only request reservations and case provenance."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("case_results", sa.Column("provenance", postgresql.JSONB()))
    op.create_table(
        "call_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("repetition", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("amount_microusd", sa.BigInteger(), nullable=False),
        sa.Column("price_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id", "case_id", "repetition"],
            [
                "case_attempts.workspace_id",
                "case_attempts.run_id",
                "case_attempts.case_id",
                "case_attempts.repetition",
            ],
        ),
        sa.UniqueConstraint(
            "workspace_id", "run_id", "case_id", "repetition", "sequence"
        ),
        sa.CheckConstraint("sequence BETWEEN 1 AND 4", name="reservation_sequence"),
        sa.CheckConstraint("amount_microusd > 0", name="reservation_positive"),
    )
    op.execute(
        "CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON call_reservations "
        "FOR EACH ROW EXECUTE FUNCTION scopecheck_reject_mutation()"
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM runs WHERE snapshot->>'mode' = 'live')")
    ):
        raise RuntimeError(
            "Cannot downgrade live runs; preserve reservations and evidence"
        )
    op.drop_table("call_reservations")
    op.drop_column("case_results", "provenance")
