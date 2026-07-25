"""hbpurseapi P3 — contract-v1 DERIVED metrics for the purse read-model.

The API's read-model is DERIVED and NON-AUTHORITATIVE (safety rule 3 / ADDENDUM
A5): it reports numbers computed from the engine-owned journal, it never invents a
journal record. This module is the pure arithmetic half of that — given a purse
journal payload the P2 envelope already VALIDATED, it computes the contract-v1
derived metrics. No IO, no DB, no file reads: the harvest service owns those and
calls in here only with a payload ``classify_purse_envelope`` accepted.

Mirrors the engine's authority (transcribe, don't reinvent)
-----------------------------------------------------------
The formulas are transcribed line-for-line from the engine's canonical
``PurseLedger.derived_metrics`` (``controllers/_shared/purse_ledger.py:423``), the
same disciplined mirror ``purse_envelope_contract`` keeps of ``_validate_document``.
Keeping the two in one place is what lets "the API and the engine agree" be checked
by reading two functions rather than trusting a lookalike. The engine's docstring
formulas (contract v1):

    contributed     = sum(opening contributed_opening_quote) + sum(deposit quote_valuation)
    withdrawn       = sum(withdrawal quote_valuation)
    earned_realized = sum(opening earned_opening_quote)
                      + sum over epochs of (quote_delta_cum + base_delta_cum * ref)
    equity          = owned_quote + owned_base * ref
    earned_total    = equity - contributed + withdrawn
    unrealized      = earned_total - earned_realized
    drift           = sum over reanchor records of the per-asset cuts valued at ref,
                      PLUS (once the journal is checkpointing) the owned-vs-records
                      reconciliation residual — undeclared cuts the records cannot
                      otherwise explain, ALWAYS surfaced, never silently absorbed.

The one API-side ADAPTATION (documented, not a divergence)
----------------------------------------------------------
The engine is the LIVE controller: it passes ``reference_price``/``owned_quote``/
``owned_base`` from its authoritative in-memory state. The API has no live
controller at retirement — only the on-disk journal — so it must DERIVE those three
inputs from the records themselves (:func:`resolve_current_state`), exactly as the
batch's step-2 spec directs ("the ``current_reference_price`` input = the journal's
newest checkpoint/epoch ``reference_price``; record which was used"). Once derived,
they feed the IDENTICAL engine formula. The single reference price is applied
uniformly to every ``fills_rollup`` term, every reanchor cut, and the final equity —
exactly as the engine applies its single passed-in ``ref`` — so a reference that
moved across epochs is NOT retro-applied per epoch (that would diverge from the
engine).
"""
import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import List, Optional


def _dec(value, field_name: str) -> Decimal:
    """Finite Decimal from a validated journal field.

    Mirrors ``purse_ledger.py:_parse_decimal`` (:56-66) — including the ``bool``
    rejection and the ``is_finite`` guard. The payload was already accepted by
    :func:`services.purse_envelope_contract.classify_purse_envelope`, so every field
    read here is present, parseable and finite; this parser is the same-discipline
    belt-and-braces (a journal that somehow reached here malformed raises rather than
    silently coercing money), and it keeps the arithmetic byte-identical to the
    engine's.
    """
    if value is None or value == "" or isinstance(value, bool):
        raise ValueError(f"purse field '{field_name}' must be a finite decimal, got {value!r}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"purse field '{field_name}' is not a decimal: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"purse field '{field_name}' must be finite, got {value!r}")
    return parsed


