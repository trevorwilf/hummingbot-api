"""Repository for the DERIVED purse read-model (hbpurseapi P3).

Safety rule 3 / ADDENDUM A5: this table is NON-AUTHORITATIVE — a mirror harvested
from the engine-owned journal at retirement, never a source of truth. So this
repository is deliberately read-mostly: one idempotent insert (used only by the
retirement-time harvest) plus read queries for the endpoints. There is no update
and no arbitrary write — the API never edits a purse record.
"""
from typing import List, Optional

from sqlalchemy import desc, nullslast, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import PurseSnapshot


def _lineage_order():
    """The ONE lineage-correct newest-first ordering (CDX-005/CLA-309), shared by BOTH the
    'latest' selection and the history list so those two surfaces can never disagree about
    which incarnation is newest (CDX-R01).

    Ordered by ``source_bot_run_id`` DESC first (the deploy chronology — a bot_run's
    auto-increment id rises with each new deploy/resume), then ``harvested_at`` DESC, then
    ``id`` DESC. This stops a LATE-ARCHIVED STALE instance from masquerading as the newest
    incarnation: an older run archived after a newer one has a LOWER run id, so it sorts
    BELOW the newer run even though its ``harvested_at`` is later. Ordering by
    ``harvested_at`` alone (the old behavior) got this wrong; ordering by the journal
    ``sequence`` across incarnations is INVALID (seq resets on a fresh journal — the
    discovery run proved it), so it is deliberately NOT used here.

    ``nullslast()`` is EXPLICIT, not incidental: SQLite sorts NULLs last under DESC but
    PostgreSQL (production) sorts them FIRST — without this an unresolved-lineage (NULL run
    id) row would jump to the newest slot on Postgres, the exact masquerade this ordering
    exists to prevent. Pinned so both dialects agree: NULL run ids fall to the bottom and a
    fleet of all-NULL rows degrades to exactly the previous newest-by-``harvested_at`` order.
    """
    return (
        nullslast(desc(PurseSnapshot.source_bot_run_id)),
        desc(PurseSnapshot.harvested_at),
        desc(PurseSnapshot.id),
    )


