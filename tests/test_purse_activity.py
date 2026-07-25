"""hbdash_api Phase 1 — GET /purse/{id}/activity (booked-fill-event read-model).

Covers the DERIVED, NON-AUTHORITATIVE activity surface (CLA-2A-01, CDX-007/CLA-2A-02;
safety invariants 1 & 6):

  * ``services.purse_read_model.compute_activity`` — the booked-fill-event count, the
    since-inception per-week rate, and the incarnation lineage id, single-sourced with
    the shipped record walk. Every EXPECTED value here is HAND-COMPUTED from the spec
    (independent ``Decimal`` / ``hashlib`` arithmetic), NEVER captured by running the
    implementation — so a dropped term (summing only the first rollup, a constant, a
    divide-by-zero, an id folding in harvested-time state) makes the arithmetic
    disagree and the test fail.
  * ``services.purse_read_model.derive_incarnation_id`` — the CDX-005/CLA-309 lineage
    key: stable across a re-harvest / resume of one lineage, changed by a no-resume
    fresh seed, and NEVER dependent on ``source_bot_run_id`` / harvest time when an
    inception epoch is present.
  * The endpoint — ``GET /purse/{id}/activity`` returns booked events + provenance +
    ``metric_note`` (honest labeling), 404s an unknown controller, is behind auth, and
    never leaks ``records_json`` — exercised both as a direct handler and THROUGH the
    real ``main.app`` ASGI stack (route registration + ``Depends(auth_user)`` wiring).

The real-sqlite DB plumbing and the ASGI harness are REUSED from
``test_purse_readmodel`` (the proven Phase-3 fixtures) rather than re-mocked, so the
activity route runs against real repositories and the real app.
"""
import hashlib
import json
from decimal import Decimal

import pytest
from fastapi import HTTPException

from database.repositories.purse_snapshot_repository import PurseSnapshotRepository
from ledger_fixtures import (
    checkpoint_record,
    fills_rollup_record,
    opening_epoch_record,
    reseed_epoch_record,
    valid_purse_payload,
)
from models.purse import ACTIVITY_METRIC_NOTE, AUTHORITY_NOTE, PurseActivityResponse
from routers.purse import get_purse_activity
from services.purse_read_model import compute_activity, derive_incarnation_id

# The proven real-DB + ASGI harness (fixtures resolved by name from this namespace).
from test_purse_readmodel import _asgi_get, asgi_main, db  # noqa: F401


# ---------------------------------------------------------------------------
# THE SPEC — hand-built journals with known booked counts and a known span, so every
# expected metric is derivable from the contract, never captured from the code.
# ---------------------------------------------------------------------------

T0 = 1700000000.0        # inception ts
WEEK = 604800            # seconds in a calendar week (7*24*3600); the rate denominator


def multi_epoch_journal(controller_id="ctrl_a", *, inception_epoch="epoch-alpha"):
    """A two-epoch journal: opening(epoch-alpha) → rollup(3 fills, +1wk) → reseed(
    epoch-beta) → rollup(7 fills, +2wk). Booked events = 3+7 = 10; last activity is the
    max rollup ``last_update_ts`` = T0+2wk; inception→last span = 2 calendar weeks."""
    records = [
        opening_epoch_record(seq=1, ts=T0, epoch_id=inception_epoch),
        fills_rollup_record(
            seq=2, ts=T0 + 100, epoch_id=inception_epoch,
            fills_seen=3, last_update_ts=T0 + 1 * WEEK,
        ),
        reseed_epoch_record(
            seq=3, ts=T0 + 200, epoch_id="epoch-beta", prev_epoch_id=inception_epoch,
        ),
        fills_rollup_record(
            seq=4, ts=T0 + 300, epoch_id="epoch-beta",
            fills_seen=7, last_update_ts=T0 + 2 * WEEK,
        ),
    ]
    return valid_purse_payload(controller_id=controller_id, records=records)


def opening_only_journal(controller_id="ctrl_a", *, inception_epoch="epoch-alpha"):
    """An inception-only journal (no rollups): 0 events, zero span → a null rate."""
    return valid_purse_payload(
        controller_id=controller_id,
        records=[opening_epoch_record(seq=1, ts=T0, epoch_id=inception_epoch)],
    )


