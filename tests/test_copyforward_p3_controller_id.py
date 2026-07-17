"""Phase 3 — CONTRACT C2: the controller ``id`` contract, API half
(CDX-008 / CLA-002).

Three things are under test:

  * ``services.controller_id_contract.classify_controller_id`` — the ONE api-side
    C2 predicate, tested directly as a spec table.
  * ``compute_copy_plan`` — a C2 violation ABORTS the deploy. The old code
    ``continue``\\-d, so the deploy SUCCEEDED with that controller's ledger left
    behind and the bot re-seeded from the wallet: the insufficient-funds bug the
    whole hook exists to prevent.
  * the abort's ATOMICITY — the violation is caught before ANY controller is
    planned, so an abort leaves no partial plan.

EVERY expected value below is derived from CONTRACT C2 as specified, never by
running the implementation. The contract, verbatim:

    ACCEPT: a ``str`` whose stripped length is >= 1. Canonical id = the stripped
    value; all identity derivations (ledger filename, ``.owner`` match) use the
    canonical id.
    REJECT: non-``str`` (int, bool, None — Pydantic v2 already rejects these
    engine-side; preserve that), ``""``, whitespace-only.
    PROHIBITED on both sides: the ``str(owner_id) != str(controller_id)``
    comparison fix.

Real ``tmp_path`` filesystems throughout. No Docker, no DB, no network — the copy
plan is pure computation over staged YAML plus the source ``data/`` tree, so
there is nothing here that needs a mock.
"""

import json
from types import SimpleNamespace

import pytest
import yaml

import services.resume_service as resume_service
from ledger_fixtures import valid_ledger_payload
from services.controller_id_contract import (
    ControllerIdStatus,
    classify_controller_id,
)
from services.resume_service import (
    ResolvedSource,
    ResumeAbortReason,
    ResumeError,
    compute_copy_plan,
)


# ---------------------------------------------------------------------------
# Helpers — deliberately local rather than imported from
# ``test_copyforward_copyset``: these tests pin a fail-closed money guard, and a
# future edit to another phase's fixtures must not be able to quietly change what
# they assert. (``tests/`` is also not a package, so a sibling import would rely
# on sys.path incidentals.)
# ---------------------------------------------------------------------------

def make_dep():
    """A minimal deploy stand-in — ``compute_copy_plan`` reads only
    ``resume_extra_paths`` and the CONTRACT C1 opt-out off it. C2 has no
    request-level opt-out: it is not opt-out-able."""
    return SimpleNamespace(resume_extra_paths=None, allow_absolute_state_file_name=False)


def make_source(tmp_path, name="SRC"):
    inst = tmp_path / "instances" / name
    data = inst / "data"
    data.mkdir(parents=True, exist_ok=True)
    return ResolvedSource(
        instance_name=name, data_dir=data, instance_dir=inst, origin="instances"
    )


def new_instance(tmp_path, name="NEW"):
    return tmp_path / "instances" / name


def write_controller(
    new_instance_dir,
    filename,
    *,
    controller_id,
    state_file_name=None,
    controller_name="range_inventory_ladder",
):
    """Stage a controller YAML. ``controller_id`` is written verbatim — including
    non-string values, which is the point of half these tests."""
    cdir = new_instance_dir / "conf" / "controllers"
    cdir.mkdir(parents=True, exist_ok=True)
    doc = {
        "id": controller_id,
        "controller_name": controller_name,
        "controller_type": "market_making",
    }
    if state_file_name is not None:
        doc["state_file_name"] = state_file_name
    (cdir / filename).write_text(yaml.safe_dump(doc), encoding="utf-8")


def write_ledger(source, filename, owner_id, ledger_controller_id="ctrl_a"):
    """A real ledger in the source ``data/``, with its ``.owner`` sidecar.

    The body is a VALID engine envelope (CDX-M02, phase 4). It was
    ``{"seed_value_quote": 100}`` — accepted by the old length+JSON validator, but
    never a ledger the engine would load. ``ledger_controller_id`` is the ledger's
    INTERNAL id and must equal the CANONICAL (stripped) id of the controller under
    test; it is separate from ``owner_id`` so the owner-mismatch tests still
    isolate the SIDECAR as the only disagreeing party.
    """
    (source.data_dir / filename).write_text(
        json.dumps(valid_ledger_payload(ledger_controller_id)), encoding="utf-8"
    )
    (source.data_dir / f"{filename}.owner").write_text(
        json.dumps({"controller_id": owner_id, "pid": 1}), encoding="utf-8"
    )


