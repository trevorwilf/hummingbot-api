import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import BotRun

# ---------------------------------------------------------------------------
# Acknowledged-retirement state machine (CDX-005 / CDX-M03)
# ---------------------------------------------------------------------------
# A bot run is VERIFIED-retired only when every postcondition below carries
# persisted evidence. Anything less — legacy rows, timeouts, missing acks,
# dirty exits — stays UNVERIFIED. Evidence comes from confirmed data only
# (bot RPC response messages, the API's own order records, Docker container
# state); MQTT publish success is never evidence (utils/mqtt_manager.py:
# publish proves broker delivery, not that the bot did anything).
RETIREMENT_VERIFIED = "VERIFIED"
RETIREMENT_UNVERIFIED = "UNVERIFIED"

# Evidence keys required (non-None) before VERIFIED may be persisted. Each is
# an ISO-8601 UTC timestamp except skip_order_cancellation (bool, recorded so
# the evidence shows whether cancellation was requested or skipped):
#   initiated_at                  — retirement state machine started
#   skip_order_cancellation       — the stop request's cancellation flag
#   stop_requested_at             — stop command reached the broker
#   stop_ack_at                   — the bot's own RPC response to the stop
#                                   command (strategy quiescence acknowledged)
#   zero_open_orders_confirmed_at — the API's order records show zero active
#                                   orders for the run's account (independent
#                                   of cancellation — see fill drain below)
#   fills_drained_at              — zero active orders still held after the
#                                   final-fill drain window
#   state_flushed_at              — container exited with code 0 (the engine's
#                                   graceful-shutdown/checkpoint path ran)
#   process_exited_at             — container observed in the exited state
#   archived_at                   — bot data archive completed
REQUIRED_RETIREMENT_EVIDENCE = (
    "initiated_at",
    "skip_order_cancellation",
    "stop_requested_at",
    "stop_ack_at",
    "zero_open_orders_confirmed_at",
    "fills_drained_at",
    "state_flushed_at",
    "process_exited_at",
    "archived_at",
)


def missing_retirement_evidence(evidence: Dict[str, Any]) -> List[str]:
    """Return the evidence keys still missing for a VERIFIED retirement.

    ``is None`` (not falsy) — ``skip_order_cancellation=False`` is present
    evidence. When cancellation was NOT skipped, the request marker
    ``cancellation_requested_at`` is additionally required; when it WAS
    skipped, verification remains possible only through the independent
    zero-open-orders confirmation, which is already in the required set.
    """
    missing = [k for k in REQUIRED_RETIREMENT_EVIDENCE if evidence.get(k) is None]
    if evidence.get("skip_order_cancellation") is False and evidence.get("cancellation_requested_at") is None:
        missing.append("cancellation_requested_at")
    return missing


class BotRunRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_bot_run(
        self,
        bot_name: str,
        instance_name: str,
        strategy_type: str,  # 'script' or 'controller'
        strategy_name: str,
        account_name: str,
        config_name: Optional[str] = None,
        image_version: Optional[str] = None,
        deployment_config: Optional[Dict[str, Any]] = None
    ) -> BotRun:
        """Create a new bot run record."""
        bot_run = BotRun(
            bot_name=bot_name,
            instance_name=instance_name,
            strategy_type=strategy_type,
            strategy_name=strategy_name,
            config_name=config_name,
            account_name=account_name,
            image_version=image_version,
            deployment_config=json.dumps(deployment_config) if deployment_config else None,
            deployment_status="DEPLOYED",
            run_status="CREATED"
        )
        
        self.session.add(bot_run)
        await self.session.flush()
        await self.session.refresh(bot_run)
        return bot_run


    async def update_bot_run_stopped(
        self,
        bot_name: str,
        final_status: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None
    ) -> Optional[BotRun]:
        """Mark a bot run as stopped and save final status."""
        stmt = select(BotRun).where(
            and_(
                BotRun.bot_name == bot_name,
                or_(BotRun.run_status == "RUNNING", BotRun.run_status == "CREATED")
            )
        ).order_by(desc(BotRun.deployed_at))
        
        result = await self.session.execute(stmt)
        bot_run = result.scalar_one_or_none()
        
        if bot_run:
            bot_run.run_status = "STOPPED" if not error_message else "ERROR"
            bot_run.stopped_at = datetime.utcnow()
            bot_run.final_status = json.dumps(final_status) if final_status else None
            bot_run.error_message = error_message
            await self.session.flush()
            await self.session.refresh(bot_run)
            
        return bot_run

    async def update_bot_run_archived(self, bot_name: str) -> Optional[BotRun]:
        """Mark a bot run as archived."""
        stmt = select(BotRun).where(
            BotRun.bot_name == bot_name
        ).order_by(desc(BotRun.deployed_at))

        result = await self.session.execute(stmt)
        bot_run = result.scalar_one_or_none()

        if bot_run:
            bot_run.deployment_status = "ARCHIVED"
            bot_run.stopped_at = datetime.now(timezone.utc)
            await self.session.flush()
            await self.session.refresh(bot_run)

        return bot_run

    async def _latest_open_run(self, bot_name: str) -> Optional[BotRun]:
        """The newest not-yet-archived run for ``bot_name`` — the retirement
        target. Includes rows already flipped to STOPPED by the plain stop-bot
        route, so a stop-then-archive sequence still accumulates evidence."""
        stmt = select(BotRun).where(
            and_(
                BotRun.bot_name == bot_name,
                BotRun.deployment_status == "DEPLOYED",
            )
        ).order_by(desc(BotRun.deployed_at))
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def update_bot_run_retirement_evidence(
        self, bot_name: str, evidence: Dict[str, Any]
    ) -> Optional[BotRun]:
        """Persist in-progress retirement evidence WITHOUT changing any status.

        Called after each stage of the retirement state machine so a crash
        mid-retirement leaves an audit trail. Never touches ``run_status`` or
        ``retirement_status`` — an interrupted retirement stays exactly as
        fail-closed as it was.
        """
        bot_run = await self._latest_open_run(bot_name)
        if bot_run:
            bot_run.retirement_evidence = json.dumps(evidence)
            await self.session.flush()
            await self.session.refresh(bot_run)
        return bot_run

    async def finalize_bot_run_retirement(
        self,
        bot_name: str,
        evidence: Dict[str, Any],
        final_status: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
    ) -> Optional[BotRun]:
        """Terminal write of the retirement state machine (CDX-005).

        ``retirement_status`` is computed HERE, from the evidence itself —
        there is deliberately no caller-supplied ``verified`` flag, so a
        verified-STOPPED row cannot be persisted before every postcondition
        (exchange-confirmed zero open orders included) carries evidence.
        Missing evidence or an error → UNVERIFIED, with the missing keys
        recorded in the persisted evidence for the operator.
        """
        bot_run = await self._latest_open_run(bot_name)
        if not bot_run:
            return None

        missing = missing_retirement_evidence(evidence)
        verified = not missing and error_message is None

        persisted = dict(evidence)
        if missing:
            persisted["missing_evidence"] = missing

        bot_run.run_status = "STOPPED" if error_message is None else "ERROR"
        bot_run.stopped_at = datetime.now(timezone.utc)
        if final_status is not None:
            bot_run.final_status = json.dumps(final_status)
        bot_run.error_message = error_message
        bot_run.retirement_evidence = json.dumps(persisted)
        bot_run.retirement_status = RETIREMENT_VERIFIED if verified else RETIREMENT_UNVERIFIED
        await self.session.flush()
        await self.session.refresh(bot_run)
        return bot_run

    async def get_bot_runs(
        self,
        bot_name: Optional[str] = None,
        account_name: Optional[str] = None,
        strategy_type: Optional[str] = None,
        strategy_name: Optional[str] = None,
        run_status: Optional[str] = None,
        deployment_status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[BotRun]:
        """Get bot runs with optional filters."""
        stmt = select(BotRun)
        
        conditions = []
        if bot_name:
            conditions.append(BotRun.bot_name == bot_name)
        if account_name:
            conditions.append(BotRun.account_name == account_name)
        if strategy_type:
            conditions.append(BotRun.strategy_type == strategy_type)
        if strategy_name:
            conditions.append(BotRun.strategy_name == strategy_name)
        if run_status:
            conditions.append(BotRun.run_status == run_status)
        if deployment_status:
            conditions.append(BotRun.deployment_status == deployment_status)
            
        if conditions:
            stmt = stmt.where(and_(*conditions))
            
        stmt = stmt.order_by(desc(BotRun.deployed_at)).limit(limit).offset(offset)
        
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_bot_run_by_id(self, bot_run_id: int) -> Optional[BotRun]:
        """Get a specific bot run by ID."""
        stmt = select(BotRun).where(BotRun.id == bot_run_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_bot_run(self, bot_name: str) -> Optional[BotRun]:
        """Get the latest bot run for a specific bot."""
        stmt = select(BotRun).where(
            BotRun.bot_name == bot_name
        ).order_by(desc(BotRun.deployed_at))
        
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_bot_runs(self) -> List[BotRun]:
        """Get all currently active (running) bot runs."""
        stmt = select(BotRun).where(
            and_(
                BotRun.run_status == "RUNNING",
                BotRun.deployment_status == "DEPLOYED"
            )
        ).order_by(desc(BotRun.deployed_at))
        
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_bot_run_stats(self) -> Dict[str, Any]:
        """Get statistics about bot runs."""
        # Total runs
        total_stmt = select(func.count(BotRun.id))
        total_result = await self.session.execute(total_stmt)
        total_runs = total_result.scalar()
        
        # Active runs
        active_stmt = select(func.count(BotRun.id)).where(
            and_(
                BotRun.run_status == "RUNNING",
                BotRun.deployment_status == "DEPLOYED"
            )
        )
        active_result = await self.session.execute(active_stmt)
        active_runs = active_result.scalar()
        
        # Runs by strategy type
        strategy_stmt = select(
            BotRun.strategy_type,
            func.count(BotRun.id).label('count')
        ).group_by(BotRun.strategy_type)
        strategy_result = await self.session.execute(strategy_stmt)
        strategy_counts = {row.strategy_type: row.count for row in strategy_result}
        
        # Runs by status
        status_stmt = select(
            BotRun.run_status,
            func.count(BotRun.id).label('count')
        ).group_by(BotRun.run_status)
        status_result = await self.session.execute(status_stmt)
        status_counts = {row.run_status: row.count for row in status_result}
        
        return {
            "total_runs": total_runs,
            "active_runs": active_runs,
            "strategy_type_counts": strategy_counts,
            "status_counts": status_counts
        }

    async def delete_bot_run(self, bot_run_id: int) -> Optional[BotRun]:
        """Delete a bot run record by ID. Returns the deleted record or None."""
        stmt = select(BotRun).where(BotRun.id == bot_run_id)
        result = await self.session.execute(stmt)
        bot_run = result.scalar_one_or_none()

        if bot_run:
            await self.session.delete(bot_run)
            await self.session.flush()

        return bot_run

    async def delete_bot_runs_by_bot_name(self, bot_name: str) -> int:
        """Delete all bot run records for a given bot_name. Returns count deleted."""
        stmt = select(BotRun).where(BotRun.bot_name == bot_name)
        result = await self.session.execute(stmt)
        bot_runs = result.scalars().all()

        count = len(bot_runs)
        for bot_run in bot_runs:
            await self.session.delete(bot_run)

        if count > 0:
            await self.session.flush()

        return count