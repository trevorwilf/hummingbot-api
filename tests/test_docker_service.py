"""Tests for Fix 1: docker_service.py VPN network mode."""
import os
import pytest
from unittest.mock import MagicMock, patch
from services.docker_service import DockerService


class TestDockerServiceVPNNetworkMode:
    """Verify spawned bot containers use VPN network mode."""

    def test_get_bot_network_mode_default(self):
        """Without env var, should default to 'host'."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("DOCKER_BOT_NETWORK_MODE", None)
            result = DockerService._get_bot_network_mode()
            assert result == "host"

    def test_get_bot_network_mode_from_env(self):
        """With env var set, should return the configured value."""
        with patch.dict(os.environ, {"DOCKER_BOT_NETWORK_MODE": "container:gluetun"}):
            result = DockerService._get_bot_network_mode()
            assert result == "container:gluetun"

    def test_get_compose_labels_with_project(self):
        """Labels should include compose project metadata when COMPOSE_PROJECT_NAME is set."""
        with patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": "hummingbot", "COMPOSE_SERVICE_PREFIX": "hbot"}):
            labels = DockerService._get_compose_labels("test-bot")
            assert labels["autoheal"] == "true"
            assert labels["com.docker.compose.project"] == "hummingbot"
            assert labels["com.docker.compose.service"] == "hbot-test-bot"

    def test_get_compose_labels_without_project(self):
        """Without COMPOSE_PROJECT_NAME, should still include autoheal label."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("COMPOSE_PROJECT_NAME", None)
            labels = DockerService._get_compose_labels("test-bot")
            assert labels == {"autoheal": "true"}

    def test_no_hardcoded_network_mode_host(self):
        """Source code must not contain hardcoded network_mode='host' in containers.run()."""
        import inspect
        source = inspect.getsource(DockerService)
        # Check there's no literal network_mode="host" left (the method default is OK)
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if 'network_mode="host"' in line or "network_mode='host'" in line:
                # Allow it only inside _get_bot_network_mode default comment/docstring
                if "_get_bot_network_mode" not in line and "default" not in line:
                    pytest.fail(
                        f"Found hardcoded network_mode='host' at line {i}: {line.strip()}"
                    )

    @patch("services.docker_service.docker")
    def test_create_instance_uses_dynamic_network_mode(self, mock_docker_module):
        """containers.run() should use _get_bot_network_mode(), not hardcoded 'host'."""
        mock_client = MagicMock()
        mock_docker_module.from_env.return_value = mock_client

        with patch.dict(os.environ, {"DOCKER_BOT_NETWORK_MODE": "container:gluetun"}):
            service = DockerService()
            service.client = mock_client

            # Create a minimal config mock
            config = MagicMock()
            config.image = "hummingbot/hummingbot:latest"
            config.script_config = None
            config.headless = False

            service.create_hummingbot_instance(
                config=config,
            )

            # Verify containers.run was called with dynamic network_mode
            call_kwargs = mock_client.containers.run.call_args
            assert call_kwargs is not None, "containers.run() was not called"
            kwargs = call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1]
            assert kwargs.get("network_mode") == "container:gluetun", (
                f"Expected network_mode='container:gluetun', got '{kwargs.get('network_mode')}'"
            )
            assert "labels" in kwargs, "labels not passed to containers.run()"
