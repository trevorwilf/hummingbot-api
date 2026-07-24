"""hbpurseapi Phase 1 — purse copy kind + fail-closed plan semantics + caller-
visible warnings (F14, F12).

Covers the purse journal half of ``services.resume_service``:

  * ``_expected_purse_name`` — the PINNED contract v1 naming
    (``<state stem>.purse.json``), incl. subdir / multi-dot cases.
  * ``_validate_purse`` — the source-file guard (read errors, zero-length, JSON
    syntax, sha256) that delegates the envelope to P2's formal purse contract
    (``services.purse_envelope_contract``); the envelope rules themselves are
    spec-tested in ``test_copyforward_purse_envelope.py``.
  * ``compute_copy_plan`` purse planning — the four fail-closed cases:
      valid -> copied (byte-exact + manifest entry),
      invalid -> PURSE_INVALID abort,
      absent + state marker -> PURSE_MISSING abort,
      absent + no marker -> bootstrap_pending + structured warning.
  * F12 — a fresh-seed decision lands in ``structured_warnings`` (was prose-only)
    and reaches the preview response.
  * F14 — an orphan ``*.json`` / ``*.purse.json`` in the source produces a log
    line + a manifest note, no behavior change otherwise.
  * a non-ladder controller with no purse resumes exactly as today (regression).
  * end-to-end deploy: the purse lands byte-identical before ``containers.run``
    and the written manifest carries the purse entry; a marker-less source
    surfaces ``PURSE_BOOTSTRAP_PENDING`` on the deploy response.

EVERY expected value is derived from the PINNED "Purse journal contract v1" and
the phase spec, never captured by running the validator. All filesystem via
``tmp_path``; Docker/DB are mocks; nothing live is touched.
"""

import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from docker.errors import NotFound

from database.repositories.bot_run_repository import REQUIRED_RETIREMENT_EVIDENCE
from ledger_fixtures import (
    checkpoint_record,
    opening_epoch_record,
    reseed_epoch_record,
    valid_ledger_payload,
    valid_purse_payload,
)
from models import V2ControllerDeployment
from services.docker_service import DockerService
from services.resume_service import (
    ResolvedSource,
    ResumeAbortReason,
    ResumeError,
    _expected_purse_name,
    _validate_purse,
    compute_copy_plan,
    preview_resume,
    _execute_copy_plan,
)

# The staged config a purse fixture's identity agrees with — controller_name /
# trading_pair resolve to these engine-compared values (see the P2 purse envelope
# contract). ``valid_purse_payload`` defaults to exactly this name + pair.
PURSE_STAGED_CONFIG = {
    "id": "ctrl_a",
    "controller_name": "range_inventory_ladder",
    "trading_pair": "XMR-USDT",
}


# ===========================================================================
# Plan-level helpers (mirroring tests/test_copyforward_copyset.py)
# ===========================================================================

def make_dep(extra_paths=None, allow_absolute_state_file_name=False):
    return SimpleNamespace(
        resume_extra_paths=extra_paths,
        allow_absolute_state_file_name=allow_absolute_state_file_name,
    )


def make_source(tmp_path, name="SRC"):
    inst = tmp_path / "instances" / name
    data = inst / "data"
    data.mkdir(parents=True, exist_ok=True)
    return ResolvedSource(
        instance_name=name, data_dir=data, instance_dir=inst, origin="instances"
    )


def new_instance(tmp_path, name="NEW"):
    return tmp_path / "instances" / name


def write_controller(new_instance_dir, filename, *, controller_id,
                     state_file_name=None, controller_name="range_inventory_ladder"):
    cdir = new_instance_dir / "conf" / "controllers"
    cdir.mkdir(parents=True, exist_ok=True)
    doc = {
        "id": controller_id,
        "controller_name": controller_name,
        "controller_type": "market_making",
        "connector_name": "nonkyc",
        "trading_pair": "XMR-USDT",
    }
    if state_file_name is not None:
        doc["state_file_name"] = state_file_name
    (cdir / filename).write_text(yaml.safe_dump(doc), encoding="utf-8")


