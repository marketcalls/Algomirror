"""
Migration: keep a record of price alerts that have fired

Until now a watch list price alert existed only for as long as the Watch List
screen was open. It was evaluated inside the request that refreshed the prices,
so closing the screen stopped alerts being checked at all, and an alert that did
fire was announced to that one page and then forgotten.

Alerts now run in the background service instead, on the same schedule as the
stop loss and target monitor, so they fire whether or not a screen is open. That
needs somewhere to put a fired alert until a browser is there to show it:

  equity_alert_events   new table, one row per alert that actually fired

Each row carries a copy of the stock, the alert price, the direction and the
traded price, so the message still reads correctly if the watch list row is
edited afterwards. notified_at records that a browser has shown it, which is
what stops the same alert popping up on every poll. A later delivery channel
reads the same rows.

The row is owned by the watch list entry: ON DELETE CASCADE means removing a
stock from a watch list removes its alert history with it, so the confirmation
on that screen stays true.

Nothing existing is altered. This migration only adds a table, so re-running it
is safe: the table is detected with the SQLAlchemy inspector rather than a
SQLite-only PRAGMA, and creation is skipped when it is already there.
"""

from sqlalchemy import text, inspect


def _is_postgres(db):
    return 'postgresql' in str(db.engine.url)


def _tables(db):
    try:
        return set(inspect(db.engine).get_table_names())
    except Exception:
        return set()


def _create_alert_events_table(db):
    if 'equity_alert_events' in _tables(db):
        print("  Table equity_alert_events already exists, skipping")
        return False

    if _is_postgres(db):
        create_sql = """
            CREATE TABLE equity_alert_events (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users (id),
                watchlist_item_id INTEGER NOT NULL
                    REFERENCES equity_watchlist_items (id) ON DELETE CASCADE,
                symbol VARCHAR(50) NOT NULL,
                exchange VARCHAR(20) NOT NULL DEFAULT 'NSE',
                alert_price DOUBLE PRECISION,
                alert_direction VARCHAR(10),
                ltp DOUBLE PRECISION,
                message VARCHAR(255) NOT NULL,
                created_at TIMESTAMP,
                notified_at TIMESTAMP
            )
        """
    else:
        create_sql = """
            CREATE TABLE equity_alert_events (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users (id),
                watchlist_item_id INTEGER NOT NULL
                    REFERENCES equity_watchlist_items (id) ON DELETE CASCADE,
                symbol VARCHAR(50) NOT NULL,
                exchange VARCHAR(20) NOT NULL DEFAULT 'NSE',
                alert_price FLOAT,
                alert_direction VARCHAR(10),
                ltp FLOAT,
                message VARCHAR(255) NOT NULL,
                created_at DATETIME,
                notified_at DATETIME
            )
        """

    db.session.execute(text(create_sql))
    db.session.commit()
    print("  Created table equity_alert_events")
    return True


def _create_indexes(db):
    for index_sql in [
        "CREATE INDEX IF NOT EXISTS ix_equity_alert_events_user_id "
        "ON equity_alert_events (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_equity_alert_events_watchlist_item_id "
        "ON equity_alert_events (watchlist_item_id)",
        "CREATE INDEX IF NOT EXISTS ix_equity_alert_events_created_at "
        "ON equity_alert_events (created_at)",
        # The pending query reads this one on every poll from every open screen.
        "CREATE INDEX IF NOT EXISTS ix_equity_alert_events_notified_at "
        "ON equity_alert_events (notified_at)",
    ]:
        try:
            db.session.execute(text(index_sql))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            print(f"  Index not created: {exc}")


def upgrade(db):
    print("Adding the fired price alert record")
    _create_alert_events_table(db)
    _create_indexes(db)
    print("Done")


def downgrade(db):
    """Drop the table. No other table is touched, so nothing else is affected."""
    for index_name in [
        'ix_equity_alert_events_user_id',
        'ix_equity_alert_events_watchlist_item_id',
        'ix_equity_alert_events_created_at',
        'ix_equity_alert_events_notified_at',
    ]:
        try:
            db.session.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            print(f"  Failed to drop {index_name}: {exc}")

    try:
        db.session.execute(text("DROP TABLE IF EXISTS equity_alert_events"))
        db.session.commit()
        print("  Dropped table equity_alert_events")
    except Exception as exc:
        db.session.rollback()
        print(f"  Failed to drop equity_alert_events: {exc}")
