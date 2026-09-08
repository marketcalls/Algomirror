"""
Migration: Record broker activity that did not originate in AlgoMirror

The admin also has the broker's own terminal and mobile app, and a family
member may hold their own credentials. A trade placed that way lands in the same
account AlgoMirror manages and moves the same holding, and until now it was
absorbed in silence: the quantity changed and nothing said why. That matters
because the stop loss monitor sizes an exit against a quantity it believes it
understands.

equity_external_trades records those fills as notices. Nothing here corrects a
holding or places an order; it makes the gap visible and lets the admin decide.
acknowledged_at is how a notice stops being shown without being deleted, since
the audit trail is the point.

The unique index on (account_id, broker_trade_id) is load bearing rather than
tidy: a trade book is re-read on every poller sweep and returns the same rows
each time, so without it the same external fill would be recorded once a sweep.

Follows 015 and 016: SQLAlchemy inspector rather than PRAGMA, per-dialect type
spellings because PostgreSQL has TIMESTAMP and no DATETIME, and each statement
committed on its own so a partial failure re-runs cleanly.
"""

from sqlalchemy import text, inspect


def _has_table(db, table_name):
    try:
        return table_name in inspect(db.engine).get_table_names()
    except Exception:
        return False


def _index_names(db, table_name):
    try:
        return {idx['name'] for idx in inspect(db.engine).get_indexes(table_name)}
    except Exception:
        return set()


def _run(db, statement, description):
    try:
        db.session.execute(text(statement))
        db.session.commit()
        print(f"  {description}")
        return 1
    except Exception:
        db.session.rollback()
        raise


def upgrade(db):
    """Create equity_external_trades and its indexes."""
    is_postgres = db.engine.dialect.name == 'postgresql'

    # PostgreSQL has TIMESTAMP and no DATETIME. Getting this wrong aborts the
    # migration on PostgreSQL while passing on SQLite.
    dt = 'TIMESTAMP' if is_postgres else 'DATETIME'
    pk = 'SERIAL PRIMARY KEY' if is_postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'

    applied = 0

    if _has_table(db, 'equity_external_trades'):
        print("  Table equity_external_trades already exists, skipping create")
    else:
        applied += _run(db, f"""
            CREATE TABLE equity_external_trades (
                id {pk},
                user_id INTEGER NOT NULL REFERENCES users(id),
                account_id INTEGER NOT NULL REFERENCES trading_accounts(id),
                broker_trade_id VARCHAR(100),
                broker_order_id VARCHAR(100),
                symbol VARCHAR(50),
                exchange VARCHAR(20),
                side VARCHAR(10),
                quantity INTEGER DEFAULT 0,
                price FLOAT,
                executed_at {dt},
                first_seen_at {dt},
                acknowledged_at {dt},
                created_at {dt}
            )
        """, "Created table equity_external_trades")

    existing = _index_names(db, 'equity_external_trades')
    indexes = [
        ('ix_equity_external_trades_account_trade_uc',
         'CREATE UNIQUE INDEX ix_equity_external_trades_account_trade_uc '
         'ON equity_external_trades (account_id, broker_trade_id)'),
        ('ix_equity_external_trades_user_id',
         'CREATE INDEX ix_equity_external_trades_user_id '
         'ON equity_external_trades (user_id)'),
        ('ix_equity_external_trades_account_id',
         'CREATE INDEX ix_equity_external_trades_account_id '
         'ON equity_external_trades (account_id)'),
        ('ix_equity_external_trades_acknowledged_at',
         'CREATE INDEX ix_equity_external_trades_acknowledged_at '
         'ON equity_external_trades (acknowledged_at)'),
        ('ix_equity_external_trades_executed_at',
         'CREATE INDEX ix_equity_external_trades_executed_at '
         'ON equity_external_trades (executed_at)'),
        ('ix_equity_external_trades_symbol',
         'CREATE INDEX ix_equity_external_trades_symbol '
         'ON equity_external_trades (symbol)'),
        ('ix_equity_external_trades_broker_trade_id',
         'CREATE INDEX ix_equity_external_trades_broker_trade_id '
         'ON equity_external_trades (broker_trade_id)'),
        ('ix_equity_external_trades_broker_order_id',
         'CREATE INDEX ix_equity_external_trades_broker_order_id '
         'ON equity_external_trades (broker_order_id)'),
        ('ix_equity_external_trades_first_seen_at',
         'CREATE INDEX ix_equity_external_trades_first_seen_at '
         'ON equity_external_trades (first_seen_at)'),
    ]
    for name, statement in indexes:
        if name in existing:
            print(f"  Index {name} already exists, skipping")
            continue
        applied += _run(db, statement, f"Created index {name}")

    print(f"\n017 complete: {applied} schema changes applied")
    return applied


def downgrade(db):
    """
    Deliberately leaves the table in place.

    It holds a record of real broker activity that happened outside AlgoMirror,
    which is exactly the kind of thing a rollback should not throw away. It is
    unread by older code, so leaving it costs nothing.
    """
    print("017 downgrade: table left in place (holds an audit record)")
    return 0
