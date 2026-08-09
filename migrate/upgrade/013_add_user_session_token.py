"""
Migration: Add session_token column to users table

Rotated on every password change (User.set_password()) and embedded in
get_id()/checked in load_user(), so a password reset actually invalidates
any session/remember-me cookie issued before it - without this column,
resetting a password doesn't revoke access already granted to a stolen
or forgotten-but-still-open session.
"""

import secrets

from sqlalchemy import inspect, text


def upgrade(db):
    """Add session_token column to users table and backfill existing rows"""

    inspector = inspect(db.engine)
    columns = [col['name'] for col in inspector.get_columns('users')]

    if 'session_token' not in columns:
        db.session.execute(text(
            "ALTER TABLE users ADD COLUMN session_token VARCHAR(32)"
        ))
        db.session.commit()
        print("Added session_token column")
    else:
        print("Column already exists, skipping")

    # Backfill - each row gets its own random token, not one shared value
    result = db.session.execute(text("SELECT id FROM users WHERE session_token IS NULL"))
    user_ids = [row[0] for row in result.fetchall()]
    for user_id in user_ids:
        db.session.execute(
            text("UPDATE users SET session_token = :token WHERE id = :id"),
            {"token": secrets.token_hex(16), "id": user_id}
        )
    if user_ids:
        db.session.commit()
        print(f"Backfilled session_token for {len(user_ids)} existing user(s)")


def downgrade(db):
    """Remove session_token column from users table"""
    try:
        db.session.execute(text("ALTER TABLE users DROP COLUMN session_token"))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Could not drop session_token column (needs SQLite 3.35+/PostgreSQL): {e}")
