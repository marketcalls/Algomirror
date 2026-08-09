"""Add session_token to users for password-reset session invalidation

Revision ID: 007_add_session_token
Revises: 004_fix_trade_quality_labels
Create Date: 2026-08-07

Adds User.session_token, embedded in get_id() and checked in load_user() so
that resetting a password invalidates any session/remember-me cookie issued
before the reset - without this, a stolen or forgotten-but-still-open
session keeps working right through a password reset.
"""
from alembic import op
import sqlalchemy as sa
import secrets


# revision identifiers, used by Alembic.
revision = '007_add_session_token'
down_revision = '004_fix_trade_quality_labels'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = [c['name'] for c in inspector.get_columns('users')]

    if 'session_token' not in existing_cols:
        with op.batch_alter_table('users', schema=None) as batch_op:
            batch_op.add_column(sa.Column('session_token', sa.String(length=32), nullable=True))

    # Backfill existing rows - the Python-side column default only applies to
    # rows inserted after this migration, not ones already in the table.
    # Each row gets its own random token (not one shared value) in case a
    # dev DB has more than the usual single admin row (e.g. init_db.py
    # testdata's second user).
    users = sa.table('users', sa.column('id', sa.Integer()), sa.column('session_token', sa.String()))
    for row in bind.execute(sa.select(users.c.id).where(users.c.session_token.is_(None))):
        bind.execute(
            users.update().where(users.c.id == row.id).values(session_token=secrets.token_hex(16))
        )


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('session_token')
