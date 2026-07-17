"""Phase 4 — CDX-M02: the versioned ledger-envelope contract, API half.

Three things are under test:

  * ``services.ledger_envelope_contract.classify_ledger_envelope`` — the ONE
    api-side envelope predicate, tested directly as a spec table.
  * ``compute_copy_plan`` — an invalid envelope ABORTS the deploy with
    ``LEDGER_INVALID``. The old validator checked length + UTF-8 + ``json.loads``
    and stopped, so ``{"levels": [1, 2]}`` was "valid": it got copied forward as a
    resumed ledger and then quarantined engine-side on load, re-seeding from the
    wallet — the insufficient-funds bug the hook exists to prevent.
  * the MIRROR itself — that the constants this module copies from the engine
    still equal the engine's. A mirror nobody checks is a mirror that drifts.

EVERY expected value below is derived from the ENGINE's load-time contract —
``hummingbot/controllers/market_making/range_inventory_ladder.py`` — read
read-only, never by running the API's implementation. Where a test needs a value
the engine defines, it is transcribed here with its engine line cited, so a
reviewer can check the transcription against the engine without trusting this
suite. The engine sources:

    _validate_loaded_state          :1962   the gate the API mirrors
    required_keys                   :1967-1983
    schema_version int() + membership :1995-2003
    SUPPORTED_STATE_SCHEMA_VERSIONS :1386   {6..10}
    STATE_SCHEMA_VERSION = 10       :1385   (writer stamps it at :2329)
    identity comparisons            :2005-2028
    initialized is not True         :2029
    numeric fields loop             :2033-2047 (finite via _safe_decimal :165,
                                                non-negative :2045)

Real ``tmp_path`` filesystems throughout. No Docker, no DB, no network — the copy
plan is pure computation over staged YAML plus the source ``data/`` tree, so
nothing here needs a mock.
"""

import json
from types import SimpleNamespace

import pytest
import yaml

from ledger_fixtures import valid_ledger_payload
from services.ledger_envelope_contract import (
    REQUIRED_LEDGER_KEYS,
    SUPPORTED_LEDGER_SCHEMA_VERSIONS,
    classify_ledger_envelope,
)
from services.resume_service import (
    ResolvedSource,
    ResumeAbortReason,
    ResumeError,
    compute_copy_plan,
)


# ---------------------------------------------------------------------------
# The ENGINE's contract, transcribed by hand from the read-only engine source.
#
# These are the SPEC. They are deliberately NOT imported from the implementation
# — asserting the implementation against itself would pass no matter how far both
# drifted from the engine. Cited line by line so the transcription is checkable.
# ---------------------------------------------------------------------------

# range_inventory_ladder.py:1967-1983
ENGINE_REQUIRED_KEYS = {
    "schema_version",
    "controller_name",
    "controller_type",
    "controller_id",
    "connector_name",
    "trading_pair",
    "base_asset",
    "quote_asset",
    "initialized",
    "reserve_quote_balance",
    "reserve_base_balance",
    "initial_managed_quote",
    "initial_claimed_base_amount",
    "initial_reference_price",
    "initialized_timestamp",
}

# range_inventory_ladder.py:1386
ENGINE_SUPPORTED_SCHEMA_VERSIONS = {6, 7, 8, 9, 10}

# range_inventory_ladder.py:2033-2039
ENGINE_NUMERIC_KEYS = [
    "reserve_quote_balance",
    "reserve_base_balance",
    "initial_managed_quote",
    "initial_claimed_base_amount",
    "initial_reference_price",
    "initialized_timestamp",
]

CANON_ID = "ctrl_a"


# ---------------------------------------------------------------------------
# Helpers — local, per the convention the C1/C2 phases set: these tests pin a
# fail-closed money guard, and a future edit to another phase's fixtures must not
# be able to quietly change what they assert. The one shared import is
# ``valid_ledger_payload``, which IS this phase's spec artifact — and
# ``test_mirror_matches_engine_*`` below pins it to the engine so an edit that
# weakens it fails here rather than silently blessing bad ledgers everywhere.
# ---------------------------------------------------------------------------