def write_ledger(source, filename, *, owner_id=..., ledger_controller_id="ctrl_a",
                 ledger_overrides=None):
    """Write a VALID engine-envelope ledger (+ optional .owner). ``owner_id=...``
    (sentinel) means no sidecar. ``ledger_overrides`` merges extra keys — e.g. the
    ``purse_initialized`` bridge marker (the envelope tolerates unknown keys)."""
    payload = valid_ledger_payload(ledger_controller_id)
    if ledger_overrides:
        payload.update(ledger_overrides)
    p = source.data_dir / filename
    p.write_text(json.dumps(payload), encoding="utf-8")
    if owner_id is not ...:
        marker = {"pid": 1}
        if owner_id is not None:
            marker["controller_id"] = owner_id
        (source.data_dir / f"{filename}.owner").write_text(
            json.dumps(marker, sort_keys=True), encoding="utf-8"
        )
    return p


def write_purse(source, filename, payload=None, *, raw=None):
    """Write a purse journal. ``raw`` (an explicit string/bytes) writes verbatim
    (truncated-JSON case); otherwise ``payload`` (default: a valid purse) is
    JSON-encoded. Returns the bytes written (for byte-exact assertions)."""
    p = source.data_dir / filename
    if raw is not None:
        data = raw.encode("utf-8") if isinstance(raw, str) else raw
    else:
        data = json.dumps(payload if payload is not None else valid_purse_payload()).encode("utf-8")
    p.write_bytes(data)
    return data


def kinds(plan, kind):
    return [it for it in plan.items if it.kind == kind]


# ===========================================================================
# 1.  _expected_purse_name — PINNED contract naming
# ===========================================================================

class TestExpectedPurseName:
    @pytest.mark.parametrize(
        "ledger, purse",
        [
            ("range_inventory_ladder_xmr_usdt.json", "range_inventory_ladder_xmr_usdt.purse.json"),
            ("range_inventory_ladder_ctrl_a.json", "range_inventory_ladder_ctrl_a.purse.json"),
            ("my_ledger.json", "my_ledger.purse.json"),
            # Multi-dot: only the final .json is the extension (engine's Path.stem).
            ("a.b.json", "a.b.purse.json"),
        ],
    )
    def test_top_level_names(self, ledger, purse):
        assert _expected_purse_name(ledger) == purse

    def test_subdir_keeps_directory(self):
        # sub/x.json lives in sub/, so its purse does too.
        assert _expected_purse_name(os.path.join("sub", "x.json")) == os.path.join(
            "sub", "x.purse.json"
        )


# ===========================================================================
# 2.  _validate_purse — the source-file guard around P2's formal contract
# ===========================================================================
#
# The envelope RULES are spec-tested against ``classify_purse_envelope`` directly
# in ``test_copyforward_purse_envelope.py``. Here we only pin ``_validate_purse``'s
# own responsibilities: it returns the SOURCE sha256 on a valid journal and it
# delegates the envelope (a formal-contract rejection must abort PURSE_INVALID).

class TestValidatePurseHelper:
    def test_validate_purse_returns_source_sha(self, tmp_path):
        # A valid journal whose identity agrees with the staged config -> the source
        # file's sha256 (computed from the bytes on disk, not re-read).
        src = make_source(tmp_path)
        data = write_purse(src, "p.purse.json", valid_purse_payload("ctrl_a"))
        assert _validate_purse(
            src.data_dir / "p.purse.json", "ctrl_a", PURSE_STAGED_CONFIG
        ) == hashlib.sha256(data).hexdigest()

    def test_validate_purse_delegates_to_formal_contract(self, tmp_path):
        # A formal-contract-only rejection (wrong controller_name — the P1 minimal
        # check never compared it) must raise PURSE_INVALID through _validate_purse,
        # proving the source-file guard delegates to the formal envelope.
        src = make_source(tmp_path)
        write_purse(
            src, "p.purse.json", valid_purse_payload("ctrl_a", controller_name="not_the_ladder")
        )
        with pytest.raises(ResumeError) as exc:
            _validate_purse(src.data_dir / "p.purse.json", "ctrl_a", PURSE_STAGED_CONFIG)
        assert exc.value.reason is ResumeAbortReason.PURSE_INVALID


