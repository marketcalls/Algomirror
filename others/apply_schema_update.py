"""
Apply Schema Updates for Risk Monitoring
Adds missing columns to strategies and strategy_executions tables
"""
import sqlite3
import os

# Database path
db_path = os.path.join('instance', 'algomirror.db')

print(f"Updating database schema: {db_path}")

try:
    # Connect to database
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check if columns already exist
    cursor.execute("PRAGMA table_info(strategies)")
    existing_columns = [row[1] for row in cursor.fetchall()]

    print(f"\nExisting columns in strategies table: {len(existing_columns)}")

    # Add columns to strategies table if they don't exist
    columns_to_add = {
        'risk_monitoring_enabled': 'INTEGER DEFAULT 1',
        'risk_check_interval': 'INTEGER DEFAULT 1',
        'auto_exit_on_max_loss': 'INTEGER DEFAULT 1',
        'auto_exit_on_max_profit': 'INTEGER DEFAULT 0',
        'trailing_sl_type': 'VARCHAR(20)',
    }

    added_count = 0
    for column_name, column_type in columns_to_add.items():
        if column_name not in existing_columns:
            try:
                cursor.execute(f"ALTER TABLE strategies ADD COLUMN {column_name} {column_type}")
                print(f"[SUCCESS] Added column: strategies.{column_name}")
                added_count += 1
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Failed to add {column_name}: {e}")
        else:
            print(f"  Column already exists: strategies.{column_name}")

    # Check strategy_executions table
    cursor.execute("PRAGMA table_info(strategy_executions)")
    existing_exec_columns = [row[1] for row in cursor.fetchall()]

    print(f"\nExisting columns in strategy_executions table: {len(existing_exec_columns)}")

    # Add columns to strategy_executions table if they don't exist
    exec_columns_to_add = {
        'last_price': 'FLOAT',
        'last_price_updated': 'DATETIME',
        'websocket_subscribed': 'INTEGER DEFAULT 0',
        'risk_exit_triggered': 'INTEGER DEFAULT 0',
    }

    for column_name, column_type in exec_columns_to_add.items():
        if column_name not in existing_exec_columns:
            try:
                cursor.execute(f"ALTER TABLE strategy_executions ADD COLUMN {column_name} {column_type}")
                print(f"[SUCCESS] Added column: strategy_executions.{column_name}")
                added_count += 1
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Failed to add {column_name}: {e}")
        else:
            print(f"  Column already exists: strategy_executions.{column_name}")

    # Commit changes
    conn.commit()

    print(f"\n[SUCCESS] Schema update complete! Added {added_count} new columns.")

    # Verify changes
    cursor.execute("PRAGMA table_info(strategies)")
    final_columns = [row[1] for row in cursor.fetchall()]
    print(f"\nFinal column count in strategies: {len(final_columns)}")

    cursor.execute("PRAGMA table_info(strategy_executions)")
    final_exec_columns = [row[1] for row in cursor.fetchall()]
    print(f"Final column count in strategy_executions: {len(final_exec_columns)}")

except Exception as e:
    print(f"\n[ERROR] Error updating schema: {e}")
    if conn:
        conn.rollback()
finally:
    if conn:
        conn.close()

print("\nDone! You can now restart the application.")
