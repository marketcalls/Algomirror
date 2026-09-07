"""
Migration: Named watch lists, several per user

Until now a user had exactly one watch list: every row in equity_watchlist_items
belonged to the user directly, and a UNIQUE(user_id, symbol, exchange) rule made
sure a stock appeared only once. This migration introduces named lists, in the
manner of Screener, so a stock can sit in several lists at once with its own
target price and its own alert in each.

  equity_watchlists       new table, one row per named list
  equity_watchlist_items  new watchlist_id column, and the uniqueness rule moves
                          from (user_id, symbol, exchange) to
                          (watchlist_id, symbol, exchange)

Every existing user is given a list called "Core Watchlist", marked as their
default, and every existing watch list row is attached to it. Nothing is lost
and nothing changes on screen until the new selector is used.

Idempotency and portability. Existing tables and columns are detected with the
SQLAlchemy inspector rather than SQLite-only PRAGMAs, so re-running this adds
nothing twice. The uniqueness rule is replaced differently per dialect:
PostgreSQL can drop and add a constraint in place, while SQLite cannot alter a
table constraint at all and needs the table rebuilt around it. The rebuild is
skipped entirely when the old constraint is already gone.
"""

from sqlalchemy import text, inspect

DEFAULT_LIST_NAME = 'Core Watchlist'


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


def _table_sql(db, table_name):
    """The stored CREATE TABLE text. SQLite only; empty elsewhere."""
    try:
        row = db.session.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"
        ), {'n': table_name}).fetchone()
        return row[0] if row and row[0] else ''
    except Exception:
        return ''


# ---------------------------------------------------------------------------
# 1. the new table
# ---------------------------------------------------------------------------

def _create_watchlists_table(db):
    if 'equity_watchlists' in _tables(db):
        print("  Table equity_watchlists already exists, skipping")
        return

    if _is_postgres(db):
        create_sql = """
            CREATE TABLE equity_watchlists (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users (id),
                name VARCHAR(60) NOT NULL,
                is_default BOOLEAN NOT NULL DEFAULT FALSE,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                CONSTRAINT _user_watchlist_name_uc UNIQUE (user_id, name)
            )
        """
    else:
        create_sql = """
            CREATE TABLE equity_watchlists (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users (id),
                name VARCHAR(60) NOT NULL,
                is_default BOOLEAN NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME,
                updated_at DATETIME,
                CONSTRAINT _user_watchlist_name_uc UNIQUE (user_id, name)
            )
        """

    db.session.execute(text(create_sql))
    db.session.commit()
    print("  Created table equity_watchlists")

    for index_sql in [
        "CREATE INDEX IF NOT EXISTS ix_equity_watchlists_user_id "
        "ON equity_watchlists (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_equity_watchlists_is_default "
        "ON equity_watchlists (is_default)",
    ]:
        try:
            db.session.execute(text(index_sql))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            print(f"  Index skipped: {exc}")


# ---------------------------------------------------------------------------
# 2. a default list for every user
# ---------------------------------------------------------------------------

def _seed_default_lists(db):
    """Give every user without one a default list. Returns rows created."""
    rows = db.session.execute(text("""
        SELECT u.id FROM users u
        WHERE NOT EXISTS (
            SELECT 1 FROM equity_watchlists w WHERE w.user_id = u.id
        )
    """)).fetchall()

    created = 0
    for (user_id,) in rows:
        db.session.execute(text("""
            INSERT INTO equity_watchlists
                (user_id, name, is_default, sort_order, created_at, updated_at)
            VALUES
                (:uid, :name, :is_default, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """), {
            'uid': user_id,
            'name': DEFAULT_LIST_NAME,
            'is_default': True if _is_postgres(db) else 1,
        })
        created += 1

    if created:
        db.session.commit()
    print(f"  Default watch lists created: {created}")
    return created


# ---------------------------------------------------------------------------
# 3. the new column, and the rows attached to their list
# ---------------------------------------------------------------------------

def _add_watchlist_id_column(db):
    existing = _column_names(db, 'equity_watchlist_items')
    if not existing:
        print("  Table equity_watchlist_items not present, skipping (run 013 first)")
        return False
    if 'watchlist_id' in existing:
        print("  equity_watchlist_items.watchlist_id already exists, skipping")
        return True

    db.session.execute(text(
        "ALTER TABLE equity_watchlist_items ADD COLUMN watchlist_id INTEGER"
    ))
    db.session.commit()
    print("  Added equity_watchlist_items.watchlist_id")

    try:
        db.session.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_equity_watchlist_items_watchlist_id "
            "ON equity_watchlist_items (watchlist_id)"
        ))
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        print(f"  Index skipped: {exc}")
    return True


def _attach_orphan_items(db):
    """Point every row with no list at its owner's default list."""
    result = db.session.execute(text("""
        UPDATE equity_watchlist_items
        SET watchlist_id = (
            SELECT w.id FROM equity_watchlists w
            WHERE w.user_id = equity_watchlist_items.user_id
            ORDER BY w.is_default DESC, w.id
            LIMIT 1
        )
        WHERE watchlist_id IS NULL
    """))
    db.session.commit()
    moved = result.rowcount if result.rowcount is not None else 0
    print(f"  Watch list rows attached to a list: {moved}")
    return moved


