"""hbpurseapi P3 — retirement-time purse harvest into the derived read-model.

Required change #9: in the verified-retirement finalization path — BEFORE the archive
step can move or ``rmtree`` the instance ``data/`` dir — read each controller's purse
journal, validate it with the P2 envelope, compute the contract-v1 derived metrics, and
insert a snapshot row so the inception history survives the archive. Harvesting is
verification-AGNOSTIC by design (CDX-R01): the archive runs regardless of whether the
retirement will be marked VERIFIED or UNVERIFIED, and it destroys the ``data/`` dir
either way, so the mirror must be captured either way — an UNVERIFIED (dirty) retirement
is precisely when preserving the journal matters most. The container has already exited
at the harvest point, so the on-disk journal is final; its validity is enforced
independently by the P2 envelope, not by the retirement verdict. ADDENDUM A5 pins the
feed: the read-model is harvested from the JOURNAL FILE itself, never from API trade
rows (the two stores are fully disjoint; there is no fill stream here).

OBSERVATION, NOT CONTROL (the load-bearing invariant)
-----------------------------------------------------
Harvesting must NEVER block or fail a retirement. A missing purse, an unreadable
file, an invalid envelope, a compute error, or a DB error is logged (a structured
outcome) and SKIPPED — the retirement proceeds regardless. Every layer is
belt-and-braces: each controller is isolated in its own try/except, each DB insert
opens its own session so one controller's failure cannot roll back another's
snapshot, and the whole function catches at the top and returns whatever it
harvested. It raises to no caller.

File location reuses the copy-forward's own helpers
---------------------------------------------------
The purse filename is derived through the SAME ``_expected_ledger_name`` /
``_expected_purse_name`` the copy-forward hook used to WRITE the file — so the harvest
reads exactly the file the hook carried, with zero chance of a lookalike drift
between "where the hook puts the purse" and "where the harvest looks for it".
"""
import hashlib
import json
import logging
from pathlib import Path
from typing import List, Optional

from database.repositories.bot_run_repository import BotRunRepository
from database.repositories.purse_snapshot_repository import PurseSnapshotRepository
from services.purse_envelope_contract import classify_purse_envelope
from services.purse_read_model import compute_derived_metrics
from services.resume_service import (
    RANGE_LADDER_CONTROLLER_NAME,
    StateFileStatus,
    _expected_ledger_name,
    _expected_purse_name,
    _iter_staged_controllers,
    classify_controller_id,
    classify_state_file_name,
)

logger = logging.getLogger(__name__)


async def _resolve_source_run_id(db_manager, bot_name: Optional[str]) -> Optional[int]:
    """Best-effort lineage link: the newest bot_run id for ``bot_name`` (the retiring
    run). Nullable and never load-bearing — any failure yields ``None`` and the
    snapshot still records its content-level provenance (sha256 + sequence)."""
    if not bot_name:
        return None
    try:
        async with db_manager.get_session_context() as session:
            run = await BotRunRepository(session).get_latest_bot_run(bot_name)
            return run.id if run else None
    except Exception as exc:  # observation-only
        logger.warning("purse harvest: could not resolve source bot_run id for %s: %s", bot_name, exc)
        return None


def _expected_purse_path(instance_path: Path, config: dict) -> Optional[Path]:
    """The purse-journal path for one range-ladder controller, or None if its
    identity/state-file name cannot be resolved (skip — observation-only).

    Uses the copy-forward's own name derivation so the harvest looks exactly where
    the hook wrote. A non-RELATIVE_OK ``state_file_name`` (unset/absolute/invalid)
    falls back to the default ledger name, mirroring ``_record_skipped_not_flagged``;
    an absolute name that points outside ``data/`` therefore simply won't be found
    here and is skipped, never chased outside the instance tree.
    """
    id_verdict = classify_controller_id(config.get("id"))
    if not id_verdict.is_valid:
        logger.warning(
            "purse harvest: controller id %r is not C2-valid (%s); skipping",
            config.get("id"), id_verdict.reason,
        )
        return None
    sfn_verdict = classify_state_file_name(config.get("state_file_name"))
    canonical_sfn = (
        sfn_verdict.canonical if sfn_verdict.status is StateFileStatus.RELATIVE_OK else None
    )
    ledger_name = _expected_ledger_name(id_verdict.canonical, canonical_sfn)
    purse_name = _expected_purse_name(ledger_name)
    return instance_path / "data" / purse_name


async def _insert_snapshot(
    db_manager, *, controller_id, source_instance_name, source_bot_run_id, sha256,
    sequence, records_json, metrics,
) -> Optional[dict]:
    """Idempotent insert of one snapshot in its OWN session. Returns the outcome dict."""
    async with db_manager.get_session_context() as session:
        repo = PurseSnapshotRepository(session)
        row = await repo.insert_snapshot_if_absent(
            controller_id=controller_id,
            source_instance_name=source_instance_name,
            source_bot_run_id=source_bot_run_id,
            purse_sha256=sha256,
            sequence=sequence,
            records_json=records_json,
            derived_contributed=str(metrics.contributed),
            derived_withdrawn=str(metrics.withdrawn),
            derived_earned_realized=str(metrics.earned_realized),
            derived_earned_total=str(metrics.earned_total),
            derived_unrealized=str(metrics.unrealized),
            derived_drift=str(metrics.drift),
            reference_price_used=str(metrics.reference_price_used),
            opening_basis_quality=metrics.opening_basis_quality,
        )
    if row is None:
        return {"controller_id": controller_id, "decision": "skipped_duplicate", "sha256": sha256}
    return {
        "controller_id": controller_id,
        "decision": "harvested",
        "sha256": sha256,
        "sequence": sequence,
        "reference_source": metrics.reference_source,
    }


