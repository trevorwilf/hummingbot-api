from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order


class OrderRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_order(self, order_data: Dict) -> Order:
        """Create a new order record."""
        order = Order(**order_data)
        self.session.add(order)
        await self.session.flush()  # Get the ID
        return order

    async def get_order_by_client_id(self, client_order_id: str) -> Optional[Order]:
        """Get an order by its client order ID."""
        result = await self.session.execute(
            select(Order).where(Order.client_order_id == client_order_id)
        )
        return result.scalar_one_or_none()

    async def update_order_status(self, client_order_id: str, status: str,
                                  error_message: Optional[str] = None) -> Optional[Order]:
        """Update order status and optional error message."""
        order = await self.get_order_by_client_id(client_order_id)
        if order:
            order.status = status
            if error_message:
                order.error_message = error_message
            await self.session.flush()
        return order

    async def update_order_fill(self, client_order_id: str, filled_amount: Decimal,
                              fill_price: Decimal, fee_paid: Decimal = None,
                              fee_currency: str = None, exchange_order_id: str = None) -> Optional[Order]:
        """Apply ONE fill to the order's aggregates (CDX-006).

        ``fill_price`` is this fill's execution price; ``average_fill_price``
        becomes the incremental VWAP over all fills applied so far. Callers
        must apply each fill exactly once — dedup is the trade insert's job
        (TradeRepository.insert_trade_if_new), in the same transaction.

        The row is read with FOR UPDATE: two DISTINCT concurrent fills would
        otherwise both read the same pre-fill aggregates and the last writer
        would erase the other fill (lost update on money). Postgres renders
        the lock; sqlite renders nothing — there the in-process per-order
        lock in OrdersRecorder plus sqlite's single-writer model cover it.
        """
        result = await self.session.execute(
            select(Order).where(Order.client_order_id == client_order_id).with_for_update()
        )
        order = result.scalar_one_or_none()
        if order:
            prev_filled = Decimal(str(order.filled_amount or 0))
            prev_avg = (
                Decimal(str(order.average_fill_price))
                if order.average_fill_price is not None else Decimal(0)
            )
            new_filled = prev_filled + filled_amount
            order.filled_amount = float(new_filled)

            # Incremental VWAP in Decimal: weight the previous average by the
            # previously filled amount and this fill by its own amount.
            if new_filled > 0:
                new_avg = (prev_avg * prev_filled + fill_price * filled_amount) / new_filled
                order.average_fill_price = float(new_avg)

            # Add to existing fees
            if fee_paid is not None:
                previous_fee = Decimal(str(order.fee_paid or 0))
                order.fee_paid = float(previous_fee + fee_paid)
            if fee_currency:
                order.fee_currency = fee_currency
            if exchange_order_id:
                order.exchange_order_id = exchange_order_id

            # Status derives from post-fill aggregates
            if new_filled >= Decimal(str(order.amount)):
                order.status = "FILLED"
            elif new_filled > 0:
                order.status = "PARTIALLY_FILLED"

            await self.session.flush()
        return order

    async def get_orders(self, account_name: Optional[str] = None, 
                        connector_name: Optional[str] = None,
                        trading_pair: Optional[str] = None, 
                        status: Optional[str] = None,
                        start_time: Optional[int] = None, 
                        end_time: Optional[int] = None,
                        limit: int = 100, offset: int = 0) -> List[Order]:
        """Get orders with filtering and pagination."""
        query = select(Order)
        
        # Apply filters
        if account_name:
            query = query.where(Order.account_name == account_name)
        if connector_name:
            query = query.where(Order.connector_name == connector_name)
        if trading_pair:
            query = query.where(Order.trading_pair == trading_pair)
        if status:
            query = query.where(Order.status == status)
        if start_time:
            start_dt = datetime.fromtimestamp(start_time / 1000)
            query = query.where(Order.created_at >= start_dt)
        if end_time:
            end_dt = datetime.fromtimestamp(end_time / 1000)
            query = query.where(Order.created_at <= end_dt)
        
        # Apply ordering and pagination
        query = query.order_by(Order.created_at.desc())
        query = query.limit(limit).offset(offset)
        
        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_active_orders(self, account_name: Optional[str] = None,
                              connector_name: Optional[str] = None,
                              trading_pair: Optional[str] = None) -> List[Order]:
        """Get active orders (SUBMITTED, OPEN, PARTIALLY_FILLED, PENDING_CANCEL)."""
        query = select(Order).where(
            Order.status.in_(["SUBMITTED", "OPEN", "PARTIALLY_FILLED", "PENDING_CANCEL"])
        )
        
        # Apply filters
        if account_name:
            query = query.where(Order.account_name == account_name)
        if connector_name:
            query = query.where(Order.connector_name == connector_name)
        if trading_pair:
            query = query.where(Order.trading_pair == trading_pair)
        
        query = query.order_by(Order.created_at.desc()).limit(1000)
        
        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_orders_summary(self, account_name: Optional[str] = None,
                               start_time: Optional[int] = None,
                               end_time: Optional[int] = None) -> Dict:
        """Get order summary statistics using a single DB-level aggregate query."""
        query = select(Order.status, func.count()).group_by(Order.status)

        # Apply the same filters as get_orders
        if account_name:
            query = query.where(Order.account_name == account_name)
        if start_time:
            start_dt = datetime.fromtimestamp(start_time / 1000)
            query = query.where(Order.created_at >= start_dt)
        if end_time:
            end_dt = datetime.fromtimestamp(end_time / 1000)
            query = query.where(Order.created_at <= end_dt)

        result = await self.session.execute(query)
        counts = {status: count for status, count in result.all()}

        total_orders = sum(counts.values())
        filled_orders = counts.get("FILLED", 0)
        cancelled_orders = counts.get("CANCELLED", 0)
        failed_orders = counts.get("FAILED", 0)
        active_orders = (
            counts.get("SUBMITTED", 0) + counts.get("OPEN", 0) + counts.get("PARTIALLY_FILLED", 0)
        )

        return {
            "total_orders": total_orders,
            "filled_orders": filled_orders,
            "cancelled_orders": cancelled_orders,
            "failed_orders": failed_orders,
            "active_orders": active_orders,
            "fill_rate": filled_orders / total_orders if total_orders > 0 else 0,
        }

    def to_dict(self, order: Order) -> Dict:
        """Convert Order model to dictionary format."""
        return {
            "order_id": order.client_order_id,
            "account_name": order.account_name,
            "connector_name": order.connector_name,
            "trading_pair": order.trading_pair,
            "trade_type": order.trade_type,
            "order_type": order.order_type,
            "amount": float(order.amount),
            "price": float(order.price) if order.price else None,
            "status": order.status,
            "filled_amount": float(order.filled_amount),
            "average_fill_price": float(order.average_fill_price) if order.average_fill_price else None,
            "fee_paid": float(order.fee_paid) if order.fee_paid else None,
            "fee_currency": order.fee_currency,
            "created_at": order.created_at.isoformat(),
            "updated_at": order.updated_at.isoformat(),
            "exchange_order_id": order.exchange_order_id,
            "error_message": order.error_message,
        }