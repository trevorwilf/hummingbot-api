"""Pydantic models for the purse read-model router (hbpurseapi P3).

These describe a DERIVED, NON-AUTHORITATIVE surface (safety rule 3 / ADDENDUM A5).
Every response carries :data:`AUTHORITY_NOTE` and the three stale-mirror detectors
(``purse_sha256`` + ``sequence`` + ``harvested_at``) so a consumer can never mistake a
harvested snapshot for the live, engine-owned journal (required change #9).

Money values are decimal STRINGS end-to-end — the snapshot stores them as strings
(sqlite ``Numeric`` loses precision; the engine persists money as strings) and they
are surfaced as strings so JSON float coercion cannot corrupt a reported balance.
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

# The disclaimer stamped on every purse response. This surface is a mirror, not the
# source of truth: the engine-owned journal is the authority, and these figures were
# harvested at the source bot's retirement — they can be stale relative to a bot that
# has since been redeployed. Staleness is detectable via the provenance fields below.
AUTHORITY_NOTE = (
    "DERIVED, NON-AUTHORITATIVE. Harvested from the controller's purse journal at "
    "retirement; the engine-owned journal is the sole authority. These figures may be "
    "stale relative to a running bot — compare purse_sha256 / sequence / harvested_at "
    "against the live source to detect drift."
)


class PurseDerivedMetrics(BaseModel):
    """Contract-v1 derived metrics (decimal strings). Reporting only — never authority."""
    contributed: str = Field(description="contributed_opening_quote + sum(deposit quote_valuation)")
    withdrawn: str = Field(description="sum(withdrawal quote_valuation)")
    earned_realized: str = Field(
        description="earned_opening_quote + sum over epochs of (quote_delta_cum + base_delta_cum * ref)"
    )
    earned_total: str = Field(description="equity - contributed + withdrawn")
    earned_total_pct: str = Field(
        default="0",
        description=(
            "earned_total / contributed * 100 — inception return in percent. "
            "Presentation-only (NOT part of the pinned contract-v1 derived set; "
            "mirrors the engine's _purse_status_block); '0' when contributed is 0."
        ),
    )
    unrealized: str = Field(description="earned_total - earned_realized")
    drift: str = Field(description="residual the records cannot explain — always surfaced")
    reference_price_used: str = Field(
        description="the journal's newest checkpoint/epoch reference_price, used for every mark"
    )
    opening_basis_quality: Optional[str] = Field(
        default=None, description="the inception opening_epoch basis: reconstructed | current_equity_only"
    )


class PurseProvenance(BaseModel):
    """Where a snapshot came from + the stale-mirror detectors (required change #9)."""
    harvested_at: datetime = Field(description="when the snapshot was harvested from the journal")
    source_instance_name: str = Field(description="the retiring instance the journal was read from")
    source_bot_run_id: Optional[int] = Field(default=None, description="best-effort lineage link to the bot run")
    purse_sha256: str = Field(description="sha256 of the exact journal bytes (staleness detector)")
    sequence: int = Field(description="the journal's top-level sequence / highest record seq (staleness detector)")


class PurseSnapshotResponse(BaseModel):
    """The newest snapshot for a controller: derived metrics + provenance + disclaimer."""
    controller_id: str
    derived: PurseDerivedMetrics
    provenance: PurseProvenance
    authority_note: str = Field(default=AUTHORITY_NOTE)


# The honest-labeling disclaimer stamped on every activity response (safety invariant
# 6, CDX-007 / CLA-2A-02). ``booked_fill_events`` are BOOKING events, not exchange
# trades; the rate carries its denominator definition so a panel cannot silently
# re-present it as "trades per week".
ACTIVITY_METRIC_NOTE = (
    "booked_fill_events counts booked fill events, not exchange trades — one booking "
    "per executor per accounting cycle with any positive base/quote/fee delta (fee-only "
    "INCLUDED); it is NOT a count of exchange fills or trades. "
    "booked_fills_per_week_since_inception = booked_fill_events / calendar weeks from the "
    "first opening_epoch ts to the last activity ts (a week = 604800 s; weeks the bot was "
    "stopped are INCLUDED in the denominator); it is null when that span is zero or "
    "undefined (never a divide-by-zero)."
)


class PurseActivityResponse(BaseModel):
    """Derived per-controller ACTIVITY over the harvested journal (hbdash_api P1).

    Reuses :class:`PurseProvenance` + :data:`AUTHORITY_NOTE`; carries a required
    :data:`ACTIVITY_METRIC_NOTE` and, per safety invariant 6, names NO field
    ``trades`` / ``trade_count``. Rates are decimal strings; the per-week rate is
    ``null`` (not 0) when the span is zero/undefined.
    """
    controller_id: str
    booked_fill_events: int = Field(
        description="count of BOOKED FILL EVENTS (not exchange trades), summed over "
        "every fills_rollup record's fills_seen"
    )
    first_journal_ts: Optional[float] = Field(
        default=None,
        description="the first opening_epoch's ts (journal/accounting epoch seconds), or null",
    )
    last_journal_ts: Optional[float] = Field(
        default=None,
        description="max fills_rollup last_update_ts (else the newest record's ts), "
        "epoch seconds, or null",
    )
    weeks_since_inception: str = Field(
        description="calendar weeks first→last activity (decimal string; a week = 604800 s; "
        "stopped weeks included). '0' when the span is zero/undefined"
    )
    booked_fills_per_week_since_inception: Optional[str] = Field(
        default=None,
        description="booked_fill_events / weeks_since_inception (decimal string); null "
        "when the span is zero/undefined (never a divide-by-zero, never a bare 0)",
    )
    incarnation_id: str = Field(
        description="stable inception-lineage id (derived from the first opening_epoch); "
        "identical across re-harvest/resume of one lineage, changed by a no-resume fresh seed"
    )
    provenance: PurseProvenance
    authority_note: str = Field(default=AUTHORITY_NOTE)
    metric_note: str = Field(default=ACTIVITY_METRIC_NOTE)


class PurseHistoryEntry(BaseModel):
    """One historical snapshot — derived metrics + provenance, NO raw records_json."""
    derived: PurseDerivedMetrics
    provenance: PurseProvenance


class PurseHistoryResponse(BaseModel):
    """All snapshots for a controller (newest first), derived-only (records excluded)."""
    controller_id: str
    authority_note: str = Field(default=AUTHORITY_NOTE)
    snapshots: List[PurseHistoryEntry] = Field(default_factory=list)
