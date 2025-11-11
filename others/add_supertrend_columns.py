import sqlite3

conn = sqlite3.connect('instance/algomirror.db')
c = conn.cursor()

# Get existing columns
c.execute('PRAGMA table_info(strategies)')
existing_cols = [row[1] for row in c.fetchall()]

print("Existing columns in strategies table:")
for col in existing_cols:
    print(f"  - {col}")

# Define columns to add
columns_to_add = [
    ('supertrend_exit_enabled', 'BOOLEAN DEFAULT 0'),
    ('supertrend_exit_type', 'VARCHAR(20)'),
    ('supertrend_period', 'INTEGER DEFAULT 7'),
    ('supertrend_multiplier', 'FLOAT DEFAULT 3.0'),
    ('supertrend_timeframe', 'VARCHAR(10) DEFAULT "5m"'),
    ('supertrend_exit_triggered', 'BOOLEAN DEFAULT 0')
]

print("\nAdding Supertrend columns:")
added_count = 0
for col_name, col_type in columns_to_add:
    if col_name not in existing_cols:
        try:
            c.execute(f'ALTER TABLE strategies ADD COLUMN {col_name} {col_type}')
            print(f"  + Added: {col_name}")
            added_count += 1
        except Exception as e:
            print(f"  x Error adding {col_name}: {e}")
    else:
        print(f"  - Already exists: {col_name}")

conn.commit()
conn.close()

print(f"\nDone! Added {added_count} new columns.")
print("Database upgrade complete!")
