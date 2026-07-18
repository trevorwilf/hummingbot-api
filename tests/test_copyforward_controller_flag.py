"""CTRLRESUME tests: controller-YAML-driven resume (flag, strip, resolver).

Phase 1 (spec §1 "The flag", §2 "Strip at staging"):

  * ``parse_controller_resume_flag`` — the ONE shared parser (staging + preview):
    latest / off (quoted and the YAML-1.1 unquoted-``off``-is-``False`` form) /
    absent-is-off; nested and ``resume_mode_*`` keys are NOT the flag; a bad
    value aborts ``RESUME_FLAG_INVALID``; malformed yml is non-flagged.
  * ``stage_controller_config`` — the strip: flagged ymls lose ONLY the
    top-level ``resume_mode:`` line (trailing comment included), byte-identical
    elsewhere (comments, ``[SET-ONCE]`` annotations and CRLF endings survive);
    non-flagged ymls are returned untouched; flow-style (unstrippable) fails
    closed via the post-strip verification.
  * ``DockerService._stage_controller_configs`` — the staging call site: writes
    the stripped copy for flagged ymls, plain byte-identical ``copy2`` for
    non-flagged, collects ``{controller_file: mode}``, raises on an invalid flag,
    warns-and-skips a missing source.

Phase 2 (spec §4 "Controller-identity resolution"):

  * ``resolve_controller_flag_source`` — identity-keyed search across
    ``instances/`` AND ``archived/`` (incl. a singly-nested archive; an
    ambiguous nest aborts ``ARCHIVE_NESTED``); ordering by the containing
    instance's NAME timestamp, never mtime (deliberately perturbed mtimes);
    newest tie -> ``LATEST_AMBIGUOUS``; divergent per-controller winners ->
    ``CONTROLLER_SOURCES_DIVERGENT``; identity verified on the WINNER ONLY
    (mismatched/corrupt ``.owner`` aborts with NO fallback to an older
    candidate); zero candidates for ALL flagged controllers -> the true
    first-run signal (the ONLY fresh-seed); found-but-unrankable history is a
    refusal, never a fresh seed; absolute ``state_file_name`` on a flagged
    controller aborts; the instance being created is excluded.

All expected values are derived from the SPEC (accepted values are exactly
``"latest"`` and ``"off"``; the staged file is byte-identical to the source
minus the removed line), never captured by running the implementation. All
filesystem via ``tmp_path``; no Docker, no DB, no network.
"""

import json
import logging
import os

import yaml

import pytest

from models.bot_orchestration import V2ControllerDeployment
from services.docker_service import DockerService
from services.resume_service import (
    ControllerResumeFlag,
    ResolvedSource,
    ResumeAbortReason,
    ResumeError,
    parse_controller_resume_flag,
    resolve_controller_flag_source,
    stage_controller_config,
)


# ---------------------------------------------------------------------------
# §1 The flag — parse_controller_resume_flag
# ---------------------------------------------------------------------------

