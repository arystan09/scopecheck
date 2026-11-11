"""Bounded recovery with retained history and append-only usage settlements."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, constraint in (("runs", "run_status"), ("run_jobs", "job_status")):
        op.drop_constraint(constraint, table, type_="check")
        op.alter_column(
            table, "status", type_=sa.String(32), existing_type=sa.String(20)
        )
        op.create_check_constraint(
            constraint,
            table,
            "status IN "
            "('queued','running','completed','failed','completed_with_errors','budget_exhausted')",
        )
    op.add_column("run_jobs", sa.Column("deadline_at", sa.DateTime(timezone=True)))
    op.execute(
        "UPDATE run_jobs j SET deadline_at = r.started_at + interval '300 seconds' "
        "FROM runs r WHERE r.id = j.run_id AND j.status = 'running'"
    )
    op.create_index("ix_run_jobs_expiry", "run_jobs", ["status", "lease_expires_at"])
    op.add_column(
        "case_attempts",
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "case_attempts",
        sa.Column("history", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.create_check_constraint(
        "attempt_number_bound", "case_attempts", "attempt_number BETWEEN 1 AND 2"
    )
    op.add_column(
        "call_reservations",
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
    )
    # PostgreSQL generated/truncated the original unnamed unique constraint name.
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints("call_reservations"):
        if set(constraint["column_names"]) == {
            "workspace_id",
            "run_id",
            "case_id",
            "repetition",
            "sequence",
        }:
            op.drop_constraint(constraint["name"], "call_reservations", type_="unique")
    op.create_unique_constraint(
        "reservation_attempt_sequence",
        "call_reservations",
        [
            "workspace_id",
            "run_id",
            "case_id",
            "repetition",
            "attempt_number",
            "sequence",
        ],
    )
    op.create_unique_constraint(
        "reservation_tenant_id", "call_reservations", ["workspace_id", "run_id", "id"]
    )
    op.create_check_constraint(
        "reservation_attempt_bound",
        "call_reservations",
        "attempt_number BETWEEN 1 AND 2",
    )
    op.create_table(
        "call_settlements",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("reservation_id", sa.Uuid(), nullable=False),
        sa.Column("amount_microusd", sa.BigInteger()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("outcome", sa.String(50), nullable=False),
        sa.Column("returned_model", sa.String()),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id", "reservation_id"],
            [
                "call_reservations.workspace_id",
                "call_reservations.run_id",
                "call_reservations.id",
            ],
        ),
        sa.UniqueConstraint("reservation_id"),
        sa.CheckConstraint(
            "amount_microusd IS NULL OR amount_microusd >= 0",
            name="settlement_nonnegative",
        ),
        sa.CheckConstraint(
            "(amount_microusd IS NULL AND input_tokens IS NULL AND output_tokens IS "
            "NULL) OR "
            "(amount_microusd IS NOT NULL AND input_tokens IS NOT NULL "
            "AND output_tokens IS NOT NULL AND input_tokens >= 0 AND output_tokens "
            ">= 0)",
            name="settlement_usage",
        ),
    )
    op.execute(
        "CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON call_settlements "
        "FOR EACH ROW EXECUTE FUNCTION scopecheck_reject_mutation()"
    )
    op.execute("""
        CREATE FUNCTION scopecheck_check_settlement() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.amount_microusd > (SELECT amount_microusd FROM call_reservations
                                     WHERE id=NEW.reservation_id) THEN
                RAISE EXCEPTION 'settlement exceeds reservation' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER settlement_bound BEFORE INSERT ON call_settlements "
        "FOR EACH ROW EXECUTE FUNCTION scopecheck_check_settlement()"
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM call_settlements) OR "
            "EXISTS(SELECT 1 FROM case_attempts WHERE attempt_number > 1) OR "
            "EXISTS(SELECT 1 FROM runs WHERE status IN "
            "('completed_with_errors','budget_exhausted'))"
        )
    ):
        raise RuntimeError("Cannot discard recovery or settlement evidence")
    op.drop_table("call_settlements")
    op.execute("DROP FUNCTION scopecheck_check_settlement()")
    op.drop_constraint(
        "reservation_attempt_sequence", "call_reservations", type_="unique"
    )
    op.drop_constraint("reservation_tenant_id", "call_reservations", type_="unique")
    op.drop_constraint("reservation_attempt_bound", "call_reservations", type_="check")
    op.drop_column("call_reservations", "attempt_number")
    op.create_unique_constraint(
        None,
        "call_reservations",
        ["workspace_id", "run_id", "case_id", "repetition", "sequence"],
    )
    op.drop_constraint("attempt_number_bound", "case_attempts", type_="check")
    op.drop_column("case_attempts", "history")
    op.drop_column("case_attempts", "attempt_number")
    op.drop_index("ix_run_jobs_expiry", table_name="run_jobs")
    op.drop_column("run_jobs", "deadline_at")
    for table, constraint in (("runs", "run_status"), ("run_jobs", "job_status")):
        op.drop_constraint(constraint, table, type_="check")
        op.alter_column(
            table, "status", type_=sa.String(20), existing_type=sa.String(32)
        )
        op.create_check_constraint(
            constraint, table, "status IN ('queued','running','completed','failed')"
        )
