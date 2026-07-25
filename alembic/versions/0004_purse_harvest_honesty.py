"""purse harvest-honesty markers (hbdash_api P3)

Revision ID: 0004_purse_harvest_honesty
Revises: 0003_purse_snapshots
Create Date: 2026-07-25

Adds three ADDITIVE, nullable columns to ``purse_snapshots`` so a retired run's
epoch boundaries and pre-stop degradation are answerable from the derived mirror
(CLA-2A-07/CLA-008 and CLA-007). Purely additive: no existing column is rewritten
or made non-nullable, so this is safe on the populated production database and
every existing snapshot simply carries NULL for the new fields (honest: the
markers were not harvested for rows written before this revision).

  * ``latest_epoch_id``  (String, nullable) — the epoch_id opened by the newest
    opening_epoch/reseed_epoch record (the currently-active accounting epoch).
  * ``reanchor_count``   (Integer, nullable) — the number of reanchor records.
  * ``source_degraded``  (Boolean, nullable) — whether the retiring instance's
    purse was flagged degraded before the stop; NULL when unavailable (never
    fabricated healthy).

All three are OBSERVATION-ONLY: they are populated best-effort at harvest and a
parse/lookup failure leaves them NULL — the harvest (hence the retirement) is
never blocked on them (safety invariant 5). Kept in sync with
``database/models.py`` PurseSnapshot; tests/test_migrations.py asserts the
schema-at-head matches the model exactly (autogenerate parity, including types and
nullability).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0004_purse_harvest_honesty"
down_revision: Union[str, None] = "0003_purse_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("purse_snapshots", sa.Column("latest_epoch_id", sa.String(), nullable=True))
    op.add_column("purse_snapshots", sa.Column("reanchor_count", sa.Integer(), nullable=True))
    op.add_column("purse_snapshots", sa.Column("source_degraded", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("purse_snapshots", "source_degraded")
    op.drop_column("purse_snapshots", "reanchor_count")
    op.drop_column("purse_snapshots", "latest_epoch_id")
