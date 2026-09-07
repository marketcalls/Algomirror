"""
Migration: remember that a sold stock has been put back on a watch list

THE RULE THIS SERVES
--------------------
One stock, one home. From 6 September a stock you HOLD does not also sit on a
watch list: its stop loss, target, exit mode, trade nature and note are changed
on Holdings, one stock at a time. When the last share is sold the stock goes
back to a watch list.

A stock that WAS on a watch list needs nothing stored. Its row is only hidden
while the shares are held, so it comes back by itself with its list, its trade
nature and its target price exactly as they were. That is a filter, not a
delete, and it is why most of this feature carries no state at all.

WHY THIS COLUMN EXISTS
----------------------
A stock that was NEVER on a watch list has no row to unhide, so one has to be
created when it is sold. Without a marker that creation would be recomputed on
every screen refresh - and a row the owner then deleted would reappear seconds
later, which would make the watch list impossible to curate.

  equity_holdings.returned_to_watchlist   BOOLEAN NOT NULL DEFAULT 0

Set to 1 when a row is created for a sold-out stock. Reset to 0 the moment
shares are held again, so the next sale returns it once more.

WHAT THIS DOES NOT DO
---------------------
Nothing existing is altered. No watch list row is created, moved or deleted by
this migration. Every holding starts at 0, which means "not returned yet" - so
a stock you hold today and sell tomorrow is returned then, and a stock already
at zero is returned the first time you open the watch list.

Re-running is safe: the column is detected with the SQLAlchemy inspector and
skipped when it is already there.
"""

from sqlalchemy import text, inspect

TABLE = 'equity_holdings'
COLUMN = 'returned_to_watchlist'


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
    print("Adding the returned-to-watch-list marker to holdings")

    if TABLE not in _tables(db):
        print("  Table %s is not there yet, nothing to do" % TABLE)
        print("Done")
        return

    if COLUMN in _column_names(db, TABLE):
        print("  Column %s.%s already exists, skipping" % (TABLE, COLUMN))
        print("Done")
        return

    db.session.execute(text(
        'ALTER TABLE %s ADD COLUMN %s BOOLEAN NOT NULL DEFAULT 0'
        % (TABLE, COLUMN)
    ))
    db.session.commit()
    print("  Added %s.%s" % (TABLE, COLUMN))
    print("  Every holding starts at 0, meaning 'not returned yet'. Nothing")
    print("  on any watch list has been changed by this migration.")
    print("Done")


def downgrade(db):
    """
    Leave the column in place.

    Dropping a column means rebuilding the table on SQLite, and this table
    carries every stop loss and target in the application. A boolean that is
    simply never read again costs nothing; rebuilding equity_holdings to remove
    it is a risk taken for no gain.
    """
    print("  The returned_to_watchlist column was left in place on purpose")
