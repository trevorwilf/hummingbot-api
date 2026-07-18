"""Phase 1 tests: controller-YAML-driven resume flag + strip-at-staging.

Covers the CTRLRESUME phase-1 surface (spec §1 "The flag", §2 "Strip at
staging"):

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

All expected values are derived from the SPEC (accepted values are exactly
``"latest"`` and ``"off"``; the staged file is byte-identical to the source
minus the removed line), never captured by running the implementation. All
filesystem via ``tmp_path``; no Docker, no DB, no network.
"""

import yaml

import pytest

from services.docker_service import DockerService
from services.resume_service import (
    ControllerResumeFlag,
    ResumeAbortReason,
    ResumeError,
    parse_controller_resume_flag,
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
        # Unparseable text can carry no usable flag; report non-flagged so
        # staging keeps today's plain copy and the engine stays the backstop.
        text = "controller_name: x\n  bad: : indentation :\n:::\n"
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load(text)  # guards the premise: this really is malformed
        assert parse_controller_resume_flag(text, controller_name="c.yml") == (
            ControllerResumeFlag(present=False, mode="off")
        )


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

    def test_invalid_flag_raises_and_aborts(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        _write(src, "bad.yml", "controller_name: x\nresume_mode: sometimes\n")

        with pytest.raises(ResumeError) as ei:
            DockerService._stage_controller_configs(["bad.yml"], str(src), str(dst))
        assert ei.value.reason == ResumeAbortReason.RESUME_FLAG_INVALID

    def test_missing_source_is_warned_and_skipped(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        _write(src, "present.yml", "controller_name: x\n")

        flags = DockerService._stage_controller_configs(
            ["absent.yml", "present.yml"], str(src), str(dst)
        )

        # The missing controller is skipped (not in the collection, no raise);
        # the present non-flagged one is copied and recorded off.
        assert flags == {"present.yml": "off"}
        assert not (dst / "absent.yml").exists()

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
