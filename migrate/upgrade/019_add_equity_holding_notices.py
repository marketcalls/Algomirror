"""
Migration: notice the admin when a holding changes for a reason AlgoMirror
did not cause

A buy or sell placed straight at the broker - from the broker's own app in an
emergency, most obviously - never passes through this application. Until now
those shares were absorbed in silence the next time holdings were read. That
silence is the problem: a stop loss could be left armed on a quantity the admin
no longer recognised, and nothing on any screen would say so.

Two things are added:

  equity_holdings.external_quantity   new column, empty to begin with
  equity_holding_notices              new table

external_quantity is the broker's share count minus AlgoMirror's own net filled
position for that account and stock. It is added empty, and empty means "not
measured yet": the first measurement writes the figure silently and only a later
change to it raises anything. It is normally a fixed number - shares
bought elsewhere, or held before AlgoMirror existed - and its value is not
interesting on its own. What matters is when it CHANGES, because a change means
shares moved at the broker with no order from here. The existing figure is
absorbed silently the first time each row is measured, so no notice is raised
for history; every later change raises one.

A notice is delivered through the same feed as watch list price alerts: the
same pop-up, the same navigation badge, the same log. The admin does not have to
learn a second place to look.

The notice table carries no foreign key to equity_holdings on purpose. A notice
is a statement about a moment and has to keep reading correctly after the
holding row is retired, exactly as a fired price alert keeps reading correctly
after its watch list row is edited.

Nothing existing is altered. The column is added empty, so every existing row
keeps working exactly as before, and the table is only created when it is
absent, so re-running this migration is safe.
"""

from sqlalchemy import text, inspect


def _is_postgres(db):
    return 'postgresql' in str(db.engine.url)


def _tables(db):
    try:
        return set(inspect(db.engine).get_table_names())
    except Exception:
        return set()


def _columns(db, table):
    try:
        return {column['name'] for column in inspect(db.engine).get_columns(table)}
    except Exception:
        return set()


def _add_external_quantity(db):
    if 'equity_holdings' not in _tables(db):
        print("  Table equity_holdings is missing, skipping the column")
        return False

    if 'external_quantity' in _columns(db, 'equity_holdings'):
        print("  Column equity_holdings.external_quantity already exists, skipping")
        return False

    db.session.execute(text(
        "ALTER TABLE equity_holdings "
        "ADD COLUMN external_quantity INTEGER"
    ))
    db.session.commit()
    print("  Added column equity_holdings.external_quantity")
    return True


def _create_notices_table(db):
    if 'equity_holding_notices' in _tables(db):
        print("  Table equity_holding_notices already exists, skipping")
        return False

    if _is_postgres(db):
        create_sql = """
            CREATE TABLE equity_holding_notices (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users (id),
                account_id INTEGER NOT NULL REFERENCES trading_accounts (id),
                symbol VARCHAR(50) NOT NULL,
                exchange VARCHAR(20) NOT NULL DEFAULT 'NSE',
                kind VARCHAR(20) NOT NULL,
                quantity_before INTEGER,
                quantity_after INTEGER,
                quantity_delta INTEGER,
                had_armed_level BOOLEAN NOT NULL DEFAULT FALSE,
                message VARCHAR(255) NOT NULL,
                created_at TIMESTAMP,
                notified_at TIMESTAMP,
                seen_at TIMESTAMP
            )
        """
    else:
        create_sql = """
            CREATE TABLE equity_holding_notices (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users (id),
                account_id INTEGER NOT NULL REFERENCES trading_accounts (id),
                symbol VARCHAR(50) NOT NULL,
                exchange VARCHAR(20) NOT NULL DEFAULT 'NSE',
                kind VARCHAR(20) NOT NULL,
                quantity_before INTEGER,
                quantity_after INTEGER,
                quantity_delta INTEGER,
                had_armed_level BOOLEAN NOT NULL DEFAULT 0,
                message VARCHAR(255) NOT NULL,
                created_at DATETIME,
                notified_at DATETIME,
                seen_at DATETIME
            )
        """

    db.session.execute(text(create_sql))
    db.session.commit()
    print("  Created table equity_holding_notices")
    return True


def _create_indexes(db):
    for index_sql in [
        "CREATE INDEX IF NOT EXISTS ix_equity_holding_notices_user_id "
        "ON equity_holding_notices (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_equity_holding_notices_account_id "
        "ON equity_holding_notices (account_id)",
        "CREATE INDEX IF NOT EXISTS ix_equity_holding_notices_symbol "
        "ON equity_holding_notices (symbol)",
        "CREATE INDEX IF NOT EXISTS ix_equity_holding_notices_kind "
        "ON equity_holding_notices (kind)",
        "CREATE INDEX IF NOT EXISTS ix_equity_holding_notices_created_at "
        "ON equity_holding_notices (created_at)",
        # Read on every poll from every open screen, the same way the alert
        # event table's notified_at index is read.
        "CREATE INDEX IF NOT EXISTS ix_equity_holding_notices_notified_at "
        "ON equity_holding_notices (notified_at)",
        "CREATE INDEX IF NOT EXISTS ix_equity_holding_notices_seen_at "
        "ON equity_holding_notices (seen_at)",
    ]:
        try:
            db.session.execute(text(index_sql))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            print(f"  Index not created: {exc}")


def upgrade(db):
    print("Adding holding notices and the external share count")
    _add_external_quantity(db)
    _create_notices_table(db)
    _create_indexes(db)
    print("Done")


def downgrade(db):
    """
    Drop the notice table and its indexes.

    The external_quantity column is deliberately left in place. SQLite cannot
    drop a column without rebuilding the whole table, and rebuilding
    equity_holdings to remove one unused integer would put every armed stop
    loss and every exit claim at risk for no benefit. An unused column costs
    nothing.
    """
    for index_name in [
        'ix_equity_holding_notices_user_id',
        'ix_equity_holding_notices_account_id',
        'ix_equity_holding_notices_symbol',
        'ix_equity_holding_notices_kind',
        'ix_equity_holding_notices_created_at',
        'ix_equity_holding_notices_notified_at',
        'ix_equity_holding_notices_seen_at',
    ]:
        try:
            db.session.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            print(f"  Failed to drop {index_name}: {exc}")

    try:
        db.session.execute(text("DROP TABLE IF EXISTS equity_holding_notices"))
        db.session.commit()
        print("  Dropped table equity_holding_notices")
    except Exception as exc:
        db.session.rollback()
        print(f"  Failed to drop equity_holding_notices: {exc}")
