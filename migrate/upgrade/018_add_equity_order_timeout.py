"""
Migration: make the order timeout an admin setting

AlgoMirror waited a fixed 30 seconds for a broker to answer an order write. On
2026-08-30 a placement against the Dhan sandbox took 63 seconds: both orders
completed at the broker, but AlgoMirror had already stopped listening and
reported them as unconfirmed, leaving a position the app did not know about.

The right wait depends entirely on what is on the other end. A live broker
answers in a second or two, so a short wait is correct and a long one would
hide a real problem. A sandbox can take a minute. So it becomes a setting:

  equity_settings.order_timeout_seconds   new column, default 30

Bounded 10 to 180 by the order engine and the API. Existing rows are given the
30 the code already used, so nothing changes until it is deliberately altered.

Only a column is added. No existing value is rewritten, and re-running is safe:
the column is detected with the SQLAlchemy inspector rather than a SQLite-only
PRAGMA, and the work is skipped when it is already there.
"""

from sqlalchemy import text, inspect

DEFAULT_ORDER_TIMEOUT_SECONDS = 30


def _is_postgres(db):
    return 'postgresql' in str(db.engine.url)


def _tables(db):
    try:
        return set(inspect(db.engine).get_table_names())
    except Exception:
        return set()


def _column_names(db, table_name):
    try:
        return {col['name'] for col in inspect(db.engine).get_columns(table_name)}
    except Exception:
        return set()


def _add_column(db):
    if 'equity_settings' not in _tables(db):
        print("  Table equity_settings is not there yet, nothing to do")
        return False

    if 'order_timeout_seconds' in _column_names(db, 'equity_settings'):
        print("  Column order_timeout_seconds already exists, skipping")
        return False

    column_type = 'INTEGER'
    db.session.execute(text(
        "ALTER TABLE equity_settings ADD COLUMN order_timeout_seconds %s "
        "NOT NULL DEFAULT %d" % (column_type, DEFAULT_ORDER_TIMEOUT_SECONDS)
    ))
    db.session.commit()
    print("  Added equity_settings.order_timeout_seconds, default %d"
          % DEFAULT_ORDER_TIMEOUT_SECONDS)
    return True


def _backfill(db):
    """Give any row that somehow has no value the default the code used."""
    try:
        result = db.session.execute(text(
            "UPDATE equity_settings SET order_timeout_seconds = :value "
            "WHERE order_timeout_seconds IS NULL OR order_timeout_seconds <= 0"
        ), {'value': DEFAULT_ORDER_TIMEOUT_SECONDS})
        db.session.commit()
        count = result.rowcount if result.rowcount is not None else 0
        if count:
            print("  Set the default on %d existing row(s)" % count)
    except Exception as exc:
        db.session.rollback()
        print("  Backfill skipped: %s" % exc)


def upgrade(db):
    print("Making the order timeout an admin setting")
    if _add_column(db):
        _backfill(db)
    print("Done")


def downgrade(db):
    """
    Leave the column in place.

    Dropping a column means rebuilding the table on SQLite, and older builds
    cannot drop one at all. The column is harmless when unused - the engine
    falls back to its own default whenever the value is missing or out of
    range - so removing it is a manual, considered operation.
    """
    print("  equity_settings.order_timeout_seconds was left in place on purpose")
