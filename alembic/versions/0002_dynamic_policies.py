"""add merchant policies and modification audit fields

Revision ID: 0002_dynamic_policies
Revises: 0001_initial
Create Date: 2026-08-27

"""
import importlib
from typing import Any, cast

import sqlalchemy as sa

op = cast(Any, importlib.import_module("alembic.op"))

revision: str = "0002_dynamic_policies"
down_revision: str = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "merchant_policies",
        sa.Column("merchant_id", sa.String(length=100), nullable=False),
        sa.Column(
            "max_discount_allowed",
            sa.Numeric(precision=5, scale=2),
            nullable=False,
            server_default=sa.text("10.00"),
        ),
        sa.PrimaryKeyConstraint("merchant_id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO merchant_policies (merchant_id, max_discount_allowed) "
            "VALUES ('default', 10.00)"
        )
    )
    op.add_column(
        "transactions",
        sa.Column("merchant_id", sa.String(length=100), nullable=True),
    )
    op.execute(sa.text("UPDATE transactions SET merchant_id = 'default'"))
    op.alter_column("transactions", "merchant_id", nullable=False)
    op.create_index("ix_transactions_merchant_id", "transactions", ["merchant_id"])
    op.add_column(
        "audit_logs",
        sa.Column(
            "action", sa.String(length=20), nullable=False, server_default="APPROVED"
        ),
    )
    op.add_column(
        "audit_logs",
        sa.Column("modified_parameters", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audit_logs", "modified_parameters")
    op.drop_column("audit_logs", "action")
    op.drop_index("ix_transactions_merchant_id", table_name="transactions")
    op.drop_column("transactions", "merchant_id")
    op.drop_table("merchant_policies")
