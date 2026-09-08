"""
Migration: Record what actually happened to a resting GTT

A GTT is placed and then nothing in AlgoMirror ever asks the broker what became
of it. The split keeps broker_gtt_id and sits at PENDING for ever, which is why
GTT "does not work" from a user's point of view: placement succeeds and the
order never resolves.

OpenAlgo's gttorderbook defaults to active triggers only, but it accepts a
status field, and status="all" returns the terminal ones too, normalised by the
broker mappers to a small vocabulary: active, triggered, cancelled, expired,
rejected (plus transit on Fyers). That is enough to close the lifecycle without
guessing, so these columns store the answer rather than inferring it from a
trigger disappearing.

  gtt_status        the broker's normalised state for this trigger
  gtt_synced_at     when we last read the GTT book for it, so a stale row is
                    visible as stale rather than silently trusted
  gtt_triggered_at  when the trigger fired, which bounds the search window used
                    to find the child order the GTT released

Idempotency and portability follow 015: the SQLAlchemy inspector rather than
PRAGMA table_info, per-dialect type spellings because PostgreSQL has TIMESTAMP
and no DATETIME, and one commit per ALTER so a partial failure re-runs cleanly.
"""

from sqlalchemy import text, inspect


def _column_names(db, table_name):
    """Existing column names for a table, empty when the table is absent"""
    try:
        return {col['name'] for col in inspect(db.engine).get_columns(table_name)}
    except Exception:
        return set()


def _index_names(db, table_name):
    """Existing index names for a table, empty when the table is absent"""
    try:
        return {idx['name'] for idx in inspect(db.engine).get_indexes(table_name)}
    except Exception:
        return set()


def _add_columns(db, table_name, columns):
    """Add each missing column to a table, committing one at a time."""
    existing = _column_names(db, table_name)
    if not existing:
        print(f"  Table {table_name} not present, skipping (run 013 first)")
        return 0

    added = 0
    for column_name, column_sql in columns:
        if column_name in existing:
            print(f"  {table_name}.{column_name} already exists, skipping")
            continue

        try:
            db.session.execute(text(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"
            ))
            db.session.commit()
            print(f"  Added {table_name}.{column_name}")
            added += 1
        except Exception:
            db.session.rollback()
            raise

    return added


def _add_index(db, index_name, table_name, columns):
    """Create one index when it is missing."""
    if not _column_names(db, table_name):
        return 0
    if index_name in _index_names(db, table_name):
        print(f"  Index {index_name} already exists, skipping")
        return 0

    try:
        db.session.execute(text(
            f"CREATE INDEX {index_name} ON {table_name} ({columns})"
        ))
        db.session.commit()
        print(f"  Created index {index_name}")
        return 1
    except Exception:
        db.session.rollback()
        raise


def upgrade(db):
    """Add the GTT lifecycle columns and the index the reconciler sweeps on."""

    is_postgres = db.engine.dialect.name == 'postgresql'

    # PostgreSQL has TIMESTAMP and no DATETIME. Getting this wrong aborts the
    # whole migration on PostgreSQL while passing on SQLite.
    dt = 'TIMESTAMP' if is_postgres else 'DATETIME'

    total_added = 0

    total_added += _add_columns(db, 'equity_order_splits', [
        ('gtt_status', 'VARCHAR(20)'),
        ('gtt_synced_at', dt),
        ('gtt_triggered_at', dt),
    ])

    # The reconciler sweeps "splits that carry a GTT id and are not finished",
    # so index the GTT state it filters on. broker_gtt_id is already indexed
    # by 015.
    total_added += _add_index(
        db, 'ix_equity_order_splits_gtt_status', 'equity_order_splits', 'gtt_status'
    )

    print(f"\n016 complete: {total_added} schema changes applied")
    return total_added


def downgrade(db):
    """
    Deliberately leaves the columns in place.

    Dropping a column is destructive and, on the SQLite path, means rebuilding
    the table. These columns are nullable and unread by older code, so leaving
    them costs nothing and keeps a rollback safe. 015 takes the same position.
    """
    print("016 downgrade: columns left in place (nullable, unread by older code)")
    return 0
