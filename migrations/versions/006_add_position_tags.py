"""Add position_tags for manual per-stock strategy tagging on Holdings

Revision ID: 009_add_position_tags
Revises: 008_unique_tracker
Create Date: 2026-08-10

Adds PositionTag(account_id, symbol, strategy) so the Holdings page can tag
a stock with a strategy name the user assigns themselves - no OpenAlgo
changes, no per-order auto-detection. Keyed on (account_id, symbol) only,
not exchange/product/quantity, matching the "no complexities" scope decided
for this feature. holdings() deletes a row automatically once its symbol
drops out of that account's live broker response, so closed positions don't
leave stale tags behind.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '006_add_position_tags'
down_revision = '005_add_app_settings'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'position_tags',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('account_id', sa.Integer(), sa.ForeignKey('trading_accounts.id'), nullable=False),
        sa.Column('symbol', sa.String(length=50), nullable=False),
        sa.Column('strategy', sa.String(length=100), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('account_id', 'symbol', name='_account_symbol_tag_uc'),
    )
    with op.batch_alter_table('position_tags', schema=None) as batch_op:
        batch_op.create_index('ix_position_tags_account_id', ['account_id'])


def downgrade():
    with op.batch_alter_table('position_tags', schema=None) as batch_op:
        batch_op.drop_index('ix_position_tags_account_id')
    op.drop_table('position_tags')
