"""
Migration: intraday shorts, and the clock that must close them

Selling shares the account does not own is a short delivery as CNC - an
auction and a penalty rather than a trade - so the equity module refused it
outright. The admin asked for the other road: warn, then let it go through as
an intraday MIS order that is bought back before the close.

That obligation needs somewhere to live. It is the only position in this
module that MUST be acted on: a holding left alone does nothing, while a short
left open past the close is unlimited loss above the entry price, a forced
buy-back by the broker at whatever price is there, and a penalty on top.

This adds:

  equity_intraday_shorts                   new table
  equity_settings.intraday_squareoff_minute  new column, default 912 (15:12)
  equity_settings.intraday_cutoff_minute     new column, default 900 (15:00)
  equity_settings.intraday_monitor_enabled   new column, default TRUE
  equity_settings.intraday_last_run_at        new column
  equity_settings.intraday_last_error         new column

The times are minutes past midnight IST, so they sort and compare with no date
arithmetic. 15:12 is the default square-off - it has to stay in front of
whoever else would close the position, and OpenAlgo's sandbox squares MIS off
at 15:15 on NSE and BSE. 15:00 is the point after which a NEW short is refused,
because one opened at 15:18 has two minutes to work and must then be bought
back whatever the price.

`intraday_monitor_enabled` does not make shorts safe when it is off - it makes
them impossible. The placement path refuses to open a short when the thing that
closes it is not running.

Nothing existing is altered and no existing behaviour changes: until a short is
actually placed, the new table stays empty and the new columns are only read by
code that has nothing to act on. Re-running is safe - the table and every
column are detected with the SQLAlchemy inspector rather than a SQLite-only
PRAGMA, and existing ones are skipped.
"""

from sqlalchemy import text, inspect

SQUAREOFF_DEFAULT = 15 * 60 + 12   # 15:12 IST
CUTOFF_DEFAULT = 15 * 60           # 15:00 IST


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


def _create_table(db):
    if 'equity_intraday_shorts' in _tables(db):
        print("  Table equity_intraday_shorts already exists, skipping")
        return False

    if _is_postgres(db):
        pk = 'SERIAL PRIMARY KEY'
        stamp, day, flag = 'TIMESTAMP', 'DATE', 'BOOLEAN'
    else:
        pk = 'INTEGER PRIMARY KEY AUTOINCREMENT'
        stamp, day, flag = 'DATETIME', 'DATE', 'BOOLEAN'

    db.session.execute(text("""
        CREATE TABLE equity_intraday_shorts (
            id %s,
            user_id INTEGER NOT NULL REFERENCES users(id),
            account_id INTEGER NOT NULL REFERENCES trading_accounts(id),
            symbol VARCHAR(50) NOT NULL,
            exchange VARCHAR(20) NOT NULL DEFAULT 'NSE',
            quantity INTEGER NOT NULL DEFAULT 0,
            entry_price FLOAT,
            opening_order_id INTEGER REFERENCES equity_orders(id),
            opening_split_id INTEGER REFERENCES equity_order_splits(id),
            opened_at %s,
            trade_date %s NOT NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'OPEN',
            cover_reason VARCHAR(20),
            cover_quantity INTEGER,
            cover_claimed_at %s,
            cover_submitted_at %s,
            cover_completed_at %s,
            cover_broker_order_id VARCHAR(64),
            cover_order_id INTEGER REFERENCES equity_orders(id),
            cover_price FLOAT,
            cover_error TEXT,
            alerted_at %s,
            created_at %s,
            updated_at %s
        )
    """ % (pk, stamp, day, stamp, stamp, stamp, stamp, stamp, stamp)))

    for statement in (
        "CREATE INDEX ix_equity_intraday_shorts_user_id "
        "ON equity_intraday_shorts (user_id)",
        "CREATE INDEX ix_equity_intraday_shorts_account_id "
        "ON equity_intraday_shorts (account_id)",
        "CREATE INDEX ix_equity_intraday_shorts_symbol "
        "ON equity_intraday_shorts (symbol)",
        "CREATE INDEX ix_equity_intraday_shorts_status "
        "ON equity_intraday_shorts (status)",
        "CREATE INDEX ix_equity_intraday_shorts_opened_at "
        "ON equity_intraday_shorts (opened_at)",
        "CREATE INDEX ix_equity_intraday_shorts_trade_date "
        "ON equity_intraday_shorts (trade_date)",
        # The one the square-off monitor asks on every tick: this user's
        # still-open shorts for today.
        "CREATE INDEX ix_equity_short_open "
        "ON equity_intraday_shorts (user_id, status, trade_date)",
    ):
        db.session.execute(text(statement))

    db.session.commit()
    print("  Created equity_intraday_shorts with its indexes")
    return True


def _add_setting(db, name, sql_type, default_sql, description):
    if 'equity_settings' not in _tables(db):
        print("  Table equity_settings is not there yet, nothing to do")
        return False
    if name in _column_names(db, 'equity_settings'):
        print("  Column %s already exists, skipping" % name)
        return False

    clause = 'ALTER TABLE equity_settings ADD COLUMN %s %s' % (name, sql_type)
    if default_sql is not None:
        clause += ' NOT NULL DEFAULT %s' % default_sql
    db.session.execute(text(clause))
    db.session.commit()
    print("  Added equity_settings.%s - %s" % (name, description))
    return True


def upgrade(db):
    print("Adding intraday shorts and the square-off clock")

    _create_table(db)

    true_value = 'TRUE' if _is_postgres(db) else '1'
    _add_setting(db, 'intraday_squareoff_minute', 'INTEGER',
                 str(SQUAREOFF_DEFAULT), 'square off at 15:12 IST')
    _add_setting(db, 'intraday_cutoff_minute', 'INTEGER',
                 str(CUTOFF_DEFAULT), 'no new short after 15:00 IST')
    _add_setting(db, 'intraday_monitor_enabled', 'BOOLEAN',
                 true_value, 'the square-off monitor is on')
    _add_setting(db, 'intraday_last_run_at', 'DATETIME', None,
                 'square-off heartbeat')
    _add_setting(db, 'intraday_last_error', 'TEXT', None,
                 'why the square-off last stopped')

    print("Done")


def downgrade(db):
    """
    Leave both the table and the columns in place.

    Dropping a column means rebuilding the table on SQLite, and an empty table
    nothing reads is harmless. Removing either is a manual, considered
    operation - and a table that may hold a record of an unclosed short is the
    last thing that should be dropped automatically.
    """
    print("  equity_intraday_shorts and its settings were left in place on purpose")