@dataclass(frozen=True)
class CurrentState:
    """The journal's CURRENT marked state, derived for the engine formula's inputs.

    Attributes:
        reference_price: The newest checkpoint/opening/reseed ``reference_price`` — the
            single ``ref`` the contract formula applies uniformly.
        reference_source: Which record supplied it (``"<kind>@seq<n>"``), so a report
            can show provenance / staleness. Empty only for the impossible no-record case.
        owned_quote / owned_base: The newest owned-bearing record's owned state — the
            best on-disk proxy for the live controller's authoritative owned_*.
        opening_basis_quality: The INCEPTION opening_epoch's basis quality (the FIRST
            opening_epoch, which the envelope guarantees is records[0]).
    """

    reference_price: Decimal
    reference_source: str
    owned_quote: Decimal
    owned_base: Decimal
    opening_basis_quality: Optional[str]
    owned_ambiguous: bool = False


def resolve_current_state(records: List[dict]) -> CurrentState:
    """Derive (reference_price, owned_*, opening_basis_quality) from a validated journal.

    Walks the records in their (envelope-guaranteed strictly-increasing) seq order,
    tracking:
      * ``reference_price`` — updated by every record that CARRIES one (opening_epoch,
        reseed_epoch, checkpoint). ``reanchor`` carries no reference_price, so it never
        moves it; the final value is "the journal's newest checkpoint/epoch reference
        price", precisely the batch's step-2 input.
      * ``owned_*`` — updated by every record whose owned_* is AUTHORITATIVE: opening_epoch,
        reseed_epoch's ``new_owned_*``, checkpoint, and an ``undeclared_outflow`` reanchor
        (the engine's v12 site resizes owned to ``new_owned_*`` and commits it, so its
        ``new_owned_*`` IS the true owned — engine range_inventory_ladder.py:6810-6860).
        The final value is the most-current on-disk owned — the API's proxy for the live
        controller's authoritative owned the engine would otherwise pass in.
      * ``owned_ambiguous`` (CDX-R03) — a ``drift``-classified reanchor's ``new_owned_*`` is
        NOT authoritative: the engine emits ``drift`` reanchors in two indistinguishable
        shapes — a pure OBSERVATION (carried-prune / uncommitted-fill: owned unchanged,
        ``new_owned_*=0`` merely encodes the surfaced magnitude —
        range_inventory_ladder.py:4708-4800) AND a real sub-dust cut (owned resized to
        ``new_owned_*`` — the v12 site when the cut ≤ dust, :6852-6860). From the journal
        fields alone the two cannot be told apart, so a ``drift`` reanchor leaves owned_*
        UNCHANGED (the safe default for the dangerous observation case, which would
        otherwise zero owned) and FLAGS the state ambiguous. A later AUTHORITATIVE
        owned record (opening/reseed/checkpoint/undeclared_outflow reanchor) supersedes
        it and clears the flag. If the flag survives to the end (a terminal ``drift``
        reanchor), the current owned cannot be established from the journal and the
        harvest SKIPS this snapshot rather than mirror a misleading balance.
      * ``opening_basis_quality`` — captured from the FIRST opening_epoch only (the
        inception basis; a later post-quarantine opening declares no new basis).
    """
    ref = Decimal("0")
    ref_source = ""
    owned_quote = Decimal("0")
    owned_base = Decimal("0")
    opening_basis_quality: Optional[str] = None
    owned_ambiguous = False
    for record in records:
        kind = record.get("kind")
        seq = record.get("seq")
        if kind == "opening_epoch":
            if opening_basis_quality is None:
                opening_basis_quality = record.get("opening_basis_quality")
            owned_quote = _dec(record["owned_quote"], "owned_quote")
            owned_base = _dec(record["owned_base"], "owned_base")
            ref = _dec(record["reference_price"], "reference_price")
            ref_source = f"opening_epoch@seq{seq}"
            owned_ambiguous = False
        elif kind == "reseed_epoch":
            owned_quote = _dec(record["new_owned_quote"], "new_owned_quote")
            owned_base = _dec(record["new_owned_base"], "new_owned_base")
            ref = _dec(record["reference_price"], "reference_price")
            ref_source = f"reseed_epoch@seq{seq}"
            owned_ambiguous = False
        elif kind == "reanchor":
            # A reanchor declares NO reference_price — ref unchanged either way.
            if record.get("classification") == "undeclared_outflow":
                # A real, committed owned cut (engine v12 site resizes owned to
                # new_owned_* — :6810-6860): new_owned_* IS the authoritative owned.
                owned_quote = _dec(record["new_owned_quote"], "new_owned_quote")
                owned_base = _dec(record["new_owned_base"], "new_owned_base")
                owned_ambiguous = False
            else:
                # "drift": indistinguishable observation-vs-dust-cut (see docstring).
                # Leave owned unchanged; mark the state ambiguous until superseded.
                owned_ambiguous = True
        elif kind == "checkpoint":
            owned_quote = _dec(record["owned_quote"], "owned_quote")
            owned_base = _dec(record["owned_base"], "owned_base")
            ref = _dec(record["reference_price"], "reference_price")
            ref_source = f"checkpoint@seq{seq}"
            owned_ambiguous = False
    return CurrentState(
        reference_price=ref,
        reference_source=ref_source,
        owned_quote=owned_quote,
        owned_base=owned_base,
        opening_basis_quality=opening_basis_quality,
        owned_ambiguous=owned_ambiguous,
    )


