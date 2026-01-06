"""
Migration: Update NIFTY and BANKNIFTY lot sizes and freeze quantities

Updates as per NSE circular effective Jan 2026:
- NIFTY: lot_size 75->65, next_month_lot_size 75->65, freeze_quantity 1800->1755
- BANKNIFTY: lot_size 35->30, next_month_lot_size 30->30
"""

from sqlalchemy import text


def upgrade(db):
    """Update NIFTY and BANKNIFTY lot sizes and freeze quantities"""

    # Update NIFTY settings for all users
    # lot_size: 75 -> 65
    # next_month_lot_size: 75 -> 65
    # freeze_quantity: 1800 -> 1755
    # max_lots_per_order: 1755 // 65 = 27
    db.session.execute(text("""
        UPDATE trading_settings
        SET lot_size = 65,
            next_month_lot_size = 65,
            freeze_quantity = 1755,
            max_lots_per_order = 27
        WHERE symbol = 'NIFTY'
    """))

    # Update BANKNIFTY settings for all users
    # lot_size: 35 -> 30
    # next_month_lot_size: -> 30
    # max_lots_per_order: 600 // 30 = 20
    db.session.execute(text("""
        UPDATE trading_settings
        SET lot_size = 30,
            next_month_lot_size = 30,
            max_lots_per_order = 20
        WHERE symbol = 'BANKNIFTY'
    """))

    db.session.commit()
    print("Updated NIFTY: lot_size=65, freeze_quantity=1755")
    print("Updated BANKNIFTY: lot_size=30")


def downgrade(db):
    """Revert to previous lot sizes"""
    db.session.execute(text("""
        UPDATE trading_settings
        SET lot_size = 75,
            next_month_lot_size = 75,
            freeze_quantity = 1800,
            max_lots_per_order = 24
        WHERE symbol = 'NIFTY'
    """))

    db.session.execute(text("""
        UPDATE trading_settings
        SET lot_size = 35,
            next_month_lot_size = 30,
            max_lots_per_order = 17
        WHERE symbol = 'BANKNIFTY'
    """))

    db.session.commit()
