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


def resolve_current_state(records: List[dict]) -> CurrentState:
    """Derive (reference_price, owned_*, opening_basis_quality) from a validated journal.

    Walks the records in their (envelope-guaranteed strictly-increasing) seq order,
    tracking:
      * ``reference_price`` — updated by every record that CARRIES one (opening_epoch,
        reseed_epoch, checkpoint). ``reanchor`` carries no reference_price, so it never
        moves it; the final value is "the journal's newest checkpoint/epoch reference
        price", precisely the batch's step-2 input.
      * ``owned_*`` — updated by every record that establishes owned (opening_epoch,
        reseed_epoch's ``new_owned_*``, reanchor's ``new_owned_*``, checkpoint). The
        final value is the most-current on-disk owned — the API's proxy for the live
        controller's authoritative owned the engine would otherwise pass in.
      * ``opening_basis_quality`` — captured from the FIRST opening_epoch only (the
        inception basis; a later post-quarantine opening declares no new basis).
    """
    ref = Decimal("0")
    ref_source = ""
    owned_quote = Decimal("0")
    owned_base = Decimal("0")
    opening_basis_quality: Optional[str] = None
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
        elif kind == "reseed_epoch":
            owned_quote = _dec(record["new_owned_quote"], "new_owned_quote")
            owned_base = _dec(record["new_owned_base"], "new_owned_base")
            ref = _dec(record["reference_price"], "reference_price")
            ref_source = f"reseed_epoch@seq{seq}"
        elif kind == "reanchor":
            # A reanchor moves owned_* but declares NO reference_price — ref unchanged.
            owned_quote = _dec(record["new_owned_quote"], "new_owned_quote")
            owned_base = _dec(record["new_owned_base"], "new_owned_base")
        elif kind == "checkpoint":
            owned_quote = _dec(record["owned_quote"], "owned_quote")
            owned_base = _dec(record["owned_base"], "owned_base")
            ref = _dec(record["reference_price"], "reference_price")
            ref_source = f"checkpoint@seq{seq}"
    return CurrentState(
        reference_price=ref,
        reference_source=ref_source,
        owned_quote=owned_quote,
        owned_base=owned_base,
        opening_basis_quality=opening_basis_quality,
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
    )
