"""
Migration: Enforce one MarginTracker row per account

Two code paths could each create a MarginTracker for the same account_id
with no coordination (MarginCalculator.get_available_margin() and the
/margin/tracker and /margin/refresh-tracker routes), so a first-time fetch
or two near-simultaneous refreshes could leave more than one row per
account. Later .first() reads/updates against those extra rows are
nondeterministic. Dedupes existing duplicates before adding the constraint,
keeping the row with the most recent last_updated (falling back to the
highest id only as a tiebreaker) - the highest id is just the most
recently *created* row, not necessarily the one later refreshes kept
updating, so picking by id alone risks deleting the actively-used row.
"""

from sqlalchemy import text


def upgrade(db):
    """Dedupe margin_trackers and add a unique constraint on account_id"""

    result = db.session.execute(text("""
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
    if result.rowcount:
        print(f"Removed {result.rowcount} duplicate margin_trackers row(s)")
    db.session.commit()

    try:
        db.session.execute(text(
            "CREATE UNIQUE INDEX ux_margin_trackers_account_id "
            "ON margin_trackers(account_id)"
        ))
        db.session.commit()
        print("Added unique index on margin_trackers.account_id")
    except Exception as e:
        db.session.rollback()
        error_msg = str(e).lower()
        # Only skip on "this exact index already exists" - anything else
        # (e.g. a duplicate-key error, meaning the dedup above didn't
        # actually leave the data unique) must fail loudly rather than be
        # silently treated as success.
        if 'already exists' in error_msg:
            print("Unique index already exists, skipping")
        else:
            raise


def downgrade(db):
    """Drop the unique constraint/index on margin_trackers.account_id.

    Tries both forms since the object could have been created as a
    PostgreSQL CONSTRAINT (via the Alembic migration's
    create_unique_constraint, which DROP INDEX can't remove there) or as a
    plain INDEX (via this migration's CREATE UNIQUE INDEX, which SQLite has
    no DROP CONSTRAINT for). Only the SQLite "no such syntax" case is an
    expected no-op - anything else is a real failure and gets surfaced
    rather than silently treated as if the downgrade succeeded.
    """
    try:
        db.session.execute(text(
            "ALTER TABLE margin_trackers DROP CONSTRAINT IF EXISTS ux_margin_trackers_account_id"
        ))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        if 'syntax error' not in str(e).lower():
            print(f"ERROR: unexpected error dropping margin_trackers constraint: {e}")
            raise

    try:
        db.session.execute(text("DROP INDEX IF EXISTS ux_margin_trackers_account_id"))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"ERROR: failed to drop index ux_margin_trackers_account_id: {e}")
        raise