# ===========================================================================
# 1.  The shared predicate, as a spec table
# ===========================================================================

class TestClassifyControllerId:
    """C2's accept/reject set, decision by decision."""

    @pytest.mark.parametrize(
        "value,expected_canonical",
        [
            ("abc", "abc"),                # plain
            (" abc ", "abc"),              # C2: 'Canonical id = the stripped value'
            ("\tabc\n", "abc"),            # any whitespace, not just spaces
            ("ladder_xmr", "ladder_xmr"),
            ("a", "a"),                    # C2: 'stripped length is >= 1'
            ("a b", "a b"),                # interior whitespace is part of the id
            ("0", "0"),                    # the STRING "0" is a valid id; the INT 0 is not
            ("False", "False"),
        ],
    )
    def test_accepts_non_empty_strings_and_canonicalizes(self, value, expected_canonical):
        verdict = classify_controller_id(value)
        assert verdict.status is ControllerIdStatus.VALID
        assert verdict.canonical == expected_canonical
        assert verdict.is_valid is True

    @pytest.mark.parametrize("value", ["", "   ", "\t", "\n", " \t\n "])
    def test_empty_or_whitespace_only_is_invalid(self, value):
        """C2 REJECT: '"", whitespace-only'.

        ``"   "`` is the interesting one: it is TRUTHY, so the old ``if not
        controller_id`` check waved it through and every identity derivation
        downstream (``range_inventory_ladder_   .json``) became ambiguous.
        """
        verdict = classify_controller_id(value)
        assert verdict.status is ControllerIdStatus.INVALID
        assert verdict.canonical is None
        assert verdict.reason

    @pytest.mark.parametrize("value", [None, 0, 1, 123, True, False, 1.5, ["a"], {"a": 1}, b"abc"])
    def test_non_str_is_invalid(self, value):
        """C2 REJECT: 'non-str (int, bool, None ...)'.

        ``0`` and ``False`` are the ones the old falsy check ALSO dropped — but it
        dropped them by skipping the controller, not by refusing the deploy. The
        engine rejects them at config load (controller_base.py:68); the API must
        not disagree with the engine about what a valid config is.
        """
        verdict = classify_controller_id(value)
        assert verdict.status is ControllerIdStatus.INVALID
        assert verdict.canonical is None

    def test_invalid_ids_have_no_canonical_form(self):
        """An INVALID verdict must never hand back something a careless caller
        could use as an id — that would re-open the fail-open through the back
        door."""
        for value in ("", "   ", None, 0, True):
            assert classify_controller_id(value).canonical is None

    def test_classifier_never_raises_on_hostile_input(self):
        """The classifier reports; the caller fails closed. A raising classifier
        would turn a C2 violation into an opaque 500 rather than a 409."""
        for value in (None, "", "   ", 0, object(), b"abc", [None]):
            assert classify_controller_id(value).status in set(ControllerIdStatus)


# ===========================================================================
# 2.  A C2 violation ABORTS the deploy (it used to be skipped)
# ===========================================================================

class TestC2AbortsTheDeploy:
    """The heart of CDX-008: ``continue`` -> ``raise``.

    Each case asserts the reason enum, not merely "something raised": a deploy
    that aborted for an unrelated reason (a missing ledger, say) would satisfy a
    bare ``pytest.raises(ResumeError)`` while proving nothing about C2.
    """

    @pytest.mark.parametrize(
        "bad_id",
        [
            "",        # C2 REJECT: empty
            "   ",     # C2 REJECT: whitespace-only (TRUTHY — the old check missed it)
            "\t",
            0,         # C2 REJECT: non-str (also falsy — the old check silently dropped it)
            False,
            123,       # C2 REJECT: non-str, and TRUTHY — the old check planned
                       # 'range_inventory_ladder_123.json' off an int
            1.5,
        ],
    )
    def test_invalid_id_aborts_with_controller_id_invalid(self, tmp_path, bad_id):
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id=bad_id)

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())

        assert exc.value.reason is ResumeAbortReason.CONTROLLER_ID_INVALID

    def test_missing_id_key_aborts(self, tmp_path):
        """A staged config with no ``id`` at all. C2 has no UNSET: ``id`` is
        required, so a missing one is a violation, not a default."""
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        cdir = new / "conf" / "controllers"
        cdir.mkdir(parents=True)
        (cdir / "c.yml").write_text(
            yaml.safe_dump({"controller_name": "range_inventory_ladder"}), encoding="utf-8"
        )

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())

        assert exc.value.reason is ResumeAbortReason.CONTROLLER_ID_INVALID

    def test_abort_fires_even_when_a_perfectly_good_ledger_is_present(self, tmp_path):
        """The source having a plausible ledger must not rescue a bad id.

        Without this, an implementation that aborted only because it could not
        FIND a ledger would pass the cases above for the wrong reason.
        """
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_   .json", owner_id="   ", ledger_controller_id="   ")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="   ")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())

        assert exc.value.reason is ResumeAbortReason.CONTROLLER_ID_INVALID

    def test_error_message_names_the_offending_file(self, tmp_path):
        """The operator has to find the config. An abort that says only 'invalid
        id' across a tree of controller YAMLs is a fail-closed guard that costs an
        outage to action."""
        src = make_source(tmp_path)
        new = new_instance(tmp_path)
        write_controller(new, "ladder_xmr.yml", controller_id="")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())

        assert "ladder_xmr.yml" in str(exc.value)


