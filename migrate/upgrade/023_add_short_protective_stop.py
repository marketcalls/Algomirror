"""
Migration: the protective stop that rests at the broker

A short's stop loss was going to live where every other AlgoMirror level lives:
in a monitor on this machine. The owner asked the right question - what happens
if the power or the wifi goes - and the answer was bad. The square-off survives
an outage, because the monitor acts on everything still open past its time and
the broker's own cut-off sits behind that. The STOP did not survive it. An
outage from 13:00 left a short with no stop at all until the broker closed it
near the end of the session.

So a resting SL-M buy is placed at the broker the moment a short fills,
triggered at the stop the admin typed. It sits in the exchange's stop-loss book
and fires whether or not this machine is switched on.

SL-M rather than SL: an SL becomes a limit order once triggered and can sit
unfilled while the price runs away from it, which on a short is the exact
scenario the stop exists for.

This adds five columns to equity_intraday_shorts:

  stop_trigger_price     where it fires
  stop_order_id          our order behind it
  stop_broker_order_id   the broker's id - what makes a CANCEL possible
  stop_status            NONE / RESTING / CANCELLED / TRIGGERED / FAILED
  stop_error             why it could not be placed, when it could not

The broker order id is not optional bookkeeping. Two things can now close the
same short - the resting order and AlgoMirror's own square-off - and buying a
short back twice leaves a LONG position nobody asked for. Every AlgoMirror
buy-back therefore cancels the resting order first and confirms the cancel
before placing anything, which is impossible without that id.

Re-running is safe: every column is detected with the SQLAlchemy inspector and
skipped when already present.
"""

from sqlalchemy import text, inspect

COLUMNS = (
    ('stop_trigger_price', 'FLOAT', None, 'where the protective stop fires'),
    ('stop_order_id', 'INTEGER', None, 'our order behind the resting stop'),
    ('stop_broker_order_id', 'VARCHAR(64)', None, "the broker's id, needed to cancel it"),
    ('stop_status', 'VARCHAR(16)', "'NONE'", 'NONE / RESTING / CANCELLED / TRIGGERED / FAILED'),
    ('stop_error', 'TEXT', None, 'why the stop could not be placed'),
)


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


def upgrade(db):
    print("Adding the protective stop that rests at the broker")

    if 'equity_intraday_shorts' not in _tables(db):
        print("  Step 48 has not been applied yet, nothing to do")
        return

    existing = _column_names(db, 'equity_intraday_shorts')
    added = 0

    for name, sql_type, default_sql, description in COLUMNS:
        if name in existing:
            print("  Column %s already exists, skipping" % name)
            continue
        clause = 'ALTER TABLE equity_intraday_shorts ADD COLUMN %s %s' % (name, sql_type)
        if default_sql is not None:
            clause += ' NOT NULL DEFAULT %s' % default_sql
        db.session.execute(text(clause))
        db.session.commit()
        added += 1
        print("  Added %s - %s" % (name, description))

    if added:
        # Any row written before this migration has no protective order behind
        # it. Said explicitly rather than left to the column default, because
        # NULL here would be read as "unknown", and unknown about a stop is the
        # sort of thing that gets treated as "probably fine".
        try:
            result = db.session.execute(text(
                "UPDATE equity_intraday_shorts SET stop_status = 'NONE' "
                "WHERE stop_status IS NULL"
            ))
            db.session.commit()
            count = result.rowcount if result.rowcount is not None else 0
            if count:
                print("  Marked %d existing short(s) as having no resting stop" % count)
        except Exception as exc:
            db.session.rollback()
            print("  Backfill skipped: %s" % exc)

    print("Done")


def downgrade(db):
    """
    Leave the columns in place.

    Dropping a column means rebuilding the table on SQLite, and these may hold
    the broker order id of a stop that is still resting at an exchange. That is
    not something to remove automatically.
    """
    print("  The protective stop columns were left in place on purpose")
