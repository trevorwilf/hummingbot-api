import asyncio
import logging
import math
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Union

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.event.event_forwarder import SourceInfoEventForwarder
from hummingbot.core.event.events import BuyOrderCreatedEvent, MarketEvent, OrderFilledEvent, SellOrderCreatedEvent, TradeType

from contextlib import asynccontextmanager

from database import AsyncDatabaseManager, OrderRepository, TradeRepository

# Initialize logger
logger = logging.getLogger(__name__)

# CDX-006 adjudication (CDX-R01): fill events are handled as independent
# asyncio tasks, so two DISTINCT fills for one order can interleave their
# read-modify-write of the order aggregates — both read the same pre-fill
# state and the last writer erases the other fill. Serialize per order id
# in-process; OrderRepository.update_order_fill additionally takes a
# SELECT ... FOR UPDATE row lock for cross-process safety on postgres.
# Entries are refcounted so the registry cannot grow without bound.
_order_fill_locks: dict = {}


@asynccontextmanager
async def _order_fill_lock(client_order_id: str):
    entry = _order_fill_locks.setdefault(client_order_id, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0 and _order_fill_locks.get(client_order_id) is entry:
            del _order_fill_locks[client_order_id]


class OrdersRecorder:
    """
    Custom orders recorder that mimics Hummingbot's MarketsRecorder functionality
    but uses our AsyncDatabaseManager for storage.
    """

    def __init__(self, db_manager: AsyncDatabaseManager, account_name: str, connector_name: str):
        self.db_manager = db_manager
        self.account_name = account_name
        self.connector_name = connector_name
        self._connector: Optional[ConnectorBase] = None

        # Strong references to in-flight event handler tasks so they are not garbage-collected before completing
        self._pending_tasks: set[asyncio.Task] = set()

        # Create event forwarders similar to MarketsRecorder
        self._create_order_forwarder = SourceInfoEventForwarder(self._did_create_order)
        self._fill_order_forwarder = SourceInfoEventForwarder(self._did_fill_order)
        self._cancel_order_forwarder = SourceInfoEventForwarder(self._did_cancel_order)
        self._fail_order_forwarder = SourceInfoEventForwarder(self._did_fail_order)
        self._complete_order_forwarder = SourceInfoEventForwarder(self._did_complete_order)

        # Event pairs mapping events to forwarders
        self._event_pairs = [
            (MarketEvent.BuyOrderCreated, self._create_order_forwarder),
            (MarketEvent.SellOrderCreated, self._create_order_forwarder),
            (MarketEvent.OrderFilled, self._fill_order_forwarder),
            (MarketEvent.OrderCancelled, self._cancel_order_forwarder),
            (MarketEvent.OrderFailure, self._fail_order_forwarder),
            (MarketEvent.BuyOrderCompleted, self._complete_order_forwarder),
            (MarketEvent.SellOrderCompleted, self._complete_order_forwarder),
        ]

    def start(self, connector: ConnectorBase):
        """Start recording orders for the given connector."""
        # Idempotency guard: prevent double-registration of listeners
        if self._connector is not None:
            logger.debug(f"OrdersRecorder already started for {self.account_name}/{self.connector_name}, skipping")
            return

        self._connector = connector

        # Subscribe to order events
        for event, forwarder in self._event_pairs:
            connector.add_listener(event, forwarder)

        logger.info(
            f"OrdersRecorder started for {self.account_name}/{self.connector_name} "
            f"({len(self._event_pairs)} events, connector={type(connector).__name__})"
        )

    async def stop(self):
        """Stop recording orders"""
        if self._connector:
            # Remove all event listeners
            for event, forwarder in self._event_pairs:
                self._connector.remove_listener(event, forwarder)

        # Wait for in-flight write tasks so no order/trade records are lost
        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)

        logger.info(f"OrdersRecorder stopped for {self.account_name}/{self.connector_name}")

    def _create_tracked_task(self, coro) -> asyncio.Task:
        """Create a task and keep a strong reference to it until it completes.

        The event loop only keeps weak references to tasks, so without this a pending
        task could be garbage-collected before finishing, dropping the DB write.
        """
        task = asyncio.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return task

    def _extract_error_message(self, event) -> str:
        """Extract error message from various possible event attributes."""
        # Try different possible attribute names for error messages
        for attr_name in ['error_message', 'message', 'reason', 'failure_reason', 'error']:
            if hasattr(event, attr_name):
                error_value = getattr(event, attr_name)
                if error_value:
                    return str(error_value)

        # If no error message found, create a descriptive one
        return f"Order failed: {event.__class__.__name__}"

    def _did_create_order(self, event_tag: int, market: ConnectorBase,
                          event: Union[BuyOrderCreatedEvent, SellOrderCreatedEvent]):
        """Handle order creation events - called by SourceInfoEventForwarder"""
        logger.debug(f"OrdersRecorder: _did_create_order called for order {getattr(event, 'order_id', 'unknown')}")
        try:
            # Determine trade type from event
            trade_type = TradeType.BUY if isinstance(event, BuyOrderCreatedEvent) else TradeType.SELL
            logger.debug(f"OrdersRecorder: Creating task to handle order created - {trade_type} order")
            self._create_tracked_task(self._handle_order_created(event, trade_type))
        except Exception as e:
            logger.error(f"Error in _did_create_order: {e}")

    def _did_fill_order(self, event_tag: int, market: ConnectorBase, event: OrderFilledEvent):
        """Handle order fill events - called by SourceInfoEventForwarder"""
        try:
            self._create_tracked_task(self._handle_order_filled(event))
        except Exception as e:
            logger.error(f"Error in _did_fill_order: {e}")

    def _did_cancel_order(self, event_tag: int, market: ConnectorBase, event: Any):
        """Handle order cancel events - called by SourceInfoEventForwarder"""
        try:
            self._create_tracked_task(self._handle_order_cancelled(event))
        except Exception as e:
            logger.error(f"Error in _did_cancel_order: {e}")

    def _did_fail_order(self, event_tag: int, market: ConnectorBase, event: Any):
        """Handle order failure events - called by SourceInfoEventForwarder"""
        try:
            self._create_tracked_task(self._handle_order_failed(event))
        except Exception as e:
            logger.error(f"Error in _did_fail_order: {e}")

    def _did_complete_order(self, event_tag: int, market: ConnectorBase, event: Any):
        """Handle order completion events - called by SourceInfoEventForwarder"""
        try:
            self._create_tracked_task(self._handle_order_completed(event))
        except Exception as e:
            logger.error(f"Error in _did_complete_order: {e}")

    async def _handle_order_created(self, event: Union[BuyOrderCreatedEvent, SellOrderCreatedEvent],
                                    trade_type: TradeType):
        """Handle order creation events"""
        logger.debug(f"OrdersRecorder: _handle_order_created started for order {event.order_id}")
        try:
            async with self.db_manager.get_session_context() as session:
                order_repo = OrderRepository(session)

                # Check if order already exists first
                existing_order = await order_repo.get_order_by_client_id(event.order_id)
                if existing_order:
                    logger.debug(
                        f"OrdersRecorder: Order {event.order_id} already exists with status {existing_order.status}")

                    # Update exchange_order_id if we have it now and it was missing
                    exchange_order_id = getattr(event, 'exchange_order_id', None)
                    if exchange_order_id and not existing_order.exchange_order_id:
                        existing_order.exchange_order_id = exchange_order_id
                        logger.debug(
                            f"OrdersRecorder: Updated exchange_order_id to {exchange_order_id} for order {event.order_id}")

                    # Update status if it's still in PENDING_CREATE or similar early state
                    if existing_order.status in ["PENDING_CREATE", "PENDING", "SUBMITTED"]:
                        existing_order.status = "OPEN"
                        logger.debug(f"OrdersRecorder: Updated status to OPEN for order {event.order_id}")

                    await session.flush()
                    return

                order_data = {
                    "client_order_id": event.order_id,
                    "account_name": self.account_name,
                    "connector_name": self.connector_name,
                    "trading_pair": event.trading_pair,
                    "trade_type": trade_type.name,
                    "order_type": event.type.name if hasattr(event, 'type') else 'UNKNOWN',
                    "amount": float(event.amount),
                    "price": float(event.price) if event.price else None,
                    "status": "OPEN",
                    "exchange_order_id": getattr(event, 'exchange_order_id', None)
                }
                await order_repo.create_order(order_data)

            logger.debug(f"OrdersRecorder: Successfully recorded order created: {event.order_id}")
        except Exception as e:
            logger.error(f"OrdersRecorder: Error recording order created: {e}")

    async def _handle_order_filled(self, event: OrderFilledEvent):
        """Handle order fill events, serialized per order id (CDX-R01)."""
        async with _order_fill_lock(event.order_id):
            await self._handle_order_filled_locked(event)

    async def _handle_order_filled_locked(self, event: OrderFilledEvent):
        """Record one fill: insert-first dedup, then aggregate update.

        Must only run under the per-order lock taken by _handle_order_filled.
        """
        try:
            async with self.db_manager.get_session_context() as session:
                order_repo = OrderRepository(session)
                trade_repo = TradeRepository(session)

                # Calculate fees
                trade_fee_paid = 0
                trade_fee_currency = None

                if event.trade_fee:
                    try:
                        base_asset, quote_asset = event.trading_pair.split("-")
                        fee_in_quote = event.trade_fee.fee_amount_in_token(
                            trading_pair=event.trading_pair,
                            price=event.price,
                            order_amount=event.amount,
                            token=quote_asset,
                        )
                        trade_fee_paid = float(fee_in_quote)
                        trade_fee_currency = quote_asset
                    except Exception as e:
                        logger.warning(f"Primary fee calculation failed: {e}. Attempting fallback...")
                        try:
                            base_asset, quote_asset = event.trading_pair.split("-")
                            fallback_fee = await self._calculate_fee_fallback(
                                trade_fee=event.trade_fee,
                                base_asset=base_asset,
                                quote_asset=quote_asset,
                                fill_price=event.price,
                                order_amount=event.amount,
                            )
                            if fallback_fee is not None:
                                trade_fee_paid = float(fallback_fee)
                                trade_fee_currency = quote_asset
                                logger.info(
                                    f"Fallback fee calculation succeeded: {trade_fee_paid} {trade_fee_currency}")
                            else:
                                logger.error(f"Fallback fee calculation returned None for {event.order_id}")
                                trade_fee_paid = 0
                                trade_fee_currency = None
                        except Exception as fallback_err:
                            logger.error(f"Fallback fee calculation also failed: {fallback_err}")
                            trade_fee_paid = 0
                            trade_fee_currency = None
                # Validate numeric inputs BEFORE touching the DB (handle
                # potential NaN values like Hummingbot does)
                try:
                    filled_amount = Decimal(str(event.amount))
                    fill_price = Decimal(str(event.price))
                    fee_paid_decimal = Decimal(str(trade_fee_paid)) if trade_fee_paid else None
                except (ValueError, InvalidOperation) as e:
                    logger.error(f"Error processing order fill for {event.order_id}: {e}, skipping update")
                    return

                order = await order_repo.get_order_by_client_id(event.order_id)
                if order is None:
                    logger.warning(f"Fill event for unknown order {event.order_id}; nothing recorded")
                    return

                # CDX-006: insert the immutable trade FIRST — the DB unique
                # constraints are the dedup authority. Aggregates are mutated
                # ONLY when the insert actually inserted, in the same
                # session/transaction.
                validated_timestamp = event.timestamp if event.timestamp and not math.isnan(
                    event.timestamp) else time.time()
                validated_fee = trade_fee_paid if trade_fee_paid and not math.isnan(trade_fee_paid) else 0

                # Empty/whitespace exchange_trade_id maps to None so the scoped
                # unique constraint never collides unrelated id-less fills
                # (NULLs are distinct); those dedup via the trade_id fallback.
                raw_exchange_trade_id = getattr(event, 'exchange_trade_id', None)
                exchange_trade_id = str(raw_exchange_trade_id).strip() if raw_exchange_trade_id else None
                exchange_trade_id = exchange_trade_id or None

                if exchange_trade_id:
                    trade_id = f"{event.order_id}_{exchange_trade_id}"
                else:
                    # Fallback: include amount to differentiate partial fills at same timestamp
                    trade_id = f"{event.order_id}_{validated_timestamp}_{float(filled_amount)}"

                trade_data = {
                    "order_id": order.id,
                    "trade_id": trade_id,
                    "account_name": self.account_name,
                    "connector_name": self.connector_name,
                    "exchange_trade_id": exchange_trade_id,
                    "timestamp": datetime.fromtimestamp(validated_timestamp),
                    "trading_pair": event.trading_pair,
                    "trade_type": event.trade_type.name,
                    "amount": float(filled_amount),  # Use validated amount
                    "price": float(fill_price),  # Use validated price
                    "fee_paid": validated_fee,
                    "fee_currency": trade_fee_currency
                }
                inserted = await trade_repo.insert_trade_if_new(trade_data)
                if inserted is None:
                    logger.info(
                        f"Duplicate fill delivery for order {event.order_id} "
                        f"(trade_id={trade_id}, exchange_trade_id={exchange_trade_id}): "
                        f"trade already recorded, aggregates left untouched")
                    return

                # A failure past this point propagates out of the session
                # context, rolling back the trade insert together with any
                # partial aggregate mutation — never one without the other.
                await order_repo.update_order_fill(
                    client_order_id=event.order_id,
                    filled_amount=filled_amount,
                    fill_price=fill_price,
                    fee_paid=fee_paid_decimal,
                    fee_currency=trade_fee_currency
                )

            logger.debug(f"Recorded order fill: {event.order_id} - {event.amount} @ {event.price}")
        except Exception as e:
            logger.error(f"Error recording order fill: {e}")

    async def _handle_order_cancelled(self, event: Any):
        """Handle order cancellation events"""
        try:
            async with self.db_manager.get_session_context() as session:
                order_repo = OrderRepository(session)
                await order_repo.update_order_status(
                    client_order_id=event.order_id,
                    status="CANCELLED"
                )

            logger.debug(f"Recorded order cancelled: {event.order_id}")
        except Exception as e:
            logger.error(f"Error recording order cancellation: {e}")

    def _get_order_details_from_connector(self, order_id: str) -> Optional[dict]:
        """Try to get order details from connector's tracked orders"""
        try:
            if self._connector and hasattr(self._connector, 'in_flight_orders'):
                in_flight_order = self._connector.in_flight_orders.get(order_id)
                if in_flight_order:
                    return {
                        "trading_pair": in_flight_order.trading_pair,
                        "trade_type": in_flight_order.trade_type.name,
                        "order_type": in_flight_order.order_type.name,
                        "amount": float(in_flight_order.amount),
                        "price": float(in_flight_order.price) if in_flight_order.price else None
                    }
        except Exception as e:
            logger.error(f"Error getting order details from connector: {e}")
        return None

    async def _fetch_conversion_rate(self, from_token: str, to_token: str) -> Optional[Decimal]:
        """Fetch the conversion rate between two tokens using the connector's REST API.
        Tries direct pair first, then inverse pair."""
        if not self._connector:
            return None
        try:
            direct_pair = f"{from_token}-{to_token}"
            price = await asyncio.wait_for(
                self._connector._get_last_traded_price(trading_pair=direct_pair),
                timeout=5.0,
            )
            if price and price > 0:
                return Decimal(str(price))
        except Exception:
            pass
        try:
            inverse_pair = f"{to_token}-{from_token}"
            price = await asyncio.wait_for(
                self._connector._get_last_traded_price(trading_pair=inverse_pair),
                timeout=5.0,
            )
            if price and price > 0:
                return Decimal(1) / Decimal(str(price))
        except Exception:
            pass
        return None

    async def _calculate_fee_fallback(
            self,
            trade_fee,
            base_asset: str,
            quote_asset: str,
            fill_price: Decimal,
            order_amount: Decimal,
    ) -> Optional[Decimal]:
        """Manually compute the trade fee in quote asset when the primary method fails."""
        fee_amount = Decimal(0)

        # Handle percent component
        if trade_fee.percent and trade_fee.percent != Decimal(0):
            fee_amount += (fill_price * order_amount) * trade_fee.percent

        # Handle flat_fees component
        for flat_fee in trade_fee.flat_fees:
            if flat_fee.token == quote_asset:
                fee_amount += flat_fee.amount
            elif flat_fee.token == base_asset:
                fee_amount += flat_fee.amount * fill_price
            else:
                rate = await self._fetch_conversion_rate(flat_fee.token, quote_asset)
                if rate is not None:
                    fee_amount += flat_fee.amount * rate
                else:
                    logger.error(
                        f"Could not fetch conversion rate for {flat_fee.token} -> {quote_asset}"
                    )
                    return None

        return fee_amount

    async def _handle_order_failed(self, event: Any):
        """Handle order failure events"""
        try:
            async with self.db_manager.get_session_context() as session:
                order_repo = OrderRepository(session)

                # Check if order exists, if not try to get details from connector's tracked orders
                existing_order = await order_repo.get_order_by_client_id(event.order_id)
                if existing_order:
                    # Extract error message from various possible attributes
                    error_msg = self._extract_error_message(event)

                    # Update existing order with failure status and error message
                    await order_repo.update_order_status(
                        client_order_id=event.order_id,
                        status="FAILED",
                        error_message=error_msg
                    )
                    logger.info(f"Updated existing order {event.order_id} to FAILED status")
                else:
                    # Try to get order details from connector's tracked orders
                    order_details = self._get_order_details_from_connector(event.order_id)
                    if order_details:
                        logger.info(f"Retrieved order details from connector for {event.order_id}: {order_details}")

                    # Create order record as FAILED with available details
                    if order_details:
                        order_data = {
                            "client_order_id": event.order_id,
                            "account_name": self.account_name,
                            "connector_name": self.connector_name,
                            "trading_pair": order_details["trading_pair"],
                            "trade_type": order_details["trade_type"],
                            "order_type": order_details["order_type"],
                            "amount": order_details["amount"],
                            "price": order_details["price"],
                            "status": "FAILED",
                            "error_message": self._extract_error_message(event)
                        }
                    else:
                        # Fallback with minimal details
                        order_data = {
                            "client_order_id": event.order_id,
                            "account_name": self.account_name,
                            "connector_name": self.connector_name,
                            "trading_pair": "UNKNOWN",
                            "trade_type": "UNKNOWN",
                            "order_type": "UNKNOWN",
                            "amount": 0.0,
                            "price": None,
                            "status": "FAILED",
                            "error_message": self._extract_error_message(event)
                        }

                    try:
                        await order_repo.create_order(order_data)
                        logger.info(f"Created failed order record for {event.order_id}")
                    except Exception as create_error:
                        # If creation fails due to duplicate key, try to update existing order
                        if "duplicate key" in str(create_error).lower() or "unique constraint" in str(
                                create_error).lower():
                            logger.info(f"Order {event.order_id} already exists, updating status to FAILED")
                            await order_repo.update_order_status(
                                client_order_id=event.order_id,
                                status="FAILED",
                                error_message=self._extract_error_message(event)
                            )
                        else:
                            raise create_error

        except Exception as e:
            logger.error(f"Error recording order failure: {e}")

    async def _handle_order_completed(self, event: Any):
        """Handle order completion events"""
        try:
            async with self.db_manager.get_session_context() as session:
                order_repo = OrderRepository(session)
                order = await order_repo.get_order_by_client_id(event.order_id)
                if order:
                    order.status = "FILLED"
                    eoid = getattr(event, 'exchange_order_id', None)
                    if eoid:
                        order.exchange_order_id = eoid

            logger.debug(f"Recorded order completed: {event.order_id}")
        except Exception as e:
            logger.error(f"Error recording order completion: {e}")