# ===========================================================================
# 3.  The abort is ATOMIC — no partial plan
# ===========================================================================

class TestAbortLeavesNoPartialPlan:
    """Phase 3's step-B focus: 'abort actually aborts (no partial ``deployed_ids``
    mutation before the raise)'.

    A raise from inside the planning loop would leave the controllers ahead of the
    violator already planned — a partial plan is the half-resumed state the hook
    exists to prevent. The observable: ``_plan_controller`` is never called at all.

    The spy WRAPS the real ``_plan_controller`` (it does not replace it), so the
    plan built in the passing case is the real one — this records calls, it does
    not stub the unit under test.
    """

    @pytest.fixture
    def spy(self, monkeypatch):
        calls = []
        real = resume_service._plan_controller

        def wrapper(config, controller_id, *args, **kwargs):
            calls.append(controller_id)
            return real(config, controller_id, *args, **kwargs)

        monkeypatch.setattr(resume_service, "_plan_controller", wrapper)
        return calls

    def test_no_controller_is_planned_when_a_later_one_violates_c2(self, tmp_path, spy):
        """``a_good.yml`` sorts BEFORE ``b_bad.yml``, so the old per-controller
        check would have planned the good one and only then hit the bad one.
        """
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_good.json", owner_id="ctrl_good", ledger_controller_id="ctrl_good")
        new = new_instance(tmp_path)
        write_controller(new, "a_good.yml", controller_id="ctrl_good")
        write_controller(new, "b_bad.yml", controller_id="   ")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())

        assert exc.value.reason is ResumeAbortReason.CONTROLLER_ID_INVALID
        # The whole point: planning never began, so there is no partial plan.
        assert spy == []

    def test_the_spy_records_planning_when_every_id_is_valid(self, tmp_path, spy):
        """Guards the test above against being vacuously green: if the spy never
        recorded anything under ANY conditions, ``spy == []`` would prove nothing.
        """
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_good.json", owner_id="ctrl_good", ledger_controller_id="ctrl_good")
        new = new_instance(tmp_path)
        write_controller(new, "a_good.yml", controller_id="ctrl_good")

        plan = compute_copy_plan(new, src, make_dep())

        assert spy == ["ctrl_good"]
        assert plan.decisions == {"ctrl_good": "copied"}


# ===========================================================================
# 4.  The canonical (stripped) id drives every identity derivation
# ===========================================================================

