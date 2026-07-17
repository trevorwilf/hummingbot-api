"""Phase 8 (CDX-006): fills accounting — insert-first dedup + incremental VWAP.

Covers:
  * Duplicate delivery of the same scoped fill (account, connector,
    exchange_trade_id) inserts exactly one trade row and counts order
    aggregates exactly once (mutation: drop the conflict guard / revert to
    aggregate-mutation-before-dedup and these fail with doubled aggregates).
  * average_fill_price is the incremental VWAP over all fills —
    1@100 then 1@200 -> 150 (spec formula; the OLD latest-price code yields
    200, so this fails on revert by construction), uneven weights
    1@100 then 3@200 -> 175.
  * The dedup constraint is SCOPED: the same exchange_trade_id under a
    different account records a second trade (and its aggregates); the same
    scoped triple with a DIFFERENT synthetic trade_id is still deduped.
  * Fills WITHOUT an exchange-assigned id map to NULL exchange_trade_id and
    never falsely collide; exact re-delivery of an id-less fill still dedups
    via the trade_id fallback.
  * Transactionality: a forced failure between the trade insert and the
    aggregate update rolls back BOTH (no trade-without-aggregate, no
    aggregate-without-trade), and a later re-delivery of that fill succeeds.
  * The startup migration (database.connection.STARTUP_MIGRATIONS +
    STARTUP_UNIQUE_INDEXES via the real _run_migrations routine) upgrades a
    PRE-migration trades table: columns added, scoped unique index enforced —
    exercised BOTH through the helpers directly and through the real startup
    entrypoint (AsyncDatabaseManager.create_tables), so unwiring the helper
    from startup fails a test (CDX-R03).
  * The scoped constraint's CONNECTOR axis: the same (account,
    exchange_trade_id) under two different connectors is two legitimate
    fills — both recorded, on the app path and on the migrated-legacy-table
    path (CDX-R02).
  * Concurrency (CDX-R01): two DISTINCT fills for one order handled as
    concurrent tasks are serialized by the per-order lock — the second
    cannot enter the critical section while the first is mid-transaction,
    and both fills land in the aggregates (no lost update).

Test authenticity: everything runs the REAL OrdersRecorder fill handler and
REAL repositories against a REAL sqlite database with real OrderFilledEvent
objects from the hummingbot package. The environment has no async sqlite
driver, so a thin adapter awaits the same calls against a synchronous Session
(the pattern established in test_retirement_fsm.py) — every SQL statement,
constraint and rollback executes for real. The only patched thing is the
fault injection for the transactionality test. Expected values are derived
from the phase spec (the VWAP formula), never from running the implementation.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee
from hummingbot.core.event.events import OrderFilledEvent

from database.connection import (
    STARTUP_MIGRATIONS,
    STARTUP_UNIQUE_INDEXES,
    AsyncDatabaseManager,
)
from database.models import Base, Order, Trade
from database.repositories.order_repository import OrderRepository
from database.repositories.trade_repository import TradeRepository
from services.orders_recorder import OrdersRecorder


# ---------------------------------------------------------------------------
# Real-DB plumbing (sync sqlite driver adapted to the async session surface)
# ---------------------------------------------------------------------------

class SyncSessionAdapter:
    """Awaitable facade over a synchronous Session — real SQL, real sqlite."""

    def __init__(self, session):
        self._session = session

    def add(self, obj):
        self._session.add(obj)

    def get_bind(self, *args, **kwargs):
        return self._session.get_bind()

    async def execute(self, *args, **kwargs):
        return self._session.execute(*args, **kwargs)

    async def flush(self):
        self._session.flush()

    async def refresh(self, obj):
        self._session.refresh(obj)


class RealSqliteDBManager:
    """get_session_context() against one shared in-memory sqlite DB."""

    def __init__(self):
        self.engine = create_engine(
            "sqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self._sessionmaker = sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def get_session_context(self):
        session = self._sessionmaker()
        try:
            yield SyncSessionAdapter(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


@pytest.fixture
def db():
    return RealSqliteDBManager()


ACCOUNT = "acct-1"
CONNECTOR = "nonkyc"
PAIR = "XMR-USDT"
TS = 1_700_000_000.0


def make_recorder(db, account=ACCOUNT, connector=CONNECTOR):
    return OrdersRecorder(db_manager=db, account_name=account, connector_name=connector)


def fill_event(order_id, price, amount, exchange_trade_id="", ts=TS):
    """A REAL hummingbot OrderFilledEvent, as the connector would emit it."""
    return OrderFilledEvent(
        timestamp=ts,
        order_id=order_id,
        trading_pair=PAIR,
        trade_type=TradeType.BUY,
        order_type=OrderType.LIMIT,
        price=Decimal(str(price)),
        amount=Decimal(str(amount)),
        trade_fee=AddedToCostTradeFee(),
        exchange_trade_id=exchange_trade_id,
    )


async def seed_order(db, client_order_id="ord-1", account=ACCOUNT, amount=2, connector=CONNECTOR):
    async with db.get_session_context() as session:
        session.add(Order(
            client_order_id=client_order_id,
            account_name=account,
            connector_name=connector,
            trading_pair=PAIR,
            trade_type="BUY",
            order_type="LIMIT",
            amount=amount,
            status="OPEN",
            filled_amount=0,
        ))
        await session.flush()


async def fetch_order(db, client_order_id="ord-1"):
    async with db.get_session_context() as session:
        res = await session.execute(
            select(Order).where(Order.client_order_id == client_order_id)
        )
        return res.scalar_one()


async def fetch_trades(db):
    async with db.get_session_context() as session:
        res = await session.execute(select(Trade).order_by(Trade.id))
        return list(res.scalars().all())


# ===========================================================================
# Duplicate delivery — aggregates counted exactly once
# ===========================================================================

class TestDuplicateDelivery:

    @pytest.mark.asyncio
    async def test_same_scoped_fill_replayed_counts_once(self, db):
        """Spec (a): replaying the same scoped exchange_trade_id leaves exactly
        one trade row and single-counted aggregates. Fails if the conflict
        guard is dropped or aggregates are mutated before the dedup."""
        await seed_order(db, amount=2)
        recorder = make_recorder(db)
        event = fill_event("ord-1", price=100, amount=1, exchange_trade_id="T1")

        await recorder._handle_order_filled(event)
        await recorder._handle_order_filled(event)

        trades = await fetch_trades(db)
        assert len(trades) == 1
        order = await fetch_order(db)
        assert float(order.filled_amount) == 1.0  # not 2.0
        assert float(order.average_fill_price) == 100.0
        assert order.status == "PARTIALLY_FILLED"  # never flipped to FILLED

    @pytest.mark.asyncio
    async def test_scoped_dedup_fires_even_with_distinct_trade_id(self, db):
        """The (account, connector, exchange_trade_id) constraint itself — a
        second insert with a DIFFERENT synthetic trade_id but the same scoped
        triple must insert nothing and return None."""
        await seed_order(db)
        order = await fetch_order(db)
        base = {
            "order_id": order.id,
            "account_name": ACCOUNT,
            "connector_name": CONNECTOR,
            "exchange_trade_id": "T9",
            "timestamp": datetime.fromtimestamp(TS),
            "trading_pair": PAIR,
            "trade_type": "BUY",
            "amount": 1.0,
            "price": 100.0,
            "fee_paid": 0,
            "fee_currency": None,
        }
        async with db.get_session_context() as session:
            repo = TradeRepository(session)
            first = await repo.insert_trade_if_new({**base, "trade_id": "A"})
            second = await repo.insert_trade_if_new({**base, "trade_id": "B"})
        assert first is not None
        assert second is None
        assert len(await fetch_trades(db)) == 1

    @pytest.mark.asyncio
    async def test_exact_duplicate_idless_fill_deduped(self, db):
        """A fill the exchange gave no id for, delivered twice byte-identically,
        still dedups (trade_id fallback: order_id + timestamp + amount)."""
        await seed_order(db, amount=2)
        recorder = make_recorder(db)
        event = fill_event("ord-1", price=100, amount=1, exchange_trade_id="")

        await recorder._handle_order_filled(event)
        await recorder._handle_order_filled(event)

        assert len(await fetch_trades(db)) == 1
        order = await fetch_order(db)
        assert float(order.filled_amount) == 1.0


# ===========================================================================
# VWAP — spec formula, never latest-price
# ===========================================================================

class TestVWAP:

    @pytest.mark.asyncio
    async def test_vwap_two_equal_fills(self, db):
        """Spec (b): 1@100 then 1@200 -> average_fill_price == 150.
        The OLD latest-price code yields 200 — this fails on revert."""
        await seed_order(db, amount=2)
        recorder = make_recorder(db)

        await recorder._handle_order_filled(
            fill_event("ord-1", price=100, amount=1, exchange_trade_id="T1"))
        await recorder._handle_order_filled(
            fill_event("ord-1", price=200, amount=1, exchange_trade_id="T2"))

        order = await fetch_order(db)
        assert float(order.average_fill_price) == 150.0
        assert float(order.filled_amount) == 2.0
        assert order.status == "FILLED"
        assert len(await fetch_trades(db)) == 2

    @pytest.mark.asyncio
    async def test_vwap_uneven_weights(self, db):
        """Spec formula with uneven weights: (100*1 + 200*3) / 4 == 175."""
        await seed_order(db, amount=4)
        async with db.get_session_context() as session:
            repo = OrderRepository(session)
            await repo.update_order_fill("ord-1", Decimal("1"), Decimal("100"))
            await repo.update_order_fill("ord-1", Decimal("3"), Decimal("200"))
        order = await fetch_order(db)
        assert float(order.average_fill_price) == 175.0
        assert order.status == "FILLED"

    @pytest.mark.asyncio
    async def test_first_fill_vwap_is_fill_price(self, db):
        """prev_filled == 0: the formula degenerates to the fill price."""
        await seed_order(db, amount=2)
        async with db.get_session_context() as session:
            await OrderRepository(session).update_order_fill(
                "ord-1", Decimal("1"), Decimal("123.5"))
        order = await fetch_order(db)
        assert float(order.average_fill_price) == 123.5
        assert order.status == "PARTIALLY_FILLED"


# ===========================================================================
# Scoping — same exchange id under another account is a different fill
# ===========================================================================

class TestScoping:

    @pytest.mark.asyncio
    async def test_same_exchange_trade_id_different_account_both_recorded(self, db):
        """Spec (c): the constraint is (account, connector, exchange_trade_id),
        not the bare exchange id — both accounts' fills are recorded and both
        orders' aggregates updated."""
        await seed_order(db, client_order_id="ord-1", account="acct-1", amount=1)
        await seed_order(db, client_order_id="ord-2", account="acct-2", amount=1)
        rec1 = make_recorder(db, account="acct-1")
        rec2 = make_recorder(db, account="acct-2")

        await rec1._handle_order_filled(
            fill_event("ord-1", price=100, amount=1, exchange_trade_id="T1"))
        await rec2._handle_order_filled(
            fill_event("ord-2", price=100, amount=1, exchange_trade_id="T1"))

        trades = await fetch_trades(db)
        assert len(trades) == 2
        assert {t.account_name for t in trades} == {"acct-1", "acct-2"}
        for cid in ("ord-1", "ord-2"):
            order = await fetch_order(db, cid)
            assert float(order.filled_amount) == 1.0
            assert order.status == "FILLED"

    @pytest.mark.asyncio
    async def test_same_exchange_trade_id_different_connector_both_recorded(self, db):
        """CDX-R02: the connector axis of the scope. The same account seeing
        the same exchange_trade_id on two DIFFERENT connectors is two
        legitimate fills — a constraint reduced to (account,
        exchange_trade_id) would wrongly discard the second one."""
        await seed_order(db, client_order_id="ord-1", amount=1, connector="nonkyc")
        await seed_order(db, client_order_id="ord-2", amount=1, connector="other-dex")
        rec1 = make_recorder(db, connector="nonkyc")
        rec2 = make_recorder(db, connector="other-dex")

        await rec1._handle_order_filled(
            fill_event("ord-1", price=100, amount=1, exchange_trade_id="T1"))
        await rec2._handle_order_filled(
            fill_event("ord-2", price=100, amount=1, exchange_trade_id="T1"))

        trades = await fetch_trades(db)
        assert len(trades) == 2
        assert {t.connector_name for t in trades} == {"nonkyc", "other-dex"}
        for cid in ("ord-1", "ord-2"):
            order = await fetch_order(db, cid)
            assert float(order.filled_amount) == 1.0
            assert order.status == "FILLED"

    @pytest.mark.asyncio
    async def test_idless_fills_never_falsely_collide(self, db):
        """Two DISTINCT fills without an exchange id (NULL scoped column) must
        both count — a non-null empty-string column would wrongly dedup the
        second one."""
        await seed_order(db, amount=2)
        recorder = make_recorder(db)

        await recorder._handle_order_filled(
            fill_event("ord-1", price=100, amount=1, exchange_trade_id="", ts=TS))
        await recorder._handle_order_filled(
            fill_event("ord-1", price=200, amount=1, exchange_trade_id="", ts=TS + 60))

        assert len(await fetch_trades(db)) == 2
        order = await fetch_order(db)
        assert float(order.filled_amount) == 2.0
        assert float(order.average_fill_price) == 150.0