def _spec_incarnation_id(controller_id, epoch_id):
    """Reimplement the DOCUMENTED derivation independently (spec-derived, NOT captured
    from the implementation): ``inc_`` + first 16 hex of sha256 of the tagged basis."""
    return "inc_" + hashlib.sha256(
        f"epoch\x00{controller_id}\x00{epoch_id}".encode("utf-8")
    ).hexdigest()[:16]


async def insert_journal(db, payload, *, source_instance_name="inst1", source_bot_run_id=None):
    """Insert a snapshot whose ``records_json`` is a REAL journal (the activity route
    re-parses + re-validates it). ``derived_*`` are placeholders — the activity route
    recomputes from ``records_json`` and never reads them."""
    body = json.dumps(payload)
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    async with db.get_session_context() as session:
        return await PurseSnapshotRepository(session).insert_snapshot_if_absent(
            controller_id=payload["controller_id"],
            source_instance_name=source_instance_name,
            source_bot_run_id=source_bot_run_id,
            purse_sha256=sha,
            sequence=payload["sequence"],
            records_json=body,
            derived_contributed="0", derived_withdrawn="0", derived_earned_realized="0",
            derived_earned_total="0", derived_unrealized="0", derived_drift="0",
            reference_price_used="0", opening_basis_quality=None,
        )


# ===========================================================================
# 1. compute_activity — booked count, since-inception rate (hand-computed)
# ===========================================================================

class TestBookedFillEvents:
    def test_sums_fills_seen_across_all_epochs(self):
        """Booked events = the hand-summed 3 (epoch-alpha) + 7 (epoch-beta) = 10.
        MUTATION GUARD: sum only the first rollup → 3 ≠ 10; return a constant → ≠ 10."""
        activity = compute_activity(multi_epoch_journal())
        assert activity.booked_fill_events == 10
        assert activity.booked_fill_events != 3   # not just the first rollup

    def test_malformed_fills_seen_is_skipped_never_summed_never_raises(self):
        """Safety invariant 6: a malformed ``fills_seen`` (negative / bool / missing) is
        SKIPPED, not summed and not fatal. Only the one valid rollup (5) counts."""
        bad_neg = fills_rollup_record(seq=3, ts=T0 + 200, epoch_id="epoch-alpha",
                                      fills_seen=-1, last_update_ts=T0 + 200)
        bad_bool = fills_rollup_record(seq=4, ts=T0 + 300, epoch_id="epoch-alpha",
                                       fills_seen=True, last_update_ts=T0 + 300)
        missing = fills_rollup_record(seq=5, ts=T0 + 400, epoch_id="epoch-alpha",
                                      last_update_ts=T0 + 400)
        missing.pop("fills_seen")
        records = [
            opening_epoch_record(seq=1, ts=T0, epoch_id="epoch-alpha"),
            fills_rollup_record(seq=2, ts=T0 + 100, epoch_id="epoch-alpha",
                                fills_seen=5, last_update_ts=T0 + 100),
            bad_neg, bad_bool, missing,
        ]
        activity = compute_activity(valid_purse_payload(records=records))  # must not raise
        assert activity.booked_fill_events == 5


class TestSinceInceptionRate:
    def test_rate_is_booked_over_calendar_weeks_since_inception(self):
        """first→last span = 2 calendar weeks; rate = booked/weeks = 10/2 = 5. Both the
        span and the rate are re-derived here from the contract denominator (604800 s),
        not read off the code."""
        activity = compute_activity(multi_epoch_journal())
        assert activity.first_journal_ts == T0
        assert activity.last_journal_ts == T0 + 2 * WEEK       # max rollup last_update_ts
        expected_weeks = (Decimal(str(T0 + 2 * WEEK)) - Decimal(str(T0))) / Decimal("604800")
        assert expected_weeks == Decimal("2")                  # sanity: the hand math
        assert activity.weeks_since_inception == expected_weeks
        assert activity.booked_fills_per_week == Decimal("10") / Decimal("2") == Decimal("5")

    def test_no_rollups_gives_zero_events_and_null_rate(self):
        """A journal with no rollups: 0 events, and — because inception==last (zero span)
        — a NULL rate (not a bare 0, and never a divide-by-zero)."""
        activity = compute_activity(opening_only_journal())
        assert activity.booked_fill_events == 0
        assert activity.booked_fills_per_week is None
        assert activity.weeks_since_inception == Decimal("0")
        assert activity.last_journal_ts == T0                  # newest-record fallback

    def test_zero_span_with_events_still_yields_null_rate_not_divide_by_zero(self):
        """Events present but first==last (all activity at one instant): the rate is
        null, never a ZeroDivisionError. MUTATION GUARD: dropping the span>0 guard would
        raise here instead of returning None."""
        records = [
            opening_epoch_record(seq=1, ts=T0, epoch_id="epoch-alpha"),
            fills_rollup_record(seq=2, ts=T0, epoch_id="epoch-alpha",
                                fills_seen=4, last_update_ts=T0),
        ]
        activity = compute_activity(valid_purse_payload(records=records))
        assert activity.booked_fill_events == 4
        assert activity.booked_fills_per_week is None
        assert activity.weeks_since_inception == Decimal("0")