class TestCanonicalIdIsUsedDownstream:
    """C2: 'Canonical id = the stripped value; all identity derivations (ledger
    filename, ``.owner`` match) use the canonical id.'

    The source tree here is written with the STRIPPED id throughout — which is
    what the engine (post-Run-A, controller_base.py:95 strips too) actually
    writes. An implementation that derived names from the raw ``" ctrl_pad "``
    would look for ``range_inventory_ladder_ ctrl_pad .json``, miss, and
    fresh-seed the controller from the wallet instead of aborting or copying.
    """

    def test_padded_id_is_planned_under_the_stripped_id(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_pad.json", owner_id="ctrl_pad", ledger_controller_id="ctrl_pad")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="  ctrl_pad  ")

        plan = compute_copy_plan(new, src, make_dep())

        # Keyed by the canonical id, and COPIED — not fresh_seed, not skipped.
        assert plan.decisions == {"ctrl_pad": "copied"}

        ledger = [it for it in plan.items if it.kind == "ledger"][0]
        # Ledger filename derived from the canonical id (C2), both ends.
        assert ledger.src == src.data_dir / "range_inventory_ladder_ctrl_pad.json"
        assert ledger.dst == new / "data" / "range_inventory_ladder_ctrl_pad.json"
        assert ledger.controller_id == "ctrl_pad"

        # The '.owner' sidecar says "ctrl_pad"; the staged config says
        # "  ctrl_pad  ". These match ONLY under canonicalization — an
        # implementation comparing raw values would abort here with
        # OWNER_MISMATCH.
        owner = [it for it in plan.items if it.kind == "owner"][0]
        assert owner.controller_id == "ctrl_pad"

    def test_padded_id_with_a_custom_state_file_name(self, tmp_path):
        """The custom-name branch takes its identity from the '.owner' sidecar
        rather than the filename, so it exercises the canonical '.owner' match on
        its own."""
        src = make_source(tmp_path)
        write_ledger(src, "custom.json", owner_id="ctrl_pad", ledger_controller_id="ctrl_pad")
        new = new_instance(tmp_path)
        write_controller(
            new, "c.yml", controller_id=" ctrl_pad ", state_file_name="custom.json"
        )

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions == {"ctrl_pad": "copied"}

    def test_owner_mismatch_still_aborts_under_canonicalization(self, tmp_path):
        """Canonicalizing must not turn the OWNER_MISMATCH guard into a
        rubber stamp: a genuinely different id still fails closed.

        This covers the ORDINARY mismatch only. Two distinct strings are
        unequal under any comparison, so this test cannot see the prohibited
        ``str(owner_id) != str(controller_id)`` fix — that is
        ``test_numerically_typed_owner_id_is_a_mismatch``'s job.
        """
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_pad.json", owner_id="somebody_else", ledger_controller_id="ctrl_pad")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="  ctrl_pad  ")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())

        assert exc.value.reason is ResumeAbortReason.OWNER_MISMATCH

    def test_numerically_typed_owner_id_is_a_mismatch(self, tmp_path):
        """A sidecar whose ``controller_id`` is the JSON number ``123`` does NOT
        own the ledger of the controller whose canonical id is the string
        ``"123"``.

        C2 admits only ``str`` ids, so a non-str sidecar id is an identity that
        cannot be verified -> fail closed. This is the one test that can see the
        PROHIBITED ``str(owner_id) != str(controller_id)`` fix: under coercion
        ``str(123) == str("123")``, the guard passes, and a ledger of
        unverifiable provenance is copied into a live trading instance.
        """
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_123.json", owner_id=123, ledger_controller_id="123")
        new = new_instance(tmp_path)
        write_controller(new, "c.yml", controller_id="123")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())

        assert exc.value.reason is ResumeAbortReason.OWNER_MISMATCH


# ===========================================================================
# 5.  C2 is scoped to range-ladder controllers
# ===========================================================================

class TestScope:
    """Phase 3's spec: 'for every staged range-ladder controller'.

    Without this, 'abort on everything' would pass every test above. The resume
    hook only carries range-ladder ledgers; a non-ladder controller's id is the
    engine's to validate, and aborting a deploy over one would be an
    out-of-contract behavior change.
    """

    def test_non_ladder_controller_with_an_invalid_id_does_not_abort(self, tmp_path):
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_good.json", owner_id="ctrl_good", ledger_controller_id="ctrl_good")
        new = new_instance(tmp_path)
        write_controller(new, "a_good.yml", controller_id="ctrl_good")
        write_controller(
            new, "b_other.yml", controller_id="", controller_name="pmm_simple"
        )

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions == {"ctrl_good": "copied"}

    def test_a_valid_deploy_is_unaffected(self, tmp_path):
        """The accept path still works end to end."""
        src = make_source(tmp_path)
        write_ledger(src, "range_inventory_ladder_ctrl_a.json", owner_id="ctrl_a")
        write_ledger(src, "range_inventory_ladder_ctrl_b.json", owner_id="ctrl_b", ledger_controller_id="ctrl_b")
        new = new_instance(tmp_path)
        write_controller(new, "a.yml", controller_id="ctrl_a")
        write_controller(new, "b.yml", controller_id="ctrl_b")

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions == {"ctrl_a": "copied", "ctrl_b": "copied"}