@dataclass(frozen=True)
class DerivedPurseMetrics:
    """The contract-v1 derived metrics plus the inputs used to compute them.

    Money values are exact :class:`~decimal.Decimal`; the caller stringifies them for
    storage/transport (sqlite ``Numeric`` would lose precision — see
    :class:`database.models.PurseSnapshot`).
    """

    contributed: Decimal
    withdrawn: Decimal
    earned_realized: Decimal
    earned_total: Decimal
    unrealized: Decimal
    drift: Decimal
    equity_quote: Decimal
    reference_price_used: Decimal
    reference_source: str
    owned_quote: Decimal
    owned_base: Decimal
    opening_basis_quality: Optional[str]
    # CDX-R03: True when the current owned_* could not be authoritatively established
    # from the journal (a terminal ``drift``-classified reanchor). The harvest treats
    # this as a skip — a metrics object with this set must NOT be mirrored.
    owned_ambiguous: bool = False


def compute_derived_metrics(payload: dict) -> DerivedPurseMetrics:
    """Compute contract-v1 derived metrics for a VALIDATED purse journal payload.

    ``payload`` MUST have passed
    :func:`services.purse_envelope_contract.classify_purse_envelope` — this function
    trusts that guarantee (records non-empty, records[0] an opening_epoch, every
    per-kind money field present/finite, seq strictly increasing). It is a
    line-for-line transcription of ``PurseLedger.derived_metrics``
    (``controllers/_shared/purse_ledger.py:423``) with the reference price / owned_*
    inputs derived by :func:`resolve_current_state` instead of passed from a live
    controller.

    Raises:
        ValueError / KeyError only if handed an UN-validated payload (a required field
        missing or non-finite). The harvest caller validates first and treats any
        exception here as a skip — harvesting is observation-only.
    """
    records = payload["records"]
    state = resolve_current_state(records)
    ref = state.reference_price
    owned_quote = state.owned_quote
    owned_base = state.owned_base

    zero = Decimal("0")
    contributed = zero
    withdrawn = zero
    earned_opening = zero
    realized_flows = zero
    drift = zero
    # CDX-R02 owned-vs-records reconciliation accumulators (engine :465-471): the
    # owned_* the records imply for the CURRENT epoch. opening/reseed reset the epoch
    # baseline; the epoch's fills_rollup carries its cumulative deltas; only REAL owned
    # cuts (classification "undeclared_outflow") move the ledger — the phantom
    # "drift"-classified surfacing records leave owned_* untouched.
    imp_open_q = imp_open_b = zero
    imp_rollup_q = imp_rollup_b = zero
    imp_cut_q = imp_cut_b = zero
    have_opening = False
    have_checkpoint = False
    for record in records:
        kind = record.get("kind")
        if kind == "opening_epoch":
            contributed += _dec(record["contributed_opening_quote"], "contributed_opening_quote")
            earned_opening += _dec(record["earned_opening_quote"], "earned_opening_quote")
            imp_open_q = _dec(record["owned_quote"], "owned_quote")
            imp_open_b = _dec(record["owned_base"], "owned_base")
            imp_rollup_q = imp_rollup_b = imp_cut_q = imp_cut_b = zero
            have_opening = True
        elif kind == "flow":
            valuation = _dec(record["quote_valuation"], "quote_valuation")
            if record.get("flow_kind") == "deposit":
                contributed += valuation
            else:
                withdrawn += valuation
        elif kind == "fills_rollup":
            realized_flows += (
                _dec(record["quote_delta_cum"], "quote_delta_cum")
                + _dec(record["base_delta_cum"], "base_delta_cum") * ref
            )
            imp_rollup_q = _dec(record["quote_delta_cum"], "quote_delta_cum")
            imp_rollup_b = _dec(record["base_delta_cum"], "base_delta_cum")
        elif kind == "reseed_epoch":
            imp_open_q = _dec(record["new_owned_quote"], "new_owned_quote")
            imp_open_b = _dec(record["new_owned_base"], "new_owned_base")
            imp_rollup_q = imp_rollup_b = imp_cut_q = imp_cut_b = zero
        elif kind == "reanchor":
            cut_quote = max(
                zero,
                _dec(record["old_owned_quote"], "old_owned_quote")
                - _dec(record["new_owned_quote"], "new_owned_quote"),
            )
            cut_base = max(
                zero,
                _dec(record["old_owned_base"], "old_owned_base")
                - _dec(record["new_owned_base"], "new_owned_base"),
            )
            drift += cut_quote + cut_base * ref
            if record.get("classification") == "undeclared_outflow":
                imp_cut_q += cut_quote
                imp_cut_b += cut_base
        elif kind == "checkpoint":
            have_checkpoint = True
    # The ledger residual surfaces only once the journal is actively checkpointing (the
    # engine's P5 discipline); a pre-checkpoint journal keeps only the reanchor drift.
    if have_checkpoint and have_opening:
        implied_q = imp_open_q + imp_rollup_q - imp_cut_q
        implied_b = imp_open_b + imp_rollup_b - imp_cut_b
        drift += (owned_quote - implied_q) + (owned_base - implied_b) * ref
    equity = owned_quote + owned_base * ref
    earned_realized = earned_opening + realized_flows
    earned_total = equity - contributed + withdrawn
    return DerivedPurseMetrics(
        contributed=contributed,
        withdrawn=withdrawn,
        earned_realized=earned_realized,
        earned_total=earned_total,
        unrealized=earned_total - earned_realized,
        drift=drift,
        equity_quote=equity,
        reference_price_used=ref,
        reference_source=state.reference_source,
        owned_quote=owned_quote,
        owned_base=owned_base,
        opening_basis_quality=state.opening_basis_quality,
        owned_ambiguous=state.owned_ambiguous,
    )


