"""
Migration: the third box on an investment note - "To Watch"

The note shipped with two boxes, thesis and risk. The owner asked for a third:
what he is watching for.

It is not the same as risk, which is why it is not folded into it. A risk is
what could go wrong. This is the number, the date or the trigger that would
tell you it IS going wrong - the quarterly figure, the order announcement, the
level being defended. One is a worry; the other is the thing you check.

Adds `to_watch` to both tables:

  equity_stock_notes.to_watch             the current note
  equity_stock_note_versions.to_watch     every earlier version

The version table gets it too, so a note saved from today onward keeps all
three boxes in its history rather than losing a third of itself the moment it
is edited.

Nothing existing is altered. Notes already written keep their thesis and risk
and simply have nothing in the new box. Re-running is safe: each column is
detected with the SQLAlchemy inspector and skipped when already present.
"""

from sqlalchemy import text, inspect

TABLES = ('equity_stock_notes', 'equity_stock_note_versions')


def _tables(db):
    try:
        return set(inspect(db.engine).get_table_names())
    except Exception:
        return set()


def _column_names(db, table_name):
    try:
        return {col['name'] for col in inspect(db.engine).get_columns(table_name)}
    except Exception:
        return set()


def upgrade(db):
    print("Adding the To Watch box to the investment note")

    existing_tables = _tables(db)
    added = 0

    for table_name in TABLES:
        if table_name not in existing_tables:
            print("  Table %s is not there yet, nothing to do" % table_name)
            continue
        if 'to_watch' in _column_names(db, table_name):
            print("  Column %s.to_watch already exists, skipping" % table_name)
            continue
        db.session.execute(text(
            'ALTER TABLE %s ADD COLUMN to_watch TEXT' % table_name
        ))
        db.session.commit()
        added += 1
        print("  Added %s.to_watch" % table_name)

    if added:
        print("  Existing notes keep their thesis and risk and start with this "
              "box empty")

    print("Done")


def downgrade(db):
    """
    Leave the columns in place.

    Dropping a column means rebuilding the table on SQLite, and this one holds
    the admin's own words. A price can be re-read from a broker; a note written
    in March exists in exactly one place, and a migration is not the thing that
    gets to delete it.
    """
    print("  The To Watch column was left in place on purpose")
