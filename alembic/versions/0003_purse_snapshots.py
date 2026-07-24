"""purse read-model snapshots (hbpurseapi P3)

Revision ID: 0003_purse_snapshots
Revises: 0002_evidence_fill_identity
Create Date: 2026-07-23

Adds the ``purse_snapshots`` table — the DERIVED, NON-AUTHORITATIVE mirror of a
controller's purse journal, harvested at VERIFIED retirement before the archive
step can move/delete the instance ``data/`` dir (required change #9: archive
``rmtree`` must not lose inception history). Purely additive: a brand-new table,
no change to any existing table, so it is safe on the populated production
database. The API refuses to serve unless the schema is at this head (CDX-013),
so the read-model endpoints can rely on the table existing.

The ``derived_*`` money columns and ``reference_price_used`` are ``String`` (decimal
strings), NOT ``Numeric``: sqlite ``Numeric`` round-trips through float and loses
money precision, and the engine/ledger already persist money as strings. Kept in
sync with ``database/models.py`` PurseSnapshot; tests/test_migrations.py asserts
the schema-at-head matches the model exactly (autogenerate parity).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003_purse_snapshots"
down_revision: Union[str, None] = "0002_evidence_fill_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "purse_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("controller_id", sa.String(), nullable=False),
        sa.Column(
            "harvested_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("source_instance_name", sa.String(), nullable=False),
        sa.Column("source_bot_run_id", sa.Integer(), nullable=True),
        sa.Column("purse_sha256", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("records_json", sa.Text(), nullable=False),
        sa.Column("derived_contributed", sa.String(), nullable=False),
        sa.Column("derived_withdrawn", sa.String(), nullable=False),
        sa.Column("derived_earned_realized", sa.String(), nullable=False),
        sa.Column("derived_earned_total", sa.String(), nullable=False),
        sa.Column("derived_unrealized", sa.String(), nullable=False),
        sa.Column("derived_drift", sa.String(), nullable=False),
        sa.Column("reference_price_used", sa.String(), nullable=False),
        sa.Column("opening_basis_quality", sa.String(), nullable=True),
        # ON DELETE SET NULL (CDX-R04): a bot_run delete must not be blocked by a
        # derived snapshot, nor cascade-destroy it — the lineage link nulls out.
        sa.ForeignKeyConstraint(
            ["source_bot_run_id"], ["bot_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "controller_id", "purse_sha256", name="uq_purse_snapshot_controller_sha"
        ),
    )
    op.create_index(
        op.f("ix_purse_snapshots_controller_id"), "purse_snapshots", ["controller_id"], unique=False
    )
    op.create_index(
        op.f("ix_purse_snapshots_harvested_at"), "purse_snapshots", ["harvested_at"], unique=False
    )
    op.create_index(op.f("ix_purse_snapshots_id"), "purse_snapshots", ["id"], unique=False)
    op.create_index(
        op.f("ix_purse_snapshots_purse_sha256"), "purse_snapshots", ["purse_sha256"], unique=False
    )
    op.create_index(
        op.f("ix_purse_snapshots_source_bot_run_id"),
        "purse_snapshots",
        ["source_bot_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purse_snapshots_source_instance_name"),
        "purse_snapshots",
        ["source_instance_name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_purse_snapshots_source_instance_name"), table_name="purse_snapshots")
    op.drop_index(op.f("ix_purse_snapshots_source_bot_run_id"), table_name="purse_snapshots")
    op.drop_index(op.f("ix_purse_snapshots_purse_sha256"), table_name="purse_snapshots")
    op.drop_index(op.f("ix_purse_snapshots_id"), table_name="purse_snapshots")
    op.drop_index(op.f("ix_purse_snapshots_harvested_at"), table_name="purse_snapshots")
    op.drop_index(op.f("ix_purse_snapshots_controller_id"), table_name="purse_snapshots")
    op.drop_table("purse_snapshots")
