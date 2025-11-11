"""Add missing trailing_sl_triggered column"""
import sqlite3

db_path = 'instance/algomirror.db'

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

try:
    # Check if column exists
    cursor.execute("PRAGMA table_info(strategy_executions)")
    columns = [row[1] for row in cursor.fetchall()]

    if 'trailing_sl_triggered' not in columns:
        cursor.execute("ALTER TABLE strategy_executions ADD COLUMN trailing_sl_triggered FLOAT")
        conn.commit()
        print("[SUCCESS] Added trailing_sl_triggered column")
    else:
        print("[INFO] Column already exists")

except Exception as e:
    print(f"[ERROR] {e}")
    conn.rollback()
finally:
    conn.close()
