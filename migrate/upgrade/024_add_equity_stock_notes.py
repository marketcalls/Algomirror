"""
Migration: the investment note, and its history

Everything else in this module is a fact somebody else reported - a price, a
fill, a balance. This is the first table that holds what the ADMIN thinks: why
he bought, and what would make him wrong. No API returns that, and it is the
first thing forgotten.

Two tables, and the second one is the point:

  equity_stock_notes            the note as it stands now, one per stock
  equity_stock_note_versions    every earlier version, dated

The history is not bookkeeping. A thesis that is quietly rewritten to match
what happened is worth nothing; the value of writing one down is being able to
read, in September, what you actually believed in March - including the parts
that turned out wrong, which are the parts worth reading. So every save copies
the current note into the version table before changing it, and nothing in that
table is ever edited.

The note is keyed to the STOCK, not to a watch list entry and not to a holding.
A thesis is about the company: it has to survive the stock being dropped from a
list, sold out of entirely, and bought back months later. Attaching it to a
holding would have destroyed it at the exact moment it became interesting - when
you sold, and later wondered why you had ever bought.

symbol and exchange are repeated on the version rows rather than only reached
through the note, so a history stays readable if the note it belongs to is ever
removed. A history that depends on the thing it outlives is not a history.

Nothing existing is altered and no existing behaviour changes: until a note is
written, both tables stay empty and nothing reads them. Re-running is safe -
each table is detected with the SQLAlchemy inspector rather than a SQLite-only
PRAGMA, and an existing one is skipped.
"""

from sqlalchemy import text, inspect


def _is_postgres(db):
    return 'postgresql' in str(db.engine.url)


def _tables(db):
    try:
        return set(inspect(db.engine).get_table_names())
    except Exception:
        return set()


def _types(db):
    if _is_postgres(db):
        return 'SERIAL PRIMARY KEY', 'TIMESTAMP'
    return 'INTEGER PRIMARY KEY AUTOINCREMENT', 'DATETIME'


def _create_notes(db):
    if 'equity_stock_notes' in _tables(db):
        print("  Table equity_stock_notes already exists, skipping")
        return False

    pk, stamp = _types(db)
    db.session.execute(text("""
        CREATE TABLE equity_stock_notes (
            id %s,
            user_id INTEGER NOT NULL REFERENCES users(id),
            symbol VARCHAR(50) NOT NULL,
            exchange VARCHAR(20) NOT NULL DEFAULT 'NSE',
            thesis TEXT,
            risk TEXT,
            created_at %s,
            updated_at %s,
            CONSTRAINT uq_equity_stock_note UNIQUE (user_id, symbol, exchange)
        )
    """ % (pk, stamp, stamp)))

    for statement in (
        "CREATE INDEX ix_equity_stock_notes_user_id "
        "ON equity_stock_notes (user_id)",
        "CREATE INDEX ix_equity_stock_notes_symbol "
        "ON equity_stock_notes (symbol)",
        # The one every table draw asks: which of this person's stocks have a
        # note behind them.
        "CREATE INDEX ix_equity_note_lookup "
        "ON equity_stock_notes (user_id, symbol, exchange)",
    ):
        db.session.execute(text(statement))

    db.session.commit()
    print("  Created equity_stock_notes with its indexes")
    return True


def _create_versions(db):
    if 'equity_stock_note_versions' in _tables(db):
        print("  Table equity_stock_note_versions already exists, skipping")
        return False

    pk, stamp = _types(db)
    db.session.execute(text("""
        CREATE TABLE equity_stock_note_versions (
            id %s,
            note_id INTEGER REFERENCES equity_stock_notes(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            symbol VARCHAR(50) NOT NULL,
            exchange VARCHAR(20) NOT NULL DEFAULT 'NSE',
            thesis TEXT,
            risk TEXT,
            saved_at %s
        )
    """ % (pk, stamp)))

    for statement in (
        "CREATE INDEX ix_equity_stock_note_versions_note_id "
        "ON equity_stock_note_versions (note_id)",
        "CREATE INDEX ix_equity_stock_note_versions_user_id "
        "ON equity_stock_note_versions (user_id)",
        "CREATE INDEX ix_equity_stock_note_versions_symbol "
        "ON equity_stock_note_versions (symbol)",
        "CREATE INDEX ix_equity_stock_note_versions_saved_at "
        "ON equity_stock_note_versions (saved_at)",
        "CREATE INDEX ix_equity_note_version_lookup "
        "ON equity_stock_note_versions (user_id, symbol, exchange, saved_at)",
    ):
        db.session.execute(text(statement))

    db.session.commit()
    print("  Created equity_stock_note_versions with its indexes")
    return True


def upgrade(db):
    print("Adding the investment note and its history")
    _create_notes(db)
    _create_versions(db)
    print("Done")


def downgrade(db):
    """
    Leave both tables in place.

    They hold the only thing in this module that cannot be fetched again from
    anywhere: what the admin thought. A price can be re-read from a broker and
    a fill can be re-read from an order book. A thesis written in March exists
    in exactly one place, and a migration is not the thing that gets to delete
    it.
    """
    print("  The note tables were left in place on purpose")
