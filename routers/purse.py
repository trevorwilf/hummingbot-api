"""Purse read-model endpoints (hbpurseapi P3) — DERIVED, read-only.

Two GET endpoints over the harvested purse snapshots. There is NO write endpoint by
design: the API never authors a purse record (safety rule 3 / ADDENDUM A5). Both
responses are clearly labeled non-authoritative and always carry the stale-mirror
detectors (sha256 + sequence + harvested_at) so a stale snapshot can never
masquerade as the live journal (required change #9). The raw ``records_json`` is
persisted for audit/recovery but is NOT exposed here — the surface is derived-only.
"""
import json
import logging
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Query

from database import AsyncDatabaseManager, PurseSnapshotRepository
from database.models import PurseSnapshot
from deps import get_database_manager
from models.purse import (
    PurseActivityResponse,
    PurseDerivedMetrics,
    PurseHistoryEntry,
    PurseHistoryResponse,
    PurseProvenance,
    PurseSnapshotResponse,
    PurseTimeseriesPoint,
    PurseTimeseriesResponse,
)
from services.purse_envelope_contract import classify_purse_envelope
from services.purse_read_model import compute_activity, compute_timeseries

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Purse (derived read-model)"], prefix="/purse")


def _earned_total_pct(contributed: str, earned_total: str) -> str:
    """Inception return in percent (earned_total / contributed * 100).

    Presentation-only — NOT part of the pinned contract-v1 derived set (it mirrors the
    engine's ``_purse_status_block``). Computed from the STORED strings at response
    time so snapshots harvested before this field existed gain it without a migration.
    '0' when nothing was contributed (or on a malformed stored value — this is a
    derived display figure, never worth a 500).
    """
    try:
        c = Decimal(contributed)
        e = Decimal(earned_total)
    except (InvalidOperation, TypeError, ValueError):
        return "0"
    if not c.is_finite() or not e.is_finite() or c <= 0:
        return "0"
    return str(e / c * Decimal("100"))