def _is_lineage_newer(incoming_run_id: Optional[int], existing_run_id: Optional[int]) -> bool:
    """True iff ``incoming_run_id`` is DEFINITIVELY a lineage-newer retirement than the
    stored row (CDX-R02). CONSERVATIVE: requires BOTH ids resolved and the incoming strictly
    greater — when either is NULL the lineage is ambiguous and we never touch the existing
    observation (never clobber on doubt, never fabricate an ordering). This is the guard that
    lets a genuinely newer degraded observation refresh a content-duplicate while a
    late-archived STALE retirement (lower run id) can never overwrite a newer one."""
    return (
        incoming_run_id is not None
        and existing_run_id is not None
        and incoming_run_id > existing_run_id
    )


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

    async def _get_existing_for_content(
        self, controller_id: str, purse_sha256: str
    ) -> Optional[PurseSnapshot]:
        """The stored row for exactly this (controller_id, purse_sha256), or None.

        Same content predicate as :meth:`snapshot_exists`, but returns the ROW so a
        content-duplicate harvest can refresh its per-retirement provenance (CDX-R02)."""
        stmt = select(PurseSnapshot).where(
            PurseSnapshot.controller_id == controller_id,
            PurseSnapshot.purse_sha256 == purse_sha256,
        ).limit(1)
        result = await self.session.execute(stmt)
        return result.scalars().first()

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
        # hbdash_api P3 harvest-honesty markers — ADDITIVE, nullable, observation-only.
        # Defaulted so pre-P3 callers/tests that do not supply them insert NULLs, exactly
        # like a snapshot harvested before these columns existed.
        latest_epoch_id: Optional[str] = None,
        reanchor_count: Optional[int] = None,
        source_degraded: Optional[bool] = None,
    ) -> Optional[PurseSnapshot]:
        """Insert a snapshot, or return None if one already exists for this content.

        Idempotent by (controller_id, purse_sha256): re-harvesting an unchanged
        journal creates NO second content row. The pre-check is the primary guard; the
        table's UNIQUE constraint is the schema-level backstop against a race (harvest
        runs in the single serial retirement background task, so the race is not expected).

        CDX-R02 — per-retirement provenance refresh on a content-duplicate. The journal
        bytes are immutable, so every DERIVED value (metrics, epoch markers) is identical
        across two harvests of the same content and needs no update. But ``source_degraded``
        and the lineage fields describe a PARTICULAR retirement and CAN differ (a resumed
        run that wrote nothing new but was degraded at stop). So when the SAME content is
        re-harvested by a DEFINITIVELY lineage-newer retirement (:func:`_is_lineage_newer`),
        refresh those per-retirement provenance fields on the existing row so a later
        degraded observation is not lost to dedup — while a late-archived STALE retirement
        (lower/again-null run id) can NEVER clobber a newer one. Still returns None: no new
        content row is created, so the harvest outcome stays ``skipped_duplicate``. Journal
        content (``records_json`` / ``derived_*``) is NEVER edited — provenance only, keeping
        the mirror append-only in content (safety rule 3 / ADDENDUM A5).
        """
        existing = await self._get_existing_for_content(controller_id, purse_sha256)
        if existing is not None:
            if _is_lineage_newer(source_bot_run_id, existing.source_bot_run_id):
                existing.source_degraded = source_degraded
                existing.source_bot_run_id = source_bot_run_id
                existing.source_instance_name = source_instance_name
                await self.session.flush()
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
            latest_epoch_id=latest_epoch_id,
            reanchor_count=reanchor_count,
            source_degraded=source_degraded,
        )
        self.session.add(snapshot)
        await self.session.flush()
        await self.session.refresh(snapshot)
        return snapshot

    async def get_latest_for_controller(self, controller_id: str) -> Optional[PurseSnapshot]:
        """The newest snapshot for a controller, by LINEAGE-CORRECT chronology (CDX-R01).

        Uses the SAME :func:`_lineage_order` as :meth:`get_history_for_controller`, so the
        primary current-state surfaces (``GET /purse/{id}`` + ``/activity`` + ``/timeseries``,
        which all consume this) can never disagree with the history list about which
        incarnation is newest. A late-archived STALE instance (higher ``harvested_at`` but
        lower ``source_bot_run_id``) therefore does NOT masquerade as the current snapshot
        here either (CDX-005/CLA-309) — the defect the old ``harvested_at``-only ordering
        left active on these surfaces. ``harvested_at`` then ``id`` remain the tiebreakers,
        so two harvests in the same DB-clock tick still resolve to the later-inserted row and
        an all-NULL-run-id fleet degrades to the previous newest-by-harvest behavior.
        """
        stmt = (
            select(PurseSnapshot)
            .where(PurseSnapshot.controller_id == controller_id)
            .order_by(*_lineage_order())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_history_for_controller(
        self, controller_id: str, limit: int = 100, offset: int = 0
    ) -> List[PurseSnapshot]:
        """All snapshots for a controller, in LINEAGE-CORRECT chronology (CDX-005/CLA-309).

        Uses the shared :func:`_lineage_order` (see its docstring for the full rationale):
        ``source_bot_run_id`` DESC (deploy chronology) then ``harvested_at`` DESC then ``id``
        DESC, with ``nullslast`` pinned so a late-archived stale instance cannot masquerade
        as the newest incarnation and an all-NULL-run-id fleet degrades to the previous
        newest-by-harvest order. This is the IDENTICAL ordering :meth:`get_latest_for_controller`
        now uses, so the history and the current-state surfaces can never disagree (CDX-R01).
        """
        stmt = (
            select(PurseSnapshot)
            .where(PurseSnapshot.controller_id == controller_id)
            .order_by(*_lineage_order())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()
