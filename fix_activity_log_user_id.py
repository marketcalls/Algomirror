#!/usr/bin/env python3
"""
Migration script to make user_id nullable in activity_logs table.
This allows logging failed login attempts before user authentication.
"""
import sqlite3
import os
import sys
from datetime import datetime

# Database path
DB_PATH = os.environ.get('DATABASE_URL', 'sqlite:////var/python/algomirror/instance/algomirror.db')
if DB_PATH.startswith('sqlite:///'):
    DB_PATH = DB_PATH.replace('sqlite:///', '')

print(f"Migrating database at: {DB_PATH}")

if not os.path.exists(DB_PATH):
    print(f"ERROR: Database not found at {DB_PATH}")
    sys.exit(1)

# Backup the database first
backup_path = f"{DB_PATH}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
print(f"Creating backup at: {backup_path}")
import shutil
shutil.copy2(DB_PATH, backup_path)
print("Backup created successfully")

# Connect to database
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

try:
    print("\n=== Checking current schema ===")
    cursor.execute("PRAGMA table_info(activity_logs)")
    columns = cursor.fetchall()

    print("Current activity_logs schema:")
    for col in columns:
        print(f"  {col[1]}: {col[2]} (nullable={col[3] == 0})")

    # SQLite doesn't support ALTER COLUMN directly, so we need to:
    # 1. Create new table with correct schema
    # 2. Copy data
    # 3. Drop old table
    # 4. Rename new table

    print("\n=== Creating new table with nullable user_id ===")
    cursor.execute("""
        CREATE TABLE activity_logs_new (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            account_id INTEGER,
            action VARCHAR(100) NOT NULL,
            details TEXT,
            ip_address VARCHAR(45),
            user_agent VARCHAR(500),
            status VARCHAR(50) DEFAULT 'success',
            error_message TEXT,
            created_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (account_id) REFERENCES trading_accounts(id)
        )
    """)

    print("=== Copying data from old table ===")
    # Check if there's any data to copy
    cursor.execute("SELECT COUNT(*) FROM activity_logs")
    count = cursor.fetchone()[0]
    print(f"Found {count} records to migrate")

    if count > 0:
        cursor.execute("""
            INSERT INTO activity_logs_new
            SELECT * FROM activity_logs
        """)
        print(f"Copied {count} records successfully")

    print("=== Dropping old table ===")
    cursor.execute("DROP TABLE activity_logs")

    print("=== Renaming new table ===")
    cursor.execute("ALTER TABLE activity_logs_new RENAME TO activity_logs")

    print("=== Creating indexes ===")
    cursor.execute("CREATE INDEX IF NOT EXISTS ix_activity_logs_created_at ON activity_logs(created_at)")

    # Commit changes
    conn.commit()

    print("\n=== Verifying new schema ===")
    cursor.execute("PRAGMA table_info(activity_logs)")
    columns = cursor.fetchall()

    print("New activity_logs schema:")
    for col in columns:
        print(f"  {col[1]}: {col[2]} (nullable={col[3] == 0})")

    print("\n✅ Migration completed successfully!")
    print(f"Database backup saved at: {backup_path}")

except Exception as e:
    print(f"\n❌ Migration failed: {e}")
    conn.rollback()
    print(f"Database backup available at: {backup_path}")
    sys.exit(1)
finally:
    conn.close()

print("\nNext steps:")
print("1. Copy this script to the server")
print("2. Run: sudo systemctl stop algomirror")
print("3. Run: sudo -u www-data python3 fix_activity_log_user_id.py")
print("4. Run: sudo systemctl start algomirror")