class TestParseFlag:
    def test_absent_key_is_off_and_not_present(self):
        text = "controller_name: range_ladder\nconnector_name: kraken\n"
        assert parse_controller_resume_flag(text, controller_name="c.yml") == (
            ControllerResumeFlag(present=False, mode="off")
        )

    def test_latest_value(self):
        text = "controller_name: x\nresume_mode: latest\n"
        assert parse_controller_resume_flag(text, controller_name="c.yml") == (
            ControllerResumeFlag(present=True, mode="latest")
        )

    def test_off_unquoted_is_yaml_false_accepted_as_off(self):
        # YAML 1.1 coerces an unquoted ``off`` to the boolean False; the
        # documented ``off`` spelling must still be accepted as present+off.
        text = "resume_mode: off\ncontroller_name: x\n"
        assert yaml.safe_load(text)["resume_mode"] is False  # guards the premise
        assert parse_controller_resume_flag(text, controller_name="c.yml") == (
            ControllerResumeFlag(present=True, mode="off")
        )

    def test_off_quoted_string(self):
        text = 'resume_mode: "off"\ncontroller_name: x\n'
        assert parse_controller_resume_flag(text, controller_name="c.yml") == (
            ControllerResumeFlag(present=True, mode="off")
        )

    def test_invalid_value_typo_aborts(self):
        text = "resume_mode: latst\ncontroller_name: x\n"
        with pytest.raises(ResumeError) as ei:
            parse_controller_resume_flag(text, controller_name="c.yml")
        assert ei.value.reason == ResumeAbortReason.RESUME_FLAG_INVALID

    def test_truthy_synonym_on_aborts(self):
        # ``on`` -> YAML True, which is NOT an accepted value (only latest/off).
        text = "resume_mode: on\ncontroller_name: x\n"
        assert yaml.safe_load(text)["resume_mode"] is True
        with pytest.raises(ResumeError) as ei:
            parse_controller_resume_flag(text, controller_name="c.yml")
        assert ei.value.reason == ResumeAbortReason.RESUME_FLAG_INVALID

    def test_numeric_zero_does_not_masquerade_as_off(self):
        # 0 == False in Python; the parser must use identity, not equality, so a
        # numeric flag is rejected rather than silently treated as off.
        text = "resume_mode: 0\ncontroller_name: x\n"
        with pytest.raises(ResumeError) as ei:
            parse_controller_resume_flag(text, controller_name="c.yml")
        assert ei.value.reason == ResumeAbortReason.RESUME_FLAG_INVALID

    def test_nested_resume_mode_is_not_the_flag(self):
        # An indented resume_mode under another key is NOT a top-level flag.
        text = "advanced:\n  resume_mode: latest\ncontroller_name: x\n"
        assert parse_controller_resume_flag(text, controller_name="c.yml") == (
            ControllerResumeFlag(present=False, mode="off")
        )

    def test_prefixed_key_is_not_the_flag(self):
        text = "resume_mode_notes: hello\ncontroller_name: x\n"
        assert parse_controller_resume_flag(text, controller_name="c.yml") == (
            ControllerResumeFlag(present=False, mode="off")
        )

    def test_malformed_yaml_is_treated_as_non_flagged(self):
        # Unparseable text carrying NO top-level resume_mode line can hold no
        # usable flag; report non-flagged so staging keeps today's plain copy and
        # the engine stays the backstop.
        text = "controller_name: x\n  bad: : indentation :\n:::\n"
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load(text)  # guards the premise: this really is malformed
        assert parse_controller_resume_flag(text, controller_name="c.yml") == (
            ControllerResumeFlag(present=False, mode="off")
        )

    # -- CDX-R01: YAML false-aliases that are NOT ``off`` must be rejected ------
    # ``false``/``no``/``OFF``/``NO`` all coerce to the SAME Python ``False`` as
    # the documented ``off``; validating on the raw spelling is what keeps them
    # from silently masquerading as off. Accepted values are EXACTLY ``latest``
    # and ``off`` (spec §1).
    @pytest.mark.parametrize("bad_value", ["false", "no", "OFF", "NO", "Off", "FALSE"])
    def test_false_aliases_that_are_not_off_are_rejected(self, bad_value):
        text = f"resume_mode: {bad_value}\ncontroller_name: x\n"
        # Premise guard: PyYAML really does coerce this spelling to boolean False,
        # so a safe_load-based check could NOT distinguish it from ``off``.
        assert yaml.safe_load(text)["resume_mode"] is False
        with pytest.raises(ResumeError) as ei:
            parse_controller_resume_flag(text, controller_name="c.yml")
        assert ei.value.reason == ResumeAbortReason.RESUME_FLAG_INVALID

    def test_uppercase_latest_is_rejected(self):
        # ``off`` and ``latest`` are the exact spellings; ``LATEST`` is not one.
        text = "resume_mode: LATEST\ncontroller_name: x\n"
        with pytest.raises(ResumeError) as ei:
            parse_controller_resume_flag(text, controller_name="c.yml")
        assert ei.value.reason == ResumeAbortReason.RESUME_FLAG_INVALID

    def test_collection_valued_flag_is_rejected(self):
        # A present-but-non-scalar resume_mode value is not a valid flag.
        text = "resume_mode:\n  - latest\ncontroller_name: x\n"
        with pytest.raises(ResumeError) as ei:
            parse_controller_resume_flag(text, controller_name="c.yml")
        assert ei.value.reason == ResumeAbortReason.RESUME_FLAG_INVALID

    # -- CDX-R02: malformed YAML that still carries the top-level flag line -----
    def test_malformed_yaml_carrying_the_flag_line_aborts(self):
        # safe_load/compose both fail, but the engine-forbidden key is lexically
        # present and cannot be safely stripped -> fail closed at staging rather
        # than routing the key to the engine via the plain-copy path.
        text = "resume_mode: latest\ncontroller_name: [\n"
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load(text)  # premise: genuinely malformed
        with pytest.raises(ResumeError) as ei:
            parse_controller_resume_flag(text, controller_name="c.yml")
        assert ei.value.reason == ResumeAbortReason.RESUME_FLAG_INVALID


# ---------------------------------------------------------------------------
# §2 Strip at staging — stage_controller_config (pure)
# ---------------------------------------------------------------------------

