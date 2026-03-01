"""
Phase 3 Hardening — Unit Tests
Run: python -m pytest tests/test_phase3_hardening.py -v
These tests verify Phase 3 fixes by reading source files.
No API keys or network access required.
"""
import re
import unittest
from pathlib import Path


def find_repo_root():
    """Walk up from this file to find repo root (contains main.py)."""
    p = Path(__file__).resolve().parent
    for _ in range(5):
        if (p / "main.py").exists():
            return p
        p = p.parent
    return Path(__file__).resolve().parent.parent


REPO = find_repo_root()


class TestFix1StatusPollingTask(unittest.TestCase):
    """P0-2: _status_polling_task must be started in _start_connector_network."""

    def setUp(self):
        self.source = (REPO / "services" / "unified_connector_service.py").read_text()

    def test_status_polling_started(self):
        """_start_connector_network starts _status_polling_task."""
        # Find the method body
        start_idx = self.source.find("async def _start_connector_network")
        stop_idx = self.source.find("async def _stop_connector_network")
        method_body = self.source[start_idx:stop_idx]
        self.assertIn("_status_polling_task", method_body,
                       "_status_polling_task not found in _start_connector_network")
        self.assertIn("safe_ensure_future", method_body)

    def test_status_polling_stopped(self):
        """_stop_connector_network cancels _status_polling_task."""
        stop_idx = self.source.find("async def _stop_connector_network")
        next_method = self.source.find("async def ", stop_idx + 10)
        method_body = self.source[stop_idx:next_method]
        self.assertIn("_status_polling_task", method_body)

    def test_six_tasks_started(self):
        """_start_connector_network starts exactly 6 background tasks."""
        start_idx = self.source.find("async def _start_connector_network")
        stop_idx = self.source.find("async def _stop_connector_network")
        method_body = self.source[start_idx:stop_idx]
        task_count = method_body.count("safe_ensure_future")
        # _user_stream_tracker_task uses _create_user_stream_tracker_task, not safe_ensure_future
        tracker_count = method_body.count("_create_user_stream_tracker_task")
        total = task_count + tracker_count
        self.assertEqual(total, 6, f"Expected 6 tasks, found {total}")


class TestFix2SecuritySettingsPrefix(unittest.TestCase):
    """P1-3: SecuritySettings must use HBOT_API_ env prefix."""

    def setUp(self):
        self.source = (REPO / "config.py").read_text()

    def test_security_settings_has_prefix(self):
        """SecuritySettings env_prefix is HBOT_API_."""
        # Find SecuritySettings class
        idx = self.source.find("class SecuritySettings")
        next_class = self.source.find("\nclass ", idx + 10)
        class_body = self.source[idx:next_class]
        self.assertIn('HBOT_API_', class_body,
                       "SecuritySettings should use HBOT_API_ env_prefix")

    def test_no_empty_env_prefix(self):
        """No settings class should have env_prefix=''."""
        # Allow empty string only if it's in a comment
        lines = self.source.split('\n')
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            if 'env_prefix=""' in stripped or "env_prefix=''" in stripped:
                self.fail(f"Empty env_prefix found at line {i+1}: {stripped}")


class TestFix3OrdersRecorderLogging(unittest.TestCase):
    """P2-1: OrdersRecorder.start() should have minimal logging."""

    def setUp(self):
        self.source = (REPO / "services" / "orders_recorder.py").read_text()

    def test_start_method_reduced_logging(self):
        """start() should have <= 2 logger.info calls."""
        start_idx = self.source.find("def start(self")
        # Find next method (def or async def at same indentation)
        next_def = self.source.find("\n    def ", start_idx + 10)
        next_async = self.source.find("\n    async def ", start_idx + 10)
        candidates = [x for x in [next_def, next_async] if x > 0]
        end_idx = min(candidates) if candidates else len(self.source)
        method_body = self.source[start_idx:end_idx]
        info_count = method_body.count("logger.info")
        self.assertLessEqual(info_count, 2,
                             f"start() has {info_count} logger.info calls (max 2)")

    def test_no_callable_debug_checks(self):
        """start() should not have 'is callable' debug checks."""
        start_idx = self.source.find("def start(self")
        next_def = self.source.find("\n    def ", start_idx + 10)
        next_async = self.source.find("\n    async def ", start_idx + 10)
        candidates = [x for x in [next_def, next_async] if x > 0]
        end_idx = min(candidates) if candidates else len(self.source)
        method_body = self.source[start_idx:end_idx]
        self.assertNotIn("callable", method_body,
                         "Remove callable() debug checks from start()")


class TestFix4RateLimiting(unittest.TestCase):
    """P2-3: API must have rate limiting."""

    def setUp(self):
        self.source = (REPO / "main.py").read_text()

    def test_rate_limiting_present(self):
        """main.py contains rate limiting code."""
        has_limiter = any(term in self.source for term in [
            "RateLimiter", "rate_limit", "SimpleRateLimiter",
            "slowapi", "429", "Rate limit"
        ])
        self.assertTrue(has_limiter, "No rate limiting found in main.py")


class TestFix5ParallelInit(unittest.TestCase):
    """P2-5: Connector initialization should be parallel."""

    def setUp(self):
        self.source = (REPO / "services" / "unified_connector_service.py").read_text()

    def test_parallel_init(self):
        """initialize_all_trading_connectors uses asyncio.gather."""
        idx = self.source.find("async def initialize_all_trading_connectors")
        next_method = self.source.find("\n    async def ", idx + 10)
        if next_method == -1:
            next_method = len(self.source)
        method_body = self.source[idx:next_method]
        self.assertIn("asyncio.gather", method_body,
                       "initialize_all_trading_connectors should use asyncio.gather")


class TestFix6CorsRestricted(unittest.TestCase):
    """P3-1: CORS should not allow all origins."""

    def setUp(self):
        self.source = (REPO / "main.py").read_text()

    def test_cors_not_wildcard(self):
        """allow_origins should not be just ['*']."""
        # Find the CORS middleware section
        idx = self.source.find("CORSMiddleware")
        if idx == -1:
            self.skipTest("CORSMiddleware not found")
        section = self.source[idx:idx+500]
        # Check it doesn't have allow_origins=["*"] as the only origin
        if 'allow_origins=["*"]' in section or "allow_origins=['*']" in section:
            self.fail("CORS should not use wildcard origins in production")


class TestFix7ContainerFilter(unittest.TestCase):
    """P2-2: Container filter should match nonkyc image."""

    def setUp(self):
        self.source = (REPO / "services" / "bots_orchestrator.py").read_text()

    def test_nonkyc_in_pattern(self):
        """Filter pattern includes hummingbot-nonkyc."""
        self.assertIn("hummingbot-nonkyc", self.source)

    def test_method_name_typo_fixed(self):
        """Method should be named 'filter' not 'fiter'."""
        self.assertNotIn("containers_fiter", self.source,
                         "Typo 'fiter' should be fixed to 'filter'")
        self.assertIn("containers_filter", self.source)


if __name__ == "__main__":
    unittest.main()
