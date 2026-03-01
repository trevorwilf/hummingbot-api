"""Tests for Fix 4: auth_user debug mode hardening."""
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient


class TestAuthDebugMode:
    """Verify debug mode behavior and warnings."""

    def test_auth_rejects_bad_credentials_when_not_debug(self):
        """Without debug mode, wrong credentials should return 401."""
        # Patch settings before importing main
        with patch("config.settings") as mock_settings:
            mock_settings.security.username = "admin"
            mock_settings.security.password = "admin"
            mock_settings.security.debug_mode = False
            mock_settings.security.config_password = "test"

            from fastapi.testclient import TestClient
            # Use httpx basic auth with wrong creds
            # This tests the auth_user function behavior
            from main import auth_user
            from fastapi import HTTPException
            creds = MagicMock()
            creds.username = "wrong"
            creds.password = "wrong"

            with pytest.raises(HTTPException) as exc_info:
                auth_user(creds)
            assert exc_info.value.status_code == 401

    def test_auth_passes_correct_credentials(self):
        """Correct credentials should authenticate successfully."""
        with patch("main.username", "admin"), \
             patch("main.password", "admin"), \
             patch("main.debug_mode", False):
            from main import auth_user
            creds = MagicMock()
            creds.username = "admin"
            creds.password = "admin"
            result = auth_user(creds)
            assert result == "admin"

    def test_debug_mode_logs_warning_on_bypass(self):
        """When debug mode is on and bad creds are sent, it should log a warning."""
        import logging
        with patch("main.username", "admin"), \
             patch("main.password", "admin"), \
             patch("main.debug_mode", True):
            from main import auth_user
            creds = MagicMock()
            creds.username = "hacker"
            creds.password = "wrong"

            with patch("main.logging") as mock_logging:
                result = auth_user(creds)
                # Should succeed (debug mode) but log
                assert result == "hacker"
                mock_logging.warning.assert_called()
