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


class TestCrossStackScoping:
    """Multiple stacks (hummingbot, hummingbot_us) share ONE Docker daemon: discovery
    by image name alone adopted the other stack's bots, which then sat at "stopped"
    forever (their MQTT goes to the other stack's broker). Discovery must ignore
    containers labeled com.docker.compose.project=<another stack> (2026-07-13)."""

    @staticmethod
    def _make_container(name, image_tag="hummingbot/hummingbot:latest", project=None):
        container = MagicMock()
        container.name = name
        container.image.tags = [image_tag]
        container.status = "running"
        container.labels = {} if project is None else {"com.docker.compose.project": project}
        return container

    def _orchestrator(self, compose_project, containers):
        orch = BotsOrchestrator.__new__(BotsOrchestrator)
        orch._compose_project = compose_project
        orch.docker_client = MagicMock()
        orch.docker_client.containers.list.return_value = containers
        return orch

    def test_foreign_project_bot_is_excluded(self):
        containers = [
            self._make_container("OUR_BOT", project="hummingbot"),
            self._make_container("KRAKEN_LADDER_V1", project="hummingbot_us"),
        ]
        orch = self._orchestrator("hummingbot", containers)
        assert orch._sync_get_active_containers() == ["OUR_BOT"]

    def test_own_project_bot_is_included(self):
        orch = self._orchestrator(
            "hummingbot_us", [self._make_container("KRAKEN_LADDER_V1", project="hummingbot_us")])
        assert orch._sync_get_active_containers() == ["KRAKEN_LADDER_V1"]

    def test_unlabeled_bot_stays_visible(self):
        # Manual runs / deploys from before label grafting keep legacy visibility.
        orch = self._orchestrator("hummingbot", [self._make_container("LEGACY_BOT")])
        assert orch._sync_get_active_containers() == ["LEGACY_BOT"]

    def test_no_project_configured_keeps_legacy_include_all(self):
        containers = [
            self._make_container("A", project="hummingbot"),
            self._make_container("B", project="hummingbot_us"),
            self._make_container("C"),
        ]
        orch = self._orchestrator("", containers)
        assert orch._sync_get_active_containers() == ["A", "B", "C"]

    def test_non_hummingbot_images_still_rejected(self):
        containers = [
            self._make_container("emqx", image_tag="emqx:5", project="hummingbot"),
            self._make_container("OUR_BOT", project="hummingbot"),
        ]
        orch = self._orchestrator("hummingbot", containers)
        assert orch._sync_get_active_containers() == ["OUR_BOT"]

    def test_labels_access_failure_is_not_fatal(self):
        broken = self._make_container("WEIRD_BOT", project="hummingbot")
        type(broken).labels = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        orch = self._orchestrator("hummingbot", [broken])
        assert orch._sync_get_active_containers() == ["WEIRD_BOT"]