def make_dep():
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


def write_controller(new_instance_dir, filename="c.yml", *, controller_id=CANON_ID, **extra):
    cdir = new_instance_dir / "conf" / "controllers"
    cdir.mkdir(parents=True, exist_ok=True)
    doc = {
        "id": controller_id,
        "controller_name": "range_inventory_ladder",
        "controller_type": "market_making",
    }
    doc.update(extra)
    (cdir / filename).write_text(yaml.safe_dump(doc), encoding="utf-8")


def write_raw_ledger(source, controller_id=CANON_ID, *, text=None, payload=None, owner_id=CANON_ID):
    """Write a ledger + ``.owner`` sidecar. ``text`` writes bytes verbatim (for
    JSON the fixture builder cannot express, e.g. ``NaN``); ``payload`` dumps a
    dict. The sidecar always matches so OWNER_MISMATCH can never be what a
    LEDGER_INVALID assertion is actually catching."""
    name = f"range_inventory_ladder_{controller_id}.json"
    body = text if text is not None else json.dumps(payload)
    (source.data_dir / name).write_text(body, encoding="utf-8")
    (source.data_dir / f"{name}.owner").write_text(
        json.dumps({"controller_id": owner_id, "pid": 1}), encoding="utf-8"
    )
    return name


# ===========================================================================
# 1.  The mirror itself — does the API still agree with the engine?
# ===========================================================================

class TestMirrorMatchesEngine:
    """The API's constants are a hand-copy of the engine's. Pin the copy.

    Keeping these in sync is a recorded, permanent obligation of the batch; these
    tests are what makes a broken sync loud instead of silent.
    """

    def test_mirror_matches_engine_required_keys(self):
        assert set(REQUIRED_LEDGER_KEYS) == ENGINE_REQUIRED_KEYS

    def test_mirror_matches_engine_supported_versions(self):
        # range_inventory_ladder.py:1386. If the engine widens or NARROWS this
        # set and the mirror is not updated, this fails — which is the point:
        # a narrowed engine set that the API still accepts costs a wallet re-seed.
        assert set(SUPPORTED_LEDGER_SCHEMA_VERSIONS) == ENGINE_SUPPORTED_SCHEMA_VERSIONS

    def test_shared_fixture_is_exactly_the_engines_required_keys(self):
        # Pins the shared spec artifact: minimal means minimal. If someone adds a
        # key here to make an unrelated test pass, this fails.
        assert set(valid_ledger_payload().keys()) == ENGINE_REQUIRED_KEYS


# ===========================================================================
# 2.  The predicate, as a spec table
# ===========================================================================

class TestValidEnvelope:
    def test_minimal_v10_fixture_is_accepted(self):
        """The engine's minimal accepted envelope must not be refused.

        This is the over-strictness guard: every other test here proves the gate
        REJECTS. Without this one, `return _invalid(...)` unconditionally would be
        a passing implementation.
        """
        verdict = classify_ledger_envelope(
            valid_ledger_payload(CANON_ID), canonical_controller_id=CANON_ID
        )
        assert verdict.is_valid, verdict.reason
        assert verdict.reason == ""

    @pytest.mark.parametrize("version", sorted(ENGINE_SUPPORTED_SCHEMA_VERSIONS))
    def test_every_engine_supported_version_is_accepted(self, version):
        verdict = classify_ledger_envelope(
            valid_ledger_payload(CANON_ID, schema_version=version),
            canonical_controller_id=CANON_ID,
        )
        assert verdict.is_valid, verdict.reason

    def test_extra_unknown_keys_are_accepted(self):
        """A real ledger carries far more than required_keys (positions,
        executors, owned_quote...). The engine's gate is required_keys-based, not
        a whitelist (:1984 checks only for MISSING keys), so a fuller ledger must
        still pass — otherwise the validator would reject every real ledger."""
        verdict = classify_ledger_envelope(
            valid_ledger_payload(CANON_ID, owned_quote="5", positions_held=[{"x": 1}]),
            canonical_controller_id=CANON_ID,
        )
        assert verdict.is_valid, verdict.reason