# ---------------------------------------------------------------------------
# Activity read-model (hbdash_api P1) — booked-fill-event count + since-inception
# rate + incarnation lineage id. DERIVED and NON-AUTHORITATIVE, single-sourced with
# the record walk above (CLA-2A-01, CDX-007/CLA-2A-02).
# ---------------------------------------------------------------------------

# A calendar week in seconds (7 * 24 * 3600). The since-inception rate's denominator
# is calendar weeks from the first opening_epoch to the last activity — WITH stopped
# weeks included (an explicit operator choice, HBDASH_FINDINGS §3 open-decision #2).
SECONDS_PER_WEEK = Decimal("604800")


def derive_incarnation_id(
    controller_id, records: List[dict], source_bot_run_id: Optional[int] = None
) -> str:
    """Stable inception-lineage id for a purse journal (CDX-005 / CLA-309).

    The identity of a journal's ACCOUNTING LINEAGE is the epoch_id of its FIRST
    ``opening_epoch`` — the inception epoch. The engine mints that epoch_id once, at
    the fresh-seed that opened the journal, and carries it forward VERBATIM through
    every copy-forward resume. So a hash of ``(controller_id, inception_epoch_id)``:

      * is STABLE across a re-harvest of the same journal (same inception epoch),
      * is STABLE across a copy-forward RESUME (the resume keeps the inception epoch —
        this is exactly why HBDASH_FINDINGS rejects ``source_instance_name`` /
        ``source_bot_run_id`` as lineage detectors: both CHANGE on a resume, which
        would wrongly split one continuous lineage), and
      * CHANGES the instant a NO-RESUME fresh seed opens a NEW ``opening_epoch`` (a new
        epoch_id → a new id), so two unrelated incarnations never share an id.

    ``source_bot_run_id`` is therefore deliberately NOT folded into the id when an
    inception epoch is present; it is used only as a last-resort lineage hint for the
    (envelope-impossible) degenerate case of a journal with no resolvable opening
    epoch_id, honoring "combined with source_bot_run_id when available" without
    breaking the stability the finding requires. The controller_id is included so two
    controllers that happen to reuse an epoch_id string never collide.

    Returns ``"inc_" + <16 hex>`` — a 64-bit prefix of the sha256 of the tagged basis
    (tagged so an ``epoch``-basis id can never coincide with a ``run``/``anon`` one).
    Never raises: every field is read defensively.
    """
    inception_epoch_id: Optional[str] = None
    for record in records:
        if isinstance(record, dict) and record.get("kind") == "opening_epoch":
            candidate = record.get("epoch_id")
            if isinstance(candidate, str) and candidate:
                inception_epoch_id = candidate
            break  # only the FIRST opening_epoch is the inception epoch
    cid = controller_id if isinstance(controller_id, str) and controller_id else "?"
    if inception_epoch_id is not None:
        basis = f"epoch\x00{cid}\x00{inception_epoch_id}"
    elif source_bot_run_id is not None:
        basis = f"run\x00{cid}\x00{source_bot_run_id}"
    else:
        basis = f"anon\x00{cid}"
    return "inc_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class PurseActivity:
    """Derived per-controller ACTIVITY metrics (hbdash_api P1).

    HONEST LABELING (safety invariant 6): ``booked_fill_events`` counts BOOKED FILL
    EVENTS — one per executor per accounting cycle with any positive base/quote/fee
    delta (fee-only INCLUDED) — NOT exchange trades. The rate's denominator is
    calendar weeks since inception (stopped weeks included); the caller stringifies the
    Decimals and stamps the ``metric_note``.

    Attributes:
        booked_fill_events: sum of ``fills_seen`` over every ``fills_rollup`` record.
        first_journal_ts: the first ``opening_epoch``'s ``ts`` (journal/accounting
            time), or None when absent.
        last_journal_ts: max ``last_update_ts`` over ``fills_rollup`` records, falling
            back to the newest record's ``ts``; None when absent.
        weeks_since_inception: calendar weeks (first→last) as an exact Decimal; 0 when
            the span is zero or undefined.
        booked_fills_per_week: ``booked_fill_events / weeks_since_inception``, or None
            when the span is zero/undefined (never a divide-by-zero, never a bare 0).
        incarnation_id: the lineage id from :func:`derive_incarnation_id`.
    """

    booked_fill_events: int
    first_journal_ts: Optional[float]
    last_journal_ts: Optional[float]
    weeks_since_inception: Decimal
    booked_fills_per_week: Optional[Decimal]
    incarnation_id: str


