"""Shared test fixtures for Phase 1 tests."""
import os
import sys
import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

# Add the app root to path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_docker_client():
    """Mock Docker client for container operations."""
    client = MagicMock()
    container = MagicMock()
    container.name = "test-bot-1"
    container.status = "running"
    container.image.tags = ["hummingbot/hummingbot:latest"]
    client.containers.list.return_value = [container]
    client.containers.run.return_value = container
    return client


@pytest.fixture
def mock_connector():
    """Mock connector with all expected task attributes."""
    connector = MagicMock()
    connector._trading_rules_polling_task = None
    connector._trading_fees_polling_task = None
    connector._status_polling_task = None
    connector._user_stream_tracker_task = None
    connector._user_stream_event_listener_task = None
    connector._lost_orders_update_task = None
    connector.order_book_tracker = None

    # Make the loop methods return coroutines
    connector._trading_rules_polling_loop = AsyncMock()
    connector._trading_fees_polling_loop = AsyncMock()
    connector._status_polling_loop = AsyncMock()
    connector._create_user_stream_tracker_task = MagicMock(return_value=MagicMock())
    connector._user_stream_event_listener = AsyncMock()
    connector._lost_orders_update_polling_loop = AsyncMock()

    return connector


@pytest.fixture
def nonkyc_api_keys():
    """Load NonKYC API keys from environment.

    Set these env vars before running live tests:
      NONKYC_API_KEY=<your_key>
      NONKYC_API_SECRET=<your_secret>
    """
    api_key = os.environ.get("NONKYC_API_KEY")
    api_secret = os.environ.get("NONKYC_API_SECRET")
    if not api_key or not api_secret:
        pytest.skip("NONKYC_API_KEY and NONKYC_API_SECRET env vars required for live tests")
    return {"api_key": api_key, "api_secret": api_secret}