class TestShape:
    """range_inventory_ladder.py:1963 — 'State payload must be a JSON object'."""

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param([], id="empty_list"),
            pytest.param(None, id="null"),
            pytest.param([{"schema_version": 10}], id="list_of_objects"),
            pytest.param("just a string", id="string"),
            pytest.param(10, id="int"),
            pytest.param(True, id="bool"),
        ],
    )
    def test_non_object_payload_rejected(self, payload):
        verdict = classify_ledger_envelope(payload, canonical_controller_id=CANON_ID)
        assert not verdict.is_valid

    def test_empty_object_rejected(self):
        # {} IS a dict, so this is the missing-keys gate, not the shape gate.
        verdict = classify_ledger_envelope({}, canonical_controller_id=CANON_ID)
        assert not verdict.is_valid
        assert "missing required key" in verdict.reason


class TestRequiredKeys:
    @pytest.mark.parametrize("missing_key", sorted(ENGINE_REQUIRED_KEYS))
    def test_each_required_key_is_required(self, missing_key):
        """Every one of the engine's 15 required keys, dropped one at a time.

        Parametrized off the SPEC set, so a key the implementation forgot to
        require fails here — the implementation's own constant is not consulted.
        """
        payload = valid_ledger_payload(CANON_ID)
        del payload[missing_key]
        verdict = classify_ledger_envelope(payload, canonical_controller_id=CANON_ID)
        assert not verdict.is_valid
        assert missing_key in verdict.reason

    def test_missing_schema_version_names_the_pre_v6_case(self):
        # :1986-1991 — the engine calls this out specially because it means a
        # pre-v6 ledger, which quarantines and re-seeds from the wallet.
        payload = valid_ledger_payload(CANON_ID)
        del payload["schema_version"]
        verdict = classify_ledger_envelope(payload, canonical_controller_id=CANON_ID)
        assert not verdict.is_valid
        assert "schema_version" in verdict.reason


class TestSchemaVersion:
    @pytest.mark.parametrize("version", [5, 11, 0, -1, 99])
    def test_unsupported_version_rejected(self, version):
        """:1999-2003 — 'Unsupported state schema_version'.

        5 and 11 are the boundaries of the engine's {6..10}: 5 is the rollback
        case (a newer ledger meeting an older engine) that CDX-M02 exists for.
        """
        verdict = classify_ledger_envelope(
            valid_ledger_payload(CANON_ID, schema_version=version),
            canonical_controller_id=CANON_ID,
        )
        assert not verdict.is_valid
        assert "schema_version" in verdict.reason

    @pytest.mark.parametrize(
        "version",
        [
            pytest.param("10", id="str_10"),
            pytest.param(10.0, id="float_10"),
            pytest.param(True, id="bool_true"),
            pytest.param(None, id="none"),
            pytest.param([10], id="list"),
        ],
    )
    def test_non_int_version_rejected(self, version):
        """The API is deliberately STRICTER than the engine's int() coercion
        (:1995): the engine's writer only ever emits an int (:2329), so any other
        type means the file was not written by the engine. Uncertainty is a
        refusal. ``True`` matters specifically because isinstance(True, int) is
        True in Python — without the explicit bool exclusion it would reach the
        membership test as 1."""
        verdict = classify_ledger_envelope(
            valid_ledger_payload(CANON_ID, schema_version=version),
            canonical_controller_id=CANON_ID,
        )
        assert not verdict.is_valid
        assert "schema_version" in verdict.reason


