"""Tests for Fix 6: /health endpoint."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestHealthEndpoint:
    """Verify /health returns correct component status."""

    @pytest.mark.asyncio
    async def test_health_endpoint_exists(self):
        """The /health endpoint should be registered."""
        from main import app
        routes = [route.path for route in app.routes]
        assert "/health" in routes, "/health endpoint not found in app routes"

    @pytest.mark.asyncio
    async def test_health_no_auth_required(self):
        """The /health endpoint should NOT require authentication."""
        from main import app
        # Find the /health route
        health_route = None
        for route in app.routes:
            if hasattr(route, "path") and route.path == "/health":
                health_route = route
                break
        assert health_route is not None, "/health route not found"
        # It should have no auth dependencies (or be outside the auth router includes)
        # The simplest check: it should respond without credentials
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/health")
            # Should NOT be 401
            assert response.status_code != 401, "/health requires authentication"

    @pytest.mark.asyncio
    async def test_health_returns_components(self):
        """Response should include database, mqtt, and connector_service status."""
        from httpx import AsyncClient, ASGITransport
        from main import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/health")
            data = response.json()
            assert "status" in data
            assert "components" in data
            assert "database" in data["components"]
            assert "mqtt" in data["components"]
            assert "connector_service" in data["components"]
