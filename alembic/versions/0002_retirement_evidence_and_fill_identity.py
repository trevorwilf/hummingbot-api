"""retirement evidence (CDX-005) + scoped fill identity (CDX-006)

Revision ID: 0002_evidence_fill_identity
Revises: 0001_baseline
Create Date: 2026-07-16

The schema phases 7 and 8 added, carried as a delta on top of the deployed
baseline. Until this run these lived as ad-hoc `ALTER TABLE` statements
executed at every API startup (database/connection.py STARTUP_MIGRATIONS /
STARTUP_UNIQUE_INDEXES, removed in this revision's phase); they have never
run against the deployed database, which is why they are a real migration
rather than part of 0001.

Both halves are additive and safe on a populated table:
  * bot_runs.retirement_status lands NOT NULL DEFAULT 'UNVERIFIED', so every
    pre-existing STOPPED row is UNVERIFIED — CDX-005 requires exactly that
    (a legacy stop carries no evidence; blessing it would be fabrication).
  * the trades identity columns are NULLable: legacy fills have no scoped
    exchange identity and must not be forced into one. NULLs are distinct in
    a unique index on both sqlite and postgres, so no legacy row can collide.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_evidence_fill_identity"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- CDX-005: acknowledged-retirement evidence -------------------------
    op.add_column(
        "bot_runs",
        sa.Column(
            "retirement_status",
            sa.String(),
            server_default="UNVERIFIED",
            nullable=False,
        ),
    )
    op.add_column("bot_runs", sa.Column("retirement_evidence", sa.Text(), nullable=True))
    op.create_index(
        op.f("ix_bot_runs_retirement_status"), "bot_runs", ["retirement_status"], unique=False
    )

    # --- CDX-006: scoped fill identity ------------------------------------
    op.add_column("trades", sa.Column("account_name", sa.String(), nullable=True))
    op.add_column("trades", sa.Column("connector_name", sa.String(), nullable=True))
    op.add_column("trades", sa.Column("exchange_trade_id", sa.String(), nullable=True))
    op.create_index(op.f("ix_trades_account_name"), "trades", ["account_name"], unique=False)
    op.create_index(op.f("ix_trades_connector_name"), "trades", ["connector_name"], unique=False)
    op.create_index(
        op.f("ix_trades_exchange_trade_id"), "trades", ["exchange_trade_id"], unique=False
    )
    # The dedup constraint itself. A unique INDEX (not an ALTER-added UNIQUE
    # constraint) because it is the one form that is native, non-table-
    # rewriting and identically reflected on BOTH sqlite and postgres — and
    # it is what phase 8 already installed on pre-existing tables. The model's
    # __table_args__ declares the matching sa.Index so autogenerate stays
    # quiet; see tests/test_migrations.py schema-parity test.
    op.create_index(
        "uq_trade_scoped_exchange_trade_id",
        "trades",
        ["account_name", "connector_name", "exchange_trade_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_trade_scoped_exchange_trade_id", table_name="trades")
    op.drop_index(op.f("ix_trades_exchange_trade_id"), table_name="trades")
    op.drop_index(op.f("ix_trades_connector_name"), table_name="trades")
    op.drop_index(op.f("ix_trades_account_name"), table_name="trades")
    op.drop_column("trades", "exchange_trade_id")
    op.drop_column("trades", "connector_name")
    op.drop_column("trades", "account_name")
    op.drop_index(op.f("ix_bot_runs_retirement_status"), table_name="bot_runs")
    op.drop_column("bot_runs", "retirement_evidence")
    op.drop_column("bot_runs", "retirement_status")
