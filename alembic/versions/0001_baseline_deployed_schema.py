"""baseline: the DEPLOYED schema (pre phases 7/8)

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-16

CDX-013 baseline. This revision reproduces the schema that the currently
DEPLOYED image builds — i.e. hummingbot-api rev b3aad361 `database/models.py`
plus the three ad-hoc startup ALTERs that image ran (executors.controller_id,
executors.error_log, position_holds.cum_fees_quote; all three are already
columns of those models here, so create_table covers them).

It deliberately does NOT contain the phase 7 (CDX-005 retirement evidence) or
phase 8 (CDX-006 scoped fill identity) columns: rev b3aad361 has none of them,
their ad-hoc ALTERs have never run against the deployed database, and
`alembic stamp 0001_baseline` on that database must therefore leave it BEHIND
head so revision 0002 actually applies them. Folding 0002 into this baseline
would let a stamped production database report "at head" while missing the
retirement evidence columns and the money-critical fill dedup index — silently
reintroducing CDX-006 double-counting. Baseline + delta keeps that fail-closed.

Schema parity is not asserted here but by tests/test_migrations.py, which
compares the models against a database at HEAD (0001 + 0002).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('account_states',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('timestamp', sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('account_name', sa.String(), nullable=False),
    sa.Column('connector_name', sa.String(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_account_states_account_name'), 'account_states', ['account_name'], unique=False)
    op.create_index(op.f('ix_account_states_connector_name'), 'account_states', ['connector_name'], unique=False)
    op.create_index(op.f('ix_account_states_id'), 'account_states', ['id'], unique=False)
    op.create_index(op.f('ix_account_states_timestamp'), 'account_states', ['timestamp'], unique=False)
    op.create_table('bot_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bot_name', sa.String(), nullable=False),
    sa.Column('instance_name', sa.String(), nullable=False),
    sa.Column('deployed_at', sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('strategy_type', sa.String(), nullable=False),
    sa.Column('strategy_name', sa.String(), nullable=False),
    sa.Column('config_name', sa.String(), nullable=True),
    sa.Column('stopped_at', sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('deployment_status', sa.String(), nullable=False),
    sa.Column('run_status', sa.String(), nullable=False),
    sa.Column('deployment_config', sa.Text(), nullable=True),
    sa.Column('final_status', sa.Text(), nullable=True),
    sa.Column('account_name', sa.String(), nullable=False),
    sa.Column('image_version', sa.String(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_bot_runs_account_name'), 'bot_runs', ['account_name'], unique=False)
    op.create_index(op.f('ix_bot_runs_bot_name'), 'bot_runs', ['bot_name'], unique=False)
    op.create_index(op.f('ix_bot_runs_config_name'), 'bot_runs', ['config_name'], unique=False)
    op.create_index(op.f('ix_bot_runs_deployed_at'), 'bot_runs', ['deployed_at'], unique=False)
    op.create_index(op.f('ix_bot_runs_deployment_status'), 'bot_runs', ['deployment_status'], unique=False)
    op.create_index(op.f('ix_bot_runs_id'), 'bot_runs', ['id'], unique=False)
    op.create_index(op.f('ix_bot_runs_image_version'), 'bot_runs', ['image_version'], unique=False)
    op.create_index(op.f('ix_bot_runs_instance_name'), 'bot_runs', ['instance_name'], unique=False)
    op.create_index(op.f('ix_bot_runs_run_status'), 'bot_runs', ['run_status'], unique=False)
    op.create_index(op.f('ix_bot_runs_stopped_at'), 'bot_runs', ['stopped_at'], unique=False)
    op.create_index(op.f('ix_bot_runs_strategy_name'), 'bot_runs', ['strategy_name'], unique=False)
    op.create_index(op.f('ix_bot_runs_strategy_type'), 'bot_runs', ['strategy_type'], unique=False)
    op.create_table('controller_performance_snapshots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('timestamp', sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('bot_name', sa.String(), nullable=False),
    sa.Column('controller_id', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('performance', sa.Text(), nullable=True),
    sa.Column('custom_info', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_controller_performance_snapshots_bot_name'), 'controller_performance_snapshots', ['bot_name'], unique=False)
    op.create_index(op.f('ix_controller_performance_snapshots_controller_id'), 'controller_performance_snapshots', ['controller_id'], unique=False)
    op.create_index(op.f('ix_controller_performance_snapshots_id'), 'controller_performance_snapshots', ['id'], unique=False)
    op.create_index(op.f('ix_controller_performance_snapshots_timestamp'), 'controller_performance_snapshots', ['timestamp'], unique=False)
    op.create_table('executors',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('executor_id', sa.String(), nullable=False),
    sa.Column('executor_type', sa.String(), nullable=False),
    sa.Column('account_name', sa.String(), nullable=False),
    sa.Column('connector_name', sa.String(), nullable=False),
    sa.Column('trading_pair', sa.String(), nullable=False),
    sa.Column('controller_id', sa.String(), nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('closed_at', sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('close_type', sa.String(), nullable=True),
    sa.Column('net_pnl_quote', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('net_pnl_pct', sa.Numeric(precision=10, scale=6), nullable=False),
    sa.Column('cum_fees_quote', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('filled_amount_quote', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('error_log', sa.Text(), nullable=True),
    sa.Column('config', sa.Text(), nullable=True),
    sa.Column('final_state', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_executors_account_name'), 'executors', ['account_name'], unique=False)
    op.create_index(op.f('ix_executors_closed_at'), 'executors', ['closed_at'], unique=False)
    op.create_index(op.f('ix_executors_connector_name'), 'executors', ['connector_name'], unique=False)
    op.create_index(op.f('ix_executors_controller_id'), 'executors', ['controller_id'], unique=False)
    op.create_index(op.f('ix_executors_created_at'), 'executors', ['created_at'], unique=False)
    op.create_index(op.f('ix_executors_executor_id'), 'executors', ['executor_id'], unique=True)
    op.create_index(op.f('ix_executors_executor_type'), 'executors', ['executor_type'], unique=False)
    op.create_index(op.f('ix_executors_id'), 'executors', ['id'], unique=False)
    op.create_index(op.f('ix_executors_status'), 'executors', ['status'], unique=False)
    op.create_index(op.f('ix_executors_trading_pair'), 'executors', ['trading_pair'], unique=False)
    op.create_table('funding_payments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('funding_payment_id', sa.String(), nullable=False),
    sa.Column('timestamp', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('account_name', sa.String(), nullable=False),
    sa.Column('connector_name', sa.String(), nullable=False),
    sa.Column('trading_pair', sa.String(), nullable=False),
    sa.Column('funding_rate', sa.Numeric(precision=20, scale=18), nullable=False),
    sa.Column('funding_payment', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('fee_currency', sa.String(), nullable=False),
    sa.Column('position_size', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('position_side', sa.String(), nullable=True),
    sa.Column('exchange_funding_id', sa.String(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_funding_payments_account_name'), 'funding_payments', ['account_name'], unique=False)
    op.create_index(op.f('ix_funding_payments_connector_name'), 'funding_payments', ['connector_name'], unique=False)
    op.create_index(op.f('ix_funding_payments_exchange_funding_id'), 'funding_payments', ['exchange_funding_id'], unique=False)
    op.create_index(op.f('ix_funding_payments_funding_payment_id'), 'funding_payments', ['funding_payment_id'], unique=True)
    op.create_index(op.f('ix_funding_payments_id'), 'funding_payments', ['id'], unique=False)
    op.create_index(op.f('ix_funding_payments_timestamp'), 'funding_payments', ['timestamp'], unique=False)
    op.create_index(op.f('ix_funding_payments_trading_pair'), 'funding_payments', ['trading_pair'], unique=False)
    op.create_table('gateway_clmm_positions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('position_address', sa.String(), nullable=False),
    sa.Column('pool_address', sa.String(), nullable=False),
    sa.Column('network', sa.String(), nullable=False),
    sa.Column('connector', sa.String(), nullable=False),
    sa.Column('wallet_address', sa.String(), nullable=False),
    sa.Column('trading_pair', sa.String(), nullable=False),
    sa.Column('base_token', sa.String(), nullable=False),
    sa.Column('quote_token', sa.String(), nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('closed_at', sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('lower_price', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('upper_price', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('lower_bin_id', sa.Integer(), nullable=True),
    sa.Column('upper_bin_id', sa.Integer(), nullable=True),
    sa.Column('entry_price', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('current_price', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('initial_base_token_amount', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('initial_quote_token_amount', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('position_rent', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('base_token_amount', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('quote_token_amount', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('in_range', sa.String(), nullable=False),
    sa.Column('percentage', sa.Numeric(precision=10, scale=6), nullable=True),
    sa.Column('base_fee_collected', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('quote_fee_collected', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('base_fee_pending', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('quote_fee_pending', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('last_updated', sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_gateway_clmm_positions_base_token'), 'gateway_clmm_positions', ['base_token'], unique=False)
    op.create_index(op.f('ix_gateway_clmm_positions_closed_at'), 'gateway_clmm_positions', ['closed_at'], unique=False)
    op.create_index(op.f('ix_gateway_clmm_positions_connector'), 'gateway_clmm_positions', ['connector'], unique=False)
    op.create_index(op.f('ix_gateway_clmm_positions_created_at'), 'gateway_clmm_positions', ['created_at'], unique=False)
    op.create_index(op.f('ix_gateway_clmm_positions_id'), 'gateway_clmm_positions', ['id'], unique=False)
    op.create_index(op.f('ix_gateway_clmm_positions_network'), 'gateway_clmm_positions', ['network'], unique=False)
    op.create_index(op.f('ix_gateway_clmm_positions_pool_address'), 'gateway_clmm_positions', ['pool_address'], unique=False)
    op.create_index(op.f('ix_gateway_clmm_positions_position_address'), 'gateway_clmm_positions', ['position_address'], unique=True)
    op.create_index(op.f('ix_gateway_clmm_positions_quote_token'), 'gateway_clmm_positions', ['quote_token'], unique=False)
    op.create_index(op.f('ix_gateway_clmm_positions_status'), 'gateway_clmm_positions', ['status'], unique=False)
    op.create_index(op.f('ix_gateway_clmm_positions_trading_pair'), 'gateway_clmm_positions', ['trading_pair'], unique=False)
    op.create_index(op.f('ix_gateway_clmm_positions_wallet_address'), 'gateway_clmm_positions', ['wallet_address'], unique=False)
    op.create_table('gateway_swaps',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('transaction_hash', sa.String(), nullable=False),
    sa.Column('timestamp', sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('network', sa.String(), nullable=False),
    sa.Column('connector', sa.String(), nullable=False),
    sa.Column('wallet_address', sa.String(), nullable=False),
    sa.Column('trading_pair', sa.String(), nullable=False),
    sa.Column('base_token', sa.String(), nullable=False),
    sa.Column('quote_token', sa.String(), nullable=False),
    sa.Column('side', sa.String(), nullable=False),
    sa.Column('input_amount', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('output_amount', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('price', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('slippage_pct', sa.Numeric(precision=10, scale=6), nullable=True),
    sa.Column('gas_fee', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('gas_token', sa.String(), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('pool_address', sa.String(), nullable=True),
    sa.Column('quote_id', sa.String(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_gateway_swaps_base_token'), 'gateway_swaps', ['base_token'], unique=False)
    op.create_index(op.f('ix_gateway_swaps_connector'), 'gateway_swaps', ['connector'], unique=False)
    op.create_index(op.f('ix_gateway_swaps_id'), 'gateway_swaps', ['id'], unique=False)
    op.create_index(op.f('ix_gateway_swaps_network'), 'gateway_swaps', ['network'], unique=False)
    op.create_index(op.f('ix_gateway_swaps_pool_address'), 'gateway_swaps', ['pool_address'], unique=False)
    op.create_index(op.f('ix_gateway_swaps_quote_token'), 'gateway_swaps', ['quote_token'], unique=False)
    op.create_index(op.f('ix_gateway_swaps_status'), 'gateway_swaps', ['status'], unique=False)
    op.create_index(op.f('ix_gateway_swaps_timestamp'), 'gateway_swaps', ['timestamp'], unique=False)
    op.create_index(op.f('ix_gateway_swaps_trading_pair'), 'gateway_swaps', ['trading_pair'], unique=False)
    op.create_index(op.f('ix_gateway_swaps_transaction_hash'), 'gateway_swaps', ['transaction_hash'], unique=True)
    op.create_index(op.f('ix_gateway_swaps_wallet_address'), 'gateway_swaps', ['wallet_address'], unique=False)
    op.create_table('orders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('client_order_id', sa.String(), nullable=False),
    sa.Column('exchange_order_id', sa.String(), nullable=True),
    sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('account_name', sa.String(), nullable=False),
    sa.Column('connector_name', sa.String(), nullable=False),
    sa.Column('trading_pair', sa.String(), nullable=False),
    sa.Column('trade_type', sa.String(), nullable=False),
    sa.Column('order_type', sa.String(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('price', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('filled_amount', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('average_fill_price', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('fee_paid', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('fee_currency', sa.String(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_orders_account_name'), 'orders', ['account_name'], unique=False)
    op.create_index(op.f('ix_orders_client_order_id'), 'orders', ['client_order_id'], unique=True)
    op.create_index(op.f('ix_orders_connector_name'), 'orders', ['connector_name'], unique=False)
    op.create_index(op.f('ix_orders_created_at'), 'orders', ['created_at'], unique=False)
    op.create_index(op.f('ix_orders_exchange_order_id'), 'orders', ['exchange_order_id'], unique=False)
    op.create_index(op.f('ix_orders_id'), 'orders', ['id'], unique=False)
    op.create_index(op.f('ix_orders_status'), 'orders', ['status'], unique=False)
    op.create_index(op.f('ix_orders_trading_pair'), 'orders', ['trading_pair'], unique=False)
    op.create_table('position_holds',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_name', sa.String(), nullable=False),
    sa.Column('connector_name', sa.String(), nullable=False),
    sa.Column('trading_pair', sa.String(), nullable=False),
    sa.Column('controller_id', sa.String(), nullable=False),
    sa.Column('buy_amount_base', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('buy_amount_quote', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('sell_amount_base', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('sell_amount_quote', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('realized_pnl_quote', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('cum_fees_quote', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('executor_ids', sa.Text(), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('last_updated', sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('cleared_at', sa.TIMESTAMP(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_name', 'connector_name', 'trading_pair', 'controller_id', name='uq_position_hold_key')
    )
    op.create_index(op.f('ix_position_holds_account_name'), 'position_holds', ['account_name'], unique=False)
    op.create_index(op.f('ix_position_holds_connector_name'), 'position_holds', ['connector_name'], unique=False)
    op.create_index(op.f('ix_position_holds_controller_id'), 'position_holds', ['controller_id'], unique=False)
    op.create_index(op.f('ix_position_holds_created_at'), 'position_holds', ['created_at'], unique=False)
    op.create_index(op.f('ix_position_holds_id'), 'position_holds', ['id'], unique=False)
    op.create_index(op.f('ix_position_holds_status'), 'position_holds', ['status'], unique=False)
    op.create_index(op.f('ix_position_holds_trading_pair'), 'position_holds', ['trading_pair'], unique=False)
    op.create_table('position_snapshots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_name', sa.String(), nullable=False),
    sa.Column('connector_name', sa.String(), nullable=False),
    sa.Column('trading_pair', sa.String(), nullable=False),
    sa.Column('timestamp', sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('side', sa.String(), nullable=False),
    sa.Column('exchange_size', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('entry_price', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('mark_price', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('unrealized_pnl', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('percentage_pnl', sa.Numeric(precision=10, scale=6), nullable=True),
    sa.Column('leverage', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('initial_margin', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('maintenance_margin', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('cumulative_funding_fees', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('fee_currency', sa.String(), nullable=True),
    sa.Column('calculated_size', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('calculated_entry_price', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('size_difference', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('exchange_position_id', sa.String(), nullable=True),
    sa.Column('is_reconciled', sa.String(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_position_snapshots_account_name'), 'position_snapshots', ['account_name'], unique=False)
    op.create_index(op.f('ix_position_snapshots_connector_name'), 'position_snapshots', ['connector_name'], unique=False)
    op.create_index(op.f('ix_position_snapshots_exchange_position_id'), 'position_snapshots', ['exchange_position_id'], unique=False)
    op.create_index(op.f('ix_position_snapshots_id'), 'position_snapshots', ['id'], unique=False)
    op.create_index(op.f('ix_position_snapshots_timestamp'), 'position_snapshots', ['timestamp'], unique=False)
    op.create_index(op.f('ix_position_snapshots_trading_pair'), 'position_snapshots', ['trading_pair'], unique=False)
    op.create_table('executor_orders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('executor_id', sa.String(), nullable=False),
    sa.Column('client_order_id', sa.String(), nullable=False),
    sa.Column('exchange_order_id', sa.String(), nullable=True),
    sa.Column('order_type', sa.String(), nullable=False),
    sa.Column('trade_type', sa.String(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('price', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('filled_amount', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('average_fill_price', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['executor_id'], ['executors.executor_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_executor_orders_client_order_id'), 'executor_orders', ['client_order_id'], unique=False)
    op.create_index(op.f('ix_executor_orders_executor_id'), 'executor_orders', ['executor_id'], unique=False)
    op.create_index(op.f('ix_executor_orders_id'), 'executor_orders', ['id'], unique=False)
    op.create_table('gateway_clmm_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('position_id', sa.Integer(), nullable=False),
    sa.Column('transaction_hash', sa.String(), nullable=False),
    sa.Column('timestamp', sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('event_type', sa.String(), nullable=False),
    sa.Column('base_token_amount', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('quote_token_amount', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('base_fee_collected', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('quote_fee_collected', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('gas_fee', sa.Numeric(precision=30, scale=18), nullable=True),
    sa.Column('gas_token', sa.String(), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['position_id'], ['gateway_clmm_positions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_gateway_clmm_events_event_type'), 'gateway_clmm_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_gateway_clmm_events_id'), 'gateway_clmm_events', ['id'], unique=False)
    op.create_index(op.f('ix_gateway_clmm_events_status'), 'gateway_clmm_events', ['status'], unique=False)
    op.create_index(op.f('ix_gateway_clmm_events_timestamp'), 'gateway_clmm_events', ['timestamp'], unique=False)
    op.create_index(op.f('ix_gateway_clmm_events_transaction_hash'), 'gateway_clmm_events', ['transaction_hash'], unique=False)
    op.create_table('token_states',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_state_id', sa.Integer(), nullable=False),
    sa.Column('token', sa.String(), nullable=False),
    sa.Column('units', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('price', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('value', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('available_units', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.ForeignKeyConstraint(['account_state_id'], ['account_states.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_token_states_id'), 'token_states', ['id'], unique=False)
    op.create_index(op.f('ix_token_states_token'), 'token_states', ['token'], unique=False)
    op.create_table('trades',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=False),
    sa.Column('trade_id', sa.String(), nullable=False),
    sa.Column('timestamp', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('trading_pair', sa.String(), nullable=False),
    sa.Column('trade_type', sa.String(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('price', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('fee_paid', sa.Numeric(precision=30, scale=18), nullable=False),
    sa.Column('fee_currency', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_trades_id'), 'trades', ['id'], unique=False)
    op.create_index(op.f('ix_trades_timestamp'), 'trades', ['timestamp'], unique=False)
    op.create_index(op.f('ix_trades_trade_id'), 'trades', ['trade_id'], unique=True)
    op.create_index(op.f('ix_trades_trading_pair'), 'trades', ['trading_pair'], unique=False)


def downgrade() -> None:
    """Refused: this is the baseline of a live trading database.

    Downgrading it means DROP TABLE on every orders/trades/bot_runs table —
    unrecoverable loss of trading history. Alembic offers no confirmation
    prompt, so an accidental `alembic downgrade base` would take the lot.
    A human who genuinely wants an empty database can drop it explicitly.
    """
    raise NotImplementedError(
        "Refusing to downgrade the baseline revision: it would DROP every "
        "table (orders, trades, bot_runs, ...) in a live trading database. "
        "Drop the database explicitly if that is genuinely intended."
    )
