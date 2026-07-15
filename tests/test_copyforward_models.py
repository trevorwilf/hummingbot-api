"""Phase 1 tests: deploy-model resume config surface.

Covers V2ControllerDeployment and V2ScriptDeployment resume fields:
- Defaults (no resume fields → same shape as pre-change models)
- Enum rejection (invalid resume_mode)
- Cross-field validation (explicit without from; from while off)
- resume_from unsafe-name rejection
- resume_extra_paths absolute/.. rejection
- Round-trip serialization
- Backward compat: payload with no resume fields validates identically to the pre-change shape
"""

import pytest
from pydantic import ValidationError

from models.bot_orchestration import V2ControllerDeployment, V2ScriptDeployment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CTRL_BASE = dict(
    instance_name="MY-BOT",
    credentials_profile="main",
    controllers_config=["ladder_xmr"],
)

_SCRIPT_BASE = dict(
    instance_name="MY-BOT",
    credentials_profile="main",
)


def make_ctrl(**overrides):
    return {**_CTRL_BASE, **overrides}


def make_script(**overrides):
    return {**_SCRIPT_BASE, **overrides}


# ---------------------------------------------------------------------------
# 1. Defaults — all new fields at "off" / None / False
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_controller_defaults(self):
        m = V2ControllerDeployment(**_CTRL_BASE)
        assert m.resume_mode == "off"
        assert m.resume_from is None
        assert m.resume_from_archive is False
        assert m.resume_extra_paths is None
        assert m.resume_accept_ungraceful is False

    def test_script_defaults(self):
        m = V2ScriptDeployment(**_SCRIPT_BASE)
        assert m.resume_mode == "off"
        assert m.resume_from is None
        assert m.resume_from_archive is False
        assert m.resume_extra_paths is None
        assert m.resume_accept_ungraceful is False


# ---------------------------------------------------------------------------
# 2. Valid enum values accepted
# ---------------------------------------------------------------------------

class TestEnumValues:
    @pytest.mark.parametrize("mode", ["off", "explicit", "latest"])
    def test_controller_valid_modes(self, mode):
        kwargs = make_ctrl(resume_mode=mode)
        if mode == "explicit":
            kwargs["resume_from"] = "OLD-BOT"
        V2ControllerDeployment(**kwargs)  # must not raise

    @pytest.mark.parametrize("mode", ["off", "explicit", "latest"])
    def test_script_valid_modes(self, mode):
        kwargs = make_script(resume_mode=mode)
        if mode == "explicit":
            kwargs["resume_from"] = "OLD-BOT"
        V2ScriptDeployment(**kwargs)  # must not raise

    def test_controller_invalid_mode_rejected(self):
        with pytest.raises(ValidationError):
            V2ControllerDeployment(**make_ctrl(resume_mode="auto"))

    def test_script_invalid_mode_rejected(self):
        with pytest.raises(ValidationError):
            V2ScriptDeployment(**make_script(resume_mode="auto"))


# ---------------------------------------------------------------------------
# 3. Cross-field validation
# ---------------------------------------------------------------------------

class TestCrossField:
    def test_explicit_without_from_rejected_controller(self):
        with pytest.raises(ValidationError, match="resume_from is required"):
            V2ControllerDeployment(**make_ctrl(resume_mode="explicit"))

    def test_explicit_without_from_rejected_script(self):
        with pytest.raises(ValidationError, match="resume_from is required"):
            V2ScriptDeployment(**make_script(resume_mode="explicit"))

    def test_from_while_off_rejected_controller(self):
        with pytest.raises(ValidationError, match="resume_from must not be set"):
            V2ControllerDeployment(**make_ctrl(resume_mode="off", resume_from="OLD-BOT"))

    def test_from_while_off_rejected_script(self):
        with pytest.raises(ValidationError, match="resume_from must not be set"):
            V2ScriptDeployment(**make_script(resume_mode="off", resume_from="OLD-BOT"))

    def test_latest_without_from_accepted_controller(self):
        m = V2ControllerDeployment(**make_ctrl(resume_mode="latest"))
        assert m.resume_mode == "latest"
        assert m.resume_from is None

    def test_latest_without_from_accepted_script(self):
        m = V2ScriptDeployment(**make_script(resume_mode="latest"))
        assert m.resume_mode == "latest"

    def test_explicit_with_from_accepted_controller(self):
        m = V2ControllerDeployment(**make_ctrl(resume_mode="explicit", resume_from="OLD-BOT"))
        assert m.resume_from == "OLD-BOT"

    def test_explicit_with_from_accepted_script(self):
        m = V2ScriptDeployment(**make_script(resume_mode="explicit", resume_from="OLD-BOT"))
        assert m.resume_from == "OLD-BOT"


# ---------------------------------------------------------------------------
# 4. resume_from unsafe-name rejection (uses _validate_safe_name)
# ---------------------------------------------------------------------------