# ===========================================================================
# 2. incarnation_id — the CDX-005/CLA-309 lineage key
# ===========================================================================

class TestIncarnationId:
    def test_stable_across_regrowth_and_distinct_across_fresh_seed(self):
        """Identical for two harvests of one lineage (the journal GREW between harvests
        but keeps its inception epoch), and DIFFERENT when a no-resume fresh seed opens a
        new inception epoch."""
        base = multi_epoch_journal(controller_id="ctrl_a", inception_epoch="epoch-alpha")
        grown = multi_epoch_journal(controller_id="ctrl_a", inception_epoch="epoch-alpha")
        grown["records"].append(
            checkpoint_record(seq=5, ts=T0 + 3 * WEEK, epoch_id="epoch-beta")
        )
        grown["sequence"] = 5

        id_base = compute_activity(base).incarnation_id
        assert compute_activity(grown).incarnation_id == id_base   # stable across re-harvest

        fresh = opening_only_journal(controller_id="ctrl_a", inception_epoch="epoch-GAMMA")
        assert compute_activity(fresh).incarnation_id != id_base    # a fresh seed differs

    def test_ignores_source_bot_run_id_when_inception_epoch_present(self):
        """A copy-forward RESUME keeps the inception epoch but is a NEW bot_run: the
        incarnation must NOT split. MUTATION GUARD: folding ``source_bot_run_id`` (or any
        harvest-time value) into the id would make these three differ."""
        payload = multi_epoch_journal(controller_id="ctrl_a")
        id1 = compute_activity(payload, source_bot_run_id=1).incarnation_id
        id2 = compute_activity(payload, source_bot_run_id=2).incarnation_id
        id_none = compute_activity(payload, source_bot_run_id=None).incarnation_id
        assert id1 == id2 == id_none

    def test_matches_documented_derivation_exactly(self):
        """Pins the derivation to its documented formula (spec-derived independently).
        MUTATION GUARD: any change to the hashed basis (adding harvested_at, sequence,
        bot_run_id, …) changes the digest and fails this."""
        payload = opening_only_journal(controller_id="ctrl_a", inception_epoch="epoch-alpha")
        assert compute_activity(payload).incarnation_id == _spec_incarnation_id("ctrl_a", "epoch-alpha")

    def test_fallback_uses_run_id_only_without_an_inception_epoch(self):
        """The documented last-resort fallback ("combined with source_bot_run_id when
        available") — reachable only for the envelope-impossible no-opening-epoch case,
        but exercised so it is not dead: run-id when available, else anonymous."""
        no_opening = [checkpoint_record(seq=1, ts=T0, epoch_id="epoch-x")]
        with_run = derive_incarnation_id("ctrl_a", no_opening, source_bot_run_id=42)
        without_run = derive_incarnation_id("ctrl_a", no_opening, source_bot_run_id=None)
        assert with_run == "inc_" + hashlib.sha256(
            "run\x00ctrl_a\x0042".encode("utf-8")
        ).hexdigest()[:16]
        assert without_run == "inc_" + hashlib.sha256(
            "anon\x00ctrl_a".encode("utf-8")
        ).hexdigest()[:16]
        assert with_run != without_run


# ===========================================================================
# 3. Honest labeling (safety invariant 6) — the response contract
# ===========================================================================

class TestHonestLabeling:
    def test_response_names_no_trade_field_and_carries_metric_note(self):
        """Invariant 6: the response MUST NOT name a field ``trades``/``trade_count`` and
        MUST carry a ``metric_note`` disclaiming exchange-trade semantics."""
        fields = set(PurseActivityResponse.model_fields.keys())
        assert "trades" not in fields
        assert "trade_count" not in fields
        assert "booked_fill_events" in fields
        assert "metric_note" in fields
        assert "not exchange trades" in ACTIVITY_METRIC_NOTE
        # the rate denominator definition travels with the response (invariant 6)
        assert "calendar weeks" in ACTIVITY_METRIC_NOTE