class TestIdentity:
    def test_internal_id_mismatch_rejected(self):
        """:2013 — the engine compares the ledger's controller_id against its
        config id and quarantines on mismatch. Copying another controller's
        ledger forward is the wrong-ledger case the .owner sidecar also guards."""
        verdict = classify_ledger_envelope(
            valid_ledger_payload("somebody_else"), canonical_controller_id=CANON_ID
        )
        assert not verdict.is_valid
        assert "controller_id" in verdict.reason

    @pytest.mark.parametrize(
        "ledger_id",
        [
            pytest.param(" ctrl_a ", id="padded"),
            pytest.param("ctrl_a ", id="trailing_space"),
            pytest.param("\tctrl_a", id="leading_tab"),
        ],
    )
    def test_padded_internal_id_is_a_mismatch_not_a_match(self, ledger_id):
        """The ledger's id is compared VERBATIM — never stripped.

        This is the fail-open guard, not a nit. The engine compares
        ``raw.get("controller_id") != self.config.id`` (:2013) and does NOT strip
        the LEDGER's copy; C2 stripped only the CONFIG id. So a ledger carrying
        " ctrl_a " against config id "ctrl_a" is a genuine mismatch that the
        engine quarantines. If the API stripped it and called it a match, the API
        would bless a ledger the engine is guaranteed to reject — exactly the
        fail-open class CDX-M02 was filed for.
        """
        verdict = classify_ledger_envelope(
            valid_ledger_payload(ledger_id), canonical_controller_id=CANON_ID
        )
        assert not verdict.is_valid
        assert "controller_id" in verdict.reason

    @pytest.mark.parametrize(
        "field,bad_value",
        [
            ("controller_name", "some_other_controller"),
            ("controller_type", "directional_trading"),
            ("connector_name", "binance"),
            ("trading_pair", "BTC-USDT"),
        ],
    )
    def test_staged_config_mismatch_rejected(self, field, bad_value):
        """:2005-2024 — each identity field is compared against the resolved
        config; a mismatch quarantines engine-side."""
        expected = {
            "expected_controller_name": "range_inventory_ladder",
            "expected_controller_type": "market_making",
            "expected_connector_name": "nonkyc",
            "expected_trading_pair": "XMR-USDT",
        }
        verdict = classify_ledger_envelope(
            valid_ledger_payload(CANON_ID, **{field: bad_value}),
            canonical_controller_id=CANON_ID,
            **expected,
        )
        assert not verdict.is_valid
        assert field in verdict.reason

    def test_matching_staged_config_accepted(self):
        verdict = classify_ledger_envelope(
            valid_ledger_payload(CANON_ID),
            canonical_controller_id=CANON_ID,
            expected_controller_name="range_inventory_ladder",
            expected_controller_type="market_making",
            expected_connector_name="nonkyc",
            expected_trading_pair="XMR-USDT",
        )
        assert verdict.is_valid, verdict.reason

    def test_unknown_expected_value_skips_that_comparison(self):
        """None means 'the staged config did not carry it'. The API holds a staged
        YAML, not the engine's resolved Pydantic config, so it cannot know the
        model default — skipping is honest; guessing would abort real deploys.
        The ledger's own field is still type-checked (see TestFieldTypes)."""
        verdict = classify_ledger_envelope(
            valid_ledger_payload(CANON_ID, connector_name="anything_at_all"),
            canonical_controller_id=CANON_ID,
            expected_connector_name=None,
        )
        assert verdict.is_valid, verdict.reason