def _derived(row: PurseSnapshot) -> PurseDerivedMetrics:
    return PurseDerivedMetrics(
        contributed=row.derived_contributed,
        withdrawn=row.derived_withdrawn,
        earned_realized=row.derived_earned_realized,
        earned_total=row.derived_earned_total,
        earned_total_pct=_earned_total_pct(row.derived_contributed, row.derived_earned_total),
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


def _snapshot_not_found(controller_id: str) -> HTTPException:
    """The shared 404 for a controller with no harvested snapshot.

    One source of the 404 so every purse route (``get_purse`` and the derived
    activity/timeseries siblings) reports it BYTE-IDENTICALLY and can never drift.
    A derived surface reports only what it holds, never a fabricated zero.
    """
    return HTTPException(
        status_code=404,
        detail=(
            f"No purse snapshot harvested for controller '{controller_id}'. Snapshots are "
            f"harvested when a bot retires (its container has exited, before its data is "
            f"archived); a controller that has never retired (or carries no purse journal) "
            f"has none."
        ),
    )


def _load_validated_journal(row: PurseSnapshot, controller_id: str) -> dict:
    """Parse + re-validate the stored journal bytes before computing over them.

    The bytes passed the P2 envelope at harvest (the harvest never stores an invalid
    journal); re-validating here — with the URL ``controller_id`` as the canonical id
    and the journal's OWN resolved ``controller_name``/``trading_pair`` — re-confirms
    ``compute_*``'s structural preconditions (records[0] an opening_epoch, seq order,
    per-kind money fields) still hold. A corrupted/tampered stored row is then a clean
    500, not silently-wrong derived numbers. A legitimately harvested row always
    re-validates (identical bytes, identical contract), so this never 500s a healthy
    snapshot. ``records_json`` is consumed here and NEVER placed on the response.
    """
    try:
        payload = json.loads(row.records_json)
    except (ValueError, TypeError) as exc:
        logger.error(
            "purse read-model: stored journal for %r is unparseable JSON: %s", controller_id, exc
        )
        raise HTTPException(status_code=500, detail="stored purse journal is corrupt (unparseable)")
    staged_config = {}
    if isinstance(payload, dict):
        staged_config = {
            "controller_name": payload.get("controller_name"),
            "trading_pair": payload.get("trading_pair"),
        }
    verdict = classify_purse_envelope(
        payload, canonical_controller_id=controller_id, staged_config=staged_config
    )
    if not verdict.is_valid:
        logger.error(
            "purse read-model: stored journal for %r failed contract re-validation: %s",
            controller_id, verdict.reason,
        )
        raise HTTPException(
            status_code=500, detail="stored purse journal failed contract re-validation"
        )
    return payload


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
        raise _snapshot_not_found(controller_id)
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


@router.get("/{controller_id}/activity", response_model=PurseActivityResponse)
async def get_purse_activity(
    controller_id: str,
    db_manager: AsyncDatabaseManager = Depends(get_database_manager),
):
    """Booked-fill-event activity for a controller, from its newest harvested snapshot.

    Same newest-snapshot fetch + 404 as :func:`get_purse` (via the shared sources).
    Parses the harvested journal, re-validates the P2 envelope, then returns the P1
    activity read-model: ``booked_fill_events`` (BOOKED FILL EVENTS, not exchange
    trades — see ``metric_note``, safety invariant 6), the since-inception per-week
    rate (with its denominator defined), and the ``incarnation_id`` lineage key. The
    raw ``records_json`` is consumed to compute and NEVER placed on the response.
    """
    async with db_manager.get_session_context() as session:
        row = await PurseSnapshotRepository(session).get_latest_for_controller(controller_id)
    if row is None:
        raise _snapshot_not_found(controller_id)
    payload = _load_validated_journal(row, controller_id)
    activity = compute_activity(payload, source_bot_run_id=row.source_bot_run_id)
    return PurseActivityResponse(
        controller_id=controller_id,
        booked_fill_events=activity.booked_fill_events,
        first_journal_ts=activity.first_journal_ts,
        last_journal_ts=activity.last_journal_ts,
        weeks_since_inception=str(activity.weeks_since_inception),
        booked_fills_per_week_since_inception=(
            str(activity.booked_fills_per_week)
            if activity.booked_fills_per_week is not None
            else None
        ),
        incarnation_id=activity.incarnation_id,
        provenance=_provenance(row),
    )


@router.get("/{controller_id}/timeseries", response_model=PurseTimeseriesResponse)
async def get_purse_timeseries(
    controller_id: str,
    db_manager: AsyncDatabaseManager = Depends(get_database_manager),
):
    """Authoritative journal-time equity/flows series for a controller (hbdash_api P2).

    Same newest-snapshot fetch + 404 as :func:`get_purse` (via the shared sources).
    Parses the harvested journal, re-validates the P2 envelope, then returns the P2
    timeseries read-model: one point at each checkpoint (an in-epoch sample) and each
    opening_epoch/reseed_epoch/reanchor (a re-baseline, flagged via ``boundary``), in
    journal-time (each point's ``ts`` is the record's own accounting time, NOT
    ``harvested_at`` — the CLA-M02 fix), segmentable by ``incarnation_id``/``boundary``
    (CLA-2A-05). Money is decimal strings; the ``note`` states it is a harvested
    snapshot, not a live tail. The raw ``records_json`` is consumed and NEVER exposed.
    """
    async with db_manager.get_session_context() as session:
        row = await PurseSnapshotRepository(session).get_latest_for_controller(controller_id)
    if row is None:
        raise _snapshot_not_found(controller_id)
    payload = _load_validated_journal(row, controller_id)
    points = compute_timeseries(payload, source_bot_run_id=row.source_bot_run_id)
    return PurseTimeseriesResponse(
        controller_id=controller_id,
        points=[
            PurseTimeseriesPoint(
                ts=p.ts,
                seq=p.seq,
                epoch_id=p.epoch_id,
                incarnation_id=p.incarnation_id,
                boundary=p.boundary,
                owned_quote=str(p.owned_quote),
                owned_base=str(p.owned_base),
                reference_price=str(p.reference_price),
                equity_quote=str(p.equity_quote),
                contributed=str(p.contributed),
                withdrawn=str(p.withdrawn),
                earned_total=str(p.earned_total),
                earned_realized=str(p.earned_realized),
            )
            for p in points
        ],
        provenance=_provenance(row),
    )
