"""Tests for Fix 2: _status_polling_task in _start_connector_network."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestStartConnectorNetwork:
    """Verify _start_connector_network starts all required tasks including status polling."""

    @pytest.mark.asyncio
    async def test_status_polling_task_is_started(self, mock_connector):
        """_status_polling_task must be started alongside other network tasks."""
        from services.unified_connector_service import UnifiedConnectorService

        service = UnifiedConnectorService.__new__(UnifiedConnectorService)

        with patch("services.unified_connector_service.safe_ensure_future") as mock_sef:
            mock_sef.side_effect = lambda coro: MagicMock()  # Return a mock task

            await service._start_connector_network(mock_connector)

            # Verify _status_polling_loop was called via safe_ensure_future
            polling_loop_calls = [
                call for call in mock_sef.call_args_list
                if "status_polling" in str(call)
            ]
            # Also verify the task attribute was set
            assert mock_connector._status_polling_task is not None, (
                "_status_polling_task was not set — this means orders won't be "
                "polled via REST if WebSocket disconnects"
            )

    @pytest.mark.asyncio
    async def test_all_six_tasks_started(self, mock_connector):
        """All 6 network tasks must be started."""
        from services.unified_connector_service import UnifiedConnectorService

        service = UnifiedConnectorService.__new__(UnifiedConnectorService)

        with patch("services.unified_connector_service.safe_ensure_future") as mock_sef:
            mock_sef.side_effect = lambda coro: MagicMock()

            await service._start_connector_network(mock_connector)

            expected_tasks = [
                "_trading_rules_polling_task",
                "_trading_fees_polling_task",
                "_status_polling_task",
                "_user_stream_event_listener_task",
                "_lost_orders_update_task",
            ]
            for task_name in expected_tasks:
                assert getattr(mock_connector, task_name, None) is not None, (
                    f"{task_name} was not started"
                )
            # user_stream_tracker is set via _create_user_stream_tracker_task
            assert mock_connector._user_stream_tracker_task is not None

    @pytest.mark.asyncio
    async def test_stop_cleans_up_status_polling(self, mock_connector):
        """_stop_connector_network must cancel _status_polling_task."""
        from services.unified_connector_service import UnifiedConnectorService

        service = UnifiedConnectorService.__new__(UnifiedConnectorService)

        # Give the connector a running task
        mock_task = MagicMock()
        mock_connector._status_polling_task = mock_task

        await service._stop_connector_network(mock_connector)

        mock_task.cancel.assert_called_once()
        assert mock_connector._status_polling_task is None
