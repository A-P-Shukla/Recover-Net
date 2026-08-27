"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-24

"""
import importlib
from typing import Sequence, Union
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

op = cast(Any, importlib.import_module("alembic.op"))

revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "transactions",
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_transaction_id", sa.String(length=64), nullable=True),
        sa.Column("user_email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=100), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=False),
        sa.Column("past_success_rate", sa.Float(), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("transaction_id"),
        sa.UniqueConstraint(
            "source_transaction_id",
            name="uq_transactions_source_transaction_id",
        ),
    )
    op.create_index("ix_transactions_user_email", "transactions", ["user_email"])
    op.create_index("ix_transactions_phone", "transactions", ["phone"])
    op.create_index("ix_transactions_error_code", "transactions", ["error_code"])

    op.create_table(
        "audit_logs",
        sa.Column("log_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("llm_proposed_action", sa.Text(), nullable=True),
        sa.Column("guardrail_decision", sa.Text(), nullable=True),
        sa.Column("final_status", sa.String(length=50), nullable=False),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.transaction_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("log_id"),
    )
    op.create_index("ix_audit_logs_transaction_id", "audit_logs", ["transaction_id"])
    op.create_index("ix_audit_logs_final_status", "audit_logs", ["final_status"])
    op.create_index("ix_audit_logs_timestamp", "audit_logs", ["timestamp"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_timestamp", table_name="audit_logs")
    op.drop_index("ix_audit_logs_final_status", table_name="audit_logs")
    op.drop_index("ix_audit_logs_transaction_id", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_transactions_error_code", table_name="transactions")
    op.drop_index("ix_transactions_phone", table_name="transactions")
    op.drop_index("ix_transactions_user_email", table_name="transactions")
    op.drop_table("transactions")