# ===========================================================================
# Transactionality — trade insert and aggregate update live or die together
# ===========================================================================

class TestTransactionality:

    @pytest.mark.asyncio
    async def test_failure_between_insert_and_aggregate_leaves_no_partial_state(self, db):
        """Spec (d): fault injected between the trade insert and the aggregate
        update — the shared transaction rolls back BOTH, and the fill is still
        recordable on re-delivery (no phantom dedup row survives)."""
        await seed_order(db, amount=2)
        recorder = make_recorder(db)
        event = fill_event("ord-1", price=100, amount=1, exchange_trade_id="T1")

        with patch.object(OrderRepository, "update_order_fill",
                          new=AsyncMock(side_effect=RuntimeError("injected crash"))):
            await recorder._handle_order_filled(event)

        assert len(await fetch_trades(db)) == 0  # insert rolled back with the failure
        order = await fetch_order(db)
        assert float(order.filled_amount) == 0.0
        assert order.average_fill_price is None
        assert order.status == "OPEN"

        # Re-delivery after the fault heals: recorded exactly once.
        await recorder._handle_order_filled(event)
        assert len(await fetch_trades(db)) == 1
        order = await fetch_order(db)
        assert float(order.filled_amount) == 1.0
        assert float(order.average_fill_price) == 100.0

    @pytest.mark.asyncio
    async def test_unknown_order_records_nothing(self, db):
        """A fill for an order we never saw mutates nothing (fail-closed)."""
        recorder = make_recorder(db)
        await recorder._handle_order_filled(
            fill_event("ghost-order", price=100, amount=1, exchange_trade_id="T1"))
        assert len(await fetch_trades(db)) == 0


