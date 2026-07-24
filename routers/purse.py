"""Purse read-model endpoints (hbpurseapi P3) — DERIVED, read-only.

Two GET endpoints over the harvested purse snapshots. There is NO write endpoint by
design: the API never authors a purse record (safety rule 3 / ADDENDUM A5). Both
responses are clearly labeled non-authoritative and always carry the stale-mirror
detectors (sha256 + sequence + harvested_at) so a stale snapshot can never
masquerade as the live journal (required change #9). The raw ``records_json`` is
persisted for audit/recovery but is NOT exposed here — the surface is derived-only.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from database import AsyncDatabaseManager, PurseSnapshotRepository
from database.models import PurseSnapshot
from deps import get_database_manager
from models.purse import (
    PurseDerivedMetrics,
    PurseHistoryEntry,
    PurseHistoryResponse,
    PurseProvenance,
    PurseSnapshotResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Purse (derived read-model)"], prefix="/purse")


def _derived(row: PurseSnapshot) -> PurseDerivedMetrics:
    return PurseDerivedMetrics(
        contributed=row.derived_contributed,
        withdrawn=row.derived_withdrawn,
        earned_realized=row.derived_earned_realized,
        earned_total=row.derived_earned_total,
        unrealized=row.derived_unrealized,
        drift=row.derived_drift,
        reference_price_used=row.reference_price_used,
        opening_basis_quality=row.opening_basis_quality,
    )


def _provenance(row: PurseSnapshot) -> PurseProvenance:
    return PurseProvenance(
        harvested_at=row.harvested_at,
        source_instance_name=row.source_instance_name,
        source_bot_run_id=row.source_bot_run_id,
        purse_sha256=row.purse_sha256,
        sequence=row.sequence,
    )


@router.get("/{controller_id}", response_model=PurseSnapshotResponse)
async def get_purse(
    controller_id: str,
    db_manager: AsyncDatabaseManager = Depends(get_database_manager),
):
    """Newest harvested purse snapshot for a controller (derived metrics + provenance).

    404 when no snapshot has been harvested for this controller — a derived surface
    reports only what it actually holds, never a fabricated zero.
    """
    async with db_manager.get_session_context() as session:
        row = await PurseSnapshotRepository(session).get_latest_for_controller(controller_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No purse snapshot harvested for controller '{controller_id}'. Snapshots are "
                f"harvested at a bot's verified retirement; a controller that has never retired "
                f"(or carries no purse journal) has none."
            ),
        )
    return PurseSnapshotResponse(
        controller_id=controller_id,
        derived=_derived(row),
        provenance=_provenance(row),
    )


@router.get("/{controller_id}/history", response_model=PurseHistoryResponse)
async def get_purse_history(
    controller_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db_manager: AsyncDatabaseManager = Depends(get_database_manager),
):
    """All harvested purse snapshots for a controller (newest first), derived-only.

    Excludes ``records_json`` (the raw journal): the history is a metrics/provenance
    time series, not a journal dump. An unknown controller yields an empty list, not
    a 404 — "no history" is a valid, non-exceptional answer for a list resource.
    """
    async with db_manager.get_session_context() as session:
        rows = await PurseSnapshotRepository(session).get_history_for_controller(
            controller_id, limit=limit, offset=offset
        )
    return PurseHistoryResponse(
        controller_id=controller_id,
        snapshots=[
            PurseHistoryEntry(derived=_derived(row), provenance=_provenance(row)) for row in rows
        ],
    )
