"""
Migration: Add risk exit reason columns to strategies table

These columns store the reason and timestamp when various risk exits are triggered:
- trailing_sl_exit_reason: Reason for TSL exit
- max_loss_triggered_at: Timestamp when max loss was triggered
- max_loss_exit_reason: Reason for max loss exit
- max_profit_triggered_at: Timestamp when max profit was triggered
- max_profit_exit_reason: Reason for max profit exit
"""

from sqlalchemy import text


def upgrade(db):
    """Add risk exit reason columns to strategies table"""

    # Check existing columns
    result = db.session.execute(text("PRAGMA table_info(strategies)"))
    columns = [row[1] for row in result.fetchall()]

    # Columns to add with their SQL definitions
    columns_to_add = [
        ('trailing_sl_exit_reason', 'VARCHAR(200)'),
        ('max_loss_triggered_at', 'DATETIME'),
        ('max_loss_exit_reason', 'VARCHAR(200)'),
        ('max_profit_triggered_at', 'DATETIME'),
        ('max_profit_exit_reason', 'VARCHAR(200)')
    ]

    added_count = 0
    for col_name, col_type in columns_to_add:
        if col_name not in columns:
            db.session.execute(text(
                f"ALTER TABLE strategies ADD COLUMN {col_name} {col_type}"
            ))
            print(f"  Added column: {col_name}")
            added_count += 1
        else:
            print(f"  Column {col_name} already exists, skipping")

    if added_count > 0:
        db.session.commit()
        print(f"  Added {added_count} new column(s)")
    else:
        print("  No new columns added")


def downgrade(db):
    """Remove risk exit reason columns (SQLite doesn't support DROP COLUMN easily)"""
    # SQLite doesn't support DROP COLUMN directly
    # Would need to recreate table - not implemented for simplicity
    pass