def compute_activity(payload: dict, *, source_bot_run_id: Optional[int] = None) -> PurseActivity:
    """Compute activity metrics for a VALIDATED purse journal payload (hbdash_api P1).

    Single-sourced with :func:`compute_derived_metrics`: the SAME per-record walk,
    reading only fields the P2 envelope contract already pinned. Defensive by design —
    it NEVER raises (invariant 1/6): a malformed ``fills_seen`` (missing/non-int/
    negative/bool) is SKIPPED, not summed and not fatal, so a single bad rollup can
    never poison the count or crash a report.

    ``source_bot_run_id`` is passed through to :func:`derive_incarnation_id` only as
    the documented last-resort lineage hint; for a valid journal the inception
    ``opening_epoch`` supplies the id and the run id is ignored (see that function).
    """
    records = payload.get("records") or []
    controller_id = payload.get("controller_id")

    booked_fill_events = 0
    first_journal_ts: Optional[float] = None
    rollup_last_ts: List[float] = []
    newest_record_ts: Optional[float] = None

    for record in records:
        if not isinstance(record, dict):
            continue
        kind = record.get("kind")
        ts = record.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            newest_record_ts = float(ts)  # records walk in seq order — last valid wins
        if kind == "opening_epoch":
            if first_journal_ts is None and isinstance(ts, (int, float)) and not isinstance(ts, bool):
                first_journal_ts = float(ts)
        elif kind == "fills_rollup":
            fills_seen = record.get("fills_seen")
            # Mirror the envelope's own fills_seen gate (purse_envelope_contract:342):
            # a non-negative int, NEVER a bool. Anything else is skipped, not summed.
            if isinstance(fills_seen, int) and not isinstance(fills_seen, bool) and fills_seen >= 0:
                booked_fill_events += fills_seen
            last_update_ts = record.get("last_update_ts")
            if isinstance(last_update_ts, (int, float)) and not isinstance(last_update_ts, bool):
                rollup_last_ts.append(float(last_update_ts))

    last_journal_ts = max(rollup_last_ts) if rollup_last_ts else newest_record_ts

    weeks_since_inception = Decimal("0")
    booked_fills_per_week: Optional[Decimal] = None
    if first_journal_ts is not None and last_journal_ts is not None:
        span_seconds = Decimal(str(last_journal_ts)) - Decimal(str(first_journal_ts))
        if span_seconds > 0:
            weeks_since_inception = span_seconds / SECONDS_PER_WEEK
            booked_fills_per_week = Decimal(booked_fill_events) / weeks_since_inception
        # span <= 0 (zero/undefined) → weeks stays 0 and the rate stays None: a rate is
        # null (not a bare 0) when the denominator is undefined, never a divide-by-zero.

    return PurseActivity(
        booked_fill_events=booked_fill_events,
        first_journal_ts=first_journal_ts,
        last_journal_ts=last_journal_ts,
        weeks_since_inception=weeks_since_inception,
        booked_fills_per_week=booked_fills_per_week,
        incarnation_id=derive_incarnation_id(controller_id, records, source_bot_run_id),
    )


