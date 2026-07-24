"""Repository for the DERIVED purse read-model (hbpurseapi P3).

Safety rule 3 / ADDENDUM A5: this table is NON-AUTHORITATIVE — a mirror harvested
from the engine-owned journal at retirement, never a source of truth. So this
repository is deliberately read-mostly: one idempotent insert (used only by the
retirement-time harvest) plus read queries for the endpoints. There is no update
and no arbitrary write — the API never edits a purse record.
"""
from typing import List, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import PurseSnapshot


class PurseSnapshotRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def snapshot_exists(self, controller_id: str, purse_sha256: str) -> bool:
        """True iff a snapshot for exactly this (controller_id, purse_sha256) exists.

        The idempotency predicate: the same journal content harvested twice must
        not create a second row. Keyed on the CONTENT hash, not just the
        controller, so a journal that GREW (new sha) is correctly treated as new.
        """
        stmt = select(PurseSnapshot.id).where(
            PurseSnapshot.controller_id == controller_id,
            PurseSnapshot.purse_sha256 == purse_sha256,
        ).limit(1)
        result = await self.session.execute(stmt)
        return result.first() is not None

    async def insert_snapshot_if_absent(
        self,
        *,
        controller_id: str,
        source_instance_name: str,
        source_bot_run_id: Optional[int],
        purse_sha256: str,
        sequence: int,
        records_json: str,
        derived_contributed: str,
        derived_withdrawn: str,
        derived_earned_realized: str,
        derived_earned_total: str,
        derived_unrealized: str,
        derived_drift: str,
        reference_price_used: str,
        opening_basis_quality: Optional[str],
    ) -> Optional[PurseSnapshot]:
        """Insert a snapshot, or return None if one already exists for this content.

        Idempotent by (controller_id, purse_sha256): re-harvesting an unchanged
        journal is a no-op. The pre-check is the primary guard; the table's UNIQUE
        constraint is the schema-level backstop against a race (harvest runs in the
        single serial retirement background task, so the race is not expected).
        """
        if await self.snapshot_exists(controller_id, purse_sha256):
            return None
        snapshot = PurseSnapshot(
            controller_id=controller_id,
            source_instance_name=source_instance_name,
            source_bot_run_id=source_bot_run_id,
            purse_sha256=purse_sha256,
            sequence=sequence,
            records_json=records_json,
            derived_contributed=derived_contributed,
            derived_withdrawn=derived_withdrawn,
            derived_earned_realized=derived_earned_realized,
            derived_earned_total=derived_earned_total,
            derived_unrealized=derived_unrealized,
            derived_drift=derived_drift,
            reference_price_used=reference_price_used,
            opening_basis_quality=opening_basis_quality,
        )
        self.session.add(snapshot)
        await self.session.flush()
        await self.session.refresh(snapshot)
        return snapshot

    async def get_latest_for_controller(self, controller_id: str) -> Optional[PurseSnapshot]:
        """The newest snapshot for a controller (by harvest time, id as tiebreak).

        ``id`` breaks a same-``harvested_at`` tie deterministically — two harvests
        in the same DB-clock tick (server_default now()) still resolve to the
        later-inserted row.
        """
        stmt = (
            select(PurseSnapshot)
            .where(PurseSnapshot.controller_id == controller_id)
            .order_by(desc(PurseSnapshot.harvested_at), desc(PurseSnapshot.id))
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_history_for_controller(
        self, controller_id: str, limit: int = 100, offset: int = 0
    ) -> List[PurseSnapshot]:
        """All snapshots for a controller, newest first (paginated)."""
        stmt = (
            select(PurseSnapshot)
            .where(PurseSnapshot.controller_id == controller_id)
            .order_by(desc(PurseSnapshot.harvested_at), desc(PurseSnapshot.id))
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()
