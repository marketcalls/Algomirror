#!/usr/bin/env python3
"""
Admin password reset for AlgoMirror.

AlgoMirror is a single-user, self-hosted app with no email-based password
recovery - server access (this script) is the recovery mechanism, matching
the app's own zero-trust/self-hosted security model. Run directly on the
server, e.g.:

    sudo -u www-data /var/python/algomirror/venv/bin/python reset_password.py
"""

import getpass
import sys

from app import create_app, db
from app.auth.forms import validate_password_policy
from app.models import ActivityLog, User


class _FieldStub:
    """Minimal stand-in for a WTForms field so validate_password_policy can run outside a form."""
    def __init__(self, data):
        self.data = data


def prompt_new_password():
    while True:
        password = getpass.getpass("New password: ")
        password2 = getpass.getpass("Confirm password: ")
        if not password:
            print("[ERROR] Password cannot be empty.\n")
            continue
        if password != password2:
            print("[ERROR] Passwords do not match. Try again.\n")
            continue
        try:
            validate_password_policy(None, _FieldStub(password))
        except Exception as e:
            print(f"[ERROR] {e}\n")
            continue
        return password


def main():
    # start_background_services=False: this is a one-off CLI action, not the
    # running app - it has no business starting pollers/threads or pinging
    # the primary account (which can flip connection_status in the DB), and
    # doing so risks racing the real gunicorn process if it's still up.
    app = create_app(start_background_services=False)

    with app.app_context():
        users = User.query.all()
        if not users:
            print("[ERROR] No users found. Register the first admin via /auth/register instead.")
            sys.exit(1)

        if len(users) == 1:
            user = users[0]
        else:
            print("[INFO] Multiple users found:")
            for u in users:
                print(f"   {u.id}: {u.username} ({u.email})")
            choice = input("Enter user id to reset: ").strip()
            user = User.query.get(int(choice)) if choice.isdigit() else None
            if not user:
                print("[ERROR] No such user.")
                sys.exit(1)

        print(f"[INFO] Resetting password for: {user.username} ({user.email})")
        if input("Continue? [y/N] ").strip().lower() != 'y':
            print("[CANCELLED] Password reset cancelled.")
            sys.exit(0)

        password = prompt_new_password()

        user.set_password(password)
        db.session.add(ActivityLog(
            user_id=user.id,
            action='password_reset_cli',
            details={'username': user.username},
            status='success',
        ))
        db.session.commit()

        print(f"[SUCCESS] Password reset for {user.username}. Existing sessions have been logged out.")


if __name__ == '__main__':
    main()
