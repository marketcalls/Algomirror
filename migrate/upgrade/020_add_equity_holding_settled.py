"""
Migration: arm a stop loss from the moment the buy fills

A delivery buy is a POSITION on the day it is bought and only becomes a
HOLDING when it settles, on T+1. The stop loss and target monitor works on
holding rows, so a stock bought this morning with a stop loss set on the order
was watched by nothing at all until tomorrow. On 2026-09-04 that was seen live:
INFY filled at 11:12 with a stop loss of 1120 and a target of 1136, and no
holding row existed for it to govern.

This adds:

  equity_holdings.is_settled   new column, default TRUE

FALSE marks a row created from AlgoMirror's own fills before the broker's
holdings book reports the stock. Such a row is monitored like any other, is not
retired by the holdings sync (which would otherwise clear it the moment the
broker failed to list it), and has its quantity verified against the POSITION
book rather than the holdings book when a sell is prepared.

Every row that exists today is settled by definition - it came from the broker's
holdings book in the first place - so the default is TRUE and no existing row
changes meaning.

Only a column is added. No existing value is rewritten, and re-running is safe:
the column is detected with the SQLAlchemy inspector rather than a SQLite-only
PRAGMA, and the work is skipped when it is already there.
"""

from sqlalchemy import text, inspect


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
    if 'equity_holdings' not in _tables(db):
        print("  Table equity_holdings is not there yet, nothing to do")
        return False

    if 'is_settled' in _column_names(db, 'equity_holdings'):
        print("  Column is_settled already exists, skipping")
        return False

    if _is_postgres(db):
        column_type, true_value = 'BOOLEAN', 'TRUE'
    else:
        column_type, true_value = 'BOOLEAN', '1'

    db.session.execute(text(
        "ALTER TABLE equity_holdings ADD COLUMN is_settled %s "
        "NOT NULL DEFAULT %s" % (column_type, true_value)
    ))
    db.session.commit()
    print("  Added equity_holdings.is_settled, default TRUE")
    return True


def _backfill(db):
    """
    Every row that already exists is settled.

    The column default covers this on both databases, but a row written by a
    build that predates the default would carry NULL, and NULL here would be
    read as "not settled" - which would exempt a perfectly ordinary holding
    from the retirement sweep. Cheap to state explicitly.
    """
    try:
        result = db.session.execute(text(
            "UPDATE equity_holdings SET is_settled = :value "
            "WHERE is_settled IS NULL"
        ), {'value': True})
        db.session.commit()
        count = result.rowcount if result.rowcount is not None else 0
        if count:
            print("  Marked %d existing holding(s) as settled" % count)
    except Exception as exc:
        db.session.rollback()
        print("  Backfill skipped: %s" % exc)


def upgrade(db):
    print("Arming stop loss and target from the moment a buy fills")
    if _add_column(db):
        _backfill(db)
    print("Done")


def downgrade(db):
    """
    Leave the column in place.

    Dropping a column means rebuilding the table on SQLite, and older builds
    cannot drop one at all. A column nothing reads is harmless, so removing it
    is a manual, considered operation.
    """
    print("  equity_holdings.is_settled was left in place on purpose")