# ===========================================================================
# 3.  compute_copy_plan — valid purse copied byte-exact + manifest entry
# ===========================================================================

class TestValidPurseCopied:
    def test_purse_queued_alongside_ledger_and_recorded(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        purse_bytes = write_purse(
            src, "range_inventory_ladder_ctrl_a.purse.json", valid_purse_payload("ctrl_a")
        )
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        plan = compute_copy_plan(new, src, make_dep())

        # Ledger + owner + purse all queued as first-class copy items.
        assert {it.kind for it in plan.files_to_copy} == {"ledger", "owner", "purse"}
        purse_items = kinds(plan, "purse")
        assert len(purse_items) == 1
        assert purse_items[0].src == src.data_dir / "range_inventory_ladder_ctrl_a.purse.json"
        assert purse_items[0].dst == new / "data" / "range_inventory_ladder_ctrl_a.purse.json"
        assert purse_items[0].controller_id == "ctrl_a"

        # Manifest purse entry: copied + the SOURCE sha256.
        assert plan.purse_decisions == [
            {
                "controller_id": "ctrl_a",
                "kind": "purse",
                "decision": "copied",
                "purse_name": "range_inventory_ladder_ctrl_a.purse.json",
                "sha256": hashlib.sha256(purse_bytes).hexdigest(),
            }
        ]
        # No bootstrap warning when the purse is actually present.
        assert [w for w in plan.structured_warnings if w["code"] == "PURSE_BOOTSTRAP_PENDING"] == []

    def test_execute_copy_lands_purse_byte_exact(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        purse_bytes = write_purse(
            src, "range_inventory_ladder_ctrl_a.purse.json", valid_purse_payload("ctrl_a")
        )
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        plan = compute_copy_plan(new, src, make_dep())
        files = _execute_copy_plan(plan, new / "data")

        landed = (new / "data" / "range_inventory_ladder_ctrl_a.purse.json").read_bytes()
        assert landed == purse_bytes
        assert hashlib.sha256(landed).hexdigest() == hashlib.sha256(purse_bytes).hexdigest()
        by_name = {f["name"]: f for f in files}
        assert "range_inventory_ladder_ctrl_a.purse.json" in by_name
        assert by_name["range_inventory_ladder_ctrl_a.purse.json"]["sha256"] == (
            hashlib.sha256(purse_bytes).hexdigest()
        )

    def test_custom_state_file_name_purse_derived(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "my_ledger.json", owner_id="ctrl_b", ledger_controller_id="ctrl_b")
        write_purse(src, "my_ledger.purse.json", valid_purse_payload("ctrl_b"))
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_b", state_file_name="my_ledger.json")

        plan = compute_copy_plan(new, src, make_dep())

        assert kinds(plan, "purse")[0].dst == new / "data" / "my_ledger.purse.json"
        assert plan.purse_decisions[0]["decision"] == "copied"


# ===========================================================================
# 4.  Invalid purse -> PURSE_INVALID abort (fail-closed)
# ===========================================================================

class TestInvalidPurseAborts:
    def _plan(self, tmp_path, purse_payload=None, raw=None):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        write_purse(
            src, "range_inventory_ladder_ctrl_a.purse.json", purse_payload, raw=raw
        )
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")
        return new, src

    def test_bad_schema_version_aborts(self, tmp_path):
        new, src = self._plan(tmp_path, valid_purse_payload("ctrl_a", purse_schema_version=2))
        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.PURSE_INVALID

    def test_wrong_controller_id_aborts(self, tmp_path):
        new, src = self._plan(tmp_path, valid_purse_payload("SOMEONE_ELSE"))
        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.PURSE_INVALID

    def test_non_monotonic_seq_aborts(self, tmp_path):
        recs = [opening_epoch_record(seq=2), opening_epoch_record(seq=1, epoch_id="epoch-2")]
        new, src = self._plan(tmp_path, valid_purse_payload("ctrl_a", records=recs))
        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.PURSE_INVALID

    @pytest.mark.parametrize("key", ["controller_name", "trading_pair", "sequence"])
    def test_missing_top_level_key_aborts(self, tmp_path, key):
        # CDX-R01 wire-through: a journal missing a pinned top-level key the engine
        # requires must ABORT the plan (PURSE_INVALID), not be copied forward.
        payload = valid_purse_payload("ctrl_a")
        payload.pop(key)
        new, src = self._plan(tmp_path, payload)
        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.PURSE_INVALID

    def test_first_seq_not_one_aborts(self, tmp_path):
        # CDX-R01 wire-through: a single-record journal whose first seq is 2 is
        # 1-based-invalid; the engine refuses it, so the plan aborts fail-closed.
        recs = [opening_epoch_record(seq=2)]
        new, src = self._plan(tmp_path, valid_purse_payload("ctrl_a", records=recs))
        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.PURSE_INVALID

    def test_seq_regression_after_valid_first_aborts(self, tmp_path):
        # Wire-through for the strictly-increasing guard (:433 ``seq <= prev_seq``),
        # distinct from test_first_seq_not_one_aborts (that trips the 1-based guard).
        # A VALID first record (seq 1) then a 3->2 regression, top-level sequence
        # still the highest (4): the ONLY invalidity is the mid-journal drop, so a
        # ``<=``->``==`` mutation lets this journal through — this test catches it,
        # which the [2, 1] first-seq fixtures could not (seq-regression audit).
        recs = [
            opening_epoch_record(seq=s, epoch_id=f"epoch-{i}")
            for i, s in enumerate([1, 3, 2, 4])
        ]
        new, src = self._plan(tmp_path, valid_purse_payload("ctrl_a", records=recs))
        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.PURSE_INVALID

    def test_overflow_ts_aborts(self, tmp_path):
        # Wire-through for the never-raises contract: an overflow-sized integer ts
        # (a 400-digit int) is parseable JSON but unrepresentable as a float. The
        # formal contract must convert the OverflowError to a STRUCTURED PURSE_INVALID
        # abort end-to-end, never let it escape as an opaque 500 (overflow-ts audit).
        recs = [opening_epoch_record(seq=1, ts=10 ** 400)]
        new, src = self._plan(tmp_path, valid_purse_payload("ctrl_a", records=recs))
        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.PURSE_INVALID

    # -- P2 wire-through: the plan path now rejects via the FORMAL contract --
    # Each case below passes P1's minimal structural check (6 top-level keys,
    # version 1, matching controller_id, non-empty records, 1-based monotonic seq)
    # yet is a journal the ENGINE refuses to adopt. They therefore ABORT only
    # because ``_validate_purse`` delegates to ``classify_purse_envelope`` — the
    # P1 minimal check would have blessed and copied every one of them.

    def test_wrong_controller_name_aborts(self, tmp_path):
        # P1 minimal never compared controller_name; the formal contract does (:558).
        new, src = self._plan(
            tmp_path, valid_purse_payload("ctrl_a", controller_name="not_the_ladder")
        )
        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.PURSE_INVALID

    def test_wrong_trading_pair_aborts(self, tmp_path):
        new, src = self._plan(
            tmp_path, valid_purse_payload("ctrl_a", trading_pair="BTC-USDT")
        )
        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.PURSE_INVALID

    def test_sequence_value_mismatch_aborts(self, tmp_path):
        # The top-level `sequence` disagrees with the highest record seq — a VALUE
        # relationship P1 never checked, enforced by the formal contract (:608).
        new, src = self._plan(tmp_path, valid_purse_payload("ctrl_a", sequence=42))
        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.PURSE_INVALID

    def test_not_beginning_with_opening_epoch_aborts(self, tmp_path):
        # A reseed_epoch-first journal is structurally P1-valid but the engine
        # requires the journal to begin with an opening_epoch (:614).
        recs = [reseed_epoch_record(seq=1, epoch_id="epoch-1", prev_epoch_id=None)]
        new, src = self._plan(tmp_path, valid_purse_payload("ctrl_a", records=recs))
        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.PURSE_INVALID

    def test_checkpoint_referencing_unopened_epoch_aborts(self, tmp_path):
        # A checkpoint whose epoch was never opened — the formal contract's epoch
        # reference gate (:682) catches it; P1's minimal check never looked at kinds.
        recs = [
            opening_epoch_record(seq=1, epoch_id="epoch-1"),
            checkpoint_record(seq=2, epoch_id="epoch-404"),
        ]
        new, src = self._plan(tmp_path, valid_purse_payload("ctrl_a", records=recs))
        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.PURSE_INVALID

    def test_nan_money_field_aborts(self, tmp_path):
        # A NaN owned_quote in the opening epoch — the per-kind money-field parse
        # (:624-627) rejects it; P1's minimal check never parsed record fields.
        recs = [opening_epoch_record(seq=1, owned_quote=float("nan"))]
        new, src = self._plan(tmp_path, valid_purse_payload("ctrl_a", records=recs))
        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.PURSE_INVALID

    def test_truncated_json_aborts(self, tmp_path):
        new, src = self._plan(tmp_path, raw='{"purse_schema_version": 1, "records": [')
        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.PURSE_INVALID

    def test_zero_length_aborts(self, tmp_path):
        new, src = self._plan(tmp_path, raw="")
        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.PURSE_INVALID

    def test_invalid_purse_is_never_copied(self, tmp_path):
        """The abort must leave nothing on disk: the deploy aborts before any copy."""
        new, src = self._plan(tmp_path, valid_purse_payload("ctrl_a", purse_schema_version=2))
        with pytest.raises(ResumeError):
            compute_copy_plan(new, src, make_dep())
        assert not (new / "data").exists()


# ===========================================================================
# 5.  Purse absent — marker aborts, no-marker bootstraps
# ===========================================================================

class TestPurseAbsent:
    def test_marker_present_but_purse_missing_aborts(self, tmp_path):
        src = make_source(tmp_path)
        # The state file declares a purse, but the journal is gone -> data loss.
        write_ledger(
            src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a",
            ledger_overrides={"purse_initialized": True},
        )
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason is ResumeAbortReason.PURSE_MISSING

    def test_no_marker_no_purse_bootstrap_pending(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        plan = compute_copy_plan(new, src, make_dep())

        # The ledger still copies; the purse is bootstrap_pending.
        assert plan.decisions == {"ctrl_a": "copied"}
        assert plan.purse_decisions == [
            {
                "controller_id": "ctrl_a",
                "kind": "purse",
                "decision": "bootstrap_pending",
                "purse_name": "range_inventory_ladder_ctrl_a.purse.json",
            }
        ]
        # F12: bootstrap_pending is a STRUCTURED warning, not just prose.
        codes = [w["code"] for w in plan.structured_warnings]
        assert "PURSE_BOOTSTRAP_PENDING" in codes
        # Nothing purse-shaped queued for copy.
        assert kinds(plan, "purse") == []

    def test_marker_falsey_value_is_not_a_marker(self, tmp_path):
        """Only exactly ``true`` is the marker — a false/absent value bootstraps."""
        src = make_source(tmp_path)
        write_ledger(
            src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a",
            ledger_overrides={"purse_initialized": False},
        )
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        plan = compute_copy_plan(new, src, make_dep())
        assert plan.purse_decisions[0]["decision"] == "bootstrap_pending"


# ===========================================================================
# 6.  F12 — fresh-seed surfaces structurally (and in the preview)
# ===========================================================================

class TestFreshSeedStructured:
    def test_fresh_seed_in_structured_warnings(self, tmp_path):
        src = make_source(tmp_path)  # empty source data/
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_new")

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions == {"ctrl_new": "fresh_seed"}
        # Would FAIL on today's prose-only ``_warn`` (the F12 fix).
        codes = [w["code"] for w in plan.structured_warnings]
        assert "RESUME_FRESH_SEED" in codes
        entry = next(w for w in plan.structured_warnings if w["code"] == "RESUME_FRESH_SEED")
        assert entry["controller_id"] == "ctrl_new"
        # Still in the prose channel too (unchanged behavior).
        assert any("fresh" in w.lower() for w in plan.warnings)


# ===========================================================================
# 7.  F14 — orphan visibility (log line + manifest note)
# ===========================================================================

class TestOrphanVisibility:
    def test_unidentified_json_noted(self, tmp_path, caplog):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        (src.data_dir / "mystery.json").write_text('{"foo": 1}', encoding="utf-8")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        with caplog.at_level(logging.INFO, logger="services.resume_service"):
            plan = compute_copy_plan(new, src, make_dep())

        notes = [n for n in plan.orphan_notes if n["name"] == "mystery.json"]
        assert len(notes) == 1
        assert notes[0]["category"] == "unidentified_json"
        # The log line was emitted (one per orphan).
        assert any("mystery.json" in r.getMessage() for r in caplog.records)
        # Never copied, never a controller decision.
        assert "mystery.json" not in {it.src.name for it in plan.files_to_copy}
        assert "mystery.json" not in plan.decisions

    def test_orphan_purse_noted_not_mined_for_id(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        # A purse for a controller NOT in this deploy.
        write_purse(
            src, "range_inventory_ladder_ctrl_z.purse.json", valid_purse_payload("ctrl_z")
        )
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        plan = compute_copy_plan(new, src, make_dep())

        notes = [n for n in plan.orphan_notes if n["name"] == "range_inventory_ladder_ctrl_z.purse.json"]
        assert len(notes) == 1 and notes[0]["category"] == "orphan_purse"
        # A bogus "ctrl_z.purse" id was NOT mined into decisions.
        assert "ctrl_z.purse" not in plan.decisions
        assert "range_inventory_ladder_ctrl_z.purse.json" not in {
            it.src.name for it in plan.files_to_copy
        }

    def test_copied_controller_purse_is_not_an_orphan(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        write_purse(
            src, "range_inventory_ladder_ctrl_a.purse.json", valid_purse_payload("ctrl_a")
        )
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="ctrl_a")

        plan = compute_copy_plan(new, src, make_dep())
        # The copied controller's purse is handled, so it is NOT flagged orphan.
        assert plan.orphan_notes == []


# ===========================================================================
# 8.  Regression — a non-ladder controller triggers no purse machinery
# ===========================================================================

class TestNonLadderRegression:
    def test_non_ladder_no_purse_activity(self, tmp_path):
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="pmm_1", controller_name="pmm_simple")

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions == {}
        assert plan.files_to_copy == []
        assert plan.purse_decisions == []
        assert plan.orphan_notes == []
        assert plan.structured_warnings == []


# ===========================================================================
# 9.  Preview parity (§5 dry-run) — the operator sees it BEFORE deploying
# ===========================================================================

SRC_NAME = "PURSE_LADDER-20260710-101010"
NEW_NAME = "PURSE_LADDER-20260714-121212"
CID = "purse_xmr"
CFILE = "purse_xmr.yml"
LEDGER = f"range_inventory_ladder_{CID}.json"
PURSE = f"range_inventory_ladder_{CID}.purse.json"
SCRIPT_CONFIG = f"{NEW_NAME}.yml"
IMAGE = "hummingbot/hummingbot:v2.9"
CONFIG_PASSWORD = "test-password"

TEMPLATE_CFG = {
    "controller_name": "range_inventory_ladder",
    "id": CID,
    "connector_name": "kraken",
    "trading_pair": "XMR-USDT",
}
LEDGER_BYTES = json.dumps(valid_ledger_payload(CID, connector_name="kraken")).encode("utf-8")
PURSE_BYTES = json.dumps(valid_purse_payload(CID)).encode("utf-8")
OWNER_BYTES = json.dumps({"controller_id": CID, "pid": 7}).encode("utf-8")

_RETIREMENT_TS = "2026-07-10T12:00:00+00:00"
_VERIFIED_EVIDENCE_JSON = json.dumps({
    **{k: _RETIREMENT_TS for k in REQUIRED_RETIREMENT_EVIDENCE},
    "skip_order_cancellation": False,
    "cancellation_requested_at": _RETIREMENT_TS,
})


def _build_bots_tree(tmp_path, monkeypatch, *, with_ledger=True, with_purse=False,
                     ledger_overrides=None, with_junk=False):
    monkeypatch.chdir(tmp_path)
    bots = tmp_path / "bots"

    creds = bots / "credentials" / "master_account"
    creds.mkdir(parents=True)
    (creds / "conf_client.yml").write_text(
        yaml.safe_dump({"db_mode": {"db_engine": "postgres+asyncpg"}}), encoding="utf-8"
    )
    (creds / "connectors").mkdir()

    controllers = bots / "conf" / "controllers"
    controllers.mkdir(parents=True)
    (controllers / CFILE).write_text(yaml.safe_dump(TEMPLATE_CFG), encoding="utf-8")

    scripts = bots / "conf" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / SCRIPT_CONFIG).write_text(
        yaml.safe_dump({"script_file_name": "v2_with_controllers.py",
                        "controllers_config": [CFILE]}),
        encoding="utf-8",
    )

    src = bots / "instances" / SRC_NAME / "data"
    src.mkdir(parents=True)
    if with_ledger:
        payload = valid_ledger_payload(CID, connector_name="kraken")
        if ledger_overrides:
            payload.update(ledger_overrides)
        (src / LEDGER).write_bytes(json.dumps(payload).encode("utf-8"))
        (src / f"{LEDGER}.owner").write_bytes(OWNER_BYTES)
    if with_purse:
        (src / PURSE).write_bytes(PURSE_BYTES)
    if with_junk:
        (src / "leftover.tmp").write_text("junk", encoding="utf-8")
    return bots


def make_docker_client():
    client = MagicMock()
    client.containers.get.side_effect = NotFound("no such container")
    return client


def make_deployment(**overrides):
    kwargs = dict(
        instance_name=NEW_NAME,
        credentials_profile="master_account",
        controllers_config=[CFILE],
        script_config=SCRIPT_CONFIG,
        image=IMAGE,
        resume_mode="explicit",
        resume_from=SRC_NAME,
        resume_accept_ungraceful=True,
    )
    kwargs.update(overrides)
    return V2ControllerDeployment(**kwargs)


def graceful_repo(*names):
    repo = MagicMock()
    repo.get_bot_runs = AsyncMock(
        return_value=[
            SimpleNamespace(
                instance_name=n,
                run_status="STOPPED",
                stopped_at=datetime(2026, 7, 10, 12, 0, 0),
                retirement_status="VERIFIED",
                retirement_evidence=_VERIFIED_EVIDENCE_JSON,
            )
            for n in names
        ]
    )
    return repo


class TestPreviewParity:
    @pytest.mark.asyncio
    async def test_preview_surfaces_bootstrap_pending(self, tmp_path, monkeypatch):
        bots = _build_bots_tree(tmp_path, monkeypatch, with_ledger=True, with_purse=False)

        result = await preview_resume(
            deployment=make_deployment(),
            bots_path=bots,
            docker_client=make_docker_client(),
            bot_run_repo=graceful_repo(SRC_NAME),
        )

        assert result["would_succeed"] is True
        assert result["purse"] == [
            {
                "controller_id": CID,
                "kind": "purse",
                "decision": "bootstrap_pending",
                "purse_name": PURSE,
            }
        ]
        codes = [w["code"] for w in result["warnings"]]
        assert "PURSE_BOOTSTRAP_PENDING" in codes

    @pytest.mark.asyncio
    async def test_preview_surfaces_fresh_seed(self, tmp_path, monkeypatch):
        # No ledger in the source -> the controller fresh-seeds; F12 says the
        # operator must see that on the PREVIEW response, not just the deploy log.
        bots = _build_bots_tree(tmp_path, monkeypatch, with_ledger=False)

        result = await preview_resume(
            deployment=make_deployment(),
            bots_path=bots,
            docker_client=make_docker_client(),
            bot_run_repo=graceful_repo(SRC_NAME),
        )

        codes = [w["code"] for w in result["warnings"]]
        assert "RESUME_FRESH_SEED" in codes  # would FAIL on prose-only _warn

    @pytest.mark.asyncio
    async def test_preview_marker_missing_purse_aborts(self, tmp_path, monkeypatch):
        bots = _build_bots_tree(
            tmp_path, monkeypatch, with_ledger=True, with_purse=False,
            ledger_overrides={"purse_initialized": True},
        )
        with pytest.raises(ResumeError) as exc:
            await preview_resume(
                deployment=make_deployment(),
                bots_path=bots,
                docker_client=make_docker_client(),
                bot_run_repo=graceful_repo(SRC_NAME),
            )
        assert exc.value.reason is ResumeAbortReason.PURSE_MISSING


# ===========================================================================
# 10. End-to-end deploy — purse lands byte-identical + manifest carries it
# ===========================================================================

@pytest.fixture
def patched_security(monkeypatch, tmp_path):
    from config import settings

    monkeypatch.setattr(settings.security, "config_password", CONFIG_PASSWORD)
    monkeypatch.setattr("services.docker_service.ensure_gateway_certs", MagicMock())
    monkeypatch.setattr(
        "services.docker_service.gateway_certs_dir",
        MagicMock(return_value=str(tmp_path / "certs")),
    )


def make_service(client):
    service = DockerService.__new__(DockerService)
    service.SOURCE_PATH = os.getcwd()
    service.db_manager = None
    service._pull_status = {}
    service._cleanup_thread = None
    service.client = client
    return service


def make_run_docker_client(on_run=None):
    client = MagicMock()
    container = MagicMock()
    container.status = "exited"
    client.containers.get.return_value = container
    if on_run is not None:
        client.containers.run.side_effect = on_run
    return client


def read_manifest(bots, name=NEW_NAME):
    return json.loads(
        (bots / "instances" / name / "data" / "resume.manifest.json").read_text(encoding="utf-8")
    )


class TestE2EPurseDeploy:
    @pytest.mark.asyncio
    async def test_purse_lands_byte_identical_and_manifested(self, tmp_path, monkeypatch, patched_security):
        bots = _build_bots_tree(tmp_path, monkeypatch, with_ledger=True, with_purse=True)
        new_data = bots / "instances" / NEW_NAME / "data"
        captured = {}

        def on_run(*args, **kwargs):
            captured["purse"] = (new_data / PURSE).read_bytes() if (new_data / PURSE).exists() else None
            captured["ledger"] = (new_data / LEDGER).read_bytes() if (new_data / LEDGER).exists() else None
            return MagicMock()

        service = make_service(make_run_docker_client(on_run=on_run))
        response = await service.create_hummingbot_instance(make_deployment())

        assert response["success"] is True
        # The purse was on disk, byte-identical, at the instant of launch.
        assert captured["purse"] == PURSE_BYTES
        assert captured["ledger"] == LEDGER_BYTES

        manifest = read_manifest(bots)
        assert manifest["purse"] == [
            {
                "controller_id": CID,
                "kind": "purse",
                "decision": "copied",
                "purse_name": PURSE,
                "sha256": hashlib.sha256(PURSE_BYTES).hexdigest(),
            }
        ]
        by_name = {f["name"]: f for f in manifest["files"]}
        assert PURSE in by_name
        assert by_name[PURSE]["sha256"] == hashlib.sha256(PURSE_BYTES).hexdigest()
        # A copied controller's purse is handled, so it is never flagged orphan.
        assert PURSE not in {n["name"] for n in manifest["orphans"]}

    @pytest.mark.asyncio
    async def test_bootstrap_pending_rides_the_deploy_response(self, tmp_path, monkeypatch, patched_security):
        bots = _build_bots_tree(tmp_path, monkeypatch, with_ledger=True, with_purse=False)
        service = make_service(make_run_docker_client())

        response = await service.create_hummingbot_instance(make_deployment())

        assert response["success"] is True
        codes = [w["code"] for w in response.get("resume_warnings", [])]
        assert "PURSE_BOOTSTRAP_PENDING" in codes
        manifest = read_manifest(bots)
        assert manifest["purse"][0]["decision"] == "bootstrap_pending"

    @pytest.mark.asyncio
    async def test_marker_missing_purse_aborts_deploy(self, tmp_path, monkeypatch, patched_security):
        bots = _build_bots_tree(
            tmp_path, monkeypatch, with_ledger=True, with_purse=False,
            ledger_overrides={"purse_initialized": True},
        )
        client = make_run_docker_client()
        service = make_service(client)

        with pytest.raises(ResumeError) as exc:
            await service.create_hummingbot_instance(make_deployment())
        assert exc.value.reason is ResumeAbortReason.PURSE_MISSING
        # Fail-closed: no container ever started.
        client.containers.run.assert_not_called()
