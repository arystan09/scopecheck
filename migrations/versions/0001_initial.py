"""Initial tenant-scoped immutable datasets/configurations and queued runs.

Revision ID: 0001
Revises: none (milestones 1 and 2 had no relational schema)
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def record_columns():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    ]


def workspace_column():
    return sa.Column(
        "workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "workspaces",
        *record_columns(),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
    )
    op.create_table(
        "api_tokens",
        *record_columns(),
        workspace_column(),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("workspace_id", "id"),
        sa.CheckConstraint("role IN ('reader', 'editor')", name="token_role"),
    )
    for table in ("dataset_versions", "assistant_configs"):
        op.create_table(
            table,
            *record_columns(),
            workspace_column(),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("content", postgresql.JSONB(), nullable=False),
            sa.UniqueConstraint("workspace_id", "id"),
            sa.UniqueConstraint("workspace_id", "content_hash"),
        )
    op.create_table(
        "runs",
        *record_columns(),
        workspace_column(),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("config_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.UniqueConstraint("workspace_id", "id"),
        sa.UniqueConstraint("workspace_id", "idempotency_key"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "dataset_id"],
            ["dataset_versions.workspace_id", "dataset_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "config_id"],
            ["assistant_configs.workspace_id", "assistant_configs.id"],
        ),
        sa.CheckConstraint("status = 'queued'", name="run_queued_only"),
    )
    op.create_table(
        "run_jobs",
        *record_columns(),
        workspace_column(),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.UniqueConstraint("workspace_id", "run_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"]
        ),
        sa.CheckConstraint("status = 'queued'", name="job_queued_only"),
    )
    op.execute("""
        CREATE FUNCTION scopecheck_reject_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'immutable record' USING ERRCODE = '23514';
        END $$
    """)
    for table in ("dataset_versions", "assistant_configs", "runs"):
        op.execute(
            f"CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION scopecheck_reject_mutation()"
        )


def downgrade() -> None:
    for table in (
        "run_jobs",
        "runs",
        "assistant_configs",
        "dataset_versions",
        "api_tokens",
        "workspaces",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION scopecheck_reject_mutation()")
