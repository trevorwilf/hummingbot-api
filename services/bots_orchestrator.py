import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import docker

from database import AsyncDatabaseManager, BotRunRepository, ControllerPerformanceRepository, OrderRepository
from services.docker_service import DockerService
from utils.bot_archiver import BotArchiver
from utils.mqtt_manager import MQTTManager

logger = logging.getLogger(__name__)

# Engine RPC status codes — mirrored from the engine's wire contract
# (E:/tradingsoftware/hummingbot/hummingbot/remote_iface/messages.py:
# MQTT_STATUS_CODE — SUCCESS=200, ERROR=400).
MQTT_RPC_SUCCESS = 200
MQTT_RPC_ERROR = 400
# The engine's status handler replies ERROR with exactly this message once
# trading_core.strategy is None (remote_iface/mqtt.py:_on_cmd_status) — and
# stop_loop() clears the strategy only after the ENTIRE graceful shutdown
# (on_stop with executor store, exchange-acked cancel_all, connector removal,
# markets-recorder stop) has completed (client/command/stop_command.py).
_NO_STRATEGY_RUNNING = "no strategy is currently running"


@dataclass(frozen=True)
class RetirementTimeouts:
    """Bounded polling for the acknowledged-retirement state machine (CDX-005).

    Replaces the old fixed 15 s shutdown sleep. Every wait is bounded and
    configurable; hitting a bound never fabricates evidence — the affected
    stage simply stays unconfirmed and the run finalizes UNVERIFIED.
    """

    stop_ack_timeout: float = 30.0
    quiescence_timeout: float = 120.0
    zero_open_orders_timeout: float = 90.0
    fill_drain_seconds: float = 10.0
    poll_interval: float = 2.0

    @classmethod
    def from_env(cls) -> "RetirementTimeouts":
        """Read overrides from RETIREMENT_*_S env vars (blank/missing → default).

        Malformed values raise: a misconfigured timeout should fail the
        retirement loudly (the run stays fail-closed), not silently pick a
        number the operator didn't set.
        """
        def read(name: str, default: float) -> float:
            raw = os.environ.get(name)
            if raw is None or not raw.strip():
                return default
            return float(raw)

        return cls(
            stop_ack_timeout=read("RETIREMENT_STOP_ACK_TIMEOUT_S", cls.stop_ack_timeout),
            quiescence_timeout=read("RETIREMENT_QUIESCENCE_TIMEOUT_S", cls.quiescence_timeout),
            zero_open_orders_timeout=read("RETIREMENT_ZERO_ORDERS_TIMEOUT_S", cls.zero_open_orders_timeout),
            fill_drain_seconds=read("RETIREMENT_FILL_DRAIN_S", cls.fill_drain_seconds),
            poll_interval=read("RETIREMENT_POLL_INTERVAL_S", cls.poll_interval),
        )