# ---------------------------------------------------------------------------
# Timeseries read-model (hbdash_api P2) — the authoritative journal-time
# checkpoint/epoch series (equity/contributed/earned), incarnation-segmented.
# DERIVED and NON-AUTHORITATIVE, single-sourced with the SAME seq-ordered record
# walk and accumulator logic as compute_derived_metrics (CLA-2A-05, CLA-M02).
# ---------------------------------------------------------------------------

# The boundary kinds a point can carry (HBDASH_FINDINGS §3 — the recommended design's
# "segment by incarnation_id / boundary"). A checkpoint is an in-epoch series sample
# and carries no boundary (None); the three RE-BASELINE events each carry one so a
# chart can mark the reset instead of drawing one continuous line across it:
#   * "opening"  — an opening_epoch (inception, or a later post-quarantine opening)
#   * "reseed"   — a reseed_epoch (owned re-anchored to new_owned_*)
#   * "reanchor" — a reanchor (an owned cut / drift surfacing)
BOUNDARY_OPENING = "opening"
BOUNDARY_RESEED = "reseed"
BOUNDARY_REANCHOR = "reanchor"


@dataclass(frozen=True)
class TimeseriesPoint:
    """One authoritative journal-time point in a purse's equity/flows series (P2).

    Emitted while walking the journal in ``seq`` order at every ``checkpoint`` (an
    in-epoch sample) and every ``opening_epoch``/``reseed_epoch``/``reanchor`` (a
    re-baseline, flagged via :attr:`boundary`). ``flow`` and ``fills_rollup`` records
    update the running accumulators but do NOT themselves emit a point.

    The running money metrics are computed with the SAME formulas as
    :func:`compute_derived_metrics` over the journal PREFIX up to and including this
    record, valued at the reference price CURRENT at this point (the newest
    checkpoint/epoch ``reference_price`` seen so far — a ``reanchor`` carries none, so
    it inherits the prior one). Because every ``reference_price``-bearing record emits
    a point, the LAST point's ``reference_price``/owned equal
    :func:`resolve_current_state`'s final values, so the final point RECONCILES with
    :func:`compute_derived_metrics` for the same journal (the P2 cross-check).

    Attributes:
        ts: the RECORD's ``ts`` = journal/accounting time (epoch seconds), NOT the
            DB ``harvested_at`` — this is the CLA-M02 fix. ``None`` only for the
            envelope-impossible non-numeric ts.
        seq: the record's ``seq`` (the series is emitted in strictly-increasing seq
            order); ``None`` for the envelope-impossible non-int seq.
        epoch_id: the record's ``epoch_id`` (``None`` when the record carries none).
        incarnation_id: the journal's inception-lineage id (:func:`derive_incarnation_id`),
            IDENTICAL on every point of one journal — a reseed re-baselines but does
            NOT open a new incarnation; only a separate no-resume fresh seed does.
        boundary: one of :data:`BOUNDARY_OPENING`/:data:`BOUNDARY_RESEED`/
            :data:`BOUNDARY_REANCHOR`, or ``None`` for a checkpoint sample.
        owned_quote / owned_base: the current on-disk owned at this point (a ``drift``
            reanchor leaves owned unchanged, mirroring :func:`resolve_current_state`).
        reference_price: the reference price current at this point.
        equity_quote / contributed / withdrawn / earned_total / earned_realized: the
            contract-v1 running metrics at this point (exact ``Decimal``; the caller
            stringifies for transport).
    """

    ts: Optional[float]
    seq: Optional[int]
    epoch_id: Optional[str]
    incarnation_id: str
    boundary: Optional[str]
    owned_quote: Decimal
    owned_base: Decimal
    reference_price: Decimal
    equity_quote: Decimal
    contributed: Decimal
    withdrawn: Decimal
    earned_total: Decimal
    earned_realized: Decimal


