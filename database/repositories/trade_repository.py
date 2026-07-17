from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Order, Trade


class TradeRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def insert_trade_if_new(self, trade_data: Dict) -> Optional[Trade]:
        """Insert the immutable trade row FIRST, dedup enforced by the DB (CDX-006).

        Uses dialect-native ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` so a
        duplicate delivery — same global ``trade_id`` or same scoped
        ``(account_name, connector_name, exchange_trade_id)`` — inserts nothing
        and returns None, WITHOUT poisoning or rolling back the enclosing
        transaction. Callers must mutate order aggregates ONLY when this
        returns a row, in the same session/transaction, so a later failure
        rolls back the trade row and the aggregates together.
        """
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        else:
            dialect_insert = None

        if dialect_insert is not None:
            stmt = (
                dialect_insert(Trade)
                .values(**trade_data)
                .on_conflict_do_nothing()
                .returning(Trade.id)
            )
            result = await self.session.execute(stmt)
            inserted_id = result.scalar_one_or_none()
            if inserted_id is None:
                return None  # duplicate — nothing inserted
            fetched = await self.session.execute(
                select(Trade).where(Trade.id == inserted_id)
            )
            return fetched.scalar_one()

        # Other dialects: IntegrityError-safe insert under a SAVEPOINT so a
        # duplicate rolls back only this insert, never the enclosing
        # transaction (which may already carry unrelated work).
        trade = Trade(**trade_data)
        try:
            async with self.session.begin_nested():
                self.session.add(trade)
                await self.session.flush()
            return trade
        except IntegrityError:
            return None

    async def get_trade_by_id(self, trade_id: str) -> Optional[Trade]:
        """Get a trade by its trade_id."""
        query = select(Trade).where(Trade.trade_id == trade_id)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_trades_with_orders(self, account_name: Optional[str] = None,
                                   connector_name: Optional[str] = None,
                                   trading_pair: Optional[str] = None,
                                   trade_type: Optional[str] = None,
                                   start_time: Optional[int] = None,
                                   end_time: Optional[int] = None,
                                   limit: int = 100, offset: int = 0) -> List[tuple]:
        """Get trades with their associated order information."""
        # Join trades with orders to get complete information
        query = select(Trade, Order).join(Order, Trade.order_id == Order.id)
        
        # Apply filters
        if account_name:
            query = query.where(Order.account_name == account_name)
        if connector_name:
            query = query.where(Order.connector_name == connector_name)
        if trading_pair:
            query = query.where(Trade.trading_pair == trading_pair)
        if trade_type:
            query = query.where(Trade.trade_type == trade_type)
        if start_time:
            start_dt = datetime.fromtimestamp(start_time / 1000)
            query = query.where(Trade.timestamp >= start_dt)
        if end_time:
            end_dt = datetime.fromtimestamp(end_time / 1000)
            query = query.where(Trade.timestamp <= end_dt)
        
        # Apply ordering and pagination
        query = query.order_by(Trade.timestamp.desc())
        query = query.limit(limit).offset(offset)
        
        result = await self.session.execute(query)
        return result.all()  # Returns tuples of (Trade, Order)

    def to_dict(self, trade: Trade, order: Optional[Order] = None) -> Dict:
        """Convert Trade model to dictionary format."""
        return {
            "trade_id": trade.trade_id,
            "order_id": order.client_order_id if order else None,
            "account_name": trade.account_name or (order.account_name if order else None),
            "connector_name": trade.connector_name or (order.connector_name if order else None),
            "trading_pair": trade.trading_pair,
            "trade_type": trade.trade_type,
            "amount": float(trade.amount),
            "price": float(trade.price),
            "fee_paid": float(trade.fee_paid),
            "fee_currency": trade.fee_currency,
            "timestamp": trade.timestamp.isoformat(),
        }