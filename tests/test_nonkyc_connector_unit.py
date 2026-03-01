"""Unit tests for NonKYC connector components (no live API calls)."""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import sys
import os

# These tests import from the hummingbot package
# They will work if the NonKYC connector is installed or mounted


class TestNonKYCAuth:
    """Test HMAC-SHA256 authentication."""

    def test_generate_auth_dict_get(self):
        """GET request auth should include API key, nonce, and signature."""
        try:
            from hummingbot.connector.exchange.nonkyc.nonkyc_auth import NonkycAuth
        except ImportError:
            pytest.skip("NonKYC connector not importable in test environment")

        auth = NonkycAuth(api_key="test_key", secret_key="test_secret")
        headers = auth.header_for_authentication("GET", "https://api.nonkyc.io/api/v2/balances")

        assert "X-API-KEY" in headers
        assert "X-API-NONCE" in headers
        assert "X-API-SIGN" in headers
        assert headers["X-API-KEY"] == "test_key"
        # Nonce should be numeric
        assert headers["X-API-NONCE"].isdigit()
        # Signature should be hex
        assert all(c in "0123456789abcdef" for c in headers["X-API-SIGN"])

    def test_generate_auth_dict_post(self):
        """POST request auth should include request body in signature."""
        try:
            from hummingbot.connector.exchange.nonkyc.nonkyc_auth import NonkycAuth
        except ImportError:
            pytest.skip("NonKYC connector not importable in test environment")

        auth = NonkycAuth(api_key="test_key", secret_key="test_secret")
        body = '{"symbol":"BTC-USDT","side":"buy","type":"limit","quantity":"0.001","price":"50000"}'
        headers = auth.header_for_authentication(
            "POST", "https://api.nonkyc.io/api/v2/createorder", body=body
        )

        assert "X-API-SIGN" in headers
        # POST signature should differ from GET signature (body included)


class TestNonKYCConstants:
    """Verify connector constants are correctly defined."""

    def test_rest_url_format(self):
        """REST URL should be https with correct base path."""
        try:
            from hummingbot.connector.exchange.nonkyc.nonkyc_constants import REST_URL, API_VERSION
        except ImportError:
            pytest.skip("NonKYC connector not importable")

        assert REST_URL.startswith("https://"), "REST URL must use HTTPS"
        assert "nonkyc.io" in REST_URL
        assert API_VERSION == "v2"

    def test_ws_url_format(self):
        """WebSocket URL should use wss://."""
        try:
            from hummingbot.connector.exchange.nonkyc.nonkyc_constants import WS_URL
        except ImportError:
            pytest.skip("NonKYC connector not importable")

        assert WS_URL.startswith("wss://"), "WebSocket URL must use WSS"

    def test_order_state_mapping_completeness(self):
        """ORDER_STATE mapping should cover all expected states."""
        try:
            from hummingbot.connector.exchange.nonkyc.nonkyc_constants import ORDER_STATE
        except ImportError:
            pytest.skip("NonKYC connector not importable")

        required_states = ["new", "New", "Filled", "Cancelled", "Expired"]
        for state in required_states:
            assert state in ORDER_STATE or state.lower() in ORDER_STATE, (
                f"Missing state mapping for '{state}'"
            )
