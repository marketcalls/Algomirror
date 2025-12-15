"""
Migration: Add performance indexes for frequently queried columns

This migration adds indexes to improve query performance, especially for:
- Dashboard loading (StrategyExecution queries)
- Risk monitoring (Strategy + StrategyExecution queries)
- Position tracking
"""

from sqlalchemy import text


def upgrade(db):
    """Add performance indexes"""

    # Helper to check if index exists
    def index_exists(index_name):
        result = db.session.execute(text(
            f"SELECT name FROM sqlite_master WHERE type='index' AND name='{index_name}'"
        ))
        return result.fetchone() is not None

    indexes_to_create = [
        # StrategyExecution indexes - most critical for performance
        ('ix_strategy_executions_strategy_status',
         'CREATE INDEX ix_strategy_executions_strategy_status ON strategy_executions(strategy_id, status)'),
        ('ix_strategy_executions_account_status',
         'CREATE INDEX ix_strategy_executions_account_status ON strategy_executions(account_id, status)'),
        ('ix_strategy_executions_status',
         'CREATE INDEX ix_strategy_executions_status ON strategy_executions(status)'),
        ('ix_strategy_executions_created_at',
         'CREATE INDEX ix_strategy_executions_created_at ON strategy_executions(created_at)'),

        # Strategy indexes
        ('ix_strategies_user_active',
         'CREATE INDEX ix_strategies_user_active ON strategies(user_id, is_active)'),
        ('ix_strategies_risk_monitoring',
         'CREATE INDEX ix_strategies_risk_monitoring ON strategies(is_active, risk_monitoring_enabled)'),

        # TradingAccount indexes
        ('ix_trading_accounts_user_active',
         'CREATE INDEX ix_trading_accounts_user_active ON trading_accounts(user_id, is_active)'),
    ]

    # Optional indexes for risk_events if table exists
    risk_event_indexes = [
        ('ix_risk_events_strategy',
         'CREATE INDEX ix_risk_events_strategy ON risk_events(strategy_id)'),
        ('ix_risk_events_triggered_at',
         'CREATE INDEX ix_risk_events_triggered_at ON risk_events(triggered_at)'),
    ]

    created_count = 0
    skipped_count = 0

    for index_name, create_sql in indexes_to_create:
        if index_exists(index_name):
            print(f"  Index {index_name} already exists, skipping")
            skipped_count += 1
        else:
            try:
                db.session.execute(text(create_sql))
                print(f"  Created index {index_name}")
                created_count += 1
            except Exception as e:
                print(f"  Failed to create {index_name}: {e}")

    # Try risk_events indexes (table may not exist)
    for index_name, create_sql in risk_event_indexes:
        if index_exists(index_name):
            print(f"  Index {index_name} already exists, skipping")
            skipped_count += 1
        else:
            try:
                db.session.execute(text(create_sql))
                print(f"  Created index {index_name}")
                created_count += 1
            except Exception as e:
                # Table might not exist, that's OK
                print(f"  Skipped {index_name} (table may not exist)")

    db.session.commit()
    print(f"\nIndexes: {created_count} created, {skipped_count} skipped")


def downgrade(db):
    """Remove performance indexes"""

    indexes_to_drop = [
        'ix_strategy_executions_strategy_status',
        'ix_strategy_executions_account_status',
        'ix_strategy_executions_status',
        'ix_strategy_executions_created_at',
        'ix_strategies_user_active',
        'ix_strategies_risk_monitoring',
        'ix_trading_accounts_user_active',
        'ix_risk_events_strategy',
        'ix_risk_events_triggered_at',
    ]

    for index_name in indexes_to_drop:
        try:
            db.session.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
            print(f"  Dropped index {index_name}")
        except Exception as e:
            print(f"  Failed to drop {index_name}: {e}")

    db.session.commit()
