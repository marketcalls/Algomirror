"""Add app_settings table for platform-wide feature toggles

Revision ID: 005_add_app_settings
Revises: 004_fix_trade_quality_labels
Create Date: 2026-07-05

Adds a singleton app_settings table with strategy_engine_enabled, so admins
can turn off the strategy execution surface (Strategy Builder, Risk Manager,
Supertrend Exit, Order Poller, broker-position reconciliation) for
deployments that only use AlgoMirror to view accounts run by an external
strategy engine.

App startup (app/__init__.py) calls db.create_all() unconditionally, which
also creates any table backing a model that doesn't exist yet - including
this one. That runs on every boot, independent of whether this migration
has been applied. So this upgrade() checks for the table/seed row first
instead of assuming a clean slate, to stay a no-op if create_all() already
did the work (and to avoid re-inserting the seed row on a second run).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '005_add_app_settings'
down_revision = '004_fix_trade_quality_labels'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'app_settings' not in inspector.get_table_names():
        op.create_table(
            'app_settings',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('strategy_engine_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
        )

    app_settings = sa.table(
        'app_settings',
        sa.column('id', sa.Integer()),
        sa.column('strategy_engine_enabled', sa.Boolean()),
    )
    existing = bind.execute(sa.select(app_settings.c.id)).first()
    if existing is None:
        # Bind the value through SQLAlchemy Core rather than a raw SQL literal -
        # PostgreSQL's boolean column rejects an integer 1 (no implicit int->bool
        # cast, unlike SQLite/MySQL), so this must go through the Boolean type.
        bind.execute(app_settings.insert().values(strategy_engine_enabled=True))


def downgrade():
    op.drop_table('app_settings')
