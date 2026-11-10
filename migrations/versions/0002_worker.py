"""Worker leases, mutable lifecycle, and immutable per-case evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER immutable_record ON runs")
    for column in ("total_cases", "completed_cases"):
        op.add_column(
            "runs", sa.Column(column, sa.Integer(), nullable=False, server_default="0")
        )
    for column in ("started_at", "finished_at"):
        op.add_column("runs", sa.Column(column, sa.DateTime(timezone=True)))
    op.add_column("runs", sa.Column("error_code", sa.String(50)))
    op.execute(
        "UPDATE runs SET total_cases = jsonb_array_length(d.content->'cases') * "
        "(runs.snapshot->>'repetitions')::integer FROM dataset_versions d "
        "WHERE d.id = runs.dataset_id"
    )
    op.drop_constraint("run_queued_only", "runs", type_="check")
    op.create_check_constraint(
        "run_status", "runs", "status IN ('queued', 'running', 'completed', 'failed')"
    )
    op.execute("""
        CREATE FUNCTION scopecheck_guard_run_config() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'immutable run configuration' USING ERRCODE = '23514';
            END IF;
            IF (to_jsonb(NEW) - ARRAY['status','completed_cases',
                    'started_at','finished_at','error_code'])
                IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['status','completed_cases',
                    'started_at','finished_at','error_code']) THEN
                RAISE EXCEPTION 'immutable run configuration' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON runs "
        "FOR EACH ROW EXECUTE FUNCTION scopecheck_guard_run_config()"
    )
    op.drop_constraint("job_queued_only", "run_jobs", type_="check")
    op.create_check_constraint(
        "job_status",
        "run_jobs",
        "status IN ('queued', 'running', 'completed', 'failed')",
    )
    op.add_column("run_jobs", sa.Column("lease_owner", sa.Uuid()))
    op.add_column(
        "run_jobs",
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("run_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.create_index("ix_run_jobs_claim", "run_jobs", ["status", "created_at"])
    for table in ("case_attempts", "case_results"):
        columns = [
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
            sa.Column("status", sa.String(20), nullable=False),
        ]
        if table == "case_attempts":
            op.create_table(
                table,
                *columns,
                sa.Column("lease_generation", sa.Integer(), nullable=False),
                sa.Column("worker_version", sa.String(100), nullable=False),
                sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
                sa.Column("finished_at", sa.DateTime(timezone=True)),
                sa.ForeignKeyConstraint(
                    ["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"]
                ),
                sa.UniqueConstraint("workspace_id", "run_id", "case_id", "repetition"),
                sa.CheckConstraint("repetition >= 1", name="attempt_repetition"),
                sa.CheckConstraint(
                    "status IN ('running', 'completed', 'limited', 'failed')",
                    name="attempt_status",
                ),
            )
        else:
            op.create_table(
                table,
                *columns,
                sa.Column("score", postgresql.JSONB()),
                sa.Column("error_code", sa.String(50)),
                sa.ForeignKeyConstraint(
                    ["workspace_id", "run_id", "case_id", "repetition"],
                    [
                        "case_attempts.workspace_id",
                        "case_attempts.run_id",
                        "case_attempts.case_id",
                        "case_attempts.repetition",
                    ],
                ),
                sa.UniqueConstraint("workspace_id", "run_id", "case_id", "repetition"),
                sa.CheckConstraint("repetition >= 1", name="result_repetition"),
                sa.CheckConstraint(
                    "status IN ('completed', 'limited', 'failed')", name="result_status"
                ),
            )
    op.execute(
        "CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON case_results "
        "FOR EACH ROW EXECUTE FUNCTION scopecheck_reject_mutation()"
    )


def downgrade() -> None:
    # Reverting after execution would discard evidence and invalidate lifecycle state.
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM runs WHERE status <> 'queued')")
    ):
        raise RuntimeError("Cannot downgrade after execution; preserve evidence")
    op.drop_table("case_results")
    op.drop_table("case_attempts")
    op.execute("DROP TRIGGER immutable_record ON runs")
    op.execute("DROP FUNCTION scopecheck_guard_run_config()")
    for column in (
        "total_cases",
        "completed_cases",
        "started_at",
        "finished_at",
        "error_code",
    ):
        op.drop_column("runs", column)
    op.drop_constraint("run_status", "runs", type_="check")
    op.create_check_constraint("run_queued_only", "runs", "status = 'queued'")
    op.execute(
        "CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON runs "
        "FOR EACH ROW EXECUTE FUNCTION scopecheck_reject_mutation()"
    )
    op.drop_index("ix_run_jobs_claim", table_name="run_jobs")
    for column in ("lease_owner", "lease_generation", "lease_expires_at"):
        op.drop_column("run_jobs", column)
    op.drop_constraint("job_status", "run_jobs", type_="check")
    op.create_check_constraint("job_queued_only", "run_jobs", "status = 'queued'")
