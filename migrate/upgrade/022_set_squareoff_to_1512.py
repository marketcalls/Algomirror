"""
Migration: move the square-off from 15:20 to 15:12

Step 48 shipped with 15:20, which is the time originally asked for. It was
then checked against what else would close the position, and OpenAlgo's own
sandbox squares MIS off at 15:15 on NSE and BSE - so at 15:20 the sandbox wins
every time, our square-off never runs, and the mechanism could not be proven
in testing. 15:12 was agreed instead.

Step 48 had already been applied by then, so its column carries 920. This
moves it to 912.

Deliberately narrow. It changes the value ONLY where:

  - it is still exactly 920, the number step 48 wrote, and
  - no intraday short has ever been recorded

Both conditions together mean nobody has used the setting yet, so nothing is
being overridden. A value the admin has since chosen for themselves - anything
that is not 920 - is left exactly as it is, and once a short exists the setting
has been in real use and is not ours to change.

Nothing else is touched. Re-running is safe: the second run finds no row at 920
and does nothing.
"""

from sqlalchemy import text, inspect

OLD_DEFAULT = 15 * 60 + 20   # 15:20, what step 48 wrote
NEW_DEFAULT = 15 * 60 + 12   # 15:12, agreed after checking the sandbox


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
    print("Moving the intraday square-off to 15:12")

    tables = _tables(db)
    if 'equity_settings' not in tables:
        print("  No equity_settings table yet, nothing to do")
        return
    if 'intraday_squareoff_minute' not in _column_names(db, 'equity_settings'):
        print("  Step 48 has not been applied yet, nothing to do")
        return

    # A setting that has been used in anger is not ours to move.
    if 'equity_intraday_shorts' in tables:
        try:
            used = db.session.execute(
                text("SELECT COUNT(*) FROM equity_intraday_shorts")
            ).scalar() or 0
        except Exception:
            used = 0
        if used:
            print("  %d short(s) already recorded, leaving the setting alone" % used)
            return

    try:
        result = db.session.execute(
            text("UPDATE equity_settings SET intraday_squareoff_minute = :new "
                 "WHERE intraday_squareoff_minute = :old"),
            {'new': NEW_DEFAULT, 'old': OLD_DEFAULT}
        )
        db.session.commit()
        count = result.rowcount if result.rowcount is not None else 0
        if count:
            print("  Moved %d setting(s) from 15:20 to 15:12" % count)
        else:
            print("  Nothing was still at 15:20, so nothing was changed")
    except Exception as exc:
        db.session.rollback()
        print("  Could not move the setting: %s" % exc)

    print("Done")


def downgrade(db):
    """
    Leave the value where it is.

    Putting it back to 15:20 would put the square-off behind the sandbox's own
    15:15 again, which is the exact problem this exists to remove.
    """
    print("  The square-off time was left at whatever it is now, on purpose")