class TestResumeFromValidation:
    @pytest.mark.parametrize("bad_name", [
        "../x",          # traversal
        "a/b",           # slash
        "a\\b",          # backslash
        "",              # empty
        "a b",           # space
        "a.b",           # dot
    ])
    def test_controller_unsafe_resume_from(self, bad_name):
        with pytest.raises(ValidationError):
            V2ControllerDeployment(**make_ctrl(resume_mode="explicit", resume_from=bad_name))

    @pytest.mark.parametrize("bad_name", [
        "../x",
        "a/b",
        "a\\b",
        "",
        "a b",
        "a.b",
    ])
    def test_script_unsafe_resume_from(self, bad_name):
        with pytest.raises(ValidationError):
            V2ScriptDeployment(**make_script(resume_mode="explicit", resume_from=bad_name))

    def test_safe_name_with_hyphens_and_underscores_accepted(self):
        m = V2ControllerDeployment(**make_ctrl(resume_mode="explicit", resume_from="BOT-v2_NEW"))
        assert m.resume_from == "BOT-v2_NEW"


# ---------------------------------------------------------------------------
# 5. resume_extra_paths — absolute and .. rejection
# ---------------------------------------------------------------------------

class TestResumeExtraPaths:
    @pytest.mark.parametrize("bad_path", [
        "/etc/passwd",       # POSIX absolute
        "C:\\windows",       # Windows drive-letter absolute
        "C:/windows",        # Windows drive-letter with forward slash
        "C:",                # bare drive letter
        "../other",          # traversal
        "sub/../other",      # traversal in middle
        "a/../../b",         # double traversal
    ])
    def test_controller_bad_extra_paths(self, bad_path):
        with pytest.raises(ValidationError):
            V2ControllerDeployment(**make_ctrl(resume_extra_paths=[bad_path]))

    @pytest.mark.parametrize("bad_path", [
        "/etc/passwd",
        "C:\\windows",
        "C:/windows",
        "C:",
        "../other",
        "sub/../other",
    ])
    def test_script_bad_extra_paths(self, bad_path):
        with pytest.raises(ValidationError):
            V2ScriptDeployment(**make_script(resume_extra_paths=[bad_path]))

    @pytest.mark.parametrize("good_path", [
        "extra.json",
        "sub/extra.json",
        "a/b/c.sqlite",
    ])
    def test_controller_good_extra_paths(self, good_path):
        m = V2ControllerDeployment(**make_ctrl(resume_extra_paths=[good_path]))
        assert good_path in m.resume_extra_paths

    @pytest.mark.parametrize("good_path", [
        "extra.json",
        "sub/extra.json",
    ])
    def test_script_good_extra_paths(self, good_path):
        m = V2ScriptDeployment(**make_script(resume_extra_paths=[good_path]))
        assert good_path in m.resume_extra_paths


# ---------------------------------------------------------------------------
# 6. Round-trip serialization
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_controller_roundtrip(self):
        m = V2ControllerDeployment(
            **make_ctrl(
                resume_mode="explicit",
                resume_from="PREV-BOT",
                resume_from_archive=True,
                resume_extra_paths=["extra.json"],
                resume_accept_ungraceful=True,
            )
        )
        data = m.model_dump()
        m2 = V2ControllerDeployment(**data)
        assert m2.resume_mode == "explicit"
        assert m2.resume_from == "PREV-BOT"
        assert m2.resume_from_archive is True
        assert m2.resume_extra_paths == ["extra.json"]
        assert m2.resume_accept_ungraceful is True

    def test_script_roundtrip(self):
        m = V2ScriptDeployment(
            **make_script(
                resume_mode="latest",
                resume_accept_ungraceful=True,
            )
        )
        data = m.model_dump()
        m2 = V2ScriptDeployment(**data)
        assert m2.resume_mode == "latest"
        assert m2.resume_accept_ungraceful is True

    def test_controller_json_roundtrip(self):
        m = V2ControllerDeployment(**make_ctrl(resume_mode="latest"))
        json_str = m.model_dump_json()
        import json
        data = json.loads(json_str)
        assert data["resume_mode"] == "latest"
        m2 = V2ControllerDeployment(**data)
        assert m2.resume_mode == "latest"


# ---------------------------------------------------------------------------
# 7. Backward compat — payload with no resume fields behaves identically to
#    the pre-change model shape (all new fields at their defaults)
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_controller_no_resume_fields(self):
        """A payload with no resume fields must produce the same effective model
        as before the change: resume_mode='off', all others at None/False."""
        m = V2ControllerDeployment(**_CTRL_BASE)
        assert m.resume_mode == "off"
        assert m.resume_from is None
        assert m.resume_from_archive is False
        assert m.resume_extra_paths is None
        assert m.resume_accept_ungraceful is False
        # Existing required fields unchanged
        assert m.instance_name == "MY-BOT"
        assert m.credentials_profile == "main"
        assert m.controllers_config == ["ladder_xmr"]

    def test_script_no_resume_fields(self):
        m = V2ScriptDeployment(**_SCRIPT_BASE)
        assert m.resume_mode == "off"
        assert m.resume_from is None
        assert m.resume_from_archive is False
        assert m.resume_extra_paths is None
        assert m.resume_accept_ungraceful is False
        assert m.instance_name == "MY-BOT"
        assert m.credentials_profile == "main"

    def test_controller_model_dump_includes_resume_defaults(self):
        """model_dump() includes all resume fields even when not supplied."""
        m = V2ControllerDeployment(**_CTRL_BASE)
        d = m.model_dump()
        assert "resume_mode" in d
        assert d["resume_mode"] == "off"
        assert "resume_from" in d
        assert d["resume_from"] is None

    def test_script_model_dump_includes_resume_defaults(self):
        m = V2ScriptDeployment(**_SCRIPT_BASE)
        d = m.model_dump()
        assert "resume_mode" in d
        assert d["resume_mode"] == "off"