class TestStripByteIdentity:
    def test_flagged_latest_removes_only_the_flag_line(self):
        source = (
            "controller_name: range_ladder\n"
            "resume_mode: latest\n"
            "connector_name: kraken\n"
        )
        expected = (
            "controller_name: range_ladder\n"
            "connector_name: kraken\n"
        )
        staged, flag = stage_controller_config(source, controller_name="c.yml")
        # Byte-identical to the source minus exactly the resume_mode line.
        assert staged == expected
        assert flag == ControllerResumeFlag(present=True, mode="latest")
        # And the spec's fail-closed post-conditions, asserted independently.
        assert "resume_mode" not in yaml.safe_load(staged)
        assert yaml.safe_load(staged) == {
            "controller_name": "range_ladder",
            "connector_name": "kraken",
        }

    def test_source_string_is_not_mutated(self):
        source = "resume_mode: latest\ncontroller_name: x\n"
        original = str(source)
        stage_controller_config(source, controller_name="c.yml")
        assert source == original

    def test_trailing_comment_on_flag_line_is_removed_with_the_line(self):
        source = (
            "controller_name: x\n"
            "resume_mode: latest  # [SET-ONCE] resume from prior run\n"
            "connector_name: kraken\n"
        )
        expected = "controller_name: x\nconnector_name: kraken\n"
        staged, flag = stage_controller_config(source, controller_name="c.yml")
        assert staged == expected
        assert flag.mode == "latest"

    def test_comment_heavy_yml_survives_strip_byte_identical(self):
        source = (
            "# Range ladder controller [SET-ONCE]\n"
            "controller_name: range_ladder  # [SET-ONCE] do not edit\n"
            "resume_mode: latest  # [SET-ONCE] enable copy-forward resume\n"
            "connector_name: kraken\n"
            "\n"
            "# sizing block below\n"
            "total_amount_quote: 1000  # [SET-ONCE]\n"
        )
        expected = (
            "# Range ladder controller [SET-ONCE]\n"
            "controller_name: range_ladder  # [SET-ONCE] do not edit\n"
            "connector_name: kraken\n"
            "\n"
            "# sizing block below\n"
            "total_amount_quote: 1000  # [SET-ONCE]\n"
        )
        staged, _ = stage_controller_config(source, controller_name="c.yml")
        assert staged == expected

    def test_crlf_line_endings_preserved(self):
        source = "a: 1\r\nresume_mode: latest\r\nb: 2\r\n"
        expected = "a: 1\r\nb: 2\r\n"
        staged, flag = stage_controller_config(source, controller_name="c.yml")
        assert staged == expected
        assert flag.present is True

    def test_off_flag_line_is_also_stripped(self):
        # The engine forbids the key regardless of value: an ``off`` flag must
        # be stripped too, not just ``latest``.
        source = "controller_name: x\nresume_mode: off\nconnector_name: k\n"
        expected = "controller_name: x\nconnector_name: k\n"
        staged, flag = stage_controller_config(source, controller_name="c.yml")
        assert staged == expected
        assert flag == ControllerResumeFlag(present=True, mode="off")

    # -- CDX-R03: byte-identity when the flag is the final, UNTERMINATED line ---
    def test_final_unterminated_flag_line_keeps_preceding_line_terminator(self):
        # Source ends with the flag line and NO trailing newline. Removing it
        # must leave the preceding line and ITS terminator byte-for-byte — the
        # naive "\n".join drops that terminator.
        source = "controller_name: x\nresume_mode: latest"
        expected = "controller_name: x\n"
        staged, flag = stage_controller_config(source, controller_name="c.yml")
        assert staged == expected
        assert flag == ControllerResumeFlag(present=True, mode="latest")

    def test_final_terminated_flag_line_still_byte_identical(self):
        # The terminated counterpart: removing the last (terminated) flag line
        # leaves the earlier content untouched.
        source = "controller_name: x\nresume_mode: latest\n"
        expected = "controller_name: x\n"
        staged, _ = stage_controller_config(source, controller_name="c.yml")
        assert staged == expected

    def test_unterminated_non_flag_final_line_is_preserved(self):
        # The flag is NOT last here; the unterminated final content line must
        # survive with no terminator added.
        source = "resume_mode: latest\ncontroller_name: x"
        expected = "controller_name: x"
        staged, _ = stage_controller_config(source, controller_name="c.yml")
        assert staged == expected


class TestStripLeavesNonFlaggedUntouched:
    def test_non_flagged_returned_verbatim(self):
        source = "controller_name: x\nconnector_name: kraken\n# tail\n"
        staged, flag = stage_controller_config(source, controller_name="c.yml")
        assert staged == source
        assert flag == ControllerResumeFlag(present=False, mode="off")

    def test_nested_resume_mode_is_not_stripped(self):
        source = (
            "controller_name: x\n"
            "advanced:\n"
            "  resume_mode: latest\n"
            "connector_name: kraken\n"
        )
        staged, flag = stage_controller_config(source, controller_name="c.yml")
        assert staged == source  # nested key preserved byte-identical
        assert flag.present is False

    def test_prefixed_key_is_not_stripped(self):
        source = "resume_mode_notes: keep me\ncontroller_name: x\n"
        staged, flag = stage_controller_config(source, controller_name="c.yml")
        assert staged == source
        assert flag.present is False


class TestPostStripVerificationFailsClosed:
    def test_flow_style_mapping_cannot_be_stripped_and_aborts(self):
        # safe_load sees the top-level key (present), but the line filter cannot
        # remove it from a flow-style mapping; the post-strip verification must
        # catch the surviving key and abort rather than ship it to the engine.
        source = "{resume_mode: latest, controller_name: x}\n"
        with pytest.raises(ResumeError) as ei:
            stage_controller_config(source, controller_name="c.yml")
        assert ei.value.reason == ResumeAbortReason.RESUME_FLAG_INVALID


# ---------------------------------------------------------------------------
# §2 Strip at staging — DockerService._stage_controller_configs (call site)
# ---------------------------------------------------------------------------

def _write(dir_path, name, text, *, encoding="utf-8", newline=""):
    path = dir_path / name
    with open(path, "w", encoding=encoding, newline=newline) as fh:
        fh.write(text)
    return path


