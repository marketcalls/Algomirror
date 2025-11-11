"""Add risk monitoring and WebSocket session management

Revision ID: 003
Revises: 002
Create Date: 2025-01-10 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None

def upgrade():
    # Create websocket_sessions table
    op.create_table(
        'websocket_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.String(length=64), nullable=False),
        sa.Column('underlying', sa.String(length=20), nullable=False),
        sa.Column('expiry', sa.String(length=20), nullable=False),
        sa.Column('subscribed_symbols', sa.JSON(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True, server_default='1'),
        sa.Column('last_heartbeat', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('session_id')
    )

    # Create indexes for websocket_sessions
    with op.batch_alter_table('websocket_sessions', schema=None) as batch_op:
        batch_op.create_index('idx_websocket_sessions_active', ['is_active', 'user_id'], unique=False)
        batch_op.create_index('idx_websocket_sessions_expiry', ['expires_at'], unique=False)

    # Create risk_events table
    op.create_table(
        'risk_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('strategy_id', sa.Integer(), nullable=False),
        sa.Column('execution_id', sa.Integer(), nullable=True),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('threshold_value', sa.Float(), nullable=True),
        sa.Column('current_value', sa.Float(), nullable=True),
        sa.Column('action_taken', sa.String(length=50), nullable=True),
        sa.Column('exit_order_ids', sa.JSON(), nullable=True),
        sa.Column('triggered_at', sa.DateTime(), nullable=True, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['execution_id'], ['strategy_executions.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )

    # Create indexes for risk_events
    with op.batch_alter_table('risk_events', schema=None) as batch_op:
        batch_op.create_index('idx_risk_events_strategy', ['strategy_id', 'triggered_at'], unique=False)
        batch_op.create_index('idx_risk_events_type', ['event_type', 'triggered_at'], unique=False)

    # Add new columns to strategies table
    with op.batch_alter_table('strategies', schema=None) as batch_op:
        batch_op.add_column(sa.Column('risk_monitoring_enabled', sa.Boolean(), nullable=True, server_default='1'))
        batch_op.add_column(sa.Column('risk_check_interval', sa.Integer(), nullable=True, server_default='1'))
        batch_op.add_column(sa.Column('auto_exit_on_max_loss', sa.Boolean(), nullable=True, server_default='1'))
        batch_op.add_column(sa.Column('auto_exit_on_max_profit', sa.Boolean(), nullable=True, server_default='1'))
        batch_op.add_column(sa.Column('trailing_sl_type', sa.String(length=20), nullable=True, server_default='percentage'))

    # Add new columns to strategy_executions table
    with op.batch_alter_table('strategy_executions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_price', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('last_price_updated', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('websocket_subscribed', sa.Boolean(), nullable=True, server_default='0'))
        batch_op.add_column(sa.Column('trailing_sl_triggered', sa.Float(), nullable=True))

def downgrade():
    # Remove columns from strategy_executions
    with op.batch_alter_table('strategy_executions', schema=None) as batch_op:
        batch_op.drop_column('trailing_sl_triggered')
        batch_op.drop_column('websocket_subscribed')
        batch_op.drop_column('last_price_updated')
        batch_op.drop_column('last_price')

    # Remove columns from strategies
    with op.batch_alter_table('strategies', schema=None) as batch_op:
        batch_op.drop_column('trailing_sl_type')
        batch_op.drop_column('auto_exit_on_max_profit')
        batch_op.drop_column('auto_exit_on_max_loss')
        batch_op.drop_column('risk_check_interval')
        batch_op.drop_column('risk_monitoring_enabled')

    # Drop indexes and table for risk_events
    with op.batch_alter_table('risk_events', schema=None) as batch_op:
        batch_op.drop_index('idx_risk_events_type')
        batch_op.drop_index('idx_risk_events_strategy')
    op.drop_table('risk_events')

    # Drop indexes and table for websocket_sessions
    with op.batch_alter_table('websocket_sessions', schema=None) as batch_op:
        batch_op.drop_index('idx_websocket_sessions_expiry')
        batch_op.drop_index('idx_websocket_sessions_active')
    op.drop_table('websocket_sessions')
