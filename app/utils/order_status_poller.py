"""
Background Order Status Polling Service
Checks pending orders every 2 seconds and updates database without blocking order placement

Uses standard threading for background tasks.
"""

import logging
import threading
from datetime import datetime, timedelta
from typing import Dict

# Cross-platform compatibility
from app.utils.compat import sleep, create_lock

from app import db
from app.models import StrategyExecution
from app.utils.openalgo_client import ExtendedOpenAlgoAPI

logger = logging.getLogger(__name__)

# Import PositionMonitor for real-time position tracking
def get_position_monitor():
    """Lazy import to avoid circular dependency"""
    from app.utils.position_monitor import position_monitor
    return position_monitor


class OrderStatusPoller:
    """
    Background service to poll order status without blocking order placement.
    Respects 1 req/sec/account rate limit.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.pending_orders: Dict[int, Dict] = {}  # execution_id: {account, order_id, strategy_name, ...}
        self.is_running = False
        self.poller_thread = None
        self.last_check_time: Dict[str, datetime] = {}  # account_key: last_check_time
        self._initialized = True
        logger.info("Order Status Poller initialized")

    def start(self):
        """Start the background polling service"""
        if not self.is_running:
            self.is_running = True
            self.poller_thread = threading.Thread(target=self._poll_loop, daemon=True, name="OrderStatusPoller")
            self.poller_thread.start()
            logger.info("[STARTED] Order Status Poller started")

    def stop(self):
        """Stop the background polling service"""
        self.is_running = False
        if self.poller_thread:
            try:
                self.poller_thread.join(timeout=5)
            except Exception:
                pass
        logger.info("[STOPPED] Order Status Poller stopped")

    def add_order(self, execution_id: int, account, order_id: str, strategy_name: str):
        """Add an order to the polling queue"""
        with self._lock:
            self.pending_orders[execution_id] = {
                'account_id': account.id,
                'account_name': account.account_name,
                'api_key': account.get_api_key(),
                'host_url': account.host_url,
                'order_id': order_id,
                'strategy_name': strategy_name,
                'added_time': datetime.utcnow(),
                'check_count': 0
            }
            logger.info(f"[POLLER] Added order {order_id} (execution {execution_id}) to polling queue. "
                       f"Queue size: {len(self.pending_orders)}")

    def remove_order(self, execution_id: int):
        """Remove an order from the polling queue"""
        with self._lock:
            order_info = self.pending_orders.pop(execution_id, None)
            if order_info:
                logger.info(f"[POLLER] Removed order {order_info['order_id']} (execution {execution_id}) from polling queue. "
                           f"Queue size: {len(self.pending_orders)}")
                return True
            return False

    def _poll_loop(self):
        """Main polling loop - runs in background"""
        from app import create_app
        app = create_app()

        with app.app_context():
            while self.is_running:
                try:
                    # Get copy of pending orders to avoid lock during API calls
                    with self._lock:
                        orders_to_check = dict(self.pending_orders)

                    if not orders_to_check:
                        sleep(3)  # Wait 3 seconds if no orders
                        continue

                    logger.debug(f"[POLLING] Checking {len(orders_to_check)} pending orders")

                    # Check each order (respecting rate limits)
                    for execution_id, order_info in orders_to_check.items():
                        self._check_order_status(execution_id, order_info, app)

                    # Wait 3 seconds before next polling cycle
                    sleep(3)

                except Exception as e:
                    logger.error(f"[ERROR] Error in polling loop: {e}", exc_info=True)
                    sleep(5)  # Wait longer on error

    def _check_order_status(self, execution_id: int, order_info: Dict, app):
        """Check status of a single order"""
        account_key = f"{order_info['account_id']}_{order_info['account_name']}"
        order_id = order_info['order_id']
        strategy_name = order_info['strategy_name']

        # Rate limiting: Ensure 1 second between checks per account
        now = datetime.utcnow()

        if account_key in self.last_check_time:
            time_since_last_check = (now - self.last_check_time[account_key]).total_seconds()
            if time_since_last_check < 1.0:
                # Skip this check to respect rate limit
                return

        try:
            # Fetch order status
            client = ExtendedOpenAlgoAPI(
                api_key=order_info['api_key'],
                host=order_info['host_url']
            )

            response = client.orderstatus(order_id=order_id, strategy=strategy_name)
            self.last_check_time[account_key] = datetime.utcnow()

            if response.get('status') == 'success':
                data = response.get('data', {})
                broker_status = data.get('order_status')  # OpenAlgo API returns 'order_status' not 'status'
                avg_price = data.get('average_price', 0)

                # Update database
                with app.app_context():
                    execution = StrategyExecution.query.get(execution_id)
                    if not execution:
                        # Order no longer exists, remove from queue
                        with self._lock:
                            self.pending_orders.pop(execution_id, None)
                        logger.warning(f"[WARNING] Execution {execution_id} not found in DB, removed from queue")
                        return

                    # Update based on broker status
                    if broker_status == 'complete':
                        # Determine if this is entry or exit order
                        is_entry_order = execution.status == 'pending'
                        is_exit_order = execution.status == 'entered' or execution.exit_order_id == order_id

                        # If average_price is missing/zero, wait 3 seconds and re-fetch
                        # Some brokers return complete status before average_price is populated
                        if not avg_price or avg_price == 0:
                            logger.warning(f"[PRICE MISSING] Order {order_id} complete but average_price is {avg_price}, waiting 3s to re-fetch...")
                            sleep(3)

                            # Re-fetch order status after delay (respecting rate limit)
                            retry_response = client.orderstatus(order_id=order_id, strategy=strategy_name)
                            self.last_check_time[account_key] = datetime.utcnow()

                            if retry_response.get('status') == 'success':
                                retry_data = retry_response.get('data', {})
                                retry_avg_price = retry_data.get('average_price', 0)

                                if retry_avg_price and retry_avg_price > 0:
                                    avg_price = retry_avg_price
                                    logger.info(f"[PRICE FETCHED] Order {order_id} average_price after retry: Rs.{avg_price}")
                                else:
                                    logger.warning(f"[PRICE STILL MISSING] Order {order_id} average_price still {retry_avg_price} after retry")
                            else:
                                logger.warning(f"[RETRY FAILED] Failed to re-fetch order {order_id}: {retry_response.get('message')}")

                        if is_entry_order:
                            execution.status = 'entered'
                            execution.broker_order_status = 'complete'
                            # Only update entry_price if we have a valid average_price
                            if avg_price and avg_price > 0:
                                execution.entry_price = avg_price
                            else:
                                logger.warning(f"[PRICE WARNING] Entry order {order_id} complete but no valid average_price, keeping existing entry_price: {execution.entry_price}")
                            if not execution.entry_time:
                                execution.entry_time = datetime.utcnow()

                            # Mark leg as executed
                            if execution.leg and not execution.leg.is_executed:
                                execution.leg.is_executed = True

                            db.session.commit()

                            # Notify PositionMonitor of new filled order
                            try:
                                position_monitor = get_position_monitor()
                                position_monitor.on_order_filled(execution)
                                logger.debug(f"[POSITION MONITOR] Notified of order fill: {order_id}")
                            except Exception as e:
                                logger.error(f"[POSITION MONITOR] Error notifying order fill: {e}")

                            logger.info(f"[FILLED] Entry order {order_id} FILLED at Rs.{avg_price} ({order_info['account_name']})")

                        elif is_exit_order:
                            execution.status = 'exited'
                            execution.broker_order_status = 'complete'
                            # Only update exit_price if we have a valid average_price
                            if avg_price and avg_price > 0:
                                execution.exit_price = avg_price
                                # Calculate realized P&L based on action (BUY/SELL)
                                if execution.leg and execution.entry_price:
                                    if execution.leg.action.upper() == 'BUY':
                                        execution.realized_pnl = (avg_price - execution.entry_price) * execution.quantity
                                    else:
                                        execution.realized_pnl = (execution.entry_price - avg_price) * execution.quantity
                                    logger.info(f"[P&L] Calculated realized P&L for {execution.symbol}: Rs.{execution.realized_pnl:.2f}")
                            else:
                                logger.warning(f"[PRICE WARNING] Exit order {order_id} complete but no valid average_price, keeping existing exit_price: {execution.exit_price}")
                                # Fallback: use unrealized_pnl if exit price unavailable
                                if execution.unrealized_pnl:
                                    execution.realized_pnl = execution.unrealized_pnl
                            if not execution.exit_time:
                                execution.exit_time = datetime.utcnow()

                            db.session.commit()

                            # Notify PositionMonitor of position closure
                            try:
                                position_monitor = get_position_monitor()
                                position_monitor.on_position_closed(execution)
                                logger.debug(f"[POSITION MONITOR] Notified of position close: {order_id}")
                            except Exception as e:
                                logger.error(f"[POSITION MONITOR] Error notifying position close: {e}")

                            logger.info(f"[CLOSED] Exit order {order_id} FILLED at Rs.{avg_price} ({order_info['account_name']})")

                        # Remove from polling queue
                        with self._lock:
                            self.pending_orders.pop(execution_id, None)

                    elif broker_status in ['rejected', 'cancelled']:
                        execution.status = 'failed'
                        execution.broker_order_status = broker_status
                        db.session.commit()

                        # Notify PositionMonitor of cancelled order
                        try:
                            position_monitor = get_position_monitor()
                            position_monitor.on_order_cancelled(execution)
                            logger.debug(f"[POSITION MONITOR] Notified of order cancel: {order_id}")
                        except Exception as e:
                            logger.error(f"[POSITION MONITOR] Error notifying order cancel: {e}")

                        # Remove from polling queue
                        with self._lock:
                            self.pending_orders.pop(execution_id, None)

                        logger.warning(f"[REJECTED] Order {order_id} {broker_status.upper()} ({order_info['account_name']})")

                    else:  # Still 'open'
                        execution.broker_order_status = 'open'
                        db.session.commit()

                        # Increment check count
                        with self._lock:
                            if execution_id in self.pending_orders:
                                self.pending_orders[execution_id]['check_count'] += 1

                        logger.debug(f"[PENDING] Order {order_id} still OPEN (check #{order_info['check_count']})")

                # Extended timeout: Remove after 8 hours (28800 seconds) for LIMIT orders
                # LIMIT orders can take much longer to fill than MARKET orders
                # Only remove if order has been open for too long (likely stale/forgotten)
                order_age = (datetime.utcnow() - order_info['added_time']).total_seconds()
                max_age_seconds = 28800  # 8 hours - allows full trading day for LIMIT orders

                if order_age > max_age_seconds:
                    with self._lock:
                        self.pending_orders.pop(execution_id, None)
                    logger.warning(f"[TIMEOUT] Order {order_id} removed from polling (timeout after {int(order_age)}s / {max_age_seconds}s max)")
            else:
                logger.warning(f"[WARNING] Failed to get status for order {order_id}: {response.get('message')}")

        except Exception as e:
            logger.error(f"[ERROR] Error checking order {order_id}: {e}")

    def get_status(self):
        """Get current poller status (for monitoring)"""
        with self._lock:
            return {
                'is_running': self.is_running,
                'pending_orders_count': len(self.pending_orders),
                'pending_order_ids': [info['order_id'] for info in self.pending_orders.values()]
            }

    def recover_pending_orders(self, app=None):
        """
        Recover pending orders from database on startup.
        Re-adds any orders with status='pending' and broker_order_status='open' to polling queue.
        This handles app restarts where in-memory poller state was lost.
        """
        try:
            if app:
                ctx = app.app_context()
                ctx.push()

            from app.models import StrategyExecution, TradingAccount

            # Find all pending orders that need tracking
            pending_executions = StrategyExecution.query.filter(
                StrategyExecution.status == 'pending',
                StrategyExecution.order_id.isnot(None)
            ).all()

            recovered_count = 0
            for execution in pending_executions:
                # Skip if already in polling queue
                if execution.id in self.pending_orders:
                    continue

                # Get account details
                account = TradingAccount.query.get(execution.account_id)
                if not account or not account.is_active:
                    logger.warning(f"[RECOVERY] Skipping execution {execution.id}: account inactive or not found")
                    continue

                # Get strategy name
                strategy_name = execution.strategy.name if execution.strategy else 'Unknown'

                # Add to polling queue
                with self._lock:
                    self.pending_orders[execution.id] = {
                        'account_id': account.id,
                        'account_name': account.account_name,
                        'api_key': account.get_api_key(),
                        'host_url': account.host_url,
                        'order_id': execution.order_id,
                        'strategy_name': strategy_name,
                        'added_time': datetime.utcnow(),  # Reset timer for recovered orders
                        'check_count': 0
                    }
                    recovered_count += 1

            if recovered_count > 0:
                logger.info(f"[RECOVERY] Recovered {recovered_count} pending orders to polling queue")
            else:
                logger.info(f"[RECOVERY] No pending orders to recover")

            if app:
                ctx.pop()

            return recovered_count

        except Exception as e:
            logger.error(f"[RECOVERY] Error recovering pending orders: {e}", exc_info=True)
            return 0

    def sync_order_status(self, execution_id: int, app=None) -> dict:
        """
        Manually sync a single order's status from broker.
        Used when refresh is triggered to ensure latest state.
        Returns updated status dict.
        """
        try:
            if app:
                ctx = app.app_context()
                ctx.push()

            from app.models import StrategyExecution, TradingAccount

            execution = StrategyExecution.query.get(execution_id)
            if not execution:
                return {'status': 'error', 'message': 'Execution not found'}

            # Skip if already in terminal state
            if execution.status in ['exited', 'failed', 'error']:
                return {'status': 'skipped', 'message': f'Order already in terminal state: {execution.status}'}

            # Get account
            account = TradingAccount.query.get(execution.account_id)
            if not account:
                return {'status': 'error', 'message': 'Account not found'}

            # Fetch status from broker
            client = ExtendedOpenAlgoAPI(
                api_key=account.get_api_key(),
                host=account.host_url
            )

            strategy_name = execution.strategy.name if execution.strategy else 'Unknown'
            response = client.orderstatus(order_id=execution.order_id, strategy=strategy_name)

            if response.get('status') == 'success':
                data = response.get('data', {})
                broker_status = data.get('order_status')
                avg_price = data.get('average_price', 0)

                old_status = execution.status
                old_broker_status = execution.broker_order_status

                # Update based on broker status
                if broker_status == 'complete':
                    if execution.status == 'pending':
                        execution.status = 'entered'
                        execution.broker_order_status = 'complete'
                        if avg_price and avg_price > 0:
                            execution.entry_price = avg_price
                        if not execution.entry_time:
                            execution.entry_time = datetime.utcnow()
                        if execution.leg and not execution.leg.is_executed:
                            execution.leg.is_executed = True
                        db.session.commit()

                        # Remove from polling queue if present
                        with self._lock:
                            self.pending_orders.pop(execution_id, None)

                        logger.info(f"[SYNC] Order {execution.order_id} synced: {old_status}->{execution.status}")

                elif broker_status in ['rejected', 'cancelled']:
                    execution.status = 'failed'
                    execution.broker_order_status = broker_status
                    db.session.commit()

                    # Remove from polling queue
                    with self._lock:
                        self.pending_orders.pop(execution_id, None)

                    logger.info(f"[SYNC] Order {execution.order_id} synced: {old_status}->{execution.status} ({broker_status})")

                else:  # Still open
                    execution.broker_order_status = 'open'
                    db.session.commit()

                    # Ensure it's in the polling queue
                    if execution_id not in self.pending_orders:
                        self.add_order(
                            execution_id=execution.id,
                            account=account,
                            order_id=execution.order_id,
                            strategy_name=strategy_name
                        )

                if app:
                    ctx.pop()

                return {
                    'status': 'success',
                    'order_status': execution.status,
                    'broker_status': execution.broker_order_status,
                    'entry_price': execution.entry_price,
                    'updated': old_status != execution.status or old_broker_status != execution.broker_order_status
                }
            else:
                if app:
                    ctx.pop()
                return {'status': 'error', 'message': response.get('message', 'Failed to fetch status')}

        except Exception as e:
            logger.error(f"[SYNC] Error syncing order {execution_id}: {e}", exc_info=True)
            if app:
                try:
                    ctx.pop()
                except:
                    pass
            return {'status': 'error', 'message': str(e)}

    def sync_all_pending_orders(self, user_id: int = None, app=None) -> dict:
        """
        Sync all pending orders from broker.
        Returns summary of updated orders.
        """
        try:
            if app:
                ctx = app.app_context()
                ctx.push()

            from app.models import StrategyExecution, Strategy

            # Build query for pending orders
            query = StrategyExecution.query.filter(
                StrategyExecution.status == 'pending',
                StrategyExecution.order_id.isnot(None)
            )

            # Filter by user if specified
            if user_id:
                query = query.join(Strategy).filter(Strategy.user_id == user_id)

            pending_executions = query.all()

            results = {
                'total': len(pending_executions),
                'updated': 0,
                'filled': 0,
                'rejected': 0,
                'still_pending': 0,
                'errors': 0
            }

            for execution in pending_executions:
                result = self.sync_order_status(execution.id)

                if result.get('status') == 'success':
                    if result.get('updated'):
                        results['updated'] += 1
                    if result.get('order_status') == 'entered':
                        results['filled'] += 1
                    elif result.get('order_status') == 'failed':
                        results['rejected'] += 1
                    elif result.get('order_status') == 'pending':
                        results['still_pending'] += 1
                elif result.get('status') == 'skipped':
                    pass  # Already in terminal state
                else:
                    results['errors'] += 1

            if app:
                ctx.pop()

            logger.info(f"[SYNC ALL] Synced {results['total']} orders: "
                       f"{results['filled']} filled, {results['rejected']} rejected, "
                       f"{results['still_pending']} pending, {results['errors']} errors")

            return results

        except Exception as e:
            logger.error(f"[SYNC ALL] Error syncing pending orders: {e}", exc_info=True)
            if app:
                try:
                    ctx.pop()
                except:
                    pass
            return {'status': 'error', 'message': str(e)}


# Global singleton instance
order_status_poller = OrderStatusPoller()