class TestFieldTypes:
    @pytest.mark.parametrize(
        "field",
        ["controller_name", "controller_type", "controller_id", "connector_name",
         "trading_pair", "base_asset", "quote_asset"],
    )
    @pytest.mark.parametrize(
        "bad_value",
        [pytest.param(123, id="int"), pytest.param(None, id="none"),
         pytest.param("", id="empty"), pytest.param("   ", id="whitespace"),
         pytest.param(["x"], id="list")],
    )
    def test_identity_fields_must_be_non_empty_strings(self, field, bad_value):
        # Mutated after the build rather than passed through the builder: some of
        # these field names are the builder's own parameters, and a bad value has
        # to reach the VALIDATOR, not the fixture's derivation logic.
        payload = valid_ledger_payload(CANON_ID)
        payload[field] = bad_value
        verdict = classify_ledger_envelope(payload, canonical_controller_id=CANON_ID)
        assert not verdict.is_valid

    @pytest.mark.parametrize(
        "value",
        [pytest.param(False, id="false"), pytest.param(1, id="int_1"),
         pytest.param("true", id="str_true"), pytest.param(None, id="none")],
    )
    def test_initialized_must_be_exactly_true(self, value):
        """:2029 uses ``is not True`` — a truthy 1 is a rejection engine-side, so
        it must be one here."""
        verdict = classify_ledger_envelope(
            valid_ledger_payload(CANON_ID, initialized=value),
            canonical_controller_id=CANON_ID,
        )
        assert not verdict.is_valid
        assert "initialized" in verdict.reason


class TestNumericFields:
    @pytest.mark.parametrize("field", ENGINE_NUMERIC_KEYS)
    def test_negative_rejected(self, field):
        # :2045-2046 — 'must be non-negative'.
        verdict = classify_ledger_envelope(
            valid_ledger_payload(CANON_ID, **{field: "-1"}),
            canonical_controller_id=CANON_ID,
        )
        assert not verdict.is_valid
        assert field in verdict.reason

    @pytest.mark.parametrize("field", ENGINE_NUMERIC_KEYS)
    @pytest.mark.parametrize(
        "bad_value",
        [pytest.param("abc", id="not_a_number"), pytest.param(None, id="none"),
         pytest.param("", id="blank"), pytest.param([1], id="list"),
         pytest.param(float("nan"), id="nan"), pytest.param(float("inf"), id="inf")],
    )
    def test_unparseable_or_non_finite_rejected(self, field, bad_value):
        """Mirrors _safe_decimal (:165): blank raises, unparseable raises, and
        non-finite raises. NaN/Infinity matter because json.loads PARSES them by
        default — a NaN reserve balance would poison every figure the controller
        derives from it."""
        verdict = classify_ledger_envelope(
            valid_ledger_payload(CANON_ID, **{field: bad_value}),
            canonical_controller_id=CANON_ID,
        )
        assert not verdict.is_valid
        assert field in verdict.reason

    @pytest.mark.parametrize(
        "value", ["0", "0.0", "100", "1e3", 100, 100.5, "  12  "]
    )
    def test_valid_decimal_forms_accepted(self, value):
        """_safe_decimal is Decimal(str(value)) — these all parse, so the API must
        not be stricter than the engine here."""
        verdict = classify_ledger_envelope(
            valid_ledger_payload(CANON_ID, initial_managed_quote=value),
            canonical_controller_id=CANON_ID,
        )
        assert verdict.is_valid, verdict.reason


# ===========================================================================
# 3.  The real path — compute_copy_plan aborts the deploy
# ===========================================================================