# ---------------------------------------------------------------------------
# 4. the uniqueness rule moves from the user to the list
# ---------------------------------------------------------------------------

_ITEM_COLUMNS = (
    "id, user_id, watchlist_id, symbol, exchange, trade_nature_id, target_price, "
    "alert_price, price_alert_enabled, alert_direction, alert_triggered_at, "
    "alert_triggered_price, created_at, updated_at"
)

_ITEM_INDEXES = [
    ("ix_equity_watchlist_items_user_id", "user_id"),
    ("ix_equity_watchlist_items_watchlist_id", "watchlist_id"),
    ("ix_equity_watchlist_items_symbol", "symbol"),
    ("ix_equity_watchlist_items_exchange", "exchange"),
    ("ix_equity_watchlist_items_trade_nature_id", "trade_nature_id"),
    ("ix_equity_watchlist_items_price_alert_enabled", "price_alert_enabled"),
]


def _replace_unique_rule(db):
    if _is_postgres(db):
        for statement, label in [
            ("ALTER TABLE equity_watchlist_items "
             "DROP CONSTRAINT IF EXISTS _user_watchlist_symbol_uc",
             "dropped the old user-scoped rule"),
            ("ALTER TABLE equity_watchlist_items "
             "ALTER COLUMN watchlist_id SET NOT NULL",
             "watchlist_id is now required"),
            ("ALTER TABLE equity_watchlist_items "
             "ADD CONSTRAINT _watchlist_symbol_uc UNIQUE (watchlist_id, symbol, exchange)",
             "added the list-scoped rule"),
        ]:
            try:
                db.session.execute(text(statement))
                db.session.commit()
                print(f"  {label}")
            except Exception as exc:
                db.session.rollback()
                print(f"  Step skipped: {exc}")
        return

    # SQLite cannot alter a table constraint, so the table is rebuilt around it.
    current = _table_sql(db, 'equity_watchlist_items')
    if '_user_watchlist_symbol_uc' not in current:
        print("  Uniqueness rule already list-scoped, no rebuild needed")
        return

    print("  Rebuilding equity_watchlist_items to move the uniqueness rule...")
    db.session.execute(text("""
        CREATE TABLE equity_watchlist_items_new (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users (id),
            watchlist_id INTEGER NOT NULL REFERENCES equity_watchlists (id),
            symbol VARCHAR(50) NOT NULL,
            exchange VARCHAR(20) NOT NULL,
            trade_nature_id INTEGER REFERENCES equity_trade_natures (id),
            target_price FLOAT,
            alert_price FLOAT,
            price_alert_enabled BOOLEAN,
            alert_direction VARCHAR(10),
            alert_triggered_at DATETIME,
            alert_triggered_price FLOAT,
            created_at DATETIME,
            updated_at DATETIME,
            CONSTRAINT _watchlist_symbol_uc UNIQUE (watchlist_id, symbol, exchange)
        )
    """))
    db.session.execute(text(
        f"INSERT INTO equity_watchlist_items_new ({_ITEM_COLUMNS}) "
        f"SELECT {_ITEM_COLUMNS} FROM equity_watchlist_items "
        f"WHERE watchlist_id IS NOT NULL"
    ))
    db.session.execute(text("DROP TABLE equity_watchlist_items"))
    db.session.execute(text(
        "ALTER TABLE equity_watchlist_items_new RENAME TO equity_watchlist_items"
    ))
    db.session.commit()

    for index_name, column in _ITEM_INDEXES:
        try:
            db.session.execute(text(
                f"CREATE INDEX IF NOT EXISTS {index_name} "
                f"ON equity_watchlist_items ({column})"
            ))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            print(f"  Index {index_name} skipped: {exc}")

    print("  Rebuild complete, uniqueness is now per list")


# ---------------------------------------------------------------------------

def upgrade(db):
    print("Adding named watch lists")
    _create_watchlists_table(db)
    _seed_default_lists(db)
    if _add_watchlist_id_column(db):
        _attach_orphan_items(db)
        _replace_unique_rule(db)
    print("Done")


def downgrade(db):
    """
    Drop the watch lists table.

    equity_watchlist_items.watchlist_id is deliberately left in place. Removing
    it would mean rebuilding the table again, and on a database that has real
    watch list rows the column is the only record of which list each row came
    from. Older SQLite builds cannot drop a column at all. Removing it is a
    manual, considered operation, not something a downgrade should do on its own.
    """
    for index_name in [
        'ix_equity_watchlists_user_id',
        'ix_equity_watchlists_is_default',
        'ix_equity_watchlist_items_watchlist_id',
    ]:
        try:
            db.session.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            print(f"  Failed to drop {index_name}: {exc}")

    try:
        db.session.execute(text("DROP TABLE IF EXISTS equity_watchlists"))
        db.session.commit()
        print("  Dropped table equity_watchlists")
    except Exception as exc:
        db.session.rollback()
        print(f"  Failed to drop equity_watchlists: {exc}")

    print("  equity_watchlist_items.watchlist_id was left in place on purpose")