async def _harvest_one_controller(
    config: dict, instance_path: Path, db_manager, source_instance_name: str,
    source_bot_run_id: Optional[int],
) -> Optional[dict]:
    """Harvest one range-ladder controller's purse. Returns an outcome dict or None.

    Every failure below is a SKIP (logged + structured outcome), never a raise: the
    purse absent (pre-purse bot), unreadable, zero-length, non-JSON, envelope-invalid,
    or a compute error. A doubtful money journal is NOT mirrored — but unlike the
    copy-forward it does not ABORT anything here (this is post-run observation, and
    the engine, not this mirror, is the authority).
    """
    controller_id_raw = config.get("id")
    purse_path = _expected_purse_path(instance_path, config)
    if purse_path is None:
        return None
    if not purse_path.exists():
        logger.debug(
            "purse harvest: no purse journal at %s for controller %r (pre-purse or "
            "not initialized); nothing to harvest", purse_path, controller_id_raw,
        )
        return None

    try:
        raw = purse_path.read_bytes()
    except OSError as exc:
        logger.error("purse harvest: cannot read %s: %s — skipping (observation-only)", purse_path, exc)
        return {"controller_id": str(controller_id_raw), "decision": "io_error", "reason": str(exc)[:200]}
    if len(raw) == 0:
        logger.error("purse harvest: %s is zero-length — skipping", purse_path)
        return {"controller_id": str(controller_id_raw), "decision": "invalid", "reason": "zero-length"}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        logger.error("purse harvest: %s is not parseable JSON (%s) — skipping", purse_path, exc)
        return {"controller_id": str(controller_id_raw), "decision": "invalid", "reason": f"json: {str(exc)[:150]}"}

    canonical_id = classify_controller_id(controller_id_raw).canonical
    verdict = classify_purse_envelope(
        payload, canonical_controller_id=canonical_id, staged_config=config,
    )
    if not verdict.is_valid:
        # A doubtful money journal is never mirrored (structured event + skip).
        logger.error(
            "purse harvest: PURSE_INVALID for controller %s at %s: %s — skipping "
            "(not harvested; the engine remains the authority)",
            canonical_id, purse_path, verdict.reason,
        )
        return {"controller_id": canonical_id, "decision": "invalid", "reason": verdict.reason[:200]}

    metrics = compute_derived_metrics(payload)
    if metrics.owned_ambiguous:
        # CDX-R03: the journal ends on a ``drift``-classified reanchor, whose new_owned_*
        # is indistinguishable between a pure observation (owned unchanged) and a real
        # sub-dust cut (owned resized). Current owned cannot be authoritatively derived,
        # so mirroring it would risk a grossly wrong equity/earned/drift. Skip (structured
        # event), consistent with the observation-only, fail-safe-on-doubt posture — the
        # raw journal still survives in the archive; only the derived mirror row is withheld.
        logger.warning(
            "purse harvest: current owned for controller %s at %s is ambiguous (journal ends "
            "on a drift-classified reanchor with no superseding checkpoint) — skipping the "
            "derived snapshot to avoid a misleading mirror",
            canonical_id, purse_path,
        )
        return {
            "controller_id": canonical_id,
            "decision": "skipped_ambiguous_owned",
            "reason": "terminal drift-classified reanchor; current owned not authoritatively established",
        }
    sha256 = hashlib.sha256(raw).hexdigest()
    sequence = payload["sequence"]  # validated: int == highest record seq
    records_json = raw.decode("utf-8")
    return await _insert_snapshot(
        db_manager,
        controller_id=canonical_id,
        source_instance_name=source_instance_name,
        source_bot_run_id=source_bot_run_id,
        sha256=sha256,
        sequence=sequence,
        records_json=records_json,
        metrics=metrics,
    )


async def harvest_instance_purses(
    instance_dir, *, db_manager, source_instance_name: str, bot_name: Optional[str] = None,
) -> List[dict]:
    """Harvest every range-ladder controller's purse in ``instance_dir`` (observation-only).

    Called from the retirement finalization path BEFORE archive. NEVER raises: on any
    error it logs and returns whatever was harvested so far. Returns a list of
    per-controller outcome dicts (``decision`` in ``harvested`` | ``skipped_duplicate``
    | ``invalid`` | ``io_error``) for the retirement evidence trail.

    Args:
        instance_dir: The retiring instance directory (``bots/instances/<container>``).
            Its ``conf/controllers/*.yml`` name the controllers and ``data/`` holds
            their purse journals.
        db_manager: Provides ``get_session_context()`` (the house DB accessor).
        source_instance_name: Recorded as the snapshot's provenance.
        bot_name: Used only to resolve the best-effort ``source_bot_run_id`` lineage link.
    """
    outcomes: List[dict] = []
    try:
        instance_path = Path(instance_dir)
        source_bot_run_id = await _resolve_source_run_id(db_manager, bot_name)
        for _yaml_path, config in _iter_staged_controllers(instance_path):
            if config.get("controller_name") != RANGE_LADDER_CONTROLLER_NAME:
                continue
            try:
                outcome = await _harvest_one_controller(
                    config, instance_path, db_manager, source_instance_name, source_bot_run_id,
                )
                if outcome is not None:
                    outcomes.append(outcome)
            except Exception as exc:  # per-controller isolation
                logger.error(
                    "purse harvest: unexpected error harvesting a controller in %s "
                    "(observation-only, retirement unaffected): %s", instance_dir, exc,
                )
    except Exception as exc:  # top-level backstop — never propagate to the retirement
        logger.error(
            "purse harvest: aborted for %s (observation-only, retirement unaffected): %s",
            instance_dir, exc,
        )
    return outcomes
