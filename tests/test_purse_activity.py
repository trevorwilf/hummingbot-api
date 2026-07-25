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


async def insert_journal_under(db, db_controller_id, payload, *,
                               source_instance_name="inst1", source_bot_run_id=None):
    """Insert a snapshot INDEXED under ``db_controller_id`` whose stored ``records_json``
    may carry a DIFFERENT internal ``controller_id`` — the one lever ``insert_journal``
    cannot pull (it indexes by ``payload["controller_id"]``). Used to prove the route
    re-validates the STORED envelope against the URL id and refuses a mismatch (CDX-R05)."""
    body = json.dumps(payload)
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    async with db.get_session_context() as session:
        return await PurseSnapshotRepository(session).insert_snapshot_if_absent(
            controller_id=db_controller_id,
            source_instance_name=source_instance_name,
            source_bot_run_id=source_bot_run_id,
            purse_sha256=sha,
            sequence=payload["sequence"],
            records_json=body,
            derived_contributed="0", derived_withdrawn="0", derived_earned_realized="0",
            derived_earned_total="0", derived_unrealized="0", derived_drift="0",
            reference_price_used="0", opening_basis_quality=None,
        )


async def insert_raw_journal(db, db_controller_id, records_json, *, sequence=1):
    """Insert a snapshot with ARBITRARY ``records_json`` bytes (possibly unparseable JSON) —
    to exercise the route's parse-error branch (routers/purse.py:108-114) for CDX-R05."""
    sha = hashlib.sha256(records_json.encode("utf-8")).hexdigest()
    async with db.get_session_context() as session:
        return await PurseSnapshotRepository(session).insert_snapshot_if_absent(
            controller_id=db_controller_id,
            source_instance_name="inst1", source_bot_run_id=None,
            purse_sha256=sha, sequence=sequence, records_json=records_json,
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
        """Safety invariant 6: EVERY malformed ``fills_seen`` kind is SKIPPED — not summed,
        not fatal. Each bad value is exercised IN ISOLATION against a single valid rollup
        (fills_seen=5), so no two errors can cancel to a passing total (CDX-R01): a summed
        negative would drop the count to 4, a summed bool would raise it to 6, and a summed
        non-int would raise or inflate — every case is pinned by the invariant total of 5,
        the exact count only a correct type/range gate produces."""
        BASELINE = 5
        # value -> what a BROKEN gate would do to the count if it let this value through.
        malformed = {
            "negative_int": -1,      # summed → 4   (a negative int passes an int-only gate)
            "boolean_true": True,    # summed → 6   (bool is an int subclass; True == 1)
            "numeric_string": "5",   # summed → TypeError, or 10 if coerced
            "float": 2.5,            # summed → 7.5 / TypeError (fills are integer events)
        }
        for label, bad in malformed.items():
            records = [
                opening_epoch_record(seq=1, ts=T0, epoch_id="epoch-alpha"),
                fills_rollup_record(seq=2, ts=T0 + 100, epoch_id="epoch-alpha",
                                    fills_seen=BASELINE, last_update_ts=T0 + 100),
                fills_rollup_record(seq=3, ts=T0 + 200, epoch_id="epoch-alpha",
                                    fills_seen=bad, last_update_ts=T0 + 200),
            ]
            activity = compute_activity(valid_purse_payload(records=records))  # must not raise
            assert activity.booked_fill_events == BASELINE, f"{label} was not skipped"
        # a MISSING fills_seen (key absent entirely) is likewise skipped, never fatal.
        missing = fills_rollup_record(seq=3, ts=T0 + 200, epoch_id="epoch-alpha",
                                      last_update_ts=T0 + 200)
        missing.pop("fills_seen")
        records = [
            opening_epoch_record(seq=1, ts=T0, epoch_id="epoch-alpha"),
            fills_rollup_record(seq=2, ts=T0 + 100, epoch_id="epoch-alpha",
                                fills_seen=BASELINE, last_update_ts=T0 + 100),
            missing,
        ]
        activity = compute_activity(valid_purse_payload(records=records))  # must not raise
        assert activity.booked_fill_events == BASELINE

    def test_ignores_fills_seen_on_non_rollup_records(self):
        """Only ``fills_rollup`` records contribute. A non-rollup record (here a checkpoint)
        carrying a stray or forward-compat ``fills_seen`` MUST NOT be counted — the counter
        keys off record KIND, never the mere presence of the field (CDX-R02). The envelope
        contract permits extra fields on a checkpoint (purse_envelope_contract:366-367), so
        this is a reachable valid journal. MUTATION GUARD: a 'sum any record whose fills_seen
        is an int' impl would add the checkpoint's 99 → 109 ≠ 10."""
        records = [
            opening_epoch_record(seq=1, ts=T0, epoch_id="epoch-alpha"),
            fills_rollup_record(seq=2, ts=T0 + 100, epoch_id="epoch-alpha",
                                fills_seen=3, last_update_ts=T0 + 1 * WEEK),
            checkpoint_record(seq=3, ts=T0 + 150, epoch_id="epoch-alpha", fills_seen=99),
            fills_rollup_record(seq=4, ts=T0 + 300, epoch_id="epoch-alpha",
                                fills_seen=7, last_update_ts=T0 + 2 * WEEK),
        ]
        activity = compute_activity(valid_purse_payload(records=records))
        assert activity.booked_fill_events == 10   # 3 + 7 only; the checkpoint's 99 ignored


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

    def test_last_ts_falls_back_to_newest_record_not_opening_when_no_rollups(self):
        """With no rollups, ``last_journal_ts`` is the NEWEST record's ts — NOT the opening
        ts. A journal of opening(T0) + a later checkpoint(T0+1wk) and no rollups: last is
        the checkpoint's ts, the span is one real week, and the zero-event rate is a DEFINED
        0 (span>0), not null. This is the discriminating fixture CDX-R03 requires — the
        opening-only journal below cannot tell 'newest record' from 'opening' because they
        coincide. MUTATION GUARD: falling the fallback back to ``first_journal_ts`` yields
        last=T0, weeks=0, and a null rate — every assertion below then fails."""
        records = [
            opening_epoch_record(seq=1, ts=T0, epoch_id="epoch-alpha"),
            checkpoint_record(seq=2, ts=T0 + 1 * WEEK, epoch_id="epoch-alpha"),
        ]
        activity = compute_activity(valid_purse_payload(records=records))
        assert activity.booked_fill_events == 0
        assert activity.first_journal_ts == T0
        assert activity.last_journal_ts == T0 + 1 * WEEK        # newest record, not the opening
        assert activity.weeks_since_inception == Decimal("1")
        assert activity.booked_fills_per_week == Decimal("0")   # a DEFINED 0 (span>0), not null

    def test_no_rollups_gives_zero_events_and_null_rate(self):
        """The zero-span/null-rate boundary: an inception-ONLY journal (no rollups, no later
        record) has first==last, so the span is 0 and the rate is NULL — not a bare 0 and
        never a divide-by-zero. (last==T0 here is the sole record; the newest-vs-opening
        fallback is discriminated in the test above, where the two timestamps diverge —
        they coincide when there is only one record, so this fixture cannot prove it.)"""
        activity = compute_activity(opening_only_journal())
        assert activity.booked_fill_events == 0
        assert activity.booked_fills_per_week is None
        assert activity.weeks_since_inception == Decimal("0")
        assert activity.last_journal_ts == T0                  # the sole (degenerate) record

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
        assert resp.first_journal_ts == T0                     # mapped through, not swapped/dropped
        assert resp.last_journal_ts == T0 + 2 * WEEK           # (CDX-R04 — the API boundary, not just compute)
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

    @pytest.mark.asyncio
    async def test_contract_invalid_stored_journal_is_500_not_cross_attributed(self, db):
        """A row indexed under ctrl_a whose stored journal's OWN controller_id is ctrl_b is a
        corrupt/tampered mirror. The route re-validates the stored envelope against the URL
        id and returns 500 — it never silently attributes ctrl_b's booked events (or its
        incarnation id) to ctrl_a (CDX-R05). MUTATION GUARD: dropping the
        ``if not verdict.is_valid`` gate (routers/purse.py:124) would 200 with ctrl_b's data
        under the ctrl_a label, so this raises HTTPException only on a route that re-checks."""
        mismatched = multi_epoch_journal(controller_id="ctrl_b")   # internal id ≠ the URL id
        await insert_journal_under(db, "ctrl_a", mismatched)
        with pytest.raises(HTTPException) as exc:
            await get_purse_activity("ctrl_a", db_manager=db)
        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_unparseable_stored_journal_is_500(self, db):
        """A stored journal that is not valid JSON is a corrupt mirror → a clean 500, never
        silently-empty derived numbers (CDX-R05, the parse-error branch routers/purse.py:108).
        MUTATION GUARD: swallowing the parse error would 200 or 500-elsewhere; here the route
        must surface a 500 on the unparseable bytes."""
        await insert_raw_journal(db, "ctrl_a", "{ this is not valid json ")
        with pytest.raises(HTTPException) as exc:
            await get_purse_activity("ctrl_a", db_manager=db)
        assert exc.value.status_code == 500


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
        assert body["first_journal_ts"] == T0                  # both timestamps survive serialization
        assert body["last_journal_ts"] == T0 + 2 * WEEK        # (CDX-R04, through the ASGI stack)
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
