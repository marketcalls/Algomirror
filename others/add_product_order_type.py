"""
Add product_order_type column to strategies table
"""
from app import create_app, db
from sqlalchemy import text

app = create_app()

with app.app_context():
    try:
        # Check if column exists
        result = db.session.execute(text("PRAGMA table_info(strategies)"))
        columns = [row[1] for row in result]

        if 'product_order_type' not in columns:
            print("Adding product_order_type column to strategies table...")
            db.session.execute(text("ALTER TABLE strategies ADD COLUMN product_order_type VARCHAR(10) DEFAULT 'MIS'"))
            db.session.commit()
            print("Column added successfully!")
        else:
            print("Column already exists!")

    except Exception as e:
        print(f"Error: {e}")
        db.session.rollback()