class TestComputeCopyPlanIntegration:
    """Real staged YAML + a real source data/ tree on tmp_path. Nothing mocked."""

    def test_valid_ledger_is_copied(self, tmp_path):
        src = make_source(tmp_path)
        write_raw_ledger(src, payload=valid_ledger_payload(CANON_ID))
        new = new_instance(tmp_path)
        write_controller(new)

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions == {CANON_ID: "copied"}
        assert {it.kind for it in plan.files_to_copy} == {"ledger", "owner"}

    def test_syntactically_valid_but_not_an_envelope_aborts(self, tmp_path):
        """THE regression test for CDX-M02.

        ``{"levels": [1, 2], "seq": 7}`` is valid JSON and non-empty, so the OLD
        validator (length + UTF-8 + json.loads) accepted it and copied it forward.
        The engine then quarantines it on load and re-seeds from the wallet. This
        test fails by construction on the old implementation.
        """
        src = make_source(tmp_path)
        write_raw_ledger(src, payload={"levels": [1, 2], "seq": 7})
        new = new_instance(tmp_path)
        write_controller(new)

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason == ResumeAbortReason.LEDGER_INVALID

    @pytest.mark.parametrize(
        "text",
        [pytest.param("{}", id="empty_object"), pytest.param("[]", id="list"),
         pytest.param("null", id="null"), pytest.param('"a string"', id="string")],
    )
    def test_non_envelope_json_documents_abort(self, tmp_path, text):
        src = make_source(tmp_path)
        write_raw_ledger(src, text=text)
        new = new_instance(tmp_path)
        write_controller(new)

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason == ResumeAbortReason.LEDGER_INVALID

    @pytest.mark.parametrize("version", [5, 11])
    def test_unsupported_version_aborts(self, tmp_path, version):
        src = make_source(tmp_path)
        write_raw_ledger(src, payload=valid_ledger_payload(CANON_ID, schema_version=version))
        new = new_instance(tmp_path)
        write_controller(new)

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason == ResumeAbortReason.LEDGER_INVALID
        assert "schema_version" in exc.value.message

    def test_internal_id_mismatch_aborts(self, tmp_path):
        """The ledger is named for ctrl_a and its .owner says ctrl_a, but its
        INTERNAL controller_id says otherwise — so this abort can only be the
        envelope's identity check, not the filename or the sidecar."""
        src = make_source(tmp_path)
        write_raw_ledger(src, payload=valid_ledger_payload("a_different_controller"))
        new = new_instance(tmp_path)
        write_controller(new)

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason == ResumeAbortReason.LEDGER_INVALID
        assert "controller_id" in exc.value.message

    def test_nan_in_ledger_aborts(self, tmp_path):
        """json.loads parses bare NaN by default, so this reaches the envelope."""
        src = make_source(tmp_path)
        payload = valid_ledger_payload(CANON_ID)
        payload["reserve_quote_balance"] = float("nan")
        write_raw_ledger(src, text=json.dumps(payload))  # emits bare NaN
        new = new_instance(tmp_path)
        write_controller(new)

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason == ResumeAbortReason.LEDGER_INVALID

    def test_identity_uses_the_c2_canonical_id(self, tmp_path):
        """C2 consistency: the staged id is ' ctrl_a ' (padded), whose canonical
        form is 'ctrl_a'. The ledger's internal id is 'ctrl_a'. The envelope check
        must compare against the CANONICAL staged id, so this is a match and the
        deploy proceeds. Comparing against the raw staged id would abort a valid
        deploy."""
        src = make_source(tmp_path)
        write_raw_ledger(src, payload=valid_ledger_payload(CANON_ID))
        new = new_instance(tmp_path)
        write_controller(new, controller_id="  ctrl_a  ")

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions == {CANON_ID: "copied"}

    def test_staged_config_identity_mismatch_aborts(self, tmp_path):
        """The staged config names a connector the ledger does not — the engine
        would quarantine on load (:2017), so the API refuses to deploy."""
        src = make_source(tmp_path)
        write_raw_ledger(src, payload=valid_ledger_payload(CANON_ID, connector_name="nonkyc"))
        new = new_instance(tmp_path)
        write_controller(new, connector_name="binance")

        with pytest.raises(ResumeError) as exc:
            compute_copy_plan(new, src, make_dep())
        assert exc.value.reason == ResumeAbortReason.LEDGER_INVALID
        assert "connector_name" in exc.value.message

    def test_missing_ledger_still_fresh_seeds(self, tmp_path):
        """The envelope gate must not turn a legitimately absent ledger (a newly
        added controller) into an abort — that path is decided before validation
        and stays a fresh_seed."""
        src = make_source(tmp_path)  # empty source data/
        new = new_instance(tmp_path)
        write_controller(new, controller_id="ctrl_new")

        plan = compute_copy_plan(new, src, make_dep())

        assert plan.decisions == {"ctrl_new": "fresh_seed"}