class TestStageControllerConfigs:
    def test_flagged_yml_is_stripped_and_flag_collected(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        _write(src, "ladder.yml", "controller_name: x\nresume_mode: latest\nconnector_name: k\n")

        flags = DockerService._stage_controller_configs(["ladder.yml"], str(src), str(dst))

        with open(dst / "ladder.yml", "r", encoding="utf-8", newline="") as fh:
            staged = fh.read()
        assert staged == "controller_name: x\nconnector_name: k\n"
        assert flags == {"ladder.yml": "latest"}

    def test_non_flagged_yml_is_byte_identical_copy(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        # CRLF + trailing comment: a plain copy must preserve every byte.
        source_text = "controller_name: x\r\nconnector_name: k  # keep\r\n"
        _write(src, "plain.yml", source_text)

        flags = DockerService._stage_controller_configs(["plain.yml"], str(src), str(dst))

        assert (dst / "plain.yml").read_bytes() == (src / "plain.yml").read_bytes()
        assert flags == {"plain.yml": "off"}

    def test_flagged_staging_does_not_touch_source_file(self, tmp_path):
        # CDX-R04: the SOURCE file on disk must be byte-for-byte unchanged after
        # staging strips its flag. Asserted on the real filesystem source (not on
        # an immutable str argument, which no callee could mutate anyway).
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        source_text = "controller_name: x\nresume_mode: latest\nconnector_name: k\n"
        source_path = _write(src, "ladder.yml", source_text)
        before = source_path.read_bytes()

        DockerService._stage_controller_configs(["ladder.yml"], str(src), str(dst))

        # Source untouched; the STRIP happened only in the destination copy.
        assert source_path.read_bytes() == before
        with open(dst / "ladder.yml", "r", encoding="utf-8", newline="") as fh:
            assert fh.read() == "controller_name: x\nconnector_name: k\n"

    def test_invalid_flag_raises_and_aborts(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        _write(src, "bad.yml", "controller_name: x\nresume_mode: sometimes\n")

        with pytest.raises(ResumeError) as ei:
            DockerService._stage_controller_configs(["bad.yml"], str(src), str(dst))
        assert ei.value.reason == ResumeAbortReason.RESUME_FLAG_INVALID

    def test_malformed_flagged_yaml_aborts_and_stages_nothing(self, tmp_path):
        # CDX-R02: a malformed yml that still carries the top-level resume_mode
        # line must abort at staging (fail-closed) and leave no destination file,
        # never plain-copy the engine-forbidden key through.
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        _write(src, "bad.yml", "resume_mode: latest\ncontroller_name: [\n")

        with pytest.raises(ResumeError) as ei:
            DockerService._stage_controller_configs(["bad.yml"], str(src), str(dst))
        assert ei.value.reason == ResumeAbortReason.RESUME_FLAG_INVALID
        assert not (dst / "bad.yml").exists()

    def test_missing_source_is_warned_and_skipped(self, tmp_path, caplog):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        _write(src, "present.yml", "controller_name: x\n")

        with caplog.at_level(logging.WARNING, logger="services.docker_service"):
            flags = DockerService._stage_controller_configs(
                ["absent.yml", "present.yml"], str(src), str(dst)
            )

        # The missing controller is skipped (not in the collection, no raise);
        # the present non-flagged one is copied and recorded off.
        assert flags == {"present.yml": "off"}
        assert not (dst / "absent.yml").exists()
        # ...and the operator is WARNED, naming the absent controller and source.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "absent.yml" in warnings[0].getMessage()
        assert str(src) in warnings[0].getMessage()

    def test_mixed_batch_collects_every_controller(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        _write(src, "a.yml", "controller_name: a\nresume_mode: latest\n")
        _write(src, "b.yml", "controller_name: b\n")
        _write(src, "c.yml", "controller_name: c\nresume_mode: off\n")

        flags = DockerService._stage_controller_configs(
            ["a.yml", "b.yml", "c.yml"], str(src), str(dst)
        )
        assert flags == {"a.yml": "latest", "b.yml": "off", "c.yml": "off"}


# ---------------------------------------------------------------------------
# CDX-R05: the deploy-staging boundary must let a RESUME_FLAG_INVALID escape.
# The staging strip runs inside ``_stage_instance``'s try/except; the new
# ``except ResumeError: raise`` clause sits immediately before a broad
# ``except Exception`` that only LOGS and continues. This test drives the REAL
# ``_stage_instance`` (real script config + real invalid controller yml) so a
# regression that swallows the abort (``raise`` -> ``pass``) is caught.
# ---------------------------------------------------------------------------

class TestStageInstancePropagatesInvalidFlag:
    @pytest.mark.asyncio
    async def test_invalid_controller_flag_aborts_the_deploy(self, tmp_path, monkeypatch):
        # ``_stage_instance`` reads bots/conf/... relative to cwd (and via
        # fs_util, whose base_path is the relative "bots"): chdir into a tmp tree.
        monkeypatch.chdir(tmp_path)
        scripts_dir = tmp_path / "bots" / "conf" / "scripts"
        controllers_dir = tmp_path / "bots" / "conf" / "controllers"
        scripts_dir.mkdir(parents=True)
        controllers_dir.mkdir(parents=True)
        _write(scripts_dir, "test_script.yml", "controllers_config:\n  - bad.yml\n")
        _write(controllers_dir, "bad.yml", "controller_name: x\nresume_mode: sometimes\n")

        # Credentials dir copytree'd into the staging instance before the strip.
        creds = tmp_path / "creds"
        creds.mkdir()
        _write(creds, "conf_client.yml", "instance_id: placeholder\n")
        (tmp_path / "staging").mkdir()
        staging_dir = str(tmp_path / "staging" / "NEWBOT")

        config = V2ControllerDeployment(
            instance_name="NEWBOT",
            credentials_profile="main",
            controllers_config=["bad.yml"],
            script_config="test_script.yml",
        )
        # Skip __init__ (no Docker client needed before the fail-closed abort).
        ds = object.__new__(DockerService)

        with pytest.raises(ResumeError) as ei:
            await ds._stage_instance(config, staging_dir, "NEWBOT", str(creds))
        assert ei.value.reason == ResumeAbortReason.RESUME_FLAG_INVALID


# ===========================================================================
# Phase 2 — §4 Controller-identity resolution: resolve_controller_flag_source
# ===========================================================================

# Realistic identity per the spec's operator reality: the controller id is
# stable across dashboard deploys; instance names carry the API timestamp.
CID = "k_range_inventory_ladder_xpl_usd_V1"
CID_B = "k_range_inventory_ladder_ada_usd_V1"
LEDGER = f"range_inventory_ladder_{CID}.json"
LEDGER_B = f"range_inventory_ladder_{CID_B}.json"

NEW_NAME = "NEWBOT-20260401-000000"

# Fixed epochs for mtime perturbation (never wall-clock-dependent).
_MTIME_OLD = 946_684_800      # 2000-01-01
_MTIME_NEW = 1_900_000_000    # 2030-03-17


def _seed_config(bots_path, config_name, controller_id, state_file_name=None):
    """Write a flagged controller config yml into bots/conf/controllers/."""
    controllers_dir = bots_path / "conf" / "controllers"
    controllers_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"id: {controller_id}",
        "controller_name: range_inventory_ladder",
        "resume_mode: latest",
    ]
    if state_file_name is not None:
        lines.append(f'state_file_name: "{state_file_name}"')
    (controllers_dir / config_name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_carrier(bots_path, name, ledger_name, tree="instances",
                  owner_id=None, owner_raw=None, instance_dir=None):
    """Create an instance dir carrying ``data/<ledger_name>`` (and optionally
    its ``.owner`` sidecar). Ledger CONTENT is a stub on purpose: envelope
    validation is compute_copy_plan's job downstream, not the resolver's."""
    inst = instance_dir if instance_dir is not None else bots_path / tree / name
    data = inst / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / ledger_name).write_text('{"stub": "ledger"}', encoding="utf-8")
    if owner_raw is not None:
        (data / f"{ledger_name}.owner").write_text(owner_raw, encoding="utf-8")
    elif owner_id is not None:
        (data / f"{ledger_name}.owner").write_text(
            json.dumps({"controller_id": owner_id}), encoding="utf-8"
        )
    return inst


def _set_mtimes(inst_dir, ledger_name, epoch):
    """Perturb every mtime an mtime-ordering implementation could plausibly
    read: the instance dir, its data/ dir, and the ledger file itself."""
    for p in (inst_dir / "data" / ledger_name, inst_dir / "data", inst_dir):
        os.utime(p, (epoch, epoch))


class TestResolverInputs:
    def test_no_latest_flagged_is_programmer_error(self, tmp_path):
        # The phase-3 gate filters on the flags; calling without one is a
        # wiring bug, exactly like resolve_source with resume_mode='off'.
        with pytest.raises(ValueError):
            resolve_controller_flag_source({"a.yml": "off"}, NEW_NAME, tmp_path)

    def test_missing_flagged_config_aborts(self, tmp_path):
        # The flags dict names a config that is not on disk: identity cannot
        # be derived for a controller that ASKED to be resumed -> refusal.
        with pytest.raises(ResumeError) as ei:
            resolve_controller_flag_source({"ghost.yml": "latest"}, NEW_NAME, tmp_path)
        assert ei.value.reason == ResumeAbortReason.CONTROLLER_ID_INVALID

    def test_whitespace_controller_id_aborts(self, tmp_path):
        controllers_dir = tmp_path / "conf" / "controllers"
        controllers_dir.mkdir(parents=True)
        (controllers_dir / "a.yml").write_text(
            'id: "   "\ncontroller_name: range_inventory_ladder\n', encoding="utf-8"
        )
        with pytest.raises(ResumeError) as ei:
            resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert ei.value.reason == ResumeAbortReason.CONTROLLER_ID_INVALID

    def test_absolute_state_file_name_aborts(self, tmp_path):
        # Spec §4: flag + absolute state_file_name is a contradiction — an
        # explicit resume request on a ledger the hook cannot manage. The C1
        # opt-out skip must NOT swallow it. A perfectly good carrier exists, so
        # an implementation that "skipped" the controller instead of aborting
        # would return a first-run/resolved result and fail this test.
        _seed_config(tmp_path, "a.yml", CID, state_file_name="/var/data/ladder.json")
        _seed_carrier(tmp_path, "SRC-20260101-000000", LEDGER, owner_id=CID)
        with pytest.raises(ResumeError) as ei:
            resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert ei.value.reason == ResumeAbortReason.STATE_FILE_PATH_INVALID

    def test_traversal_state_file_name_aborts(self, tmp_path):
        _seed_config(tmp_path, "a.yml", CID, state_file_name="../escape.json")
        with pytest.raises(ResumeError) as ei:
            resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert ei.value.reason == ResumeAbortReason.STATE_FILE_PATH_INVALID


class TestResolverFirstRun:
    def test_missing_trees_is_first_run(self, tmp_path):
        # No instances/ and no archived/ at all (fresh install) counts as
        # empty -> the TRUE first-run signal, source=None, flagged populated.
        _seed_config(tmp_path, "a.yml", CID)
        res = resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert res.first_run is True
        assert res.source is None
        assert [fc.controller_id for fc in res.flagged] == [CID]
        assert res.flagged[0].ledger_name == LEDGER
        assert res.flagged[0].config_name == "a.yml"

    def test_no_carrier_anywhere_is_first_run(self, tmp_path):
        # Prior instances exist but NONE holds the flagged controller's ledger
        # -> zero candidates for all flagged controllers -> true first run.
        _seed_config(tmp_path, "a.yml", CID)
        inst = tmp_path / "instances" / "OTHER-20260101-000000" / "data"
        inst.mkdir(parents=True)
        (inst / "range_inventory_ladder_some_other_controller.json").write_text("{}", encoding="utf-8")
        res = resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert res.first_run is True
        assert res.source is None

    def test_all_carriers_unrankable_aborts_not_first_run(self, tmp_path):
        # THE cardinal-sin guard: the ledger provably exists on disk but its
        # only carrier has no parseable API timestamp. Found-but-unusable
        # history must be a REFUSAL — an implementation that dropped the
        # unrankable carrier to zero-and-fresh-seed would return first_run and
        # fail this test on the missing raise.
        _seed_config(tmp_path, "a.yml", CID)
        _seed_carrier(tmp_path, "NOSTAMP", LEDGER, owner_id=CID)
        with pytest.raises(ResumeError) as ei:
            resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert ei.value.reason == ResumeAbortReason.SOURCE_NOT_FOUND
        assert CID in ei.value.message
        assert "NOSTAMP" in ei.value.message
        assert "explicit" in ei.value.message

    def test_excludes_instance_being_created(self, tmp_path):
        # The new instance's own dir already carries the ledger (staged ahead)
        # in BOTH trees and would be the newest candidate by timestamp; it must
        # be excluded, leaving the older prior instance as the winner.
        _seed_config(tmp_path, "a.yml", CID)
        prior = "SRC-20260101-000000"
        _seed_carrier(tmp_path, prior, LEDGER, owner_id=CID)
        _seed_carrier(tmp_path, NEW_NAME, LEDGER, owner_id=CID)
        _seed_carrier(tmp_path, NEW_NAME, LEDGER, tree="archived", owner_id=CID)
        res = resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert res.source.instance_name == prior


class TestResolverSearchAndOrdering:
    def test_finds_newest_across_instances_and_archived(self, tmp_path):
        # The newest carrier lives in the ARCHIVE (default stop flow moves it
        # there); an older one is still live. Newest wins, origin=archived,
        # and the ResolvedSource shape matches the request-level strategies'.
        _seed_config(tmp_path, "a.yml", CID)
        older = "KRAKEN_LADDER_V1-20260718-0101-20260101-000000"
        newest = "KRAKEN_LADDER_V1-20260718-0101-20260201-000000-123456-abcdef"
        _seed_carrier(tmp_path, older, LEDGER, owner_id=CID)
        newest_dir = _seed_carrier(tmp_path, newest, LEDGER, tree="archived", owner_id=CID)
        res = resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert res.first_run is False
        assert isinstance(res.source, ResolvedSource)
        assert res.source.instance_name == newest
        assert res.source.origin == "archived"
        assert res.source.instance_dir == newest_dir
        assert res.source.data_dir == newest_dir / "data"

    def test_singly_nested_archived_carrier_resolves(self, tmp_path):
        # archived/<name>/<name>/data — nothing plausible at the base level.
        # The shared archive resolver must find the nested level.
        _seed_config(tmp_path, "a.yml", CID)
        name = "SRC-20260101-000000"
        nested = tmp_path / "archived" / name / name
        _seed_carrier(tmp_path, name, LEDGER, owner_id=CID, instance_dir=nested)
        res = resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert res.source.instance_name == name
        assert res.source.origin == "archived"
        assert res.source.instance_dir == nested
        assert res.source.data_dir == nested / "data"

    def test_ambiguous_nested_archive_aborts(self, tmp_path):
        # Both the base and its nested same-name copy look like complete
        # instances -> ARCHIVE_NESTED propagates; never silently pick one.
        _seed_config(tmp_path, "a.yml", CID)
        name = "SRC-20260101-000000"
        _seed_carrier(tmp_path, name, LEDGER, tree="archived", owner_id=CID)
        _seed_carrier(
            tmp_path, name, LEDGER, owner_id=CID,
            instance_dir=tmp_path / "archived" / name / name,
        )
        with pytest.raises(ResumeError) as ei:
            resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert ei.value.reason == ResumeAbortReason.ARCHIVE_NESTED

    def test_unrelated_ambiguous_nested_archive_still_aborts(self, tmp_path):
        # The ambiguous nest belongs to a DIFFERENT instance that (as far as
        # anyone can tell without picking a level) might carry the flagged
        # ledger — its carrying CANNOT be checked without first choosing a
        # level, so enumeration refuses outright (spec §4: every name under
        # archived/ goes through the resolver; ARCHIVE_NESTED propagates).
        _seed_config(tmp_path, "a.yml", CID)
        _seed_carrier(tmp_path, "GOOD-20260201-000000", LEDGER, owner_id=CID)
        other = "OTHER-20260101-000000"
        (tmp_path / "archived" / other / "data").mkdir(parents=True)
        (tmp_path / "archived" / other / other / "data").mkdir(parents=True)
        with pytest.raises(ResumeError) as ei:
            resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert ei.value.reason == ResumeAbortReason.ARCHIVE_NESTED

    def test_ordering_by_name_timestamp_with_perturbed_mtimes(self, tmp_path):
        # mtimes are deliberately INVERTED relative to the name timestamps:
        # the older-NAMED instance gets the newest mtime and vice versa. An
        # implementation ordering by any mtime (ledger, data/, instance dir)
        # would pick OLD and fail; ordering by _parse_api_timestamp picks NEW.
        _seed_config(tmp_path, "a.yml", CID)
        old_name = "SRC-20260101-000000"
        new_name = "SRC-20260301-000000"
        old_dir = _seed_carrier(tmp_path, old_name, LEDGER, owner_id=CID)
        new_dir = _seed_carrier(tmp_path, new_name, LEDGER, owner_id=CID)
        _set_mtimes(old_dir, LEDGER, _MTIME_NEW)   # older name, newest mtime
        _set_mtimes(new_dir, LEDGER, _MTIME_OLD)   # newer name, oldest mtime
        res = resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert res.source.instance_name == new_name

    def test_newest_tie_aborts_latest_ambiguous(self, tmp_path):
        # Two distinct instances share the newest parsed timestamp (legacy
        # second-granular names) -> ambiguous, refuse.
        _seed_config(tmp_path, "a.yml", CID)
        _seed_carrier(tmp_path, "AAA-20260101-000000", LEDGER, owner_id=CID)
        _seed_carrier(tmp_path, "BBB-20260101-000000", LEDGER, owner_id=CID)
        with pytest.raises(ResumeError) as ei:
            resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert ei.value.reason == ResumeAbortReason.LATEST_AMBIGUOUS
        assert CID in ei.value.message

    def test_unrankable_carrier_dropped_with_warning_when_rankable_exists(self, tmp_path, caplog):
        # One carrier has no parseable timestamp (and the newest mtime, to
        # tempt an mtime ranking); a rankable carrier exists -> the unrankable
        # one is dropped WITH a warning and the rankable one wins.
        _seed_config(tmp_path, "a.yml", CID)
        stamped = "SRC-20260101-000000"
        _seed_carrier(tmp_path, stamped, LEDGER, owner_id=CID)
        nostamp_dir = _seed_carrier(tmp_path, "NOSTAMP", LEDGER, owner_id=CID)
        _set_mtimes(nostamp_dir, LEDGER, _MTIME_NEW)
        with caplog.at_level(logging.WARNING, logger="services.resume_service"):
            res = resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert res.source.instance_name == stamped
        assert any("NOSTAMP" in r.getMessage() for r in caplog.records)

    def test_instances_precedence_same_name_both_trees(self, tmp_path):
        # Same name in both trees: live instances/<name>/data wins outright and
        # the archive is NOT consulted — the archived copy is deliberately an
        # ambiguous nest, so consulting it would raise ARCHIVE_NESTED instead
        # of resolving. Byte-consistent with the request-level latest path.
        _seed_config(tmp_path, "a.yml", CID)
        name = "SRC-20260101-000000"
        live_dir = _seed_carrier(tmp_path, name, LEDGER, owner_id=CID)
        _seed_carrier(tmp_path, name, LEDGER, tree="archived", owner_id=CID)
        _seed_carrier(
            tmp_path, name, LEDGER, owner_id=CID,
            instance_dir=tmp_path / "archived" / name / name,
        )
        res = resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert res.source.origin == "instances"
        assert res.source.instance_dir == live_dir
        assert res.source.data_dir == live_dir / "data"


class TestResolverWinnerVerification:
    def test_owner_mismatch_on_winner_aborts_no_fallback(self, tmp_path):
        # The NEWEST carrier's sidecar claims another controller; an OLDER,
        # perfectly valid carrier exists. The abort must name the winner —
        # falling back to the older candidate would silently resume older
        # state, and an implementation doing so would RETURN here (no raise)
        # and fail this test.
        _seed_config(tmp_path, "a.yml", CID)
        older = "SRC-20260101-000000"
        newest = "SRC-20260301-000000"
        _seed_carrier(tmp_path, older, LEDGER, owner_id=CID)
        _seed_carrier(tmp_path, newest, LEDGER, owner_id="some_other_controller")
        with pytest.raises(ResumeError) as ei:
            resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert ei.value.reason == ResumeAbortReason.OWNER_MISMATCH
        assert newest in ei.value.message

    def test_corrupt_owner_on_winner_aborts_no_fallback(self, tmp_path):
        _seed_config(tmp_path, "a.yml", CID)
        _seed_carrier(tmp_path, "SRC-20260101-000000", LEDGER, owner_id=CID)
        _seed_carrier(
            tmp_path, "SRC-20260301-000000", LEDGER, owner_raw="not-json{{{",
        )
        with pytest.raises(ResumeError) as ei:
            resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert ei.value.reason == ResumeAbortReason.OWNER_MISMATCH

    def test_verification_runs_on_winner_only(self, tmp_path):
        # The OLDER candidate's sidecar is corrupt; the winner's is valid.
        # Spec §4 verifies identity ON THE WINNER ONLY, so this resolves.
        _seed_config(tmp_path, "a.yml", CID)
        newest = "SRC-20260301-000000"
        _seed_carrier(tmp_path, "SRC-20260101-000000", LEDGER, owner_raw="not-json{{{")
        _seed_carrier(tmp_path, newest, LEDGER, owner_id=CID)
        res = resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert res.source.instance_name == newest

    def test_default_named_ledger_without_sidecar_resolves(self, tmp_path):
        # No sidecar, but the DEFAULT ledger filename embeds the id — the
        # _plan_controller filename rule accepts it.
        _seed_config(tmp_path, "a.yml", CID)
        winner = "SRC-20260101-000000"
        _seed_carrier(tmp_path, winner, LEDGER)  # no owner sidecar
        res = resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert res.source.instance_name == winner

    def test_custom_named_winner_without_sidecar_aborts(self, tmp_path):
        # A custom state_file_name carries no id in its name; with no sidecar
        # the identity is unverifiable -> refusal (never "probably fine").
        _seed_config(tmp_path, "a.yml", CID, state_file_name="my_ladder.json")
        _seed_carrier(tmp_path, "SRC-20260101-000000", "my_ladder.json")
        with pytest.raises(ResumeError) as ei:
            resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert ei.value.reason == ResumeAbortReason.OWNER_MISMATCH

    def test_custom_state_file_name_keys_the_search(self, tmp_path):
        # The controller's expected ledger is its CUSTOM name (C1 canonical +
        # _expected_ledger_name). A NEWER instance carrying only the
        # default-named ledger is NOT a carrier; the older true carrier wins.
        # Kills an implementation that always derives the default name.
        _seed_config(tmp_path, "a.yml", CID, state_file_name="my_ladder.json")
        true_carrier = "SRC-20260101-000000"
        _seed_carrier(tmp_path, true_carrier, "my_ladder.json", owner_id=CID)
        _seed_carrier(tmp_path, "SRC-20260301-000000", LEDGER, owner_id=CID)  # decoy: default name only
        res = resolve_controller_flag_source({"a.yml": "latest"}, NEW_NAME, tmp_path)
        assert res.source.instance_name == true_carrier


class TestResolverAgreement:
    def test_agreeing_winners_resolve_single_source(self, tmp_path):
        # Two flagged controllers, both ledgers newest in the SAME instance
        # (an older instance also carries one of them) -> one agreed source.
        _seed_config(tmp_path, "a.yml", CID)
        _seed_config(tmp_path, "b.yml", CID_B)
        winner = "SRC-20260301-000000"
        _seed_carrier(tmp_path, "SRC-20260101-000000", LEDGER, owner_id=CID)
        winner_dir = _seed_carrier(tmp_path, winner, LEDGER, owner_id=CID)
        _seed_carrier(tmp_path, winner, LEDGER_B, owner_id=CID_B)
        res = resolve_controller_flag_source(
            {"a.yml": "latest", "b.yml": "latest"}, NEW_NAME, tmp_path
        )
        assert res.first_run is False
        assert res.source.instance_name == winner
        assert res.source.origin == "instances"
        assert res.source.instance_dir == winner_dir
        assert res.source.data_dir == winner_dir / "data"
        assert sorted(fc.controller_id for fc in res.flagged) == sorted([CID, CID_B])

    def test_divergent_winners_abort_naming_each(self, tmp_path):
        # Controller A's newest ledger lives in one instance, controller B's in
        # another -> CONTROLLER_SOURCES_DIVERGENT, message naming each
        # controller AND its winning instance so the operator can settle it.
        _seed_config(tmp_path, "a.yml", CID)
        _seed_config(tmp_path, "b.yml", CID_B)
        inst_a = "AAA-20260101-000000"
        inst_b = "BBB-20260201-000000"
        _seed_carrier(tmp_path, inst_a, LEDGER, owner_id=CID)
        _seed_carrier(tmp_path, inst_b, LEDGER_B, owner_id=CID_B)
        with pytest.raises(ResumeError) as ei:
            resolve_controller_flag_source(
                {"a.yml": "latest", "b.yml": "latest"}, NEW_NAME, tmp_path
            )
        assert ei.value.reason == ResumeAbortReason.CONTROLLER_SOURCES_DIVERGENT
        for token in (CID, CID_B, inst_a, inst_b):
            assert token in ei.value.message

    def test_partial_carriage_is_not_divergence(self, tmp_path):
        # One controller resolves, the other has NO carrier anywhere: that is
        # per-controller fresh-seed territory (compute_copy_plan's job), not
        # divergence — the resolver returns the single winner.
        _seed_config(tmp_path, "a.yml", CID)
        _seed_config(tmp_path, "b.yml", CID_B)
        winner = "SRC-20260101-000000"
        _seed_carrier(tmp_path, winner, LEDGER, owner_id=CID)
        res = resolve_controller_flag_source(
            {"a.yml": "latest", "b.yml": "latest"}, NEW_NAME, tmp_path
        )
        assert res.first_run is False
        assert res.source.instance_name == winner

    def test_off_entries_do_not_constrain_resolution(self, tmp_path):
        # b.yml is staged but NOT flagged latest; its ledger lives in a NEWER
        # different instance. It must not participate: no divergence, and the
        # flagged list carries only the latest-flagged controller.
        _seed_config(tmp_path, "a.yml", CID)
        _seed_config(tmp_path, "b.yml", CID_B)
        a_winner = "AAA-20260101-000000"
        _seed_carrier(tmp_path, a_winner, LEDGER, owner_id=CID)
        _seed_carrier(tmp_path, "BBB-20260201-000000", LEDGER_B, owner_id=CID_B)
        res = resolve_controller_flag_source(
            {"a.yml": "latest", "b.yml": "off"}, NEW_NAME, tmp_path
        )
        assert res.source.instance_name == a_winner
        assert [fc.controller_id for fc in res.flagged] == [CID]