def compute_timeseries(
    payload: dict, *, source_bot_run_id: Optional[int] = None
) -> List[TimeseriesPoint]:
    """Build the journal-time checkpoint/epoch series for a VALIDATED payload (P2).

    Single-sourced with :func:`compute_derived_metrics`: the SAME per-record walk in
    ``seq`` order, the SAME ``_dec`` money parser, the SAME accumulator semantics
    (``contributed`` = opening ``contributed_opening_quote`` + deposit valuations;
    ``withdrawn`` = withdrawal valuations; ``earned_opening`` from opening records;
    the signed ``quote_delta_cum``/``base_delta_cum`` summed across ALL epochs — a
    reseed does NOT reset them, matching ``compute_derived_metrics``; owned/ref moved
    exactly as :func:`resolve_current_state` moves them). It emits a point at each
    ``checkpoint``/``opening_epoch``/``reseed_epoch``/``reanchor`` so the series
    starts, re-baselines and owned cuts are all visible; the two computations agree at
    the final point by construction.

    ``payload`` MUST have passed
    :func:`services.purse_envelope_contract.classify_purse_envelope` (the route
    re-validates before calling); like ``compute_derived_metrics`` this trusts that
    guarantee and reads pinned fields directly, so an UN-validated payload raises
    (a corrupt stored row becomes a clean 500, never silently-wrong points).

    ``source_bot_run_id`` is threaded to :func:`derive_incarnation_id` only as the
    documented last-resort lineage hint; a valid journal's inception ``opening_epoch``
    supplies the id and the run id is ignored (see that function).
    """
    records = payload["records"]
    incarnation_id = derive_incarnation_id(
        payload.get("controller_id"), records, source_bot_run_id
    )

    zero = Decimal("0")
    contributed = zero
    withdrawn = zero
    earned_opening = zero
    quote_delta_sum = zero
    base_delta_sum = zero
    owned_quote = zero
    owned_base = zero
    ref = zero

    points: List[TimeseriesPoint] = []
    for record in records:
        kind = record.get("kind")
        boundary: Optional[str] = None
        emit = False
        if kind == "opening_epoch":
            contributed += _dec(record["contributed_opening_quote"], "contributed_opening_quote")
            earned_opening += _dec(record["earned_opening_quote"], "earned_opening_quote")
            owned_quote = _dec(record["owned_quote"], "owned_quote")
            owned_base = _dec(record["owned_base"], "owned_base")
            ref = _dec(record["reference_price"], "reference_price")
            boundary = BOUNDARY_OPENING
            emit = True
        elif kind == "flow":
            valuation = _dec(record["quote_valuation"], "quote_valuation")
            if record.get("flow_kind") == "deposit":
                contributed += valuation
            else:
                withdrawn += valuation
        elif kind == "fills_rollup":
            quote_delta_sum += _dec(record["quote_delta_cum"], "quote_delta_cum")
            base_delta_sum += _dec(record["base_delta_cum"], "base_delta_cum")
        elif kind == "reseed_epoch":
            owned_quote = _dec(record["new_owned_quote"], "new_owned_quote")
            owned_base = _dec(record["new_owned_base"], "new_owned_base")
            ref = _dec(record["reference_price"], "reference_price")
            boundary = BOUNDARY_RESEED
            emit = True
        elif kind == "reanchor":
            # A reanchor carries no reference_price — ref unchanged. An
            # ``undeclared_outflow`` is a committed owned cut (new_owned_* IS the true
            # owned); a ``drift`` reanchor is the indistinguishable observation case,
            # so owned is left UNCHANGED — the SAME safe default resolve_current_state
            # applies (CDX-R03). Either way it is a re-baseline worth a marker.
            if record.get("classification") == "undeclared_outflow":
                owned_quote = _dec(record["new_owned_quote"], "new_owned_quote")
                owned_base = _dec(record["new_owned_base"], "new_owned_base")
            boundary = BOUNDARY_REANCHOR
            emit = True
        elif kind == "checkpoint":
            owned_quote = _dec(record["owned_quote"], "owned_quote")
            owned_base = _dec(record["owned_base"], "owned_base")
            ref = _dec(record["reference_price"], "reference_price")
            emit = True
        if not emit:
            continue
        equity = owned_quote + owned_base * ref
        earned_realized = earned_opening + quote_delta_sum + base_delta_sum * ref
        earned_total = equity - contributed + withdrawn
        ts = record.get("ts")
        seq = record.get("seq")
        epoch_id = record.get("epoch_id")
        points.append(
            TimeseriesPoint(
                ts=float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None,
                seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
                epoch_id=epoch_id if isinstance(epoch_id, str) and epoch_id else None,
                incarnation_id=incarnation_id,
                boundary=boundary,
                owned_quote=owned_quote,
                owned_base=owned_base,
                reference_price=ref,
                equity_quote=equity,
                contributed=contributed,
                withdrawn=withdrawn,
                earned_total=earned_total,
                earned_realized=earned_realized,
            )
        )
    return points
