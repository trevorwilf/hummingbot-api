"""Tests for Fix 5: BotsOrchestrator container filter."""
import pytest
from unittest.mock import MagicMock
from services.bots_orchestrator import BotsOrchestrator


class TestContainerFilter:
    """Verify the container filter matches expected image patterns."""

    @pytest.fixture
    def make_container(self):
        """Factory for mock containers with specific image tags."""
        def _make(image_tag):
            container = MagicMock()
            container.image.tags = [image_tag]
            container.status = "running"
            return container
        return _make

    def test_method_name_typo_fixed(self):
        """The old typo 'fiter' should no longer exist."""
        assert not hasattr(BotsOrchestrator, "hummingbot_containers_fiter"), (
            "Old typo method 'hummingbot_containers_fiter' still exists"
        )
        assert hasattr(BotsOrchestrator, "hummingbot_containers_filter"), (
            "Corrected method 'hummingbot_containers_filter' not found"
        )

    def test_matches_registry_prefixed_image(self, make_container):
        """Should match hummingbot/hummingbot:latest."""
        container = make_container("hummingbot/hummingbot:latest")
        assert BotsOrchestrator.hummingbot_containers_filter(container) is True

    def test_matches_nonkyc_custom_image(self, make_container):
        """Should match hummingbot-nonkyc:latest."""
        container = make_container("hummingbot-nonkyc:latest")
        assert BotsOrchestrator.hummingbot_containers_filter(container) is True

    def test_matches_bare_hummingbot_image(self, make_container):
        """Should match hummingbot:latest."""
        container = make_container("hummingbot:latest")
        assert BotsOrchestrator.hummingbot_containers_filter(container) is True

    def test_matches_versioned_image(self, make_container):
        """Should match hummingbot/hummingbot:v1.28.0."""
        container = make_container("hummingbot/hummingbot:v1.28.0")
        assert BotsOrchestrator.hummingbot_containers_filter(container) is True

    def test_rejects_unrelated_image(self, make_container):
        """Should NOT match unrelated images."""
        container = make_container("postgres:16")
        assert BotsOrchestrator.hummingbot_containers_filter(container) is False

    def test_rejects_emqx_image(self, make_container):
        """Should NOT match EMQX broker."""
        container = make_container("emqx:5")
        assert BotsOrchestrator.hummingbot_containers_filter(container) is False

    def test_handles_no_tags_gracefully(self):
        """Container with no image tags should not crash."""
        container = MagicMock()
        container.image.tags = []
        container.image.__str__ = lambda self: "sha256:abc123"
        result = BotsOrchestrator.hummingbot_containers_filter(container)
        assert result is False