# ===========================================================================
# 4. Endpoint — direct handler + through the real ASGI app
# ===========================================================================

class TestActivityEndpoint:
    @pytest.mark.asyncio
    async def test_returns_metrics_provenance_and_metric_note(self, db):
        payload = multi_epoch_journal(controller_id="ctrl_a")
        await insert_journal(db, payload, source_instance_name="inst1", source_bot_run_id=99)

        resp = await get_purse_activity("ctrl_a", db_manager=db)

        assert resp.controller_id == "ctrl_a"
        assert resp.booked_fill_events == 10
        assert Decimal(resp.weeks_since_inception) == Decimal("2")
        assert Decimal(resp.booked_fills_per_week_since_inception) == Decimal("5")
        assert resp.incarnation_id == _spec_incarnation_id("ctrl_a", "epoch-alpha")
        assert resp.authority_note == AUTHORITY_NOTE
        assert resp.metric_note == ACTIVITY_METRIC_NOTE
        assert resp.provenance.sequence == payload["sequence"] == 4
        assert resp.provenance.source_instance_name == "inst1"

    @pytest.mark.asyncio
    async def test_null_rate_serializes_as_null(self, db):
        await insert_journal(db, opening_only_journal(controller_id="ctrl_a"))
        resp = await get_purse_activity("ctrl_a", db_manager=db)
        assert resp.booked_fill_events == 0
        assert resp.booked_fills_per_week_since_inception is None

    @pytest.mark.asyncio
    async def test_404_for_unknown_controller(self, db):
        with pytest.raises(HTTPException) as exc:
            await get_purse_activity("nobody", db_manager=db)
        assert exc.value.status_code == 404


class TestActivityEndpointASGI:
    @pytest.mark.asyncio
    async def test_registered_authed_200_no_records_leak(self, db, asgi_main):
        """A real GET /purse/{id}/activity through the app: 200 with the booked count,
        the spec-derived incarnation id, the metric_note, provenance — and NO
        ``records_json`` leak. Fails if the route is unregistered or the prefix wrong."""
        from deps import get_database_manager
        payload = multi_epoch_journal(controller_id="ctrl_a")
        await insert_journal(db, payload, source_instance_name="inst1", source_bot_run_id=7)
        asgi_main.app.dependency_overrides[get_database_manager] = lambda: db
        asgi_main.app.dependency_overrides[asgi_main.auth_user] = lambda: "tester"

        resp = await _asgi_get(asgi_main.app, "/purse/ctrl_a/activity")
        assert resp.status_code == 200
        body = resp.json()
        assert body["controller_id"] == "ctrl_a"
        assert body["booked_fill_events"] == 10
        assert Decimal(body["booked_fills_per_week_since_inception"]) == Decimal("5")
        assert body["incarnation_id"] == _spec_incarnation_id("ctrl_a", "epoch-alpha")
        assert body["metric_note"] == ACTIVITY_METRIC_NOTE
        assert body["authority_note"] == AUTHORITY_NOTE
        assert body["provenance"]["sequence"] == 4
        assert "records_json" not in json.dumps(body)

    @pytest.mark.asyncio
    async def test_unauthenticated_request_is_rejected(self, db, asgi_main):
        """The financial read-model MUST be behind auth. DB overridden, auth NOT →
        the real ``Depends(auth_user)`` rejects with 401 (mutation: drop the dependency
        at main.py and this becomes 200/404)."""
        from deps import get_database_manager
        await insert_journal(db, multi_epoch_journal(controller_id="ctrl_a"))
        asgi_main.app.dependency_overrides[get_database_manager] = lambda: db

        resp = await _asgi_get(asgi_main.app, "/purse/ctrl_a/activity")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_controller_404_through_router(self, db, asgi_main):
        from deps import get_database_manager
        asgi_main.app.dependency_overrides[get_database_manager] = lambda: db
        asgi_main.app.dependency_overrides[asgi_main.auth_user] = lambda: "tester"

        resp = await _asgi_get(asgi_main.app, "/purse/nobody/activity")
        assert resp.status_code == 404
