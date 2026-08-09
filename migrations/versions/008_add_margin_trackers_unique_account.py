"""Enforce one MarginTracker row per account

Revision ID: 008_unique_tracker
Revises: 007_add_session_token
Create Date: 2026-08-07

Two code paths could each create a MarginTracker for the same account_id
with no coordination (MarginCalculator.get_available_margin() and the
/margin/tracker and /margin/refresh-tracker routes), so a first-time fetch
or two near-simultaneous refreshes could leave more than one row per
account. Dedupes existing duplicates before adding the constraint, keeping
the row with the most recent last_updated (falling back to the highest id
only as a tiebreaker) - the highest id is just the most recently *created*
row, not necessarily the one later refreshes kept updating, so picking by
id alone risks deleting the actively-used row.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '008_unique_tracker'
down_revision = '007_add_session_token'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    # Keep the row with the most recent last_updated per account (falling
    # back to highest id only as a tiebreaker) - the highest id is just the
    # most recently *created* row, not necessarily the one later refreshes
    # kept updating, so picking by id alone risks deleting the actively-used
    # row.
    bind.execute(sa.text("""
        DELETE FROM margin_trackers
        WHERE id NOT IN (
            SELECT keep_id FROM (
                SELECT t1.id AS keep_id
                FROM margin_trackers t1
                WHERE t1.id = (
                    SELECT t2.id FROM margin_trackers t2
                    WHERE t2.account_id = t1.account_id
                    ORDER BY (t2.last_updated IS NULL) ASC, t2.last_updated DESC, t2.id DESC
                    LIMIT 1
                )
            ) keepers
        )
    """))

    inspector = sa.inspect(bind)
    existing_indexes = [ix['name'] for ix in inspector.get_indexes('margin_trackers')]
    if 'ux_margin_trackers_account_id' not in existing_indexes:
        with op.batch_alter_table('margin_trackers', schema=None) as batch_op:
            batch_op.create_unique_constraint('ux_margin_trackers_account_id', ['account_id'])


def downgrade():
    with op.batch_alter_table('margin_trackers', schema=None) as batch_op:
        batch_op.drop_constraint('ux_margin_trackers_account_id', type_='unique')