# ===========================================================================
# Concurrency — distinct fills for one order must serialize (CDX-R01)
# ===========================================================================

class TestConcurrentDistinctFills:

    @pytest.mark.asyncio
    async def test_concurrent_distinct_fills_serialize_and_both_count(self, db):
        """Two DIFFERENT fills for one order arrive as concurrent tasks (the
        recorder spawns one task per event). Without per-order serialization
        both read the same pre-fill aggregates and the last writer erases the
        other fill (lost update). This pins the mutual-exclusion property
        deterministically: the first fill is parked INSIDE its critical
        section (trade inserted, aggregates not yet written) while the second
        is launched — the second must not reach the aggregate update until
        the first completes, and both fills must land in the final aggregates.
        Removing the per-order lock in _handle_order_filled fails the
        call-count assertion (the second fill runs to completion during the
        pause). The real update logic still executes — the wrapper only
        injects scheduling."""
        await seed_order(db, amount=2)
        recorder = make_recorder(db)

        real_update = OrderRepository.update_order_fill
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        calls = []

        async def paused_update(repo_self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                first_entered.set()
                await release_first.wait()
            return await real_update(repo_self, *args, **kwargs)

        with patch.object(OrderRepository, "update_order_fill", new=paused_update):
            t1 = asyncio.ensure_future(recorder._handle_order_filled(
                fill_event("ord-1", price=100, amount=1, exchange_trade_id="T1")))
            await asyncio.wait_for(first_entered.wait(), timeout=5)

            # First fill is mid-critical-section. A second DISTINCT fill
            # arrives now and gets ample opportunity to run.
            t2 = asyncio.ensure_future(recorder._handle_order_filled(
                fill_event("ord-1", price=200, amount=1, exchange_trade_id="T2")))
            for _ in range(50):
                await asyncio.sleep(0)

            # Serialization property: the second fill must NOT have entered
            # the critical section while the first holds the per-order lock.
            assert len(calls) == 1

            release_first.set()
            await asyncio.gather(t1, t2)

        # Both fills counted exactly once — no lost update.
        order = await fetch_order(db)
        assert float(order.filled_amount) == 2.0
        assert float(order.average_fill_price) == 150.0  # VWAP, not last price
        assert order.status == "FILLED"
        assert len(await fetch_trades(db)) == 2


# ===========================================================================
# Startup migration — pre-existing trades tables gain the dedup constraint
# ===========================================================================

class TestStartupMigration:

    @pytest.mark.asyncio
    async def test_legacy_trades_table_gains_scoped_unique_index(self):
        """Run the REAL production migration routine
        (AsyncDatabaseManager._run_migrations, executing STARTUP_MIGRATIONS +
        STARTUP_UNIQUE_INDEXES) against a PRE-migration trades table: the new
        columns appear, the scoped unique index actually rejects a duplicate
        triple, and NULL-id rows (legacy + id-less fills) never collide."""

        class SyncConnAdapter:
            def __init__(self, conn):
                self._conn = conn

            async def execute(self, stmt, params=None):
                if params is not None:
                    return self._conn.execute(stmt, params)
                return self._conn.execute(stmt)

        engine = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        with engine.begin() as conn:
            # The trades schema as it existed BEFORE this phase.
            conn.execute(text(
                "CREATE TABLE trades ("
                "id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, "
                "trade_id TEXT NOT NULL UNIQUE, timestamp TIMESTAMP NOT NULL, "
                "trading_pair TEXT NOT NULL, trade_type TEXT NOT NULL, "
                "amount NUMERIC NOT NULL, price NUMERIC NOT NULL, "
                "fee_paid NUMERIC NOT NULL DEFAULT 0, fee_currency TEXT)"
            ))
            conn.execute(text(
                "INSERT INTO trades (order_id, trade_id, timestamp, trading_pair,"
                " trade_type, amount, price) "
                "VALUES (1, 'legacy-1', '2026-01-01 00:00:00', 'XMR-USDT', 'BUY', 1, 100)"
            ))

            mgr = AsyncDatabaseManager.__new__(AsyncDatabaseManager)  # migrations only
            await AsyncDatabaseManager._run_migrations(mgr, SyncConnAdapter(conn))
            await AsyncDatabaseManager._create_unique_indexes(mgr, SyncConnAdapter(conn))

            # Legacy row survived with NULL scoped identity.
            row = conn.execute(text(
                "SELECT account_name, connector_name, exchange_trade_id "
                "FROM trades WHERE trade_id='legacy-1'"
            )).fetchone()
            assert row == (None, None, None)

            # The scoped unique index exists on the migrated table.
            conn.execute(text(
                "INSERT INTO trades (order_id, trade_id, timestamp, trading_pair,"
                " trade_type, amount, price, account_name, connector_name,"
                " exchange_trade_id) "
                "VALUES (1, 'n-1', '2026-01-01', 'XMR-USDT', 'BUY', 1, 100,"
                " 'acct-1', 'nonkyc', 'T1')"
            ))

            # CDX-R02: the index is the FULL triple — the same account and
            # exchange id under a DIFFERENT connector is a legitimate distinct
            # fill and must insert (a two-column (account, exchange_trade_id)
            # index would reject it here).
            conn.execute(text(
                "INSERT INTO trades (order_id, trade_id, timestamp, trading_pair,"
                " trade_type, amount, price, account_name, connector_name,"
                " exchange_trade_id) "
                "VALUES (1, 'n-1b', '2026-01-01', 'XMR-USDT', 'BUY', 1, 100,"
                " 'acct-1', 'other-dex', 'T1')"
            ))

        # ... and it is ENFORCED: a duplicate scoped triple is rejected
        # (own transaction — the IntegrityError poisons it).
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO trades (order_id, trade_id, timestamp, trading_pair,"
                    " trade_type, amount, price, account_name, connector_name,"
                    " exchange_trade_id) "
                    "VALUES (1, 'n-2', '2026-01-01', 'XMR-USDT', 'BUY', 1, 100,"
                    " 'acct-1', 'nonkyc', 'T1')"
                ))

        # A second NULL-id row inserts fine (NULLs distinct — legacy data and
        # id-less fills can never violate the new index).
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO trades (order_id, trade_id, timestamp, trading_pair,"
                " trade_type, amount, price) "
                "VALUES (1, 'legacy-2', '2026-01-02 00:00:00', 'XMR-USDT', 'BUY', 1, 100)"
            ))

        # And the constants actually carry this phase's migration at all.
        assert any(t == "trades" and c == "exchange_trade_id" for t, c, _ in STARTUP_MIGRATIONS)
        assert any("uq_trade_scoped_exchange_trade_id" in sql for sql in STARTUP_UNIQUE_INDEXES)

    @pytest.mark.asyncio
    async def test_real_startup_path_installs_scoped_index_on_legacy_db(self):
        """CDX-R03: the direct-helper test above proves the helpers work but
        not that startup CALLS them. This one runs the REAL production startup
        entrypoint (AsyncDatabaseManager.create_tables — create_all +
        _run_migrations + _create_unique_indexes + _drop_hummingbot_tables)
        against a legacy database whose trades table predates this phase, then
        proves the scoped dedup index is actually enforced. Unwiring
        _create_unique_indexes from create_tables fails this test: create_all
        skips the pre-existing trades table, so ONLY the startup call installs
        the index there. The engine is a thin awaitable facade over a real
        sync sqlite engine (no async sqlite driver in this env) — every
        statement create_tables issues executes for real."""

        class RunSyncConnAdapter:
            def __init__(self, conn):
                self._conn = conn

            async def execute(self, stmt, params=None):
                if params is not None:
                    return self._conn.execute(stmt, params)
                return self._conn.execute(stmt)

            async def run_sync(self, fn, *args, **kwargs):
                return fn(self._conn, *args, **kwargs)

        class SyncEngineAdapter:
            def __init__(self, engine):
                self._engine = engine

            @asynccontextmanager
            async def begin(self):
                with self._engine.begin() as conn:
                    yield RunSyncConnAdapter(conn)

        engine = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        with engine.begin() as conn:
            # The trades schema as it existed BEFORE this phase.
            conn.execute(text(
                "CREATE TABLE trades ("
                "id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, "
                "trade_id TEXT NOT NULL UNIQUE, timestamp TIMESTAMP NOT NULL, "
                "trading_pair TEXT NOT NULL, trade_type TEXT NOT NULL, "
                "amount NUMERIC NOT NULL, price NUMERIC NOT NULL, "
                "fee_paid NUMERIC NOT NULL DEFAULT 0, fee_currency TEXT)"
            ))

        mgr = AsyncDatabaseManager.__new__(AsyncDatabaseManager)  # skip asyncpg-only __init__
        mgr.engine = SyncEngineAdapter(engine)
        await mgr.create_tables()  # the REAL startup entrypoint

        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO trades (order_id, trade_id, timestamp, trading_pair,"
                " trade_type, amount, price, account_name, connector_name,"
                " exchange_trade_id) "
                "VALUES (1, 's-1', '2026-01-01', 'XMR-USDT', 'BUY', 1, 100,"
                " 'acct-1', 'nonkyc', 'T1')"
            ))
        # Enforced: the same scoped triple under a different synthetic
        # trade_id is rejected by the index startup just installed.
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO trades (order_id, trade_id, timestamp, trading_pair,"
                    " trade_type, amount, price, account_name, connector_name,"
                    " exchange_trade_id) "
                    "VALUES (1, 's-2', '2026-01-01', 'XMR-USDT', 'BUY', 1, 100,"
                    " 'acct-1', 'nonkyc', 'T1')"
                ))
