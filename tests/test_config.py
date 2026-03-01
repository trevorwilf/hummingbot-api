"""Tests for Fix 3: SecuritySettings env_prefix safety."""
import os
import pytest
from unittest.mock import patch


class TestSecuritySettingsEnvPrefix:
    """Verify SecuritySettings handles environment variables safely."""

    def test_explicit_env_vars_still_work(self):
        """USERNAME and PASSWORD env vars should still configure auth."""
        with patch.dict(os.environ, {
            "USERNAME": "testuser",
            "PASSWORD": "testpass",
            "CONFIG_PASSWORD": "secret123",
        }, clear=False):
            # Re-import to pick up new env
            from config import SecuritySettings
            settings = SecuritySettings()
            assert settings.username == "testuser"
            assert settings.password == "testpass"
            assert settings.config_password == "secret123"

    def test_debug_mode_not_set_by_bare_env(self):
        """A bare DEBUG_MODE env var should NOT enable debug mode.
        Only HBOT_API_DEBUG_MODE should work (after the prefix fix).
        """
        with patch.dict(os.environ, {"DEBUG_MODE": "true"}, clear=False):
            from config import SecuritySettings
            settings = SecuritySettings()
            # After fix, DEBUG_MODE alone shouldn't enable debug mode
            # It should require HBOT_API_DEBUG_MODE
            assert settings.debug_mode is False, (
                "DEBUG_MODE env var enabled debug mode — "
                "env_prefix fix did not work"
            )

    def test_prefixed_debug_mode_works(self):
        """HBOT_API_DEBUG_MODE=true should enable debug mode."""
        with patch.dict(os.environ, {"HBOT_API_DEBUG_MODE": "true"}, clear=False):
            from config import SecuritySettings
            settings = SecuritySettings()
            assert settings.debug_mode is True

    def test_defaults_when_no_env(self):
        """Without any env vars, defaults should apply."""
        # Clear relevant env vars
        env_clear = {
            k: v for k, v in os.environ.items()
            if k not in ("USERNAME", "PASSWORD", "CONFIG_PASSWORD",
                        "HBOT_API_DEBUG_MODE", "DEBUG_MODE",
                        "HBOT_API_USERNAME", "HBOT_API_PASSWORD")
        }
        with patch.dict(os.environ, env_clear, clear=True):
            from config import SecuritySettings
            settings = SecuritySettings()
            assert settings.username == "admin"
            assert settings.password == "admin"
            assert settings.debug_mode is False
            assert settings.config_password == "a"
