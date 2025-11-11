"""
Fix pending orders that have entry_price set incorrectly.

This script clears entry_price for all pending/open orders.
entry_price should ONLY be set when order status is 'complete'.
"""

from app import create_app
from app.models import StrategyExecution, db

app = create_app()

with app.app_context():
    # Find all pending executions that have entry_price set
    pending_with_price = StrategyExecution.query.filter(
        StrategyExecution.status.in_(['pending', 'entered']),
        StrategyExecution.broker_order_status.in_(['open', None]),
        StrategyExecution.entry_price.isnot(None)
    ).all()

    print(f"Found {len(pending_with_price)} pending orders with entry_price set")

    for execution in pending_with_price:
        print(f"  - Order {execution.order_id}: status={execution.status}, "
              f"broker_status={execution.broker_order_status}, "
              f"entry_price={execution.entry_price} -> clearing entry_price")
        execution.entry_price = None

    db.session.commit()
    print(f"Fixed {len(pending_with_price)} orders")
    print("Done!")
