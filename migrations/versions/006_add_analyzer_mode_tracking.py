"""Add analyzer mode tracking to trading_accounts

Revision ID: 006_add_analyzer_mode
Revises: 004_fix_trade_quality_labels
Create Date: 2026-08-03

Adds last_analyzer_mode/last_analyzer_check so the dashboard can show
whether each account's OpenAlgo instance is running in Live or Analyzer
(simulated) mode, alongside the existing connection-status badge.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '006_add_analyzer_mode'
down_revision = '004_fix_trade_quality_labels'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('trading_accounts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_analyzer_mode', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('last_analyzer_check', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('trading_accounts', schema=None) as batch_op:
        batch_op.drop_column('last_analyzer_check')
        batch_op.drop_column('last_analyzer_mode')
