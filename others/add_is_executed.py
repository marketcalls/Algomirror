"""
Add is_executed column to strategy_legs table
"""
from app import create_app, db
from sqlalchemy import text

app = create_app()

with app.app_context():
    try:
        # Check if column exists
        result = db.session.execute(text("PRAGMA table_info(strategy_legs)"))
        columns = [row[1] for row in result]

        if 'is_executed' not in columns:
            print("Adding is_executed column to strategy_legs table...")
            db.session.execute(text("ALTER TABLE strategy_legs ADD COLUMN is_executed BOOLEAN DEFAULT 0"))
            db.session.commit()
            print("Column added successfully!")
        else:
            print("Column already exists!")

    except Exception as e:
        print(f"Error: {e}")
        db.session.rollback()