class BotsOrchestrator:
    """Orchestrates Hummingbot instances using Docker and MQTT communication."""

    def __init__(self, broker_host, broker_port, broker_username, broker_password,
                 db_manager: AsyncDatabaseManager, performance_dump_interval: int = 5):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.broker_username = broker_username
        self.broker_password = broker_password

        # Initialize Docker client
        self.docker_client = docker.from_env()

        # Initialize MQTT manager
        self.mqtt_manager = MQTTManager(host=broker_host, port=broker_port, username=broker_username, password=broker_password)

        # Active bots tracking
        self.active_bots = {}
        self._update_bots_task: Optional[asyncio.Task] = None

        # Track bots that are currently being stopped and archived
        self.stopping_bots = set()

        # Controller performance dump (similar to AccountsService.dump_account_state)
        self.performance_dump_interval = performance_dump_interval * 60  # Convert minutes to seconds
        self._performance_dump_task: Optional[asyncio.Task] = None
        # Shared manager injected from main.py; tables are created once at startup,
        # so no per-service bootstrap is needed here.
        self.db_manager = db_manager

        # Cross-stack scoping (2026-07-13): multiple stacks (hummingbot, hummingbot_us)
        # share ONE Docker daemon on the host. Discovery by image name alone adopted the
        # OTHER stack's bot containers too — they publish MQTT to their own stack's
        # broker, so they surfaced here as permanently "stopped" phantoms whose stop
        # button would kill the other stack's live bot. Bot containers are launched with
        # com.docker.compose.project=<COMPOSE_PROJECT_NAME> (see DockerService.
        # _get_compose_labels); discovery now ignores containers labeled for a DIFFERENT
        # project. Unlabeled containers stay included (manual runs / legacy deploys),
        # and with COMPOSE_PROJECT_NAME unset the legacy include-all behavior applies.
        self._compose_project = os.environ.get("COMPOSE_PROJECT_NAME", "")

        # MQTT manager will be started asynchronously later

    @staticmethod
    def hummingbot_containers_filter(container):
        """Filter for Hummingbot containers based on image name pattern."""
        try:
            image_name = container.image.tags[0] if container.image.tags else str(container.image)
            # Match: hummingbot/hummingbot:*, hummingbot-nonkyc:*, or any */hummingbot:*
            pattern = r'(.*\/)?hummingbot(-nonkyc)?:'
            return bool(re.match(pattern, image_name))
        except Exception:
            return False

    def _is_our_container(self, container) -> bool:
        """True for hummingbot containers that belong to THIS stack (see the
        cross-stack scoping note in __init__)."""
        if not self.hummingbot_containers_filter(container):
            return False
        if not self._compose_project:
            return True  # single-stack / legacy: no scoping configured
        try:
            project = (container.labels or {}).get("com.docker.compose.project", "")
        except Exception:
            project = ""
        # Only containers explicitly labeled for ANOTHER compose project are foreign;
        # unlabeled ones (manual runs, pre-labeling deploys) remain visible.
        return not project or project == self._compose_project

    async def get_active_containers(self):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_get_active_containers)

    def _sync_get_active_containers(self):
        return [
            container.name
            for container in self.docker_client.containers.list()
            if container.status == "running" and self._is_our_container(container)
        ]

    def start(self):
        """Start the loop that monitors active bots."""
        # Start MQTT manager and update loop in async context
        self._update_bots_task = asyncio.create_task(self._start_async())

        # Start controller performance dump loop
        self._performance_dump_task = asyncio.create_task(self._performance_dump_loop())
        logger.info(f"Controller performance dump started ({self.performance_dump_interval}s interval)")

    async def _start_async(self):
        """Start MQTT manager and update loop asynchronously."""
        logger.info("Starting MQTT manager...")
        await self.mqtt_manager.start()

        # Then start the update loop
        await self.update_active_bots()

    async def stop(self):
        """Stop the active bots monitoring loop."""
        if self._update_bots_task:
            self._update_bots_task.cancel()
            try:
                await self._update_bots_task
            except asyncio.CancelledError:
                pass
        self._update_bots_task = None

        if self._performance_dump_task:
            self._performance_dump_task.cancel()
            try:
                await self._performance_dump_task
            except asyncio.CancelledError:
                pass
        self._performance_dump_task = None

        # Stop MQTT manager
        await self.mqtt_manager.stop()

    async def update_active_bots(self, sleep_time=1.0):
        """Monitor and update active bots list using both Docker and MQTT discovery."""
        while True:
            try:
                # Get bots from Docker containers
                docker_bots = await self.get_active_containers()

                # Get bots from MQTT messages (auto-discovered)
                mqtt_bots = self.mqtt_manager.get_discovered_bots(timeout_seconds=30)  # 30 second timeout

                # Combine both sources
                all_active_bots = set([bot for bot in docker_bots + mqtt_bots if not self.is_bot_stopping(bot)])

                # Remove bots that are no longer active
                for bot_name in list(self.active_bots):
                    if bot_name not in all_active_bots:
                        self.mqtt_manager.clear_bot_data(bot_name)
                        del self.active_bots[bot_name]

                # Add new bots
                for bot_name in all_active_bots:
                    if bot_name not in self.active_bots:
                        self.active_bots[bot_name] = {
                            "bot_name": bot_name,
                            "status": "connected",
                            "source": "docker" if bot_name in docker_bots else "mqtt",
                        }
                        # Subscribe to this specific bot's topics
                        await self.mqtt_manager.subscribe_to_bot(bot_name)

            except Exception as e:
                logger.error(f"Error in update_active_bots: {e}", exc_info=True)

            await asyncio.sleep(sleep_time)

    # Interact with a specific bot
    async def start_bot(self, bot_name, **kwargs):
        """
        Start a bot with optional script.
        Maintains backward compatibility with kwargs.
        """
        if bot_name not in self.active_bots:
            logger.warning(f"Bot {bot_name} not found in active bots")
            return {"success": False, "message": f"Bot {bot_name} not found"}

        # Create StartCommandMessage.Request format
        data = {
            "log_level": kwargs.get("log_level"),
            "script": kwargs.get("script"),
            "conf": kwargs.get("conf"),
            "is_quickstart": kwargs.get("is_quickstart", False),
            "async_backend": kwargs.get("async_backend", True),
        }

        success = await self.mqtt_manager.publish_command(bot_name, "start", data)
        return {"success": success}

    async def stop_bot(self, bot_name, **kwargs):
        """
        Stop a bot.
        Maintains backward compatibility with kwargs.
        """
        if bot_name not in self.active_bots:
            logger.warning(f"Bot {bot_name} not found in active bots")
            return {"success": False, "message": f"Bot {bot_name} not found"}

        # Create StopCommandMessage.Request format
        data = {
            "skip_order_cancellation": kwargs.get("skip_order_cancellation", False),
            "async_backend": kwargs.get("async_backend", True),
        }

        success = await self.mqtt_manager.publish_command(bot_name, "stop", data)

        # Clear performance data after stop command to immediately reflect stopped status
        if success:
            self.mqtt_manager.clear_bot_controller_reports(bot_name)

        return {"success": success}

    async def import_strategy_for_bot(self, bot_name, strategy, **kwargs):
        """
        Import a strategy configuration for a bot.
        Maintains backward compatibility.
        """
        if bot_name not in self.active_bots:
            logger.warning(f"Bot {bot_name} not found in active bots")
            return {"success": False, "message": f"Bot {bot_name} not found"}

        # Create ImportCommandMessage.Request format
        data = {"strategy": strategy}
        success = await self.mqtt_manager.publish_command(bot_name, "import_strategy", data)
        return {"success": success}

    async def configure_bot(self, bot_name, params, **kwargs):
        """
        Configure bot parameters.
        Maintains backward compatibility.
        """
        if bot_name not in self.active_bots:
            logger.warning(f"Bot {bot_name} not found in active bots")
            return {"success": False, "message": f"Bot {bot_name} not found"}

        # Create ConfigCommandMessage.Request format
        data = {"params": params}
        success = await self.mqtt_manager.publish_command(bot_name, "config", data)
        return {"success": success}

    async def get_bot_history(self, bot_name, **kwargs):
        """
        Request bot trading history and wait for the response.
        Maintains backward compatibility.
        """
        if bot_name not in self.active_bots:
            logger.warning(f"Bot {bot_name} not found in active bots")
            return {"success": False, "message": f"Bot {bot_name} not found"}

        # Create HistoryCommandMessage.Request format
        data = {
            "days": kwargs.get("days", 0),
            "verbose": kwargs.get("verbose", False),
            "precision": kwargs.get("precision"),
            "async_backend": kwargs.get("async_backend", False),
        }

        # Use the new RPC method to wait for response
        timeout = kwargs.get("timeout", 30.0)  # Default 30 second timeout
        response = await self.mqtt_manager.publish_command_and_wait(bot_name, "history", data, timeout=timeout)

        if response is None:
            return {
                "success": False,
                "message": f"No response received from {bot_name} within {timeout} seconds",
                "timeout": True,
            }

        return {"success": True, "data": response}

    @staticmethod
    def determine_controller_performance(controller_reports):
        """Process controller reports and extract performance and custom_info.

        Args:
            controller_reports: Dict with controller_id as key and report dict as value.
                New format: Each report contains 'performance' and 'custom_info' keys.
                Old format: Report contains performance metrics directly (backward compatible).

        Returns:
            Dict with cleaned controller data including status, performance, and custom_info.
        """
        cleaned_data = {}
        for controller_id, report in controller_reports.items():
            try:
                # Support both new format (nested) and old format (flat)
                # New format: {"performance": {...}, "custom_info": {...}}
                # Old format: {...performance metrics directly...}
                if "performance" in report:
                    # New format with nested structure
                    performance = report.get("performance", {})
                    custom_info = report.get("custom_info", {})
                else:
                    # Old format - metrics are directly in the report
                    performance = report
                    custom_info = {}

                # Validate performance metrics are numeric (skip known non-numeric fields)
                non_numeric_fields = ("positions_summary", "close_type_counts")
                _ = sum(
                    metric for key, metric in performance.items()
                    if key not in non_numeric_fields and isinstance(metric, (int, float))
                )

                cleaned_data[controller_id] = {
                    "status": "running",
                    "performance": performance,
                    "custom_info": custom_info
                }
            except Exception as e:
                # Handle both formats in error case too
                if "performance" in report:
                    perf = report.get("performance", {})
                    info = report.get("custom_info", {})
                else:
                    perf = report
                    info = {}
                cleaned_data[controller_id] = {
                    "status": "error",
                    "error": f"Error processing controller data: {e}",
                    "performance": perf,
                    "custom_info": info
                }
        return cleaned_data

    def get_all_bots_status(self):
        """Get status information for all active bots."""
        all_bots_status = {}
        for bot in [bot for bot in self.active_bots if not self.is_bot_stopping(bot)]:
            status = self.get_bot_status(bot)
            status["source"] = self.active_bots[bot].get("source", "unknown")
            all_bots_status[bot] = status
        return all_bots_status

    def get_bot_status(self, bot_name):
        """
        Get status information for a specific bot.
        """
        if bot_name not in self.active_bots:
            return {"status": "not_found", "error": f"Bot {bot_name} not found"}

        try:
            # Check if bot is currently being stopped and archived
            if bot_name in self.stopping_bots:
                return {
                    "status": "stopping",
                    "message": "Bot is currently being stopped and archived",
                    "performance": {},
                    "error_logs": [],
                    "general_logs": [],
                    "recently_active": False,
                }

            # Get data from MQTT manager
            controller_reports = self.mqtt_manager.get_bot_controller_reports(bot_name)
            performance = self.determine_controller_performance(controller_reports)
            error_logs = self.mqtt_manager.get_bot_error_logs(bot_name)
            general_logs = self.mqtt_manager.get_bot_logs(bot_name)

            # Check if bot has sent recent messages (within last 30 seconds)
            discovered_bots = self.mqtt_manager.get_discovered_bots(timeout_seconds=30)
            recently_active = bot_name in discovered_bots

            # Determine status based on performance data and recent activity
            if len(performance) > 0 and recently_active:
                status = "running"
            elif len(performance) > 0 and not recently_active:
                status = "idle"  # Has performance data but no recent activity
            else:
                status = "stopped"

            return {
                "status": status,
                "performance": performance,
                "error_logs": error_logs,
                "general_logs": general_logs,
                "recently_active": recently_active,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def set_bot_stopping(self, bot_name: str):
        """Mark a bot as currently being stopped and archived."""
        self.stopping_bots.add(bot_name)
        logger.info(f"Marked bot {bot_name} as stopping")

    def clear_bot_stopping(self, bot_name: str):
        """Clear the stopping status for a bot."""
        self.stopping_bots.discard(bot_name)
        logger.info(f"Cleared stopping status for bot {bot_name}")

    def is_bot_stopping(self, bot_name: str) -> bool:
        """Check if a bot is currently being stopped."""
        return bot_name in self.stopping_bots

    # ============================================
    # Controller Performance Snapshots
    # ============================================

    async def _performance_dump_loop(self):
        """Periodically dump controller performance to the database (default every 5 minutes)."""
        while True:
            try:
                await self.dump_controller_performance()
            except Exception as e:
                logger.error(f"Error dumping controller performance: {e}")
            finally:
                await asyncio.sleep(self.performance_dump_interval)

    async def dump_controller_performance(self):
        """Save current controller performance for all active bots to the database."""
        snapshot_timestamp = datetime.now(timezone.utc)
        saved_count = 0

        try:
            async with self.db_manager.get_session_context() as session:
                repo = ControllerPerformanceRepository(session)

                snapshots = []
                for bot_name in list(self.active_bots):
                    if self.is_bot_stopping(bot_name):
                        continue

                    controller_reports = self.mqtt_manager.get_bot_controller_reports(bot_name)
                    performance_data = self.determine_controller_performance(controller_reports)

                    for controller_id, data in performance_data.items():
                        snapshots.append({
                            "bot_name": bot_name,
                            "controller_id": controller_id,
                            "status": data.get("status", "unknown"),
                            "performance": data.get("performance", {}),
                            "custom_info": data.get("custom_info", {}),
                            "snapshot_timestamp": snapshot_timestamp,
                        })

                saved_rows = await repo.save_controller_performances(snapshots)
                saved_count = len(saved_rows)

            if saved_count > 0:
                logger.info(f"Dumped {saved_count} controller performance snapshots")
        except Exception as e:
            logger.error(f"Error saving controller performance to database: {e}")
            raise

    async def get_controller_performance_history(
        self,
        bot_name: Optional[str] = None,
        controller_id: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        interval: str = "5m"
    ):
        """Get historical controller performance with pagination and interval sampling."""
        try:
            async with self.db_manager.get_session_context() as session:
                repo = ControllerPerformanceRepository(session)
                return await repo.get_performance_history(
                    bot_name=bot_name,
                    controller_id=controller_id,
                    limit=limit,
                    cursor=cursor,
                    start_time=start_time,
                    end_time=end_time,
                    interval=interval
                )
        except Exception as e:
            logger.error(f"Error getting controller performance history: {e}")
            return [], None, False

    async def get_latest_controller_performance(
        self,
        bot_name: Optional[str] = None
    ) -> List[Dict]:
        """Get the most recent performance snapshot for each bot/controller."""
        try:
            async with self.db_manager.get_session_context() as session:
                repo = ControllerPerformanceRepository(session)
                return await repo.get_latest_performance(bot_name=bot_name)
        except Exception as e:
            logger.error(f"Error getting latest controller performance: {e}")
            return []

    # ============================================
    # Bot Run persistence
    # ============================================

    async def mark_bot_run_stopped(self, bot_name: str, final_status: Optional[Dict] = None):
        """Update a bot run status to STOPPED, capturing the final status snapshot."""
        async with self.db_manager.get_session_context() as session:
            bot_run_repo = BotRunRepository(session)
            await bot_run_repo.update_bot_run_stopped(bot_name, final_status=final_status)
            logger.info(f"Updated bot run status to STOPPED for {bot_name}")

    async def get_bot_runs(
        self,
        bot_name: Optional[str] = None,
        account_name: Optional[str] = None,
        strategy_type: Optional[str] = None,
        strategy_name: Optional[str] = None,
        run_status: Optional[str] = None,
        deployment_status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict]:
        """Get bot runs with optional filtering, serialized as dictionaries."""
        async with self.db_manager.get_session_context() as session:
            bot_run_repo = BotRunRepository(session)
            bot_runs = await bot_run_repo.get_bot_runs(
                bot_name=bot_name,
                account_name=account_name,
                strategy_type=strategy_type,
                strategy_name=strategy_name,
                run_status=run_status,
                deployment_status=deployment_status,
                limit=limit,
                offset=offset,
            )
            return [self._serialize_bot_run(run) for run in bot_runs]

    async def get_bot_run_stats(self) -> Dict[str, Any]:
        """Get statistics about bot runs."""
        async with self.db_manager.get_session_context() as session:
            bot_run_repo = BotRunRepository(session)
            return await bot_run_repo.get_bot_run_stats()

    async def get_bot_run_by_id(self, bot_run_id: int) -> Optional[Dict]:
        """Get a specific bot run by ID, serialized as a dictionary (None if not found)."""
        async with self.db_manager.get_session_context() as session:
            bot_run_repo = BotRunRepository(session)
            bot_run = await bot_run_repo.get_bot_run_by_id(bot_run_id)
            if not bot_run:
                return None
            return self._serialize_bot_run(bot_run)

    async def delete_bot_run(self, bot_run_id: int) -> Optional[Dict]:
        """Delete a bot run record and its archived folder.

        Returns a dict with ``bot_name`` and ``archived_folder_deleted`` keys,
        or None if the bot run does not exist.
        """
        async with self.db_manager.get_session_context() as session:
            bot_run_repo = BotRunRepository(session)
            bot_run = await bot_run_repo.delete_bot_run(bot_run_id)

            if not bot_run:
                return None

            # Also delete the archived bot folder if it exists
            archived_dir = os.path.join('bots', 'archived', bot_run.instance_name)
            archived_deleted = False
            if os.path.isdir(archived_dir):
                try:
                    import platform
                    import subprocess
                    if platform.system() == 'Darwin':
                        # Strip macOS ACLs (Docker adds "deny delete" ACLs)
                        subprocess.run(['chmod', '-R', '-N', archived_dir], check=False)
                    shutil.rmtree(archived_dir)
                    archived_deleted = True
                    logger.info(f"Deleted archived folder: {archived_dir}")
                except Exception as e:
                    logger.warning(f"Failed to delete archived folder {archived_dir}: {e}")

            return {
                "bot_name": bot_run.bot_name,
                "archived_folder_deleted": archived_deleted,
            }

    async def create_bot_run(self, **kwargs):
        """Create a bot run record. Errors are logged and swallowed so that a
        failed tracking write never fails the caller's deployment."""
        try:
            async with self.db_manager.get_session_context() as session:
                bot_run_repo = BotRunRepository(session)
                await bot_run_repo.create_bot_run(**kwargs)
                logger.info(f"Created bot run record for deployment {kwargs.get('instance_name')}")
        except Exception as e:
            logger.error(f"Failed to create bot run record: {e}")
            # Don't fail the deployment if bot run creation fails

    @staticmethod
    def _serialize_bot_run(run) -> Dict:
        """Serialize a BotRun ORM object into a JSON-friendly dictionary."""
        return {
            "id": run.id,
            "bot_name": run.bot_name,
            "instance_name": run.instance_name,
            "deployed_at": run.deployed_at.isoformat() if run.deployed_at else None,
            "stopped_at": run.stopped_at.isoformat() if run.stopped_at else None,
            "strategy_type": run.strategy_type,
            "strategy_name": run.strategy_name,
            "config_name": run.config_name,
            "account_name": run.account_name,
            "image_version": run.image_version,
            "deployment_status": run.deployment_status,
            "run_status": run.run_status,
            "deployment_config": run.deployment_config,
            "final_status": run.final_status,
            "error_message": run.error_message,
            "retirement_status": getattr(run, "retirement_status", None),
            "retirement_evidence": BotsOrchestrator._safe_json_loads(
                getattr(run, "retirement_evidence", None)
            ),
        }

    @staticmethod
    def _safe_json_loads(raw: Optional[str]):
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    # ============================================
    # Stop & Archive orchestration
    # ============================================

    # -- Retirement evidence helpers (CDX-005) --------------------------------

    @staticmethod
    def _utc_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def _bot_run_account(self, bot_name: str) -> Optional[str]:
        """Account of the bot's latest run, or None (no row / DB unavailable)."""
        try:
            async with self.db_manager.get_session_context() as session:
                run = await BotRunRepository(session).get_latest_bot_run(bot_name)
                return run.account_name if run else None
        except Exception as e:
            logger.error(f"Failed to look up bot run account for {bot_name}: {e}")
            return None

    async def _persist_retirement_evidence(self, bot_name: str, evidence: Dict[str, Any]):
        """Progressive evidence write; never changes statuses (see repository)."""
        try:
            async with self.db_manager.get_session_context() as session:
                await BotRunRepository(session).update_bot_run_retirement_evidence(bot_name, evidence)
        except Exception as e:
            logger.error(f"Failed to persist retirement evidence for {bot_name}: {e}")

    async def _finalize_retirement(
        self,
        bot_name: str,
        evidence: Dict[str, Any],
        final_status: Optional[Dict] = None,
        error_message: Optional[str] = None,
    ):
        """Terminal retirement write — the repository computes VERIFIED /
        UNVERIFIED from the evidence itself (never from a caller flag)."""
        try:
            async with self.db_manager.get_session_context() as session:
                row = await BotRunRepository(session).finalize_bot_run_retirement(
                    bot_name, evidence, final_status=final_status, error_message=error_message
                )
            if row is not None:
                logger.info(
                    f"Finalized retirement for {bot_name}: run_status={row.run_status}, "
                    f"retirement_status={row.retirement_status}"
                )
            else:
                logger.warning(f"No open bot run row to finalize retirement for {bot_name}")
        except Exception as e:
            logger.error(f"Failed to finalize retirement for {bot_name}: {e}")

    @staticmethod
    def _rpc_response_data(response) -> Optional[Dict[str, Any]]:
        """Extract the payload from the engine's RPC reply envelope
        ``{"header": {...}, "data": {"status": <int>, "msg": <str>, ...}}``
        (engine remote_iface/mqtt.py:_wrap_response). Anything else — string,
        bare dict without the ``data`` envelope, None — is malformed and maps
        to None: an unparseable reply is never evidence (fail-closed)."""
        if isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, dict):
                return data
        return None

    async def _await_strategy_quiescence(
        self, bot_id: str, evidence: Dict[str, Any], timeouts: RetirementTimeouts
    ):
        """Bounded poll of the bot's status RPC for strategy quiescence.

        The engine clears ``trading_core.strategy`` only at the END of
        ``stop_loop()`` — after ``StrategyV2Base.on_stop()`` (controllers
        stopped, executors stored), exchange-acknowledged ``cancel_all``,
        connector removal and markets-recorder shutdown. Once cleared, the
        status RPC replies ERROR / 'No strategy is currently running!': that
        reply is the bot's OWN confirmation that the graceful shutdown ran to
        completion — unlike the stop ack, which (async_backend) only proves
        the command was accepted. Silence, unrelated errors, or malformed
        replies are never quiescence; on timeout the stage stays unconfirmed.
        """
        deadline = time.monotonic() + timeouts.quiescence_timeout
        while True:
            result = await self.mqtt_manager.publish_command_with_ack(
                bot_id,
                "status",
                {"async_backend": True},
                timeout=max(timeouts.poll_interval, 5.0),
            )
            data = self._rpc_response_data(result.get("response")) if result.get("published") else None
            if (
                data is not None
                and data.get("status") == MQTT_RPC_ERROR
                and _NO_STRATEGY_RUNNING in str(data.get("msg", "")).lower()
            ):
                evidence["quiescence_confirmed_at"] = self._utc_iso()
                evidence["quiescence_basis"] = "bot status RPC reports no strategy running"
                return
            if time.monotonic() >= deadline:
                evidence["quiescence_basis"] = "timeout"
                logger.warning(
                    f"Bot {bot_id} did not report strategy quiescence within "
                    f"{timeouts.quiescence_timeout}s — retirement cannot be verified"
                )
                return
            await asyncio.sleep(timeouts.poll_interval)

    async def _await_zero_open_orders(
        self, account_name: Optional[str], evidence: Dict[str, Any], timeouts: RetirementTimeouts
    ):
        """Bounded poll of the API's own order records for zero active orders.

        Confirmation requires the account to HAVE order history: an account
        with no recorded orders at all proves nothing about the exchange
        (a disconnected recorder looks identical), so silence is never read
        as zero (fail-closed). On timeout the remaining count is recorded and
        the stage stays unconfirmed.
        """
        if not account_name:
            evidence["zero_open_orders_basis"] = "no_bot_run_row"
            return

        deadline = time.monotonic() + timeouts.zero_open_orders_timeout
        last_summary = None
        while True:
            try:
                async with self.db_manager.get_session_context() as session:
                    summary = await OrderRepository(session).get_orders_summary(account_name=account_name)
            except Exception as e:
                logger.warning(f"Order summary poll failed for account {account_name}: {e}")
                summary = None

            if summary is not None:
                last_summary = summary
                if summary.get("total_orders", 0) == 0:
                    evidence["zero_open_orders_basis"] = "no_order_history"
                    return
                if summary.get("active_orders", 0) == 0:
                    evidence["zero_open_orders_confirmed_at"] = self._utc_iso()
                    evidence["zero_open_orders_basis"] = (
                        f"orders_recorded_total={summary.get('total_orders')}"
                    )
                    return

            if time.monotonic() >= deadline:
                evidence["open_orders_remaining"] = (
                    last_summary.get("active_orders") if last_summary is not None else None
                )
                evidence["zero_open_orders_basis"] = "timeout"
                return
            await asyncio.sleep(timeouts.poll_interval)

    async def _active_order_count(self, account_name: str) -> Optional[int]:
        """One-shot active-order count for the fill-drain recheck (None on error)."""
        try:
            async with self.db_manager.get_session_context() as session:
                summary = await OrderRepository(session).get_orders_summary(account_name=account_name)
                return summary.get("active_orders")
        except Exception as e:
            logger.warning(f"Fill-drain recheck failed for account {account_name}: {e}")
            return None

    async def stop_and_archive_bot(
        self,
        bot_name: str,
        container_name: str,
        bot_name_for_orchestrator: str,
        skip_order_cancellation: bool,
        archive_locally: bool,
        s3_bucket: Optional[str],
        docker_manager: DockerService,
        bot_archiver: BotArchiver,
        retirement_timeouts: Optional[RetirementTimeouts] = None,
    ):
        """Stop a bot and archive its data via the acknowledged-retirement
        state machine (CDX-005 / CDX-M03).

        This is the background-task body for ``stop-and-archive-bot``. It is
        FastAPI-agnostic and can be invoked/tested directly.

        Each stage persists evidence gathered from CONFIRMED data only — the
        bot's own validated RPC replies (stop accepted; strategy quiescent —
        the bot reporting "no strategy running", which the engine sets only
        after its full graceful stop sequence), the API's order records, and
        Docker container state. MQTT publish success is never evidence. Where
        confirmation is impossible (silent bot, timeout, dirty exit) the stage
        stays unconfirmed and the run finalizes UNVERIFIED — never fabricated,
        never defaulted to verified. Archival proceeds regardless; only the
        VERIFIED marker is withheld. The bot_runs row is flipped to STOPPED
        only at finalization, after the container has actually exited — never
        before the stop is even requested (the old code wrote STOPPED first).
        """
        evidence: Dict[str, Any] = {
            "initiated_at": self._utc_iso(),
            "skip_order_cancellation": bool(skip_order_cancellation),
        }
        try:
            timeouts = retirement_timeouts or RetirementTimeouts.from_env()
            logger.info(f"Starting background stop-and-archive for {bot_name}")

            # Step 1: Capture bot final status before stopping (while bot is still running)
            logger.info(f"Capturing final status for {bot_name_for_orchestrator}")
            final_status = None
            try:
                final_status = self.get_bot_status(bot_name_for_orchestrator)
                logger.info(f"Captured final status for {bot_name_for_orchestrator}: {final_status}")
            except Exception as e:
                logger.warning(f"Failed to capture final status for {bot_name_for_orchestrator}: {e}")

            account_name = await self._bot_run_account(bot_name)
            await self._persist_retirement_evidence(bot_name, evidence)

            # Step 2: Mark the bot as stopping and request the stop, waiting for
            # the bot's OWN response — publish success is not an acknowledgement.
            if bot_name_for_orchestrator not in self.active_bots:
                logger.error(
                    f"Bot {bot_name_for_orchestrator} not found in active bots — cannot request stop"
                )
                evidence["failure"] = "bot not in active bots at stop time"
                await self._persist_retirement_evidence(bot_name, evidence)
                return

            self.set_bot_stopping(bot_name_for_orchestrator)
            logger.info(f"Stopping bot trading process for {bot_name_for_orchestrator}")

            async def _on_stop_published():
                # Persist "stop requested" at the TRUE stage boundary — the
                # moment the broker accepts the publish — not after the (up to
                # stop_ack_timeout) wait for the bot's reply: a crash during
                # that wait must not lose the fact the stop was sent.
                evidence["stop_requested_at"] = self._utc_iso()
                if not skip_order_cancellation:
                    evidence["cancellation_requested_at"] = evidence["stop_requested_at"]
                await self._persist_retirement_evidence(bot_name, evidence)

            stop_result = await self.mqtt_manager.publish_command_with_ack(
                bot_name_for_orchestrator,
                "stop",
                {"skip_order_cancellation": skip_order_cancellation, "async_backend": True},
                timeout=timeouts.stop_ack_timeout,
                on_published=_on_stop_published,
            )

            if not stop_result.get("published"):
                # The stop request never reached the broker: the bot may still be
                # trading. Leave the run row untouched (fail-closed) and do NOT
                # touch the container.
                logger.error(f"Failed to publish stop command for {bot_name_for_orchestrator}")
                evidence["failure"] = "stop command could not be published to the broker"
                await self._persist_retirement_evidence(bot_name, evidence)
                return

            # Defensive: an MQTT implementation that never ran the callback
            # still leaves a correct trail (published implies requested).
            if evidence.get("stop_requested_at") is None:
                evidence["stop_requested_at"] = self._utc_iso()
                if not skip_order_cancellation:
                    evidence["cancellation_requested_at"] = evidence["stop_requested_at"]
            # Clear performance data after the stop request so status reflects it.
            self.mqtt_manager.clear_bot_controller_reports(bot_name_for_orchestrator)

            stop_data = self._rpc_response_data(stop_result.get("response"))
            if stop_data is not None and stop_data.get("status") == MQTT_RPC_SUCCESS:
                # The bot's own SUCCESS reply: the stop command was received
                # and accepted. With async_backend this proves acceptance, not
                # completion — completion is confirmed by the quiescence stage
                # below. Error, malformed or absent replies are never acks.
                evidence["stop_ack_at"] = self._utc_iso()
                evidence["stop_ack"] = str(stop_result["response"])[:500]
            else:
                logger.warning(
                    f"No valid stop acknowledgement from {bot_name_for_orchestrator} "
                    f"(reply: {str(stop_result.get('response'))[:200]!r}) — "
                    f"retirement cannot be verified"
                )
            await self._persist_retirement_evidence(bot_name, evidence)

            # Step 3: Strategy quiescence — the bot itself must report that no
            # strategy is running (the engine sets that only at the END of its
            # graceful stop sequence). Everything downstream keys off this.
            await self._await_strategy_quiescence(bot_name_for_orchestrator, evidence, timeouts)
            await self._persist_retirement_evidence(bot_name, evidence)

            # Step 4: Zero open orders + final-fill drain (bounded polling
            # replaces the old fixed 15 s sleep). Only meaningful AFTER
            # quiescence: until the bot confirms its stop sequence finished it
            # may still be trading, so a momentary zero proves nothing.
            if evidence.get("quiescence_confirmed_at"):
                await self._await_zero_open_orders(account_name, evidence, timeouts)
                if evidence.get("zero_open_orders_confirmed_at"):
                    await asyncio.sleep(timeouts.fill_drain_seconds)
                    remaining = await self._active_order_count(account_name)
                    if remaining == 0:
                        evidence["fills_drained_at"] = self._utc_iso()
                    else:
                        evidence["orders_active_after_drain"] = remaining
                        logger.warning(
                            f"Active orders reappeared (or were unreadable) during the fill drain "
                            f"for {bot_name}: {remaining!r} — retirement cannot be verified"
                        )
            else:
                evidence["zero_open_orders_basis"] = "quiescence_unconfirmed"
            await self._persist_retirement_evidence(bot_name, evidence)

            # Step 5: Stop the container with monitoring
            max_retries = 10
            retry_interval = 2
            container_stopped = False
            exit_code = None

            for i in range(max_retries):
                logger.info(f"Attempting to stop container {container_name} (attempt {i+1}/{max_retries})")
                docker_manager.stop_container(container_name)

                # Check if container is already stopped
                container_status = docker_manager.get_container_status(container_name)
                state = container_status.get("state", {}) if container_status.get("success") else {}
                if state.get("status") == "exited":
                    container_stopped = True
                    exit_code = state.get("exit_code")
                    logger.info(f"Container {container_name} is already stopped (exit code {exit_code!r})")
                    break

                await asyncio.sleep(retry_interval)

            if not container_stopped:
                # The bot process may still be alive; leave the run row untouched
                # (fail-closed) rather than recording a stop that did not happen.
                logger.error(f"Failed to stop container {container_name} after {max_retries} attempts")
                evidence["failure"] = f"container did not exit after {max_retries} stop attempts"
                await self._persist_retirement_evidence(bot_name, evidence)
                return

            evidence["process_exited_at"] = self._utc_iso()
            evidence["container_exit_code"] = exit_code
            if exit_code == 0 and evidence.get("quiescence_confirmed_at"):
                # State flush/checkpoint evidence requires BOTH: the bot's own
                # confirmation that stop_loop() completed (which runs the
                # durable-state shutdown — executor store, markets-recorder
                # stop; the ladder controller additionally write-throughs its
                # state ledger on every mutation) AND a clean process exit
                # afterwards. Exit code 0 ALONE is never flush evidence — a
                # container can exit 0 without ever running that path.
                evidence["state_flushed_at"] = self._utc_iso()
                evidence["state_flush_basis"] = "quiescence_confirmed+clean_exit"
            else:
                logger.warning(
                    f"Container {container_name} exited with code {exit_code!r} "
                    f"(quiescence confirmed: {bool(evidence.get('quiescence_confirmed_at'))}) — "
                    f"state flush unconfirmed, retirement cannot be verified"
                )
            await self._persist_retirement_evidence(bot_name, evidence)

            # Step 6: Archive the bot data
            instance_dir = os.path.join('bots', 'instances', container_name)
            logger.info(f"Archiving bot data from {instance_dir}")

            try:
                if archive_locally:
                    bot_archiver.archive_locally(container_name, instance_dir)
                else:
                    bot_archiver.archive_and_upload(container_name, instance_dir, bucket_name=s3_bucket)
                evidence["archived_at"] = self._utc_iso()
                logger.info(f"Successfully archived bot data for {container_name}")
            except Exception as e:
                logger.error(f"Archive failed: {str(e)}")
                evidence["archive_error"] = str(e)[:500]
                # Continue with removal even if archive fails
            # Archive evidence must survive a crash during container removal —
            # persist BEFORE the next destructive stage, not at finalization.
            await self._persist_retirement_evidence(bot_name, evidence)

            # Step 7: Remove the container
            logging.info(f"Removing container {container_name}")
            remove_response = docker_manager.remove_container(container_name, force=False)

            if not remove_response.get("success"):
                # If graceful remove fails, try force remove
                logging.warning("Graceful container removal failed, attempting force removal")
                remove_response = docker_manager.remove_container(container_name, force=True)

            if remove_response.get("success"):
                logging.info(f"Successfully completed stop-and-archive for bot {bot_name}")

                # Step 8: Finalize retirement (STOPPED + VERIFIED/UNVERIFIED from
                # the evidence), then flip deployment status to ARCHIVED.
                await self._finalize_retirement(bot_name, evidence, final_status=final_status)
                try:
                    async with self.db_manager.get_session_context() as session:
                        bot_run_repo = BotRunRepository(session)
                        await bot_run_repo.update_bot_run_archived(bot_name)
                        logger.info(f"Updated bot run deployment status to ARCHIVED for {bot_name}")
                except Exception as e:
                    logger.error(f"Failed to update bot run to archived: {e}")
            else:
                logging.error(f"Failed to remove container {container_name}")
                await self._finalize_retirement(
                    bot_name,
                    evidence,
                    final_status=final_status,
                    error_message="Failed to remove container during archive process",
                )

        except Exception as e:
            logging.error(f"Error in background stop-and-archive for {bot_name}: {str(e)}")
            # Terminal error: the run is over but nothing more can be confirmed —
            # finalize as ERROR / UNVERIFIED with the evidence gathered so far.
            await self._finalize_retirement(bot_name, evidence, error_message=str(e))
        finally:
            # Always clear the stopping status when the background task completes
            self.clear_bot_stopping(bot_name_for_orchestrator)
            logger.info(f"Cleared stopping status for bot {bot_name}")

            # Remove bot from active_bots and clear all MQTT data
            if bot_name_for_orchestrator in self.active_bots:
                self.mqtt_manager.clear_bot_data(bot_name_for_orchestrator)
                del self.active_bots[bot_name_for_orchestrator]
                logger.info(f"Removed bot {bot_name_for_orchestrator} from active_bots and cleared MQTT data")
